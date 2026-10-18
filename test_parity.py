#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops 一致性 + 性能对比测试

目的：确认原生版 trim.exe 与 Python 版 trim.py 行为完全一致，
      并量化启动开销的下降幅度。

跑法：python test_parity.py
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))  # 仓库根
PLUGIN = os.path.join(ROOT, "plugins", "wb-ops")  # 插件本体（官方市场结构）
PY_SCRIPT = os.path.join(PLUGIN, "hooks", "trim.py")
EXE = os.path.join(PLUGIN, "bin", "wb-ops-trim.exe")
PYTHON = sys.executable


# --------------------------------------------------------------------------
# 构造测试用例：(名称, tool_name, tool_response, 事件名)
# --------------------------------------------------------------------------
def build_cases():
    cases = []

    # 1. 典型 Grep 输出：400 行，中间埋 ERROR / warning
    lines = []
    for i in range(1, 401):
        if i == 150:
            lines.append("parse-line-%d: ERROR undefined reference to solve()" % i)
        elif i == 300:
            lines.append("parse-line-%d: warning: unused variable ret" % i)
        else:
            lines.append("parse-line-%d: ok compiling module" % i)
    cases.append(("Grep 400 行含 ERROR/warning", "Grep", lines))

    # 2. 大量行但无关键字
    cases.append(("Glob 300 行无关键字", "Glob",
                  ["src/module_%03d.cpp" % i for i in range(300)]))

    # 3. 短输出 —— 不该裁
    cases.append(("短输出（不裁）", "Grep", ["a.cpp:1:match", "b.cpp:2:match"]))

    # 4. 中文 + 关键字
    cases.append(("中文含\u201c错误\u201d", "Grep",
                  ["\u7b2c%d\u884c \u6b63\u5e38\u8f93\u51fa" % i for i in range(200)] +
                  ["\u7b2c 99 \u884c \u9519\u8bef\uff1a\u672a\u5b9a\u4e49\u7684\u5f15\u7528"]))

    # 5. 超长单行（行数少、字符多）
    cases.append(("超长单行 20000 字符", "Grep", ["x" * 20000]))

    # 6. MCP 风格 content 数组
    cases.append(("MCP 风格 text 数组", "Grep",
                  [{"type": "text", "text": "line %d" % i} for i in range(200)]))

    # 7. Bash 沙箱元数据（没有正文，应透传）
    cases.append(("Bash 仅元数据（透传）", "Bash",
                  {"exitCode": 1, "signal": None, "sandboxDenied": False,
                   "stdoutBytesTruncated": 0}))

    # 8. Bash 带 stdout
    cases.append(("Bash 带 stdout", "Bash",
                  {"stdout": "\n".join("out %d" % i for i in range(200)), "exitCode": 2}))

    # 9. 工具不在名单（WebSearch 输出紧凑，不纳入裁剪）
    cases.append(("工具不在名单（透传）", "WebSearch", ["a"] * 300))

    # 10. 事件名不对
    cases.append(("事件名不对（透传）", "Grep", ["a"] * 300))

    # 11. 字符串形式的 tool_response
    cases.append(("tool_response 是字符串", "Grep", "\n".join("s %d" % i for i in range(300))))

    # 12. 空数组
    cases.append(("空数组（透传）", "Grep", []))

    # 13. 带转义字符与引号
    cases.append(("含引号与反斜杠", "Grep",
                  ['line %d: "quoted \\ backslash\t tab"' % i for i in range(200)]))

    # 14. 非 ASCII 边界：emoji + 多字节
    cases.append(("emoji 与多字节字符", "Grep",
                  ["\U0001f680 line %d \u4e2d\u6587\u5185\u5bb9" % i for i in range(200)]))

    # 15. 长行组合：100 行 × 800 字符（旧版裁剪后仍 5 万+ 字符，验证字符上界）
    cases.append(("长行 100 行 × 800 字符", "Grep",
                  ["%03d " % i + "x" * 796 for i in range(100)]))

    # 16. 长行 + 关键字：90 行 × 600 字符，中间埋 ERROR / command not found（都靠捞回）
    long_lines = ["%03d " % i + "y" * 596 for i in range(90)]
    long_lines[60] = "%03d ERROR undefined reference to solve()" % 60 + "y" * 550
    long_lines[62] = "%03d bash: foo: command not found" % 62 + "y" * 570
    cases.append(("长行 + 关键字捞回", "Grep", long_lines))

    # 17. 多字节长行：85 行，每行 500 个中文字符（>80 行走行裁剪路径，验证不切坏 UTF-8）
    cases.append(("多字节长行（中文）", "Grep",
                  ["\u7b2c%03d\u884c " % i + "\u6d4b\u8bd5" * 250 for i in range(85)]))

    # 18. 新增关键字覆盖：cannot / denied / timed out / panic / No such file
    kw_lines = []
    for i in range(200):
        if i == 50:
            kw_lines.append("build: cannot find -lboost_system")
        elif i == 80:
            kw_lines.append("open /etc/shadow: Permission denied")
        elif i == 110:
            kw_lines.append("connection timed out after 30s")
        elif i == 140:
            kw_lines.append("thread 'main' panicked at src/main.rs")
        elif i == 170:
            kw_lines.append("gcc: No such file or directory")
        else:
            kw_lines.append("normal line %d" % i)
    cases.append(("新关键字 5 条命中", "Grep", kw_lines))

    # 19. 整行保真：行长约 130 字节（旧版按行均分预算，会被截到 ~69 字节）
    med = ["%03d path/to/file_%d.cpp:%d: some reasonably long match text that "
           "exceeds one hundred bytes for sure" % (i, i, i) for i in range(300)]
    cases.append(("中等长度行整行保留", "Grep", med))

    # 20. 关键字超过 40 条：均匀抽样应覆盖前、中、后，而不是只看最前面 40 条
    many = []
    for i in range(3000):
        if i % 50 == 0:
            many.append("err-%04d: ERROR code %d" % (i, i))
        else:
            many.append("line %d fine" % i)
    cases.append(("关键字超 40 均匀抽样", "Grep", many))

    # 21. WebFetch 长页面（字符串形式，真实形状：宿主传入已转换的 markdown 文本）
    cases.append(("WebFetch 长页面", "WebFetch",
                  "\n".join("line %d: fetched page content with some text here" % i
                            for i in range(400))))

    # 22. WebFetch 短输出 —— 不该裁
    cases.append(("WebFetch 短输出（不裁）", "WebFetch",
                  "# Example Domain\n\nShort body text."))

    # ---- v0.6.1 新增 ----
    # 23. 退出码语义（X3）：bool 不当作退出码（不得出现 [exit code]）
    cases.append(("Bash bool 退出码（不视为退出码）", "Bash",
                  {"stdout": "\n".join("out %d" % i for i in range(200)), "exitCode": True}))

    # 24. 退出码语义（X3）：整值 float（2.0）按 2 处理
    cases.append(("Bash 整值 float 退出码", "Bash",
                  {"stdout": "\n".join("out %d" % i for i in range(200)), "exitCode": 2.0}))

    # 25. 退化输入（S9）：121 行空行 —— 输出不比原文小，应透传
    cases.append(("121 行空行（越裁越大→透传）", "Grep", "\n" * 120))

    # 26. S1：头段关键字行被预算挤出 —— 应被捞回，且不得谎称「未发现」
    lh = ["L%03d %s" % (i, "x" * 180) for i in range(1, 201)]
    lh[44] = "CRITICAL ERROR: database connection failed " + "y" * 160
    cases.append(("头段关键字被挤出后捞回", "Grep", lh))

    # 27. S1：尾段最早一行（被尾段填充挤出）的关键字行 —— 应被捞回
    lt = ["T%03d %s" % (i, "x" * 190) for i in range(1, 201)]
    lt[175] = "warning: tail squeeze " + "z" * 173
    cases.append(("尾段关键字被挤出后捞回", "Grep", lt))

    # 28. S1 文案：关键字全在显示区（第 3 行短行）—— 标记应说明「已包含在显示区域」
    lc = ["short line %03d" % i for i in range(1, 201)]
    lc[2] = "CRITICAL ERROR: boom"
    cases.append(("关键字全在显示区（文案）", "Grep", lc))

    # 29. S1 文案：预算截断（捞回 K 条 < 全文命中 M 条）—— 应补「共命中 M 条」
    # v1.1 头尾自适应后单靠行长撑不满预算，改用 6 条关键字行撑爆捞回区
    lb = ["L%03d %s" % (i, "x" * 245) for i in range(1, 201)]
    for k in (44, 60, 80, 100, 120, 140):
        tag = chr(65 + (44, 60, 80, 100, 120, 140).index(k))
        lb[k] = "ERROR budget-cut %s " % tag + tag.lower() * 1100
    cases.append(("预算截断：共命中提示", "Grep", lb))

    # ---- v0.6.9 新增 ----
    # 30. Bash 成功（宿主真实形状：tool_response 是纯字符串，末尾带 Exit Code: 0）—— 应裁剪
    bash_ok = "Command: pytest -q\nStdout: " + \
        "\n".join("test_%03d PASSED" % i for i in range(600)) + \
        "\nStderr: \nExit Code: 0\nSignal: (none)"
    cases.append(("Bash 字符串 Exit Code 0（裁剪）", "Bash", bash_ok))

    # 31. Bash 失败（Exit Code: 1）—— 报错现场必须完整保留，透传
    bash_bad = "Command: g++ a.cpp\nStdout: " + \
        "\n".join("cc1plus: note %d" % i for i in range(600)) + \
        "\nStderr: a.cpp:3: error: x was not declared\nExit Code: 1\nSignal: (none)"
    cases.append(("Bash 字符串 Exit Code 1（透传）", "Bash", bash_bad))

    # 32. Read 默认关闭 —— 非数据类文件（无路径 / 源码）长文件也原样透传
    read_text = "\n".join("    body_line_%d = compute(x) + 1" % i for i in range(2000))
    cases.append(("Read 非数据类且无路径（透传）", "Read", read_text))

    # 33. Read 开启：>1500 行才生效，保留首尾 + 符号行
    rd = []
    for i in range(2000):
        rd.append("def func_%d(x):" % i if i % 300 == 0 else "    body_%d = compute(x)" % i)
    cases.append(("Read 开启（符号行捞回）", "Read", "\n".join(rd),
                  {"WB_OPS_READ_TRIM": "1"}))

    # 34. 行号区间标记：省略段起止行号应可读（第 X–Y 行）
    rng = ["line %d: ok" % i for i in range(400)]
    rng[200] = "ERROR: boom"
    cases.append(("省略标记含行号区间", "Grep", rng))

    # ---- v1.2 新增：Read 按文件类型分档 ----
    log_lines = []
    for i in range(600):
        if i in (120, 430):
            log_lines.append("2026-10-17 12:%02d:%02d ERROR connect timeout" % (i // 60, i % 60))
        else:
            log_lines.append("2026-10-17 12:%02d:%02d INFO  handled request id=%d"
                             % (i // 60, i % 60, i))

    # 35. 数据类 .log：普通关键字模式 + 200 行门槛 —— 应裁剪
    cases.append(("Read .log 数据类（裁剪）", "Read", "\n".join(log_lines),
                  {}, {"file_path": "D:/tmp/service.log"}))

    # 36. 数据类但只有 120 行 —— 门槛未达，透传
    cases.append(("Read .log 未达 200 行（透传）", "Read", "\n".join(log_lines[:120]),
                  {}, {"file_path": "D:/tmp/short.log"}))

    # 37. 源码类 .py 2000 行、没设 WB_OPS_READ_TRIM —— 透传（源码要完整读）
    src = []
    for i in range(2000):
        src.append("def func_%d(x):" % i if i % 300 == 0 else "    body_%d = compute(x)" % i)
    src = "\n".join(src)
    cases.append(("Read .py 未开 --with-read（透传）", "Read", src,
                  {}, {"file_path": "D:/tmp/module.py"}))

    # 38. 同文件 + WB_OPS_READ_TRIM=1 —— 走符号行档
    cases.append(("Read .py 开 --with-read（符号行）", "Read", src,
                  {"WB_OPS_READ_TRIM": "1"}, {"file_path": "D:/tmp/module.py"}))

    # 39. 大写扩展名 + 反斜杠路径（ext 判定与宿主 glob 都要求大小写不敏感）
    cases.append(("Read .LOG 大写扩展名（裁剪）", "Read", "\n".join(log_lines),
                  {}, {"file_path": "D:\\tmp\\SERVICE.LOG"}))

    # 40. tool_input 用 path 键而非 file_path（不同工具键名不同，两个都要认）
    cases.append(("Read tool_input.path 键（裁剪）", "Read", "\n".join(log_lines),
                  {}, {"path": "D:/tmp/other.jsonl"}))

    # ---- v1.2.2 新增：结构类 .json 单独一档 ----
    # 结构类触发条件与日志不同：要「行数 > 800 且字节 > 预算」两项都过线才裁。
    # 所以 700 行 / 15KB 的配置（字节已超预算）应当透传——旧规则只看字节会误裁。
    def json_dump(n):
        """造 n 行、每行约 22 字节的 json 字段（700 行≈15KB，850 行≈19KB）。"""
        rows = ["{"]
        for i in range(n):
            rows.append('  "key_%04d": %d,' % (i, i * 7 + 1))
        rows.append('  "end": 0')
        rows.append("}")
        return "\n".join(rows)
    # 41. .json 700 行 / ~15KB：字节超预算但行数未过 800 → 透传
    cases.append(("Read .json 700 行（透传）", "Read", json_dump(700),
                  {}, {"file_path": "D:/tmp/package-lock.json"}))
    # 42. .json 850 行 / ~19KB：两项都过线 → 裁剪
    cases.append(("Read .json 850 行（裁剪）", "Read", json_dump(850),
                  {}, {"file_path": "D:/tmp/package-lock.json"}))
    # 43. 对照：日志类仍是 200 行且只看单项，600 行 .log（~28KB）照裁
    cases.append(("Read .log 600 行（裁剪）", "Read", "\n".join(log_lines[:600]),
                  {}, {"file_path": "D:/tmp/service.log"}))
    # 44. 硬阀：行数很少但体量巨大的 dump（30 行 / ~60KB）仍要裁
    big_rows = ["{"] + ['  "blob": "%s",' % ("x" * 2000) for _ in range(28)] + ["}"]
    cases.append(("Read .json 30 行超大（硬阀裁剪）", "Read", "\n".join(big_rows),
                  {}, {"file_path": "D:/tmp/dump.json"}))

    # ---- v1.3.0 新增：文档档 .md ----
    # 门槛同结构档（800 行且超预算），但中段捞回特征换成 Markdown 标题行：
    # md 里没有 error 关键字可言，按关键字捞只会捞出一堆噪声。
    def md_doc(n, headings=True, tail_tag=False):
        """造 n 行 md：每 100 行一个二级标题，其余是短正文行。

        刻意把每行压到 20 字节以内：700 行≈13KB，能超 8KB 预算但够不到 40KB 硬阀，
        这样「700 行透传」才真的在验证 800 行门槛，而不是被体量硬阀顺手裁掉。
        """
        rows = []
        for i in range(n):
            if headings and i % 100 == 0:
                rows.append("## section %d" % (i // 100))
            elif tail_tag and i == n - 1:
                rows.append("#hashtag 不是标题")   # '#' 后无空白 → 不算标题
            else:
                rows.append("body line %d text" % i)
        return "\n".join(rows)

    # 45. .md 700 行：行数未过 800 → 透传（与结构档同一道门槛）
    cases.append(("Read .md 700 行（透传）", "Read", md_doc(700),
                  {}, {"file_path": "D:/tmp/guide.md"}))

    # 46. .md 1200 行带标题：两项都过线 → 裁剪，且捞回的是标题行
    cases.append(("Read .md 1200 行（标题行捞回）", "Read", md_doc(1200),
                  {}, {"file_path": "D:/tmp/guide.md"}))

    # 47. .md 无标题：标记应说「未发现 Markdown 标题」，不能套用 error 关键字文案
    cases.append(("Read .md 无标题（文案）", "Read", md_doc(1200, headings=False),
                  {}, {"file_path": "D:/tmp/plain.md"}))

    # 48. 大写 .MD + 反斜杠路径 + 行尾 `#hashtag`（不算标题）→ 仍走文档档
    cases.append(("Read .MD 大写（文档档）", "Read", md_doc(1200, tail_tag=True),
                  {}, {"file_path": "D:\\tmp\\README.MD"}))

    return cases


def make_payload(tool, response, event="PostToolUse", tool_input=None):
    obj = {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input or {},
        "tool_response": response,
    }
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# 主流程/性能测试统一关闭记账（记账一致性由 test_stats_parity 专项覆盖），
# 防止测试流量污染真实的 ~/.workbuddy/wb-ops-data/saves-*.jsonl
ENV_NO_STATS = dict(os.environ, WB_OPS_STATS="0")


def run(cmd, payload, env=ENV_NO_STATS):
    return subprocess.run(cmd, input=payload, capture_output=True, env=env).stdout


def normalize(out):
    """把输出规整成可比对的 updatedToolOutput（None 表示'不改动'）"""
    if not out or not out.strip():
        return None
    try:
        d = json.loads(out.decode("utf-8"))
    except Exception:
        return ("UNPARSEABLE", out[:150])
    h = d.get("hookSpecificOutput") or {}
    v = h.get("updatedToolOutput")
    return v


def to_msys(path):
    """Windows 路径 -> MSYS/Git Bash 路径（D:\\wb-ops -> /d/wb-ops）"""
    p = os.path.abspath(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def test_stats_parity():
    """v0.3.0 记账专项：格式、双实现一致（除 t）、数值正确、开关与边界。"""
    print()
    print("=" * 72)
    print("v0.3.0 节省记账测试")
    print("=" * 72)

    fails = 0
    raw_lines = ["line %d: some text here for testing purposes" % i for i in range(500)]
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "abcdef12-3456-7890-abcd-ef1234567890",
        "tool_name": "Grep",
        "tool_input": {},
        "tool_response": raw_lines,
    }, ensure_ascii=False).encode("utf-8")

    def read_lines(d):
        fs = glob.glob(os.path.join(d, "saves-*.jsonl"))
        if not fs:
            return []
        with open(fs[0], encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def read_raw(d):
        """二进制读取首份记账文件（S4：逐字节比对用，不做任何行尾归一化）。"""
        fs = glob.glob(os.path.join(d, "saves-*.jsonl"))
        return open(fs[0], "rb").read() if fs else b""

    d_ex = tempfile.mkdtemp(prefix="wbops-stats-ex-")
    d_py = tempfile.mkdtemp(prefix="wbops-stats-py-")
    r_ex = subprocess.run([EXE], input=payload, capture_output=True,
                          env=dict(os.environ, WB_OPS_STATS_DIR=d_ex))
    subprocess.run([PYTHON, PY_SCRIPT], input=payload, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_py))
    l_ex, l_py = read_lines(d_ex), read_lines(d_py)

    checks = []
    checks.append(("exe/py 各写 1 行", len(l_ex) == 1 and len(l_py) == 1))
    if l_ex and l_py:
        o_ex, o_py = json.loads(l_ex[0]), json.loads(l_py[0])
        t_ex, t_py = o_ex.pop("t"), o_py.pop("t")
        checks.append(("除 t 外逐字段一致", o_ex == o_py))
        checks.append(("字段恰为 s/tool/b/a", set(o_ex) == {"s", "tool", "b", "a"}))
        checks.append(("sid 取前 8 位", o_ex["s"] == "abcdef12"))
        checks.append(("tool 正确", o_ex["tool"] == "Grep"))
        src = "\n".join(raw_lines).encode("utf-8")
        trimmed = normalize(r_ex.stdout)
        checks.append(("b=原始字节", o_ex["b"] == len(src)))
        checks.append(("a=裁剪后字节",
                       isinstance(trimmed, str) and o_ex["a"] == len(trimmed.encode("utf-8"))))
        checks.append(("t 是毫秒时间戳", isinstance(t_ex, int) and 10 ** 12 < t_ex < 10 ** 14))
        # S4（v0.6.1）：二进制逐字节比对（含行尾）——此前文本模式读取恰好把 \r\n 归一化，测不出差异
        b_ex, b_py = read_raw(d_ex), read_raw(d_py)
        strip_t = lambda b: re.sub(rb'"t":\d+', b'"t":T', b)
        checks.append(("记账行按二进制比对一致（仅 t 不同）",
                       bool(b_ex) and strip_t(b_ex) == strip_t(b_py)))
        checks.append(("记账行尾为 LF（两侧均无 \\r\\n）",
                       b_ex.endswith(b"}\n") and b"\r" not in b_ex and b"\r" not in b_py))
    else:
        checks.append(("两侧都应产生记账文件", False))

    # S4 附带（X2）：sid 含引号/反斜杠 —— JSON 转义后仍为合法 JSONL，双实现一致
    payload2 = json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": 'ab"c\\d-12345678',
        "tool_name": "Grep",
        "tool_input": {},
        "tool_response": raw_lines,
    }, ensure_ascii=False).encode("utf-8")
    d_ex2 = tempfile.mkdtemp(prefix="wbops-stats-ex2-")
    d_py2 = tempfile.mkdtemp(prefix="wbops-stats-py2-")
    subprocess.run([EXE], input=payload2, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_ex2))
    subprocess.run([PYTHON, PY_SCRIPT], input=payload2, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_py2))
    b_ex2, b_py2 = read_raw(d_ex2), read_raw(d_py2)
    checks.append(("特殊 sid：双实现记账行一致（t 除外）",
                   bool(b_ex2) and strip_t(b_ex2) == strip_t(b_py2)))
    try:
        ok_special = json.loads(b_ex2.decode("utf-8"))["s"] == 'ab"c\\d-1'
    except Exception:
        ok_special = False
    checks.append(("特殊 sid：仍是合法 JSONL 且按 8 字节截断", ok_special))

    # 开关：WB_OPS_STATS=0 不写
    d_off = tempfile.mkdtemp(prefix="wbops-stats-off-")
    subprocess.run([EXE], input=payload, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS="0", WB_OPS_STATS_DIR=d_off))
    checks.append(("WB_OPS_STATS=0 不写", read_lines(d_off) == []))

    # 边界：无 session_id -> v1.1 起不入账（无法归属会话，视为探头）
    p2 = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Grep",
                     "tool_response": ["x%d" % i for i in range(300)]}).encode("utf-8")
    d_no = tempfile.mkdtemp(prefix="wbops-stats-nosid-")
    subprocess.run([EXE], input=p2, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_no))
    d_no_py = tempfile.mkdtemp(prefix="wbops-stats-nosid-py-")
    subprocess.run([PYTHON, PY_SCRIPT], input=p2, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_no_py))
    checks.append(("无 session_id -> 不入账（双实现一致）",
                   read_lines(d_no) == [] and read_lines(d_no_py) == []))

    # 边界：短输出（不裁剪）不记账
    p3 = json.dumps({"hook_event_name": "PostToolUse", "session_id": "zzzz0000-1111",
                     "tool_name": "Grep", "tool_response": ["short"]}).encode("utf-8")
    d_short = tempfile.mkdtemp(prefix="wbops-stats-short-")
    subprocess.run([EXE], input=p3, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_short))
    checks.append(("短输出（不裁剪）不记账", read_lines(d_short) == []))

    # 边界：非 PostToolUse 事件不记账
    p4 = json.dumps({"hook_event_name": "PreToolUse", "session_id": "zzzz0000-1111",
                     "tool_name": "Grep", "tool_response": ["y%d" % i for i in range(300)]}).encode("utf-8")
    d_pre = tempfile.mkdtemp(prefix="wbops-stats-pre-")
    subprocess.run([EXE], input=p4, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_pre))
    checks.append(("PreToolUse 事件不记账", read_lines(d_pre) == []))

    for name, ok in checks:
        print("  %s  %s" % ("OK  " if ok else "FAIL", name))
        if not ok:
            fails += 1
    for d in (d_ex, d_py, d_ex2, d_py2, d_off, d_no, d_short, d_pre):
        shutil.rmtree(d, ignore_errors=True)
    return fails


def test_budget_env():
    """v0.3.5 预算专项：WB_OPS_BUDGET 覆盖、双实现一致、边界收敛。"""
    print()
    print("=" * 72)
    print("v0.3.5 预算可配置测试（WB_OPS_BUDGET）")
    print("=" * 72)

    # 200 行 × ~90 字节：行路径下预算会被真实消耗（head/tail 都超过各自额度）
    line = ("path/to/some/module/file_%03d.cpp:%d: this is a reasonably "
            "long match line padding here")
    lines = [line % (i, i) for i in range(200)]
    payload = json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "Grep",
        "tool_input": {}, "tool_response": lines,
    }, ensure_ascii=False).encode("utf-8")

    def run_env(prog, extra):
        env = dict(os.environ, WB_OPS_STATS="0", **extra)
        out = subprocess.run(prog, input=payload, capture_output=True, env=env).stdout
        return normalize(out)

    checks = []
    ex_default = run_env([EXE], {})
    checks.append(("默认预算双实现一致",
                   run_env([PYTHON, PY_SCRIPT], {}) == ex_default))

    outs = {}
    for v in ("2000", "4000", "8000", "16000"):
        a = run_env([PYTHON, PY_SCRIPT], {"WB_OPS_BUDGET": v})
        b = run_env([EXE], {"WB_OPS_BUDGET": v})
        outs[v] = b
        checks.append(("预算 %s 双实现一致 / 输出%s字节" % (v, len(b.encode("utf-8"))), a == b))
    sizes = [len(outs[v].encode("utf-8")) for v in ("2000", "4000", "8000", "16000")]
    checks.append(("预算越大输出越大（严格单调）", sizes[0] < sizes[1] < sizes[2] < sizes[3]))
    checks.append(("输出不超过各自预算", all(s <= b for s, b in
                                         zip(sizes, (2000, 4000, 8000, 16000)))))

    # 非法值回退默认；带空白/前导零按数值处理
    ok = all(run_env([EXE], {"WB_OPS_BUDGET": v}) == outs["8000"]
             for v in ("abc", "12x3", "", "  "))
    checks.append(("非法值回退默认 8000", ok))
    checks.append(("前导零按数值解析（000016000 == 16000）",
                   run_env([EXE], {"WB_OPS_BUDGET": "000016000"}) == outs["16000"]))
    checks.append(("两端空白被忽略（\"  16000 \"）",
                   run_env([EXE], {"WB_OPS_BUDGET": "  16000 "}) == outs["16000"]))

    # 越界收敛：<2000 收敛到 2000；10 位以上收敛到 131072（本例内容不裁 -> 透传）
    mn = run_env([EXE], {"WB_OPS_BUDGET": "2000"})
    checks.append(("下界收敛（0/999 同 2000）",
                   run_env([EXE], {"WB_OPS_BUDGET": "0"}) == mn and
                   run_env([EXE], {"WB_OPS_BUDGET": "999"}) == mn))
    checks.append(("上界收敛两侧一致（超位数）",
                   run_env([EXE], {"WB_OPS_BUDGET": "99999999999"}) ==
                   run_env([PYTHON, PY_SCRIPT], {"WB_OPS_BUDGET": "99999999999"})))

    for name, ok in checks:
        print("  %s  %s" % ("OK  " if ok else "FAIL", name))
    return sum(1 for _, ok in checks if not ok)


def test_io_chain():
    """v0.6.1 输入链专项：BOM（S5）/ 孤立代理项（S3）。"""
    print()
    print("=" * 72)
    print("v0.6.1 输入链测试（BOM / 孤立代理项）")
    print("=" * 72)

    checks = []
    big = "\n".join("line %03d %s" % (i, "z" * 40) for i in range(1, 301))
    payload = make_payload("Grep", big)

    # S5：带 UTF-8 BOM 的输入 —— 双实现均应裁剪且逐字节一致（此前 py 侧透传）
    bom = b"\xef\xbb\xbf" + payload
    n_py = normalize(run([PYTHON, PY_SCRIPT], bom))
    n_ex = normalize(run([EXE], bom))
    checks.append(("BOM 输入：双实现均裁剪且一致",
                   isinstance(n_py, str) and n_py == n_ex))

    # S3：孤立代理项（合法 JSON 转义载荷）—— 不得崩溃；输出结构合法即可（不追字节 parity）
    obj = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "session_id": "s3",
           "tool_response": "\ud800\n" * 300}
    raw = json.dumps(obj, ensure_ascii=True).encode("ascii")
    p_py = subprocess.run([PYTHON, PY_SCRIPT], input=raw, capture_output=True, env=ENV_NO_STATS)
    checks.append(("代理项输入：py 返回码 0（不崩溃）", p_py.returncode == 0))
    u = normalize(p_py.stdout)
    checks.append(("代理项输入：py 输出结构合法（透传或裁剪均可）",
                   u is None or isinstance(u, str)))
    p_ex = subprocess.run([EXE], input=raw, capture_output=True, env=ENV_NO_STATS)
    checks.append(("代理项输入：exe 返回码 0（不崩溃）", p_ex.returncode == 0))

    for name, ok in checks:
        print("  %s  %s" % ("OK  " if ok else "FAIL", name))
    return sum(1 for _, ok in checks if not ok)


def main():
    if not os.path.exists(EXE):
        print("!! 未找到 %s，请先编译" % EXE)
        return 1

    cases = build_cases()
    print("=" * 72)
    print("wb-ops 一致性测试：Python 版 vs 原生版")
    print("=" * 72)

    passed = 0
    failed = 0
    assert_fail = 0
    ratios = []

    for case in cases:
        name, tool, resp = case[0], case[1], case[2]
        extra_env = case[3] if len(case) > 3 else {}
        tool_input = case[4] if len(case) > 4 else {}
        event = "PostToolUse"
        payload = make_payload(tool, resp, event, tool_input)
        # 事件名测试用例需要特殊处理
        if "事件名不对" in name:
            payload = make_payload(tool, resp, "PreToolUse", tool_input)

        env = dict(ENV_NO_STATS, **extra_env)
        r_py = run([PYTHON, PY_SCRIPT], payload, env)
        r_ex = run([EXE], payload, env)
        n_py = normalize(r_py)
        n_ex = normalize(r_ex)

        same = (n_py == n_ex)
        if same:
            passed += 1
            mark = "OK  "
        else:
            failed += 1
            mark = "DIFF"

        # 统计压缩率
        info = ""
        if isinstance(n_ex, str) and isinstance(resp, (list, str)):
            try:
                src = "\n".join(x if isinstance(x, str) else x.get("text", "") for x in resp) \
                    if isinstance(resp, list) else resp
                if len(src) > 0:
                    ratio = 1 - len(n_ex) / len(src)
                    ratios.append((name, ratio))
                    info = "裁剪 %d->%d 字符 (省 %.0f%%)" % (len(src), len(n_ex), ratio * 100)
            except Exception:
                pass
        elif n_ex is None:
            info = "透传（未裁剪）"

        print("  %s  %-26s %s" % (mark, name, info))
        if not same:
            print("        python -> %s" % (str(n_py)[:160],))
            print("        native -> %s" % (str(n_ex)[:160],))

        # ---- v1.2.2：结构类 .json 门槛 800 行（三条都要在 isinstance 判断之外：
        #      透传时 n_ex 为 None，放进下面的块里就永远执行不到）----
        if name == "Read .json 700 行（透传）" and n_ex is not None:
            assert_fail += 1
            print("        !! 结构类 700 行被裁了：结构类要行数、字节都过线才裁")
        if name == "Read .json 850 行（裁剪）" and n_ex is None:
            assert_fail += 1
            print("        !! 结构类 850 行未裁剪：800 行门槛失效")
        if name == "Read .log 600 行（裁剪）" and n_ex is None:
            assert_fail += 1
            print("        !! 日志类 600 行未裁剪：日志门槛应保持 200 行")
        if name == "Read .json 30 行超大（硬阀裁剪）" and n_ex is None:
            assert_fail += 1
            print("        !! 超大 dump 未裁剪：40KB 硬阀失效")

        # ---- v1.3.0：文档档 .md（同样要放在 isinstance 之外）----
        if name == "Read .md 700 行（透传）" and n_ex is not None:
            assert_fail += 1
            print("        !! .md 700 行被裁了：文档档应与结构档同门槛（800 行且超预算）")
        if name == "Read .md 1200 行（标题行捞回）" and n_ex is None:
            assert_fail += 1
            print("        !! .md 1200 行未裁剪：文档档门槛失效")

        # ---- 专项断言：字节上界 / 整行保真 / 关键行捞回 / 抽样覆盖 ----
        if isinstance(n_ex, str):
            blen_ex = len(n_ex.encode("utf-8"))
            if blen_ex > 8130:
                assert_fail += 1
                print("        !! 裁剪结果超出 8000 字节上界：实际 %d 字节" % blen_ex)
            if name == "长行 + 关键字捞回" and not ("ERROR" in n_ex and "command not found" in n_ex):
                assert_fail += 1
                print("        !! 关键行未被捞回")
            if name == "新关键字 5 条命中" and sum(
                    w in n_ex for w in ("cannot find", "Permission denied",
                                        "timed out", "panicked", "No such file")) < 5:
                assert_fail += 1
                print("        !! 新关键字未全部捞回")
            if name == "中等长度行整行保留" and (
                    "path/to/file_0.cpp:0: some reasonably long match text "
                    "that exceeds one hundred bytes for sure" not in n_ex):
                assert_fail += 1
                print("        !! 中等长度行未被完整保留（整行保真失败）")
            if name == "关键字超 40 均匀抽样" and not ("err-0050" in n_ex and "err-2950" in n_ex):
                assert_fail += 1
                print("        !! 关键字超限抽样未覆盖全局")
            # ---- v1.3.0：文档档捞回特征 ----
            if name == "Read .md 1200 行（标题行捞回）" and not (
                    "以下为捞回的" in n_ex and "条标题行" in n_ex and "## section" in n_ex):
                assert_fail += 1
                print("        !! .md 未按标题行捞回（应出现「条标题行」与 ## section）")
            if name == "Read .md 无标题（文案）" and "未发现 Markdown 标题" not in n_ex:
                assert_fail += 1
                print("        !! .md 无标题时文案不对：应写明未发现 Markdown 标题")
            if name == "Read .MD 大写（文档档）" and "未发现 Markdown 标题" in n_ex:
                assert_fail += 1
                print("        !! `#hashtag` 被误判成标题（'#' 后无空白不算标题）")
            # 行号可用性只跟「工具是不是 Read」有关，与裁剪档位无关：
            # 数据类 Read 走关键字模式（read_mode 为假），但它照样能用 offset 回查。
            if name == "Read .log 数据类（裁剪）" and "可 Read offset=" not in n_ex:
                assert_fail += 1
                print("        !! 数据类 Read 没给 offset 回查指路（line_mode 判定错了）")
            if name == "关键字超 40 均匀抽样" and "可 Read offset=" in n_ex:
                assert_fail += 1
                print("        !! Grep 结果不该给 offset 指路：它没有文件行号")

            # ---- v0.6.1 新增断言 ----
            if name == "头段关键字被挤出后捞回" and not (
                    "database connection failed" in n_ex and "未发现" not in n_ex):
                assert_fail += 1
                print("        !! 头段关键字未捞回，或标记仍谎称未发现")
            if name == "尾段关键字被挤出后捞回" and "warning: tail squeeze" not in n_ex:
                assert_fail += 1
                print("        !! 尾段关键字未捞回")
            if name == "关键字全在显示区（文案）" and not (
                    "关键字行已包含在显示区域" in n_ex and "未发现" not in n_ex):
                assert_fail += 1
                print("        !! 显示区文案不正确")
            if name == "预算截断：共命中提示" and "共命中 6 条" not in n_ex:
                assert_fail += 1
                print("        !! 未给出「共命中 M 条」的预算截断提示")
            if name == "Bash bool 退出码（不视为退出码）" and "[exit code" in n_ex:
                assert_fail += 1
                print("        !! bool 被误当作退出码")
            if name == "Bash 整值 float 退出码" and "[exit code 2]" not in n_ex:
                assert_fail += 1
                print("        !! 整值 float 退出码未被带上")
            # ---- v0.6.9 新增断言 ----
            if name == "Bash 字符串 Exit Code 0（裁剪）" and "wb-ops 省略" not in n_ex:
                assert_fail += 1
                print("        !! Bash 成功输出未被裁剪")
            if name == "Read 开启（符号行捞回）" and not (
                    "符号行" in n_ex and "def func_0" in n_ex and "offset" in n_ex):
                assert_fail += 1
                print("        !! Read 模式未保留符号行，或标记文案缺符号行/offset 提示")
            if name == "省略标记含行号区间" and not re.search(r"第 \d+\u2013\d+ 行", n_ex):
                assert_fail += 1
                print("        !! 省略标记缺行号区间")
        else:
            if name == "121 行空行（越裁越大→透传）" and n_ex is not None:
                assert_fail += 1
                print("        !! 越裁越大时未透传")
            # ---- v0.6.9 新增断言（应透传的用例）----
            if name == "Bash 字符串 Exit Code 1（透传）" and n_ex is not None:
                assert_fail += 1
                print("        !! Bash 失败输出被裁剪，报错现场丢失")
            if name == "Read 默认关闭（透传）" and n_ex is not None:
                assert_fail += 1
                print("        !! Read 未开启按需裁剪却改了输出")

    print()
    print("一致：%d / %d    不一致：%d" % (passed, len(cases), failed))
    if assert_fail:
        print("边界断言失败：%d" % assert_fail)

    # ---- v0.3.0 记账专项 ----
    stats_fail = test_stats_parity()

    # ---- v0.3.5 预算专项 ----
    budget_fail = test_budget_env()

    # ---- v0.6.1 输入链专项 ----
    io_fail = test_io_chain()

    # ---- 性能对比 ----
    print()
    print("=" * 72)
    print("性能对比（400 行 Grep 输出，各跑 10 次取平均）")
    print("=" * 72)

    payload = make_payload("Grep", build_cases()[0][2])

    # 预热
    run([PYTHON, PY_SCRIPT], payload)
    run([EXE], payload)

    N = 10
    t0 = time.perf_counter()
    for _ in range(N):
        run([PYTHON, PY_SCRIPT], payload)
    t_py = (time.perf_counter() - t0) / N * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        run([EXE], payload)
    t_ex = (time.perf_counter() - t0) / N * 1000

    print("  Python 版（裸 python 直调） : %7.1f ms" % t_py)
    print("  原生版  （裸 exe 直调）     : %7.1f ms" % t_ex)
    print("  提速                        : %7.1f 倍" % (t_py / t_ex if t_ex else 0))

    # 模拟 hook 真实调用（经 bash 包装）
    bash = os.environ.get("WB_OPS_TEST_BASH") or shutil.which("bash") or ""
    if not bash:
        for cand in ("C:/Program Files/Git/bin/bash.exe",
                     "C:/Program Files/Git/usr/bin/bash.exe",
                     "C:/Program Files (x86)/Git/bin/bash.exe"):
            if os.path.exists(cand):
                bash = cand
                break
    if os.path.exists(bash):
        root_msys = to_msys(PLUGIN)  # 永远指向当前所在的这份代码，不再硬编码旧路径
        cmd_new = '"%s/bin/wb-ops-trim.exe" 2>/dev/null || exit 0' % root_msys
        env = {"CODEBUDDY_PLUGIN_ROOT": root_msys, "PATH": "/usr/bin:/bin",
               "TMP": "/tmp", "WB_OPS_STATS": "0"}
        subprocess.run([bash, "-c", cmd_new], input=payload, capture_output=True, env=env)
        t0 = time.perf_counter()
        for _ in range(N):
            r = subprocess.run([bash, "-c", cmd_new], input=payload,
                               capture_output=True, env=env, text=False)
        t_bash_new = (time.perf_counter() - t0) / N * 1000

        cmd_old = ('for PY in "$(command -v python)" "%s"; do [ -n "$PY" ] && [ -x "$PY" ] '
                   '&& "$PY" "%s/hooks/trim.py" && break; done; exit 0'
                   % (to_msys(sys.executable), root_msys))
        subprocess.run([bash, "-c", cmd_old], input=payload, capture_output=True, env=env)
        t0 = time.perf_counter()
        for _ in range(N):
            r2 = subprocess.run([bash, "-c", cmd_old], input=payload,
                                capture_output=True, env=env, text=False)
        t_bash_old = (time.perf_counter() - t0) / N * 1000

        print()
        print("  hook 真实调用形式（bash -c 包装）:")
        print("    改造前（bash + python）   : %7.1f ms" % t_bash_old)
        print("    改造后（bash + exe）      : %7.1f ms" % t_bash_new)
        print("    提速                      : %7.1f 倍" % (t_bash_old / t_bash_new if t_bash_new else 0))
        print()
        ok = bool(r.stdout.strip())
        print("    经 bash 调用是否有输出    : %s" % ("有 ✓" if ok else "无 ✗"))

    print()
    return 0 if (failed == 0 and assert_fail == 0 and stats_fail == 0 and
                 budget_fail == 0 and io_fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
