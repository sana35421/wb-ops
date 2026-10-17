#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops / PostToolUse 输出裁剪 hook

作用：工具跑完之后，把过长的输出压短再交给模型，省上下文 token。
      原始输出仍然真实存在（文件、终端里都在），只是发给模型的那份变短了。

安全原则（很重要）：
    1. 认不出来就原样透传 —— 绝不猜测、绝不破坏结构化输出。
    2. 输出不长就不动 —— 而不是无脑截断。
    3. 被丢掉的行里，命中 error/warning 等关键词的会捞回来（含头/尾段被预算挤掉的）。
    4. 任何异常都静默退出（不输出 = 宿主保持原样），绝不让 hook 挂住会话。

输入：stdin 收到 JSON，含 hook_event_name / tool_name / tool_input / tool_response
输出：stdout 写 JSON，用 hookSpecificOutput.updatedToolOutput 替换工具结果

v0.6.1：捞回域扩展至头/尾被挤行 + 标记文案按实际命中措辞（S1）；输入链改二进制读入 +
显式 UTF-8 解码 + 剥 BOM（S5/X9，读取时即设 8MB 上限 X1）；孤立代理项容错（S3）；
记账行尾与原生版逐字节对齐（S4）；退化输入不比原文小就透传（S9）。
"""

import json
import os
import re
import sys
import time
from datetime import datetime

# ---- 裁剪阈值：超过才动手 ----
# v0.2.2 起预算一律按「字节」计（UTF-8 字节数）：同样字节量的中英文，
# 上下文成本量级一致；按码点计会被中文内容绕过上界（1 个汉字 ≈ 3 字节 ≈ 1 token）。
MAX_LINES = 120         # 行数超过这个才裁（触发条件）
MAX_BYTES = 8000        # 默认预算：字节数超过这个才裁（触发条件），也是结果硬上界
HEAD_LINES = 50         # 开头区域最多看多少行
TAIL_LINES = 25         # 结尾区域最多看多少行
MAX_KEY_LINES = 40      # 中间区域最多捞回多少条关键行（超出则均匀抽样）
MIN_LINE_BYTES = 60     # 截断展示时单行至少保留的字节数，低于此值整行丢弃
MAX_LINE_BYTES = 1000   # 单行最多展示的字节数（防 minified 超长行吃掉预算）
MARKER_RESERVE = 300    # 给省略标记预留的字节数

# ---- v0.3.5：预算可配置（WB_OPS_BUDGET，单位字节）----
BUDGET_MIN = 2000       # 允许的最小预算（再窄内容只剩骨架）
BUDGET_CAP = 131072     # 允许的最大预算（再大等于放弃裁剪）
MAX_INPUT = 8 * 1024 * 1024   # v0.6.1：stdin 读取上限（读取时即生效，超限透传）
MAX_FILL_ROUNDS = 4     # v0.6.1（S1）：捞回候选迭代填充轮数上限（双实现一致，保 parity）

# ---- v0.6.9：Read 按需裁剪（默认关闭）----
READ_MIN_LINES = 1500   # Read 小于这么多行一律不裁（源码要完整读）
READ_HEAD_LINES = 200   # Read 头段行数
READ_TAIL_LINES = 120   # Read 尾段行数

# Read 模式下的「符号行」：函数 / 类 / 结构 / 导入等骨架行
# （全是字面量，用子串匹配，与 src/trim.cpp 的 SYMBOLS 逐项一致，保 parity）
SYMBOLS = ("#include", "function ", "def ", "class ", "struct ", "namespace ",
           "impl ", "interface ", "pub fn", "export ", "typedef ", "import ")


def is_symbol_line(line):
    return any(sym in line for sym in SYMBOLS)


def env_flag(name):
    """环境变量取 "1"（容忍两侧空白），与 src/trim.cpp 的 env_flag 一致"""
    return (os.environ.get(name) or "").strip(" \t\r\n\v\f") == "1"


def bash_failed(text):
    """Bash 失败透传：宿主给 Bash 的是纯文本「… / Exit Code: N / Signal: …」，
    命令失败时整段输出都可能有用，不裁剪。只认文本里的 "Exit Code: N"；
    结构化对象（带 exitCode 字段）仍按原逻辑裁剪并补退出码。
    """
    key = "Exit Code:"
    p = text.rfind(key)
    if p < 0:
        return False
    m = re.match(r"[ \t]*(-?[0-9]+)", text[p + len(key):p + len(key) + 24])
    return bool(m) and int(m.group(1)) != 0


def resolve_budget():
    """解析 WB_OPS_BUDGET 环境变量，得到本次裁剪的字节预算；默认 MAX_BYTES。

    纯 ASCII 数字（与 src/trim.cpp 的 resolve_budget 完全一致，parity 有专项用例）：
    空值/含非数字 -> 回退默认；有效值收敛到 [BUDGET_MIN, BUDGET_CAP]。
    """
    s = (os.environ.get("WB_OPS_BUDGET") or "").strip(" \t\r\n\v\f")
    if not s or not re.fullmatch(r"[0-9]+", s):
        return MAX_BYTES
    if len(s) > 9:
        return BUDGET_CAP  # 位数过多，必然越上界
    return min(max(int(s), BUDGET_MIN), BUDGET_CAP)

# ---- 只处理这几类高频大输出工具 ----
TRIM_TOOLS = {"Bash", "Grep", "Glob", "Read", "WebFetch"}

# ---- 关键行特征 ----
KEY_PATTERN = re.compile(
    r"(error|Error|ERROR|fail|Fail|FAIL|"
    r"warn|Warn|WARN|fatal|Fatal|"
    r"Traceback|Exception|"
    r"cannot|Cannot|denied|refused|panic|timeout|timed out|"
    r"not found|Not Found|No such file|"
    r"错误|失败|警告|异常|未能|无法)",
)


def trace(msg):
    """诊断日志：仅在设置了 WB_OPS_TRACE 时写文件，平时零开销。

    用来排查「hook 到底有没有被调用 / 用的是哪个解释器」这类问题。
    文件写在脚本同目录下的 trace.log，超过 256KB 自动重来。
    """
    if not os.environ.get("WB_OPS_TRACE"):
        return
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace.log")
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            os.remove(path)
        with open(path, "a", encoding="utf-8", newline="") as f:  # newline="": 不翻译 \n（S4）
            f.write("%s | py=%s | %s\n" % (
                datetime.now().strftime("%H:%M:%S"), sys.executable, msg))
    except Exception:
        pass


def stats(tool, sid, before, after):
    """v0.3.0 节省记账：每次裁剪成功后 append 一行 JSONL（默认开启，失败静默）。

    落点：%USERPROFILE%\\.workbuddy\\wb-ops-data\\saves-YYYYMM.jsonl
    （WB_OPS_STATS_DIR 覆盖数据目录；WB_OPS_STATS=0 整关）。
    行格式与 src/trim.cpp 的 stats() 逐字节对齐（时间戳除外）：
      {"t":毫秒,"s":会话id前8字节,"tool":"Grep","b":原始字节,"a":裁剪后字节}
    只记尺寸与时间，不落任何输出内容。
    """
    try:
        if os.environ.get("WB_OPS_STATS") == "0":
            return
        base = os.environ.get("WB_OPS_STATS_DIR")
        if not base:
            home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            if not home:
                return
            base = os.path.join(home, ".workbuddy", "wb-ops-data")
        os.makedirs(base, exist_ok=True)
        month = datetime.now().strftime("%Y%m")
        path = os.path.join(base, "saves-%s.jsonl" % month)
        line = json.dumps({"t": int(time.time() * 1000), "s": u8_prefix(sid or "", 8),
                           "tool": tool, "b": before, "a": after},
                          ensure_ascii=False, separators=(",", ":"))
        # newline=""：关闭文本模式的行尾翻译，与原生版的二进制写 \n 逐字节对齐（S4）
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")
    except Exception:
        pass


def extract_text(resp):
    """把 tool_response 拍平成文本。认不出来返回 None（表示不改）。"""
    if isinstance(resp, str):
        return resp

    if isinstance(resp, list):
        # 两种真实形状：
        #   1. MCP 风格 [{"type":"text","text":"..."}]
        #   2. Grep/Glob 这类直接给字符串数组，每个元素是一行
        parts = []
        for item in resp:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else None

    if isinstance(resp, dict):
        parts = []
        for key in ("stdout", "stderr", "output", "text", "content"):
            value = resp.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        if not parts:
            return None
        # 把退出码也带上，避免丢失信息。
        # v0.6.1（X3）：语义与原生版对齐 —— 只认「非 bool 的整数值且非 0」：
        # 接受整值 float（2.0 -> 2）；bool 不当作退出码；NaN/±inf/越界值不处理
        # （对应 C++ 侧先 isfinite + 范围判断再转 long long，杜绝 UB）。
        code = resp.get("exitCode", resp.get("exit_code"))
        if (isinstance(code, (int, float)) and not isinstance(code, bool)
                and code == code and code not in (float("inf"), float("-inf"))
                and -(2 ** 63) <= code < 2 ** 63):
            ic = int(code)
            if ic == code and ic != 0:
                parts.append("[exit code %d]" % ic)
        return "\n".join(parts)

    return None


def blen(s):
    """字节数（UTF-8）。预算一律按字节计。

    v0.6.1（S3）：孤立代理项等不可编码字符不应让 hook 崩溃 ——
    errors="replace" 兜底（主流程另有整体 try 兜底），目标「不崩溃 + 行为可预期」。
    """
    return len(s.encode("utf-8", "replace"))


def u8_prefix(s, nbytes):
    """取前 nbytes 个字节（若切到字符中间则回退到最后一个完整字符）。"""
    b = s.encode("utf-8", "replace")
    if len(b) <= nbytes:
        return s
    k = nbytes
    while k > 0 and (b[k] & 0xC0) == 0x80:
        k -= 1
    return b[:k].decode("utf-8")


def u8_suffix(s, nbytes):
    """取后 nbytes 个字节（若切到字符中间则前进到下一个完整字符）。"""
    b = s.encode("utf-8", "replace")
    if len(b) <= nbytes:
        return s
    st = len(b) - nbytes
    while st < len(b) and (b[st] & 0xC0) == 0x80:
        st += 1
    return b[st:].decode("utf-8")


def fill_lines(cands, cap, used):
    """段内填充（对应 trim.cpp 的 fill_lines）：

    优先整行；单行超过 MAX_LINE_BYTES 先自截到上限再算；
    放不下而剩余额度 ≥ MIN_LINE_BYTES+3 时，末行截断后结束本段；
    连截断都放不下则丢弃该行、结束本段。整段消耗不超过 cap。
    返回 (保留的行列表, 更新后的累计额度 used)。
    """
    chosen = []
    for line in cands:
        n = blen(line)
        if n > MAX_LINE_BYTES:
            disp = u8_prefix(line, MAX_LINE_BYTES) + "\u2026"
            want = MAX_LINE_BYTES + 3
        else:
            disp = line
            want = n
        if used + want + 1 <= cap:
            chosen.append(disp)
            used += want + 1
        else:
            rem = cap - used - 1 if cap > used + 1 else 0
            if rem >= MIN_LINE_BYTES + 3:
                chosen.append(u8_prefix(line, rem - 3) + "\u2026")
                used = cap
            break
    return chosen, used


def trim(text, read_mode=False):
    """返回裁剪后的文本；如果不需要裁剪则返回 None。

    输出结构：开头行 → [省略标记] → 捞回的关键行 → [末尾标记] → 结尾行
    预算按字节计：整体 MAX_BYTES，扣掉标记预留后按段使用；
    关键字段与结尾段各有保底额度，前面的段用不完的额度留给后面的段。

    v0.6.1（S1）：捞回候选 = 中段全部命中行 + 头/尾段「未被本段填中」的命中行
    （保持原文顺序）。候选集依赖上一轮填充的省略集，故迭代填充至候选稳定
    （上限 MAX_FILL_ROUNDS 轮；两实现使用相同循环，保 parity）。
    v0.6.9：标记带行号区间（供模型 offset 定向回查）；read_mode 时按「头 + 尾 + 符号行」
    处理源码，并用符号行措辞。
    """
    lines = text.split("\n")
    total = len(lines)
    tbytes = blen(text)
    budget_bytes = resolve_budget()   # v0.3.5：可用 WB_OPS_BUDGET 覆盖（默认 8000）
    head_n = READ_HEAD_LINES if read_mode else HEAD_LINES
    tail_n = READ_TAIL_LINES if read_mode else TAIL_LINES

    # 触发条件：普通工具「行数 > 120 或字节 > 预算」；Read 只认长文件（行数 > 1500 且字节超预算）
    if not read_mode:
        if total <= MAX_LINES and tbytes <= budget_bytes:
            return None
    elif total <= READ_MIN_LINES or tbytes <= budget_bytes:
        return None

    if total <= head_n + tail_n + 5:
        # 行数少但字节多（超长单行），按字节截首尾（额度随预算等比缩放；默认 8000 -> 5000/2400）
        lh = budget_bytes * 5 // 8
        lt = budget_bytes * 3 // 10
        head = u8_prefix(text, lh)
        tail = u8_suffix(text, lt)
        return "%s\n… [wb-ops 省略 %d 字节] …\n%s" % (
            head, tbytes - blen(head) - blen(tail), tail)

    head = lines[:head_n]
    tail = lines[total - tail_n:]
    mid_lo, mid_hi = head_n, total - tail_n

    # 命中统计（全文一次预计算，供候选集与标记文案复用）
    # 普通模式认 error/warning 等关键字；Read 模式认函数/类/import 等符号行
    hit = is_symbol_line if read_mode else (lambda ln: bool(KEY_PATTERN.search(ln)))
    is_key = [bool(hit(line)) for line in lines]
    hit_total = sum(is_key)   # 全文命中总数 M（标记文案用）

    # 初始候选：中段全部命中行（原捞回域）
    key_idx = [i for i in range(mid_lo, mid_hi) if is_key[i]]

    budget = budget_bytes - MARKER_RESERVE
    head_shown, key_shown, tail_shown = [], [], []
    cand_idx, sampled, pool_n = [], False, 0
    for _ in range(MAX_FILL_ROUNDS):
        # 候选超过捞出上限时按固定步长均匀抽样（覆盖全局，而非只看前 40 条）
        pool_n = len(key_idx)
        if pool_n > MAX_KEY_LINES:
            sampled = True
            step = (pool_n + MAX_KEY_LINES - 1) // MAX_KEY_LINES  # 向上取整
            cand_idx = key_idx[::step]
        else:
            sampled = False
            cand_idx = key_idx

        used = 0
        head_shown, used = fill_lines(head, budget * 40 // 100, used)
        key_shown, used = fill_lines([lines[i] for i in cand_idx],
                                     budget * 70 // 100, used)
        tail_rev, used = fill_lines(list(reversed(tail)), budget, used)
        tail_shown = list(reversed(tail_rev))

        # 本轮的显示行索引集合
        shown = set(range(len(head_shown)))
        shown.update(cand_idx[:len(key_shown)])
        shown.update(range(total - len(tail_shown), total))

        # 新一轮候选：中段全部命中行 + 头/尾段「未被本段填中」的命中行（原文顺序）
        new_idx = []
        for i in range(total):
            if not is_key[i]:
                continue
            if mid_lo <= i < mid_hi or i not in shown:
                new_idx.append(i)
        if new_idx == key_idx:   # 候选已稳定：本轮填充即最终结果
            break
        key_idx = new_idx

    shown_count = len(head_shown) + len(key_shown) + len(tail_shown)
    silent = total - shown_count

    # v0.6.9：未显示区间的首末行号（1 基），写进标记供模型 offset 定向回查
    omitted_idx = [i for i in range(total) if i not in shown]
    rng_lo = omitted_idx[0] + 1 if omitted_idx else 0
    rng_hi = omitted_idx[-1] + 1 if omitted_idx else 0

    key_word = "符号行" if read_mode else "关键字行"
    parts = list(head_shown)
    if key_shown:
        if sampled:
            parts.append("… [wb-ops 省略 %d 行（第 %d–%d 行），%s的 %d 条中均匀抽取 %d 条如下] …"
                         % (silent, rng_lo, rng_hi,
                            "命中符号" if read_mode else "命中关键字",
                            pool_n, len(key_shown)))
        else:
            marker = ("… [wb-ops 省略 %d 行（第 %d–%d 行），以下为捞回的 %d 条%s"
                      % (silent, rng_lo, rng_hi, len(key_shown), key_word))
            if len(key_shown) < hit_total:
                marker += "（共命中 %d 条）" % hit_total
            parts.append(marker + ("，可加 offset 回查] …" if read_mode else "] …"))
    elif hit_total:
        parts.append("… [wb-ops 省略 %d 行（第 %d–%d 行，%s已包含在显示区域）] …"
                     % (silent, rng_lo, rng_hi, key_word))
    else:
        parts.append("… [wb-ops 省略 %d 行（第 %d–%d 行，未发现 %s）] …"
                     % (silent, rng_lo, rng_hi,
                        "函数/类/import 行" if read_mode else "error/warning 关键字"))
    parts.extend(key_shown)
    if tail_shown:
        parts.append("… [wb-ops 以下为原文末尾 %d 行（第 %d–%d 行）] …"
                     % (len(tail_shown), total - len(tail_shown) + 1, total))
        parts.extend(tail_shown)

    res = "\n".join(parts)

    # v0.6.1（S9）：退化输入（如大量空行）可能越裁越大 —— 不比原文小就直接透传
    if blen(res) >= tbytes:
        return None

    # 兜底硬上界（正常不会触发）
    if blen(res) > budget_bytes:
        res = u8_prefix(res, budget_bytes - 3) + "\u2026"
    return res


def main():
    # Windows 下 stdout 默认可能不是 UTF-8，中文标记会乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # v0.6.1（S5/X9/X1）：二进制读入（读取时即设 8MB 上限）→ 显式 UTF-8 解码 → 剥 BOM。
    # 之前文本模式读 sys.stdin：非 UTF-8 locale（无 PYTHONUTF8=1）下中文输入可能解码失败；
    # 带 BOM 的输入会因 json.loads 报错而整条透传，与原生版（主动剥 BOM）行为分歧。
    try:
        raw_bytes = sys.stdin.buffer.read(MAX_INPUT + 1)
    except Exception:
        return
    if len(raw_bytes) > MAX_INPUT:
        trace("输入超过 8MB，直接透传")
        return
    try:
        raw = raw_bytes.decode("utf-8")
    except Exception:
        trace("stdin 不是合法 UTF-8，透传")
        return
    if raw[:1] == "\ufeff":
        raw = raw[1:]

    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        trace("stdin 不是合法 JSON，透传")
        return  # 解析不了就静默透传

    if not isinstance(data, dict):
        return
    if data.get("hook_event_name") != "PostToolUse":
        trace("事件不是 PostToolUse，透传")
        return
    if data.get("tool_name") not in TRIM_TOOLS:
        resp = data.get("tool_response")
        trace("工具 %s 不在裁剪名单，透传 | keys=%s | 样本=%s"
              % (data.get("tool_name"),
                 (sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__),
                 json.dumps(resp, ensure_ascii=False)[:500]))
        return

    text = extract_text(data.get("tool_response"))
    if not text:
        resp = data.get("tool_response")
        trace("工具 %s 输出无法解析为文本，透传 | resp_type=%s | keys=%s | 样本=%s"
              % (data.get("tool_name"), type(resp).__name__,
                 (sorted(resp.keys()) if isinstance(resp, dict) else ""),
                 json.dumps(resp, ensure_ascii=False)[:500]))
        return

    # v0.6.1（S3）：孤立代理项等不可编码字符先归一化，保证后续编码/输出全程可预期；
    # 目标「不崩溃」，不追与原生版的字节 parity（exe 侧对孤立代理项产出替换字符）。
    try:
        text = text.encode("utf-8", "replace").decode("utf-8")
    except Exception:
        pass

    try:
        tool = data.get("tool_name")
        read_mode = tool == "Read" and env_flag("WB_OPS_READ_TRIM")
        if tool == "Read" and not read_mode:
            trace("Read 未开启按需裁剪（WB_OPS_READ_TRIM!=1），透传")
            return
        if tool == "Bash" and bash_failed(text):
            trace("Bash 退出码非 0，保留完整输出，透传")
            return
        trimmed = trim(text, read_mode)
    except Exception as e:   # 兜底：裁剪出任何意外都静默透传，绝不让 hook 挂住会话
        trace("裁剪异常，透传：%r" % (e,))
        return
    if trimmed is None:
        trace("工具 %s 输出不长（或无省空间收益，%d 字符 / %d 行），不裁"
              % (data.get("tool_name"), len(text), text.count("\n") + 1))
        return  # 不长，不动

    trace("工具 %s 已裁剪：%d -> %d 字节"
          % (data.get("tool_name"), blen(text), blen(trimmed)))

    # v0.3.0 节省记账（默认开启；任何异常静默，绝不影响裁剪输出）
    sid = data.get("session_id")
    stats(data.get("tool_name"), sid if isinstance(sid, str) else "",
          blen(text), blen(trimmed))

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": trimmed,
        },
        "suppressOutput": True,
    }
    try:
        json.dump(payload, sys.stdout, ensure_ascii=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
