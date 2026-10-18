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
MAX_KEY_LINES = 40      # 中间区域最多捞回多少条关键行（超出则均匀抽样）
MIN_LINE_BYTES = 60     # 截断展示时单行至少保留的字节数，低于此值整行丢弃
MAX_LINE_BYTES = 1000   # 单行最多展示的字节数（防 minified 超长行吃掉预算）
MARKER_RESERVE = 300    # 给省略标记预留的字节数

# ---- v0.3.5：预算可配置（WB_OPS_BUDGET，单位字节）----
BUDGET_MIN = 2000       # 允许的最小预算（再窄内容只剩骨架）
BUDGET_CAP = 131072     # 允许的最大预算（再大等于放弃裁剪）
MAX_INPUT = 8 * 1024 * 1024   # v0.6.1：stdin 读取上限（读取时即生效，超限透传）
MAX_FILL_ROUNDS = 4     # v0.6.1（S1）：捞回候选迭代填充轮数上限（双实现一致，保 parity）

# ---- v1.1：头尾行数按「平均行长」自适应，并向预算扩张 ----
# 旧版固定头 50 / 尾 25 行：短行输出（日志、文件清单）只能用到预算的 5~6 成，
# 长行输出又可能一段就吃满。改为「先按平均行长估一个头尾行数，
# 再用「未达标就放大头尾」的扩张循环把预算填到 FILL_TARGET_PCT」，两端都自适应。
HEAD_PCT = 30           # 头段初始按预算的这个百分比估行数
TAIL_PCT = 22           # 尾段初始按预算的这个百分比估行数
HEAD_MIN, HEAD_MAX = 8, 400      # 普通工具头段行数夹紧区间
TAIL_MIN, TAIL_MAX = 5, 300      # 普通工具尾段行数夹紧区间
READ_HEAD_MIN, READ_HEAD_MAX = 20, 400   # Read 模式头段
READ_TAIL_MIN, READ_TAIL_MAX = 10, 300   # Read 模式尾段
GROW_ROUNDS = 6         # 预算扩张轮数上限（每轮头尾各 ×1.25）
FILL_TARGET_PCT = 85    # 预算利用率目标：低于这个就放大头尾，达到就停（不追求打满：
                        # 打满会为凑字节塞入价值低的整段行，token 反而更贵）

# 幂等护栏：输出里已带本插件标记，说明这段文本裁剪过，不再二次裁剪
MARK_OMIT = "[wb-ops 省略"
MARK_TAIL = "[wb-ops 以下为原文末尾"

# ---- v0.6.9：Read 按需裁剪（v1.2 起按文件类型分档）----
READ_MIN_LINES = 1500   # 源码类 Read 小于这么多行一律不裁（源码要完整读）
# v1.2：数据类文件（日志/表格/清单）走「低门槛 + 关键字行」，不需要 WB_OPS_READ_TRIM。
# hooks.json 用 if="Read(*.{log,jsonl,…})" 精确限定，源码文件不会触发 hook（零开销）。
READ_DATA_MIN_LINES = 200   # 数据类 Read 的门槛（>200 行或超预算就裁，与普通工具同语义）
DATA_EXTS = (".log", ".jsonl", ".ndjson", ".csv", ".tsv", ".out", ".err", ".txt")
# v1.1：Read 的头尾行数也走自适应（见 READ_HEAD_MIN/READ_HEAD_MAX 等）

# Read 模式下的「符号行」：函数 / 类 / 结构 / 导入等骨架行
# （全是字面量，用子串匹配，与 src/trim.cpp 的 SYMBOLS 逐项一致，保 parity）
SYMBOLS = ("#include", "function ", "def ", "class ", "struct ", "namespace ",
           "impl ", "interface ", "pub fn", "export ", "typedef ", "import ")


def is_symbol_line(line):
    return any(sym in line for sym in SYMBOLS)


def is_data_file(path):
    """v1.2：Read 的目标是不是「数据类」文件（日志 / 表格 / 清单）。

    数据类文件的特点是：没有函数/类骨架行，但中段常有 error/timeout 之类的关键行，
    所以走普通模式（关键字行）+ 低行数门槛，而不是源码模式（符号行）。
    判定只看扩展名（大小写不敏感），与 hooks.json 的 if 规则保持同一份清单。
    """
    p = (path or "").strip().lower().replace("\\", "/")
    return p.endswith(DATA_EXTS)


def env_flag(name):
    """环境变量取 "1"（容忍两侧空白），与 src/trim.cpp 的 env_flag 一致"""
    return (os.environ.get(name) or "").strip(" \t\r\n\v\f") == "1"


def text_failed(text):
    """文本形态的失败信号：宿主给 Bash 的纯文本里带「Exit Code: N」且 N != 0。

    命令失败时整段输出都可能有用，不裁剪。
    """
    key = "Exit Code:"
    p = text.rfind(key)
    if p < 0:
        return False
    m = re.match(r"[ \t]*(-?[0-9]+)", text[p + len(key):p + len(key) + 24])
    return bool(m) and int(m.group(1)) != 0


def _nonzero_int(value):
    """非 bool 的整数值且非 0（与 src/trim.cpp 的 same 判定一致）。"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf"))
            and value != 0)


def resp_failed(resp):
    """v1.1（P0-2）：结构化失败判定。

    宿主在 tool_response 里给了权威字段，不能再靠猜正文：
      is_error / isError 为真           —— 宿主自己打的失败标
      exitCode / exit_code 非 0         —— Bash / 命令类工具
      tool_error_code 不是 "0"/"Success" —— 宿主工具错误码（"0" 表示成功）
      error 非空字符串                   —— 兜底
    失败一律整段透传：报错现场被裁掉比多花几百 token 贵得多。
    """
    if not isinstance(resp, dict):
        return False
    for key in ("is_error", "isError"):
        value = resp.get(key)
        if value is True or _nonzero_int(value):
            return True
    if _nonzero_int(resp.get("exitCode", resp.get("exit_code"))):
        return True
    code = resp.get("tool_error_code", resp.get("toolErrorCode"))
    if isinstance(code, str) and code.strip() not in ("", "0", "Success", "success"):
        return True
    err = resp.get("error")
    if isinstance(err, str) and err.strip():
        return True
    return False


def looks_json(text):
    """粗判原文是 JSON 结构（两端括号）。两实现同构，不做真正的解析。

    用途：裁剪后必然不再是合法 JSON，在标记里点明一句，免得模型拿去 json.loads 白跑。
    """
    s = text.strip(" \t\r\n")
    if len(s) < 2:
        return False
    if not ((s[0] == "{" and s[-1] == "}") or (s[0] == "[" and s[-1] == "]")):
        return False
    return True


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
        # v1.1（P2）：只记真实会话。测试/基准探头没有 session_id 或带探针前缀，
        # 混进账本会把报表口径带偏（0.6.9 的账本里就混着 7 条测试产物）。
        if not sid or sid[:5] in ("test", "bench", "probe", "demo", "sampl"):
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


def trim(text, read_mode=False, min_lines=MAX_LINES):
    """返回裁剪后的文本；如果不需要裁剪则返回 None。

    输出结构：开头行 → [省略标记] → 捞回的关键行 → [末尾标记] → 结尾行
    预算按字节计：整体 MAX_BYTES，扣掉标记预留后按段使用；
    关键字段与结尾段各有保底额度，前面的段用不完的额度留给后面的段。

    v0.6.1（S1）：捞回候选 = 中段全部命中行 + 头/尾段「未被本段填中」的命中行
    （保持原文顺序）。候选集依赖上一轮填充的省略集，故迭代填充至候选稳定
    （上限 MAX_FILL_ROUNDS 轮；两实现使用相同循环，保 parity）。
    v0.6.9：标记带行号区间（供模型 offset 定向回查）；read_mode 时按「头 + 尾 + 符号行」
    处理源码，并用符号行措辞。
    v1.2：min_lines 参数化行数门槛——普通 120、源码 Read 1500、数据类 Read 200。
    """
    lines = text.split("\n")
    total = len(lines)
    tbytes = blen(text)
    budget_bytes = resolve_budget()   # v0.3.5：可用 WB_OPS_BUDGET 覆盖（默认 8000）

    # v1.1：已带本插件标记的文本不再二次裁剪（幂等，免得越裁越碎）
    if MARK_OMIT in text or MARK_TAIL in text:
        return None

    # 触发条件：普通工具「行数 > min_lines 且字节 > 预算」；
    # Read 模式（源码 / 数据类）「行数 > min_lines 或字节 > 预算」——只认长文件
    if not read_mode:
        if total <= min_lines and tbytes <= budget_bytes:
            return None
    elif total <= min_lines or tbytes <= budget_bytes:
        return None

    # v1.1：头尾行数按平均行长估，再在下面按预算扩张
    avg = tbytes // total if total > 0 else 1
    if avg < 1:
        avg = 1
    if read_mode:
        head_lo, head_hi, tail_lo, tail_hi = READ_HEAD_MIN, READ_HEAD_MAX, READ_TAIL_MIN, READ_TAIL_MAX
    else:
        head_lo, head_hi, tail_lo, tail_hi = HEAD_MIN, HEAD_MAX, TAIL_MIN, TAIL_MAX
    head_n = min(max((budget_bytes - MARKER_RESERVE) * HEAD_PCT // 100 // avg, head_lo), head_hi)
    tail_n = min(max((budget_bytes - MARKER_RESERVE) * TAIL_PCT // 100 // avg, tail_lo), tail_hi)
    # 头尾不能盖满全文：留出至少 6 行当「中段」
    side_max = (total - 6) // 2
    if side_max < 1:
        side_max = 1
    head_n = min(head_n, side_max)
    tail_n = min(tail_n, side_max)

    if total <= head_n + tail_n + 5:
        # 行数少但字节多（超长单行），按字节截首尾（额度随预算等比缩放；默认 8000 -> 5000/2400）
        lh = budget_bytes * 5 // 8
        lt = budget_bytes * 3 // 10
        head = u8_prefix(text, lh)
        tail = u8_suffix(text, lt)
        marker = "… [wb-ops 省略 %d 字节] …" % (tbytes - blen(head) - blen(tail))
        if looks_json(text):
            marker += "\n… [wb-ops 注：原文为 JSON 结构，截断后已非合法 JSON] …"
        return "%s\n%s\n%s" % (head, marker, tail)

    # 命中统计（全文一次预计算，供候选集与标记文案复用）
    # 普通模式认 error/warning 等关键字；Read 模式认函数/类/import 等符号行
    hit = is_symbol_line if read_mode else (lambda ln: bool(KEY_PATTERN.search(ln)))
    is_key = [bool(hit(line)) for line in lines]
    hit_total = sum(is_key)   # 全文命中总数 M（标记文案用）

    budget = budget_bytes - MARKER_RESERVE

    # ---- v1.1：预算扩张。头尾行数按平均行长估出来后，
    # 若填充没用到 FILL_TARGET_PCT 预算，就放大头尾再来一轮（最多 GROW_ROUNDS 轮）；
    # 取「用掉预算最多」的那一轮。两端实现同一循环，保 parity。
    best = None
    for _grow in range(GROW_ROUNDS + 1):
        head = lines[:head_n]
        tail = lines[total - tail_n:]
        mid_lo, mid_hi = head_n, total - tail_n

        # 初始候选：中段全部命中行（原捞回域）
        key_idx = [i for i in range(mid_lo, mid_hi) if is_key[i]]

        head_shown, key_shown, tail_shown = [], [], []
        cand_idx, sampled, pool_n = [], False, 0
        used = 0
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

        if best is None or used > best[0]:
            best = (used, head_shown, key_shown, tail_shown, cand_idx,
                    sampled, pool_n, shown)
        if used * 100 >= budget * FILL_TARGET_PCT:   # 预算用够了，收工
            break
        next_head = min(head_n * 5 // 4 + 1, side_max)
        next_tail = min(tail_n * 5 // 4 + 1, side_max)
        if next_head == head_n and next_tail == tail_n:   # 顶到上限，再放大也没用
            break
        head_n, tail_n = next_head, next_tail

    _used, head_shown, key_shown, tail_shown, cand_idx, sampled, pool_n, shown = best

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
    if looks_json(text):
        # P1-2：行导向裁剪后必然不是合法 JSON，明说一句，省得模型拿去 json.loads 白跑
        parts.append("… [wb-ops 注：原文为 JSON 结构，截断后已非合法 JSON] …")
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
        resp = data.get("tool_response")
        # v1.2：Read 分两档——
        #   数据类（.log/.jsonl/.csv…）：普通关键字模式 + 200 行门槛，默认就裁（无开关）；
        #   源码类：仍要 WB_OPS_READ_TRIM=1，门槛 1500 行，按符号行裁剪。
        # hooks.json 的 if 规则已把源码文件挡在 hook 之外，所以这里两条路径互不干扰。
        read_mode = False
        min_lines = MAX_LINES
        if tool == "Read":
            tool_input = data.get("tool_input")
            path = ""
            if isinstance(tool_input, dict):
                for key in ("file_path", "path"):
                    value = tool_input.get(key)
                    if isinstance(value, str) and value:
                        path = value
                        break
            if is_data_file(path):
                min_lines = READ_DATA_MIN_LINES
            elif env_flag("WB_OPS_READ_TRIM"):
                read_mode = True
                min_lines = READ_MIN_LINES
            else:
                trace("Read 非数据文件且未开启 WB_OPS_READ_TRIM，透传 | %s" % path[:200])
                return
        # v1.1（P0-2）：失败一律整段透传。结构化字段优先（is_error / exitCode /
        # tool_error_code / error），再兜文本里的「Exit Code: N」。
        if resp_failed(resp) or text_failed(text):
            trace("工具 %s 失败（结构化或文本信号），保留完整输出，透传" % tool)
            return
        trimmed = trim(text, read_mode, min_lines)
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
