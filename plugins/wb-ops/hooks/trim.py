#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops / PostToolUse 输出裁剪 hook

作用：工具跑完之后，把过长的输出压短再交给模型，省上下文 token。
      原始输出仍然真实存在（文件、终端里都在），只是发给模型的那份变短了。

安全原则（很重要）：
    1. 认不出来就原样透传 —— 绝不猜测、绝不破坏结构化输出。
    2. 输出不长就不动 —— 而不是无脑截断。
    3. 中间被丢掉的行里，命中 error/warning 等关键词的会捞回来。
    4. 任何异常都静默退出（不输出 = 宿主保持原样），绝不让 hook 挂住会话。

输入：stdin 收到 JSON，含 hook_event_name / tool_name / tool_input / tool_response
输出：stdout 写 JSON，用 hookSpecificOutput.updatedToolOutput 替换工具结果
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
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s | py=%s | %s\n" % (
                datetime.now().strftime("%H:%M:%S"), sys.executable, msg))
    except Exception:
        pass


def stats(tool, sid, before, after):
    """v0.3.0 节省记账：每次裁剪成功后 append 一行 JSONL（默认开启，失败静默）。

    落点：%USERPROFILE%\\.workbuddy\\wb-ops-data\\saves-YYYYMM.jsonl
    （WB_OPS_STATS_DIR 覆盖数据目录；WB_OPS_STATS=0 整关）。
    行格式与 src/trim.cpp 的 stats() 逐字节对齐（时间戳除外）：
      {"t":毫秒,"s":会话id前8位,"tool":"Grep","b":原始字节,"a":裁剪后字节}
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
        line = json.dumps({"t": int(time.time() * 1000), "s": (sid or "")[:8],
                           "tool": tool, "b": before, "a": after},
                          ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as f:
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
        # 把退出码也带上，避免丢失信息
        code = resp.get("exitCode", resp.get("exit_code"))
        if isinstance(code, int) and code != 0:
            parts.append("[exit code %d]" % code)
        return "\n".join(parts)

    return None


def blen(s):
    """字节数（UTF-8）。预算一律按字节计。"""
    return len(s.encode("utf-8"))


def u8_prefix(s, nbytes):
    """取前 nbytes 个字节（若切到字符中间则回退到最后一个完整字符）。"""
    b = s.encode("utf-8")
    if len(b) <= nbytes:
        return s
    k = nbytes
    while k > 0 and (b[k] & 0xC0) == 0x80:
        k -= 1
    return b[:k].decode("utf-8")


def u8_suffix(s, nbytes):
    """取后 nbytes 个字节（若切到字符中间则前进到下一个完整字符）。"""
    b = s.encode("utf-8")
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


def trim(text):
    """返回裁剪后的文本；如果不需要裁剪则返回 None。

    输出结构：开头行 → [省略标记] → 捞回的关键行 → [末尾标记] → 结尾行
    预算按字节计：整体 MAX_BYTES，扣掉标记预留后按段使用；
    关键字段与结尾段各有保底额度，前面的段用不完的额度留给后面的段。
    """
    lines = text.split("\n")
    total = len(lines)
    tbytes = blen(text)
    budget_bytes = resolve_budget()   # v0.3.5：可用 WB_OPS_BUDGET 覆盖（默认 8000）

    if total <= MAX_LINES and tbytes <= budget_bytes:
        return None

    if total <= HEAD_LINES + TAIL_LINES + 5:
        # 行数少但字节多（超长单行），按字节截首尾（额度随预算等比缩放；默认 8000 -> 5000/2400）
        lh = budget_bytes * 5 // 8
        lt = budget_bytes * 3 // 10
        head = u8_prefix(text, lh)
        tail = u8_suffix(text, lt)
        return "%s\n… [wb-ops 省略 %d 字节] …\n%s" % (
            head, tbytes - blen(head) - blen(tail), tail)

    head = lines[:HEAD_LINES]
    tail = lines[total - TAIL_LINES:]
    middle = lines[HEAD_LINES:total - TAIL_LINES]

    # 中间区域捞回关键行；超过上限时按固定步长均匀抽样（覆盖全局，而非只看前 40 条）
    hits = [line for line in middle if KEY_PATTERN.search(line)]
    key = []
    sampled = False
    if len(hits) > MAX_KEY_LINES:
        sampled = True
        step = (len(hits) + MAX_KEY_LINES - 1) // MAX_KEY_LINES  # 向上取整
        key = hits[::step]
    else:
        key = hits

    budget = budget_bytes - MARKER_RESERVE
    used = 0
    head_shown, used = fill_lines(head, budget * 40 // 100, used)
    key_shown, used = fill_lines(key, budget * 70 // 100, used)
    tail_rev, used = fill_lines(list(reversed(tail)), budget, used)
    tail_shown = list(reversed(tail_rev))

    shown = len(head_shown) + len(key_shown) + len(tail_shown)
    silent = total - shown

    parts = list(head_shown)
    if key_shown:
        if sampled:
            parts.append("… [wb-ops 省略 %d 行，命中关键字的 %d 条中均匀抽取 %d 条如下] …"
                         % (silent, len(hits), len(key_shown)))
        else:
            parts.append("… [wb-ops 省略 %d 行，以下为捞回的 %d 条关键字行] …"
                         % (silent, len(key_shown)))
    elif not hits:
        parts.append("… [wb-ops 省略 %d 行，未发现 error/warning 关键字] …" % silent)
    else:
        parts.append("… [wb-ops 省略 %d 行] …" % silent)
    parts.extend(key_shown)
    if tail_shown:
        parts.append("… [wb-ops 以下为原文末尾 %d 行] …" % len(tail_shown))
        parts.extend(tail_shown)

    res = "\n".join(parts)
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

    try:
        raw = sys.stdin.read()
    except Exception:
        return

    if len(raw) > 8 * 1024 * 1024:  # 防极端大输入占用内存（宿主一般远小于此）
        trace("输入超过 8MB，直接透传")
        return

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

    trimmed = trim(text)
    if trimmed is None:
        trace("工具 %s 输出不长（%d 字符 / %d 行），不裁"
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
