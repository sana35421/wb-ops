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

    return cases


def make_payload(tool, response, event="PostToolUse"):
    obj = {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {},
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
    else:
        checks.append(("两侧都应产生记账文件", False))

    # 开关：WB_OPS_STATS=0 不写
    d_off = tempfile.mkdtemp(prefix="wbops-stats-off-")
    subprocess.run([EXE], input=payload, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS="0", WB_OPS_STATS_DIR=d_off))
    checks.append(("WB_OPS_STATS=0 不写", read_lines(d_off) == []))

    # 边界：无 session_id -> s 为空串
    p2 = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Grep",
                     "tool_response": ["x%d" % i for i in range(300)]}).encode("utf-8")
    d_no = tempfile.mkdtemp(prefix="wbops-stats-nosid-")
    subprocess.run([EXE], input=p2, capture_output=True,
                   env=dict(os.environ, WB_OPS_STATS_DIR=d_no))
    l_no = read_lines(d_no)
    checks.append(("无 session_id -> s 为空串",
                   len(l_no) == 1 and json.loads(l_no[0])["s"] == ""))

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
    for d in (d_ex, d_py, d_off, d_no, d_short, d_pre):
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

    for name, tool, resp in cases:
        event = "PostToolUse"
        payload = make_payload(tool, resp, event)
        # 事件名测试用例需要特殊处理
        if "事件名不对" in name:
            payload = make_payload(tool, resp, "PreToolUse")

        r_py = run([PYTHON, PY_SCRIPT], payload)
        r_ex = run([EXE], payload)
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

    print()
    print("一致：%d / %d    不一致：%d" % (passed, len(cases), failed))
    if assert_fail:
        print("边界断言失败：%d" % assert_fail)

    # ---- v0.3.0 记账专项 ----
    stats_fail = test_stats_parity()

    # ---- v0.3.5 预算专项 ----
    budget_fail = test_budget_env()

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

    # 模拟 hook 真实调用（经 bash 包装）；bash 定位：系统 PATH -> WorkBuddy 便携版
    bash = shutil.which("bash") or ""
    if not bash:
        _g = glob.glob(os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                                    "PortableGit", "versions", "*", "bin", "bash.exe"))
        bash = _g[0] if _g else ""
    if bash and os.path.exists(bash):
        root_msys = to_msys(PLUGIN)  # 永远指向当前所在的这份代码（插件本体），不再硬编码旧路径
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
                   % (to_msys(PYTHON), root_msys))
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
                 budget_fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
