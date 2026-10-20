#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装自检：在临时宿主里走一遍「全新安装 → 运行 → 卸载」，不碰你自己的 WorkBuddy。

    python test_release.py

退出码 = 失败项数。它会用 WB_OPS_HOST_HOME 指向临时目录，
按 hooks.json 里的命令原样执行（与宿主真实调用形式一致），最后清理干净。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
fails = []
checks = 0


def ck(name, cond, extra=""):
    global checks
    checks += 1
    if cond:
        print("  PASS  %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        fails.append(name)
        print("  FAIL  %s%s" % (name, ("  " + extra) if extra else ""))


def run(cmd, env=None, data=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(cmd, input=data, capture_output=True, env=e)


def hook_run(command, root, tool, response, extra=None):
    env = {"CODEBUDDY_PLUGIN_ROOT": root, "WB_OPS_STATS": "0"}
    env.update(extra or {})
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_response": response}
    return run(["bash", "-c", command], env=env,
               data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def bash_out(n, code):
    body = "\n".join("test_%03d PASSED  (0.01s)" % i for i in range(n))
    return "Command: pytest -q\nStdout: " + body + "\nStderr: \nExit Code: %d\nSignal: (none)" % code


def trimmed(raw):
    try:
        return json.loads(raw.decode("utf-8"))["hookSpecificOutput"]["updatedToolOutput"]
    except Exception:
        return None


ver = json.load(open(os.path.join(ROOT, "plugins", "wb-ops", ".codebuddy-plugin", "plugin.json"),
                     encoding="utf-8"))["version"]
mkt = "local"
tmp = tempfile.mkdtemp(prefix="wbops-selfcheck-")
home = os.path.join(tmp, "home")
env = {"WB_OPS_HOST_HOME": home, "WB_OPS_STATS": "0"}

print("自检 %s（临时宿主 %s）" % (ver, home.replace(tmp, "<tmp>")))
r = run([PY, os.path.join(ROOT, "install_wb_ops.py"), "install"], env=env)
ck("install 退出码 0", r.returncode == 0, "rc=%d" % r.returncode)

cache = os.path.join(home, "plugins", "cache", mkt, "wb-ops", ver)
market = os.path.join(home, "plugins", "marketplaces", mkt, "plugins", "wb-ops")
ck("安装快照落盘", os.path.isdir(cache))
ck("市场副本落盘", os.path.isdir(market))
ck("exe 随快照安装", os.path.isfile(os.path.join(cache, "bin", "wb-ops-trim.exe")))

spec = json.load(open(os.path.join(cache, "hooks", "hooks.json"), encoding="utf-8"))
matcher = spec["hooks"]["PostToolUse"][0]["matcher"]
command = spec["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
# v1.1：默认 matcher 只含 Grep/Glob/WebFetch —— Bash 的 tool_response 只有元数据、
# Read 默认不裁剪，两者进名单只会每次白付约 270ms 进程开销。
ck("matcher 默认只含 Grep/Glob/WebFetch",
   matcher.split("|") == ["Grep", "Glob", "WebFetch"], matcher)
ck("无 modules 入口（不加载压缩督导）", "modules" not in spec["hooks"])

# Bash 真实载荷形态：只有元数据，没有正文 → hook 无从裁剪，透传
t = bash_out(600, 0)
bash_meta = {"exitCode": 0, "signal": None, "stdoutBytesTruncated": False,
             "tool_error_code": "0"}
ck("Bash 元数据载荷透传（正文不在载荷里）",
   hook_run(command, cache, "Bash", bash_meta).stdout == b"")

grep_text = "\n".join("src/a.c:%d: matched content here" % i for i in range(600))
tt = trimmed(hook_run(command, cache, "Grep", grep_text).stdout)
ck("Grep 输出被裁剪", tt is not None and "wb-ops 省略" in tt,
   "%d -> %d 字节" % (len(grep_text.encode()), len(tt.encode()) if tt else 0))

# v1.1（P0-2）：结构化失败信号 → 整段透传（报错现场优先于省字节）
for label, resp in (
    ("is_error", {"stdout": grep_text, "is_error": True}),
    ("exitCode=2", {"stdout": grep_text, "exitCode": 2}),
    ("tool_error_code", {"stdout": grep_text, "tool_error_code": "CommandFailed"}),
):
    ck("失败透传（%s）" % label, hook_run(command, cache, "Grep", resp).stdout == b"")

# Bash 纯文本形态（退出码在正文末尾）—— 文本兜底仍生效
tf = bash_out(600, 1)
ck("Bash 文本失败信号透传", hook_run(command, cache, "Bash", tf).stdout == b"")

read_text = "\n".join("    body_line_%d = compute(x) + 1" % i for i in range(2000))
ck("Read 默认透传", hook_run(command, cache, "Read", read_text).stdout == b"")
tt = trimmed(hook_run(command, cache, "Read", read_text, {"WB_OPS_READ_TRIM": "1"}).stdout)
ck("Read 开启后裁剪", tt is not None and "wb-ops 省略" in tt)

ck("反斜杠插件根仍可用",
   hook_run(command, cache.replace("/", "\\"), "Grep", grep_text).stdout != b"")

outdir = os.path.join(tmp, "rep")
os.makedirs(outdir)
html = os.path.join(outdir, "dashboard.html")
r = run([PY, os.path.join(cache, "report.py"), "--out", html], env={"WB_OPS_STATS": "0"})
ck("报告生成", os.path.isfile(html), "%d 字节" % (os.path.getsize(html) if os.path.isfile(html) else 0))

r = run([PY, os.path.join(ROOT, "install_wb_ops.py"), "uninstall"], env=env)
ck("uninstall 退出码 0", r.returncode == 0)
ck("快照与副本已清除", not os.path.isdir(cache) and not os.path.isdir(market))

shutil.rmtree(tmp, ignore_errors=True)
print()
print("检查项 %d，失败 %d" % (checks, len(fails)))
for f in fails:
    print("  !! %s" % f)
sys.exit(len(fails))
