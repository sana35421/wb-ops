#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-ops 0.2.2 稳定性测试。

覆盖：
  1. 边界用例表：空/垃圾/类型异常/阈值边界/8MB 护栏/转义密集
  2. fuzz：随机文本 × 随机形态，C++ 与 Python 逐字节一致 + 输出合法 + ≤8000 字节
  3. 幂等：对裁剪结果再处理，应不再触发裁剪
  4. 确定性：同输入重复运行，输出哈希唯一
  5. 并发：16 进程同时跑，输出全同
  6. 长跑：300 次混合负载零失败

用法: python test_stability.py [fuzz_n=240]
退出码：0 全部通过；1 有失败。
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))  # 仓库根
PLUGIN = os.path.join(ROOT, "plugins", "wb-ops")  # 插件本体（官方市场结构）
EXE = os.path.join(PLUGIN, "bin", "wb-ops-trim.exe")
PY_IMPL = os.path.join(PLUGIN, "hooks", "trim.py")
PYTHON = sys.executable
# v0.3.0 起：测试流量不写入真实记账数据（~/.workbuddy/wb-ops-data/）
os.environ["WB_OPS_STATS"] = "0"
PY_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
MAX_BYTES = 8000
MB = 1024 * 1024

FAILS = []


def fail(name, detail):
    FAILS.append((name, detail))
    print("  FAIL | %s | %s" % (name, detail))


def check(name, cond, detail=""):
    if not cond:
        fail(name, detail)
    return cond


def payload_of(tool, resp, ascii_escape=False):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": tool,
                       "tool_input": {}, "tool_response": resp},
                      ensure_ascii=ascii_escape).encode("utf-8")


def run_one(cmd, payload, env=None):
    try:
        r = subprocess.run(cmd, input=payload, capture_output=True,
                           timeout=60, env=env)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return -999, b"<timeout>"


def run_exe(p):
    return run_one([EXE], p)


def run_py(p):
    return run_one([PYTHON, PY_IMPL], p, env=PY_ENV)


def trimmed_of(name, out, side):
    """输出空 → None；否则解析结构返回 updatedToolOutput；结构非法返回 '!INVALID'。"""
    if not out:
        return None
    try:
        obj = json.loads(out.decode("utf-8"))
        ho = obj["hookSpecificOutput"]
        t = ho["updatedToolOutput"]
        assert isinstance(t, str) and ho["hookEventName"] == "PostToolUse"
    except Exception as e:
        fail(name, "%s 输出结构非法: %s" % (side, e))
        return "!INVALID"
    return t


def valid_utf8(b):
    try:
        b.decode("utf-8")
        return True
    except Exception:
        return False


def verify_pair(name, payload, expect=None):
    """两侧一起跑：exit 0、结构合法、裁剪一致、逐字节相同、≤8000、输出带标记。

    expect: "pass" 期望透传 / "trim" 期望裁剪 / None 只要求两侧一致。
    返回 (t_exe, t_py)（结构失败返回 None）。
    """
    rc1, o1 = run_exe(payload)
    rc2, o2 = run_py(payload)
    if rc1 != 0:
        fail(name, "C++ 退出码 %d (out=%d 字节)" % (rc1, len(o1)))
    if rc2 != 0:
        fail(name, "Python 退出码 %d" % rc2)
    t1 = trimmed_of(name, o1, "C++")
    t2 = trimmed_of(name, o2, "Python")
    if t1 == "!INVALID" or t2 == "!INVALID":
        return None
    if (t1 is None) != (t2 is None):
        fail(name, "两侧裁剪与否不一致: cpp=%s py=%s" %
             (t1 is not None, t2 is not None))
        return None
    if t1 is not None:
        b1, b2 = t1.encode("utf-8"), t2.encode("utf-8")
        if b1 != b2:
            fail(name, "两侧输出不同: cpp %d 字节 / py %d 字节" % (len(b1), len(b2)))
        if len(b1) > MAX_BYTES:
            fail(name, "输出超上界: %d 字节" % len(b1))
        if not valid_utf8(b1):
            fail(name, "输出非合法 UTF-8")
        if "wb-ops" not in t1:
            fail(name, "输出缺少 wb-ops 标记")
    if expect == "pass" and t1 is not None:
        fail(name, "期望透传，实际裁剪了")
    if expect == "trim" and t1 is None:
        fail(name, "期望裁剪，实际透传了")
    return (t1, t2)


def verify_idempotent(name, t):
    """对裁剪结果再处理一次：不应再产生输出。"""
    if t is None:
        return
    p = payload_of("Grep", t)
    rc, o = run_exe(p)
    if rc != 0 or o:
        fail(name + " [幂等C++]", "二次处理有输出 %d 字节 / rc=%d" % (len(o), rc))
    rc, o = run_py(p)
    if rc != 0 or o:
        fail(name + " [幂等Py]", "二次处理有输出 %d 字节 / rc=%d" % (len(o), rc))


# ---------------------------------------------------------------------------
# 边界用例表
# ---------------------------------------------------------------------------
def build_edges():
    cs = []
    cs.append(("空 stdin", b"", "pass"))
    cs.append(("非 JSON 文本", b"hello world, not json at all", "pass"))
    cs.append(("二进制垃圾", bytes(range(256)) * 8, "pass"))
    cs.append(("JSON 数组非对象", b"[1,2,3]", "pass"))
    cs.append(("JSON null", b"null", "pass"))
    cs.append(("缺 hook_event_name",
               b'{"tool_name":"Grep","tool_response":"' + b"x" * 9000 + b'"}', "pass"))
    cs.append(("事件名错误",
               b'{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_response":"'
               + b"x" * 9000 + b'"}', "pass"))
    cs.append(("缺 tool_name",
               b'{"hook_event_name":"PostToolUse","tool_response":"'
               + b"x" * 9000 + b'"}', "pass"))
    cs.append(("tool_name 非字符串",
               b'{"hook_event_name":"PostToolUse","tool_name":123,"tool_response":"'
               + b"x" * 9000 + b'"}', "pass"))
    cs.append(("缺 tool_response", b'{"hook_event_name":"PostToolUse","tool_name":"Grep"}', "pass"))
    cs.append(("工具不在名单", payload_of("WebSearch", "x" * 9000), "pass"))
    cs.append(("tool_response 为 null", payload_of("Grep", None), "pass"))
    cs.append(("tool_response 为数字", payload_of("Grep", 12345), "pass"))
    cs.append(("tool_response 为布尔", payload_of("Grep", True), "pass"))
    cs.append(("tool_response 为空串", payload_of("Grep", ""), "pass"))
    cs.append(("tool_response 为空数组", payload_of("Grep", []), "pass"))
    cs.append(("数组含非字符串元素", payload_of("Grep", [1, None, {"a": 1}]), "pass"))
    cs.append(("恰好 120 行", payload_of("Grep", ["line %03d short" % i for i in range(120)]), "pass"))
    cs.append(("121 行", payload_of("Grep", ["line %03d short" % i for i in range(121)]), "trim"))
    cs.append(("恰好 8000 字节单行", payload_of("Grep", "a" * 8000), "pass"))
    cs.append(("8001 字节单行", payload_of("Grep", "a" * 8001), "trim"))
    cs.append(("80 行×120B=9600（长文本路径）", payload_of("Grep", ["x" * 120] * 80), "trim"))
    cs.append(("81 行×120B=9720（正常路径）", payload_of("Grep", ["x" * 120] * 81), "trim"))
    cs.append(("CRLF 行文本", payload_of("Grep", ["line with crlf %03d" % i for i in range(200)]), "trim"))
    cs.append(("200KB 单行", payload_of("Grep", "q" * 200000), "trim"))
    cs.append(("转义密集文本（引号/反斜杠/制表符）",
               payload_of("Grep", ('quotes " and \\ backslash and \t tab ' * 600)), "trim"))
    cs.append(("控制字符转义（\\u0000 \\u001b）",
               payload_of("Grep", ("a\u0000b\u001bc " * 3000)), "trim"))
    cs.append(("emoji 文本", payload_of("Grep", ("🙂" * 3000)), "trim"))
    cs.append(("emoji 转义载荷（代理对）",
               payload_of("Grep", "🙂" * 3000, ascii_escape=True), None))
    cs.append(("dict/stdout 形态",
               payload_of("Bash", {"stdout": "o" * 20000, "stderr": "", "exitCode": 1}), "trim"))
    cs.append(("MCP 文本数组", payload_of("Grep", [{"type": "text", "text": "m" * 15000}]), "trim"))
    cs.append(("贴上限裁剪（8MB-4KB）",
               payload_of("Grep", "z" * (8 * MB - 4096)), "trim"))
    cs.append(("8MB+1KB 护栏透传",
               payload_of("Grep", "z" * (8 * MB + 1024)), "pass"))
    return cs


def run_edges():
    print("[边界]")
    edges = build_edges()
    n0 = len(FAILS)
    for name, p, exp in edges:
        r = verify_pair(name, p, expect=exp)
        if r and r[0] is not None:
            verify_idempotent(name, r[0])
    bad = len(set(f[0].split(" [")[0] for f in FAILS[n0:]))
    print("  边界 %d/%d 通过" % (len(edges) - bad, len(edges)))
    return len(edges), bad


# ---------------------------------------------------------------------------
# fuzz
# ---------------------------------------------------------------------------
WORDS_ASCII = ["alpha", "beta", "gamma", "return", "error", "Error", "ERROR",
               "warning", "WARNING", "failed", "cannot find", "file.cpp",
               "node_modules", "const", "using", "namespace", "0xdeadbeef",
               "TODO", "FIXME", "test", "case", "index", "main", "traceback",
               "Traceback", "overflow", "undefined", "0x7f"]
WORDS_CJK = ["错误", "警告", "失败", "超时", "权限不足", "未找到", "配置文件",
             "测试用例", "模块", "编译", "链接", "变量", "函数", "返回值", "路径"]
KEYWORDS = ["error", "Error", "ERROR", "warning", "Warning", "WARN", "failed",
            "cannot find", "Permission denied", "denied", "timed out",
            "timeout", "panic", "Traceback", "Exception", "not found",
            "No such file", "错误", "警告", "失败", "超时", "未找到", "权限不足"]


def gen_line(rng):
    if rng.random() < 0.3:
        s = "，".join(rng.choice(WORDS_CJK) for _ in range(rng.randint(2, 14)))
    else:
        s = " ".join(rng.choice(WORDS_ASCII) for _ in range(rng.randint(3, 30)))
    if rng.random() < 0.18:
        s = rng.choice(KEYWORDS) + " " + s
    return s


def gen_text(rng):
    n = rng.choice([0, 1, 2, 5, 40, 79, 80, 81, 119, 120, 121, 150, 400, 1000])
    mode = rng.choice(["short", "short", "med", "med", "mix", "huge"])
    if mode == "huge":
        n = min(n, 5)
    lines = []
    for i in range(n):
        m = mode if mode != "mix" else ("huge" if rng.random() < 0.05 else "short")
        if m == "huge":
            lines.append("L" * rng.randint(3000, 30000))
        elif m == "med":
            lines.append("%s/%s:%d: %s %s" % (
                rng.choice(WORDS_ASCII), rng.choice(WORDS_ASCII), i,
                gen_line(rng), gen_line(rng)))
        else:
            lines.append("%s/%s:%d: %s" % (
                rng.choice(WORDS_ASCII), rng.choice(WORDS_ASCII), i, gen_line(rng)))
    return "\n".join(lines)


def gen_fuzz_payload(rng):
    tool = rng.choice(["Grep", "Glob", "Grep", "Glob", "Bash", "Read", "WebFetch"])
    text = gen_text(rng)
    f = rng.random()
    if f < 0.55:
        resp = text.split("\n") if text else []
    elif f < 0.9:
        resp = text
    elif f < 0.95:
        resp = [{"type": "text", "text": text}]
    else:
        resp = {"stdout": text}
    return payload_of(tool, resp), tool


def run_fuzz(n):
    print("[fuzz] %d 例随机一致性（C++ vs Python 逐字节）" % n)
    n0 = len(FAILS)
    for i in range(n):
        rng = random.Random(20261015 + i)
        p, tool = gen_fuzz_payload(rng)
        r = verify_pair("fuzz#%d(%s)" % (i, tool), p)
        if r and r[0] is not None:
            verify_idempotent("fuzz#%d" % i, r[0])
        if (i + 1) % 80 == 0:
            print("   ...%d/%d" % (i + 1, n))
    bad = len(set(f[0].split(" [")[0] for f in FAILS[n0:]))
    print("  fuzz %d/%d 通过" % (n - bad, n))
    return n, bad


# ---------------------------------------------------------------------------
# 确定性 / 并发 / 长跑
# ---------------------------------------------------------------------------
def run_determinism():
    payload = payload_of("Grep", ["det %04d some line of search output" % i for i in range(2000)])
    hs = set()
    for _ in range(40):
        rc, o = run_exe(payload)
        if rc != 0:
            fail("确定性", "退出码 %d" % rc)
            return
        hs.add(hashlib.md5(o).hexdigest())
    check("确定性(40 次)", len(hs) == 1, "唯一输出 %d 个" % len(hs))
    print("  [确定性] 40 次输出唯一值 %d 个" % len(hs))


def run_concurrency():
    payload = payload_of("Grep", ["conc %04d concurrent load line" % i for i in range(3000)])

    def task(_):
        rc, o = run_exe(payload)
        return rc, hashlib.md5(o).hexdigest()

    with ThreadPoolExecutor(16) as ex:
        rs = list(ex.map(task, range(32)))
    nz = sum(1 for rc, _ in rs if rc != 0)
    uniq = len(set(h for _, h in rs))
    check("并发(32 任务/16 线程)", nz == 0 and uniq == 1, "非零退出 %d / 唯一输出 %d" % (nz, uniq))
    print("  [并发] 32 任务，非零退出 %d，唯一输出 %d 个" % (nz, uniq))


def run_soak():
    rng = random.Random(7)
    pool = [gen_fuzz_payload(rng)[0] for _ in range(40)]
    bad = 0
    t0 = time.time()
    for i in range(300):
        rc, o = run_exe(pool[i % len(pool)])
        if rc != 0:
            bad += 1
    dt = time.time() - t0
    check("长跑(300 次)", bad == 0, "失败 %d" % bad)
    print("  [长跑] 300 次混合负载，失败 %d，总耗时 %.1fs（平均 %.1fms/次）"
          % (bad, dt, dt * 1000 / 300))


def main():
    fuzz_n = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    t0 = time.time()
    print("wb-ops 0.2.2 稳定性测试")
    print("  C++ : %s" % EXE)
    print("  Py  : %s" % PY_IMPL)
    if not os.path.exists(EXE):
        print("! 缺少 %s" % EXE)
        return 1

    e_n, e_bad = run_edges()
    f_n, f_bad = run_fuzz(fuzz_n)
    print("[确定性 / 并发 / 长跑]")
    run_determinism()
    run_concurrency()
    run_soak()

    total, bad = e_n + f_n, e_bad + f_bad
    tail_bad = 0
    for f in FAILS:
        nm = f[0]
        if nm.startswith("确定性") or nm.startswith("并发") or nm.startswith("长跑"):
            tail_bad += 1
    verdict = "ALL PASS" if not FAILS else "%d 项失败" % len(FAILS)
    print("\n== 稳定性结论: %s ==  用例 %d（边界 %d + fuzz %d），失败明细 %d，耗时 %.1fs"
          % (verdict, total, e_n, f_n, len(FAILS), time.time() - t0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
