#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops / 裁剪 hook 的自测

把模拟的 PostToolUse 输入喂给 trim.py，检查三种情况：
    1. 大输出          -> 应该被裁剪，且中间的关键行被保留
    2. 小输出          -> 应该原样透传（hook 不输出任何东西）
    3. 畸形输入        -> 应该静默透传，不能崩（含孤立代理项容错，v0.6.1）
另检查记忆守卫（v0.3.6 起，SessionStart hook；v0.6.1 起用例自包含：临时 HOME + 自建记忆夹具）：
    4. 正常阈值静默 / 超限自动压缩指令 / 非法阈值回退 / remind 模式 / 非法模式回退
另检查上下文守卫（v0.3.8 起 v1 / v0.6.0 起 v2，UserPromptSubmit hook）：
    5. 基础：超线注入 / 同档节流 / 小上下文静默 / 内部延续静默 / 非法阈值回退 / 关闭开关 / 事件注册（四事件）
       v2 专项：auto 样本学习 / 手动样本排除 / 学习线生效 / 压缩后 >100K 告警 / ≤100K 静默 /
                pending 时序 / env 覆盖轻档线 / 垃圾输入静默 / 状态文件 v2 格式
       v0.6.2：越过实测压缩点仍未触发 → band 3 强提醒（点明"自动压缩没触发"机理）
另检查 PreCompact hook（v0.6.0 起，压缩保留清单注入）：
    6. 保留清单输出 / 空 stdin 容错 / 垃圾输入容错

用法：python test_hook.py
"""

import json
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))  # 仓库根
PLUGIN = os.path.join(HERE, "plugins", "wb-ops")  # 插件本体（官方市场结构）
HOOK = os.path.join(PLUGIN, "hooks", "trim.py")
GUARD = os.path.join(PLUGIN, "hooks", "memory-guard.mjs")
CTX_GUARD = os.path.join(PLUGIN, "hooks", "ctx-guard.mjs")
PRECOMPACT = os.path.join(PLUGIN, "hooks", "precompact.mjs")
# v0.3.0 起：测试流量不写入真实记账数据（~/.workbuddy/wb-ops-data/）
os.environ["WB_OPS_STATS"] = "0"


# v0.6.6/D3：统计因缺少 node 而未执行的用例数，收尾时显式告警，避免「整体 SKIP 却全绿」
_SKIPPED = [0]


def find_node():
    """定位 node 可执行文件（守卫脚本的运行时）。

    v0.6.6/D3：不再硬编码某一套宿主的绝对路径。本机可能并存两套宿主
    （~/.workbuddy 与 ~/.workbuddy-ai），且各自 binaries 下的 node 版本号不同，
    因此按「PATH -> 两套宿主 binaries 通配（新版本优先）-> 系统安装路径」查找。
    """
    n = shutil.which("node")
    if n:
        return n
    home = os.path.expanduser("~")
    cands = []
    for host in (".workbuddy-ai", ".workbuddy"):
        cands += sorted(glob.glob(os.path.join(
            home, host, "binaries", "node", "versions", "*", "node.exe")), reverse=True)
    cands += [r"C:\Program Files\nodejs\node.exe", "/usr/local/bin/node", "/usr/bin/node"]
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def skip(tag, name):
    """记录一项因缺少 node 而未执行的用例（收尾时统一告警）。"""
    _SKIPPED[0] += 1
    print("%s %-18s SKIP（未找到 node，本项未验证）" % (tag, name))


def run_guard(limit=None, mode=None, home=None):
    """跑记忆守卫脚本，返回 (stdout, 返回码)。limit/mode/home 覆盖对应环境变量。

    home（v0.6.1/T1）：注入 USERPROFILE/HOME —— 守卫经 os.homedir() 定位
    ~/.workbuddy/user-*-personal/MEMORY.md，注入后用例即可完全自包含，
    不再依赖本机真实记忆文件是否存在。
    """
    node = find_node()
    env = dict(os.environ)
    if limit is None:
        env.pop("WB_OPS_MEM_GUARD_LIMIT", None)
    else:
        env["WB_OPS_MEM_GUARD_LIMIT"] = str(limit)
    if mode is None:
        env.pop("WB_OPS_MEM_GUARD_MODE", None)
    else:
        env["WB_OPS_MEM_GUARD_MODE"] = str(mode)
    if home:
        env["USERPROFILE"] = home   # Node os.homedir() 在 Windows 优先读 USERPROFILE
        env["HOME"] = home          # 非 Windows 平台的回退变量
    proc = subprocess.run(
        [node, GUARD], input="{}", capture_output=True, text=True, encoding="utf-8", env=env
    )
    return proc.stdout.strip(), proc.returncode


def run_ctx_guard(payload, state_dir, extra_env=None):
    """跑上下文守卫脚本，返回 (stdout, 返回码)。state_dir 隔离守卫状态文件。"""
    node = find_node()
    env = dict(os.environ)
    env["WB_OPS_CTX_GUARD_DIR"] = state_dir
    env.pop("WB_OPS_CTX_GUARD", None)
    env.pop("WB_OPS_CTX_GUARD_STEP", None)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    proc = subprocess.run(
        [node, CTX_GUARD], input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    return proc.stdout.strip(), proc.returncode


def run_hook(payload, extra_env=None):
    """把 payload 喂给 hook，返回 (stdout, 返回码)。"""
    env = dict(os.environ, **extra_env) if extra_env else None
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return proc.stdout.strip(), proc.returncode


def build_big_output():
    # v1.1：输出预算默认 8KB，夹具随之放大到 ~1900 行，
    # 否则「省 >50%」在物理上不可能达到（9KB 输入最多只能省 18%）。
    lines = ["$ make"]
    lines += ["  CC  src/parser.o" for _ in range(700)]
    lines.append("src/parser.cpp:87:14: error: 'node' was not declared in this scope")
    lines += ["  CC  src/tree.o" for _ in range(600)]
    lines.append("src/tree.cpp:203:5: warning: unused variable 'tmp'")
    lines += ["  CC  src/main.o" for _ in range(600)]
    lines.append("make: *** [Makefile:12: src/parser.o] Error 1")
    return "\n".join(lines)


def main():
    ok = True

    # ---- 用例 1：大输出应被裁剪且保留关键行（成功输出）----
    big = build_big_output()
    out, code = run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "make -k"},
            "tool_response": {"stdout": big, "stderr": "", "exitCode": 0},
        }
    )
    if code != 0 or not out:
        print("[1a] 大输出（成功）      FAIL  预期会输出裁剪结果，实际为空")
        ok = False
    else:
        payload = json.loads(out)
        trimmed = payload["hookSpecificOutput"]["updatedToolOutput"]
        ratio = 1 - len(trimmed) / max(len(big), 1)
        has_err = "was not declared in this scope" in trimmed
        has_warn = "unused variable" in trimmed
        has_tail = "Error 1" in trimmed
        has_marker = "wb-ops" in trimmed
        print("[1a] 大输出（成功）      原 %d 字符 -> %d 字符，省 %.0f%%" % (len(big), len(trimmed), ratio * 100))
        print("     保留 error 行：%s   保留 warning 行：%s" % (has_err, has_warn))
        print("     保留结尾：%s       带省略标记：%s" % (has_tail, has_marker))
        if not (has_err and has_warn and has_tail and has_marker and ratio > 0.5):
            print("     FAIL  裁剪结果不符合预期")
            ok = False
        else:
            print("     PASS")
    print()

    # ---- 用例 1b（v1.1）：失败输出整段透传，报错现场绝不裁剪 ----
    for label, resp in (
        ("exitCode: 2", {"stdout": big, "stderr": "", "exitCode": 2}),
        ("is_error: true", {"stdout": big, "is_error": True}),
        ("tool_error_code", {"stdout": big, "tool_error_code": "CommandFailed"}),
    ):
        out, code = run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "make"},
                "tool_response": resp,
            }
        )
        if code == 0 and out == "":
            print("[1b] 失败透传 %-16s PASS" % label)
        else:
            print("[1b] 失败透传 %-16s FAIL  失败输出被裁剪，报错现场丢失" % label)
            ok = False
    print()

    # ---- 用例 1c（v1.2）：Read 按文件类型分档 ----
    log_lines = []
    for i in range(600):
        if i == 333:
            log_lines.append("ERROR connect timeout after 30s")
        else:
            log_lines.append("2026-10-17 10:%02d:%02d INFO request id=%d" % (i // 60, i % 60, i))
    log_text = "\n".join(log_lines)
    src_text = "\n".join(("def f_%d(x):" % i if i % 300 == 0 else "    b_%d = compute(x)" % i)
                         for i in range(2000))
    # v1.2.2：结构类 .json 要「行数 > 800 且字节 > 预算」两项都过线才裁：
    #   700 行 / ~15KB → 透传（字节已超预算，旧规则会误裁）
    #   900 行 / ~20KB → 裁剪
    def json_text(n, with_err=False):
        rows = ["{"]
        for i in range(n):
            rows.append('  "key_%04d": %d,' % (i, i * 7 + 1))
        if with_err:
            rows.append('  "error": "connect timeout after 30s",')
        rows.append('  "end": 0')
        rows.append("}")
        return "\n".join(rows)
    json_short = json_text(700)
    json_long = json_text(900, with_err=True)
    for label, resp, tin, env, want in (
        ("数据类 .log", log_text, {"file_path": "C:/tmp/a.log"}, {}, True),
        ("数据类短文件", "\n".join(log_lines[:80]), {"file_path": "C:/tmp/a.log"}, {}, False),
        ("结构类 .json 700 行", json_short, {"file_path": "C:/tmp/lock.json"}, {}, False),
        ("结构类 .json 900 行", json_long, {"file_path": "C:/tmp/lock.json"}, {}, True),
        ("源码 .py 默认", src_text, {"file_path": "C:/tmp/a.py"}, {}, False),
        ("源码 .py 开闸", src_text, {"file_path": "C:/tmp/a.py"}, {"WB_OPS_READ_TRIM": "1"}, True),
    ):
        out, code = run_hook(
            {"hook_event_name": "PostToolUse", "tool_name": "Read",
             "tool_input": tin, "tool_response": resp},
            env,
        )
        if code != 0:
            print("[1c] Read %-12s FAIL  返回码 %s" % (label, code))
            ok = False
            continue
        if not want:
            status = "PASS" if out == "" else "FAIL  不应裁剪却裁了"
            if out:
                ok = False
            print("[1c] Read %-12s %s" % (label, status))
            continue
        try:
            body = json.loads(out)["hookSpecificOutput"]["updatedToolOutput"]
        except Exception:
            print("[1c] Read %-12s FAIL  输出无法解析：%r" % (label, out[:80]))
            ok = False
            continue
        keep = ("connect timeout" in body) if label.startswith(("数据类", "结构类")) else ("def f_0" in body)
        mark = "wb-ops" in body
        ratio = 1 - len(body) / max(len(resp), 1)
        status = "PASS" if (keep and mark and ratio > 0.5) else "FAIL  关键字/标记/压缩率不符"
        if status != "PASS":
            ok = False
        print("[1c] Read %-12s 原 %d -> %d 字符（省 %.0f%%）  关键行 %s  标记 %s  %s"
              % (label, len(resp), len(body), ratio * 100, keep, mark, status))
    print()

    # ---- 用例 1d（v1.2，v1.3 扩到二进制）：三处清单同源 + exe 新鲜度 ----
    # hooks.json 的 if 规则 / hooks/trim.py 的 DATA_EXTS / bin/wb-ops-trim.exe 自报，
    # 任一漂移都会静默错配：hook 不启动，或启动了却按源码档处理。
    # 生产实际跑的是 exe，只比源码等于没比；再加一道构建戳抓「改完忘重编译」。
    sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
    import trim as trim_mod
    with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
        hk = json.load(f)
    post = hk["hooks"]["PostToolUse"]
    read_groups = [g for g in post if g.get("matcher") == "Read"]
    checks = []
    checks.append(("有独立的 Read 组", len(read_groups) == 1))
    if read_groups:
        rule = read_groups[0].get("if") or ""
        checks.append(("Read 组带 if 条件", rule.startswith("Read(*.{") and rule.endswith("})")))
        ext_in_rule = tuple("." + x for x in rule[len("Read(*.{"):-2].split(","))
        checks.append(("if 清单 == DATA_EXTS", ext_in_rule == trim_mod.DATA_EXTS))
        checks.append(("默认不带 WB_OPS_READ_TRIM",
                       all("WB_OPS_READ_TRIM" not in (h.get("command") or "")
                           for h in read_groups[0].get("hooks", []))))

    # exe 自报清单：二进制里真实生效的那一份，才算生产口径
    exe = os.path.join(PLUGIN, "bin", "wb-ops-trim.exe")
    exe_exts = None
    if os.path.exists(exe):
        try:
            r = subprocess.run([exe, "--dump-exts"], capture_output=True, timeout=10)
            exe_exts = tuple(json.loads(r.stdout.decode("utf-8", "replace")))
        except Exception:
            exe_exts = None
    checks.append(("exe 可执行且自报清单", exe_exts is not None))
    checks.append(("exe 自报 == DATA_EXTS", exe_exts is not None and exe_exts == trim_mod.DATA_EXTS))

    # 构建戳：build.sh 落盘的源码 sha256 与当前 trim.cpp 一致，才说明二进制是这份源码编的
    stamp = os.path.join(PLUGIN, "bin", "wb-ops-trim.build.json")
    src = os.path.join(PLUGIN, "src", "trim.cpp")
    stamp_src = ""
    try:
        with open(stamp, encoding="utf-8") as f:
            stamp_src = json.load(f).get("src_sha256") or ""
    except Exception:
        stamp_src = ""
    cur_src = ""
    if os.path.exists(src):
        with open(src, "rb") as f:
            cur_src = hashlib.sha256(f.read()).hexdigest()
    checks.append(("构建戳 == trim.cpp", bool(stamp_src) and stamp_src == cur_src))

    # 普通工具那组仍在，且没被顺手塞进 Read
    checks.append(("普通组 matcher 未含 Read",
                   any(g.get("matcher") == "Grep|Glob|WebFetch" for g in post)))
    for name, good in checks:
        if not good:
            ok = False
        print("[1d] %-28s %s" % (name, "PASS" if good else "FAIL"))
    if stamp_src != cur_src:
        print("[1d] 提示：trim.cpp 在 build.sh 之后改动过，跑 ./build.sh 重新编译")
    print()

    # ---- 用例 2：小输出应原样透传 ----
    small = "\n".join("line %d" % i for i in range(12))
    out, code = run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": small, "exitCode": 0},
        }
    )
    if code == 0 and out == "":
        print("[2] 小输出           透传（hook 无输出）        PASS")
    else:
        print("[2] 小输出           FAIL  不应该动它，实际输出：%r" % out[:80])
        ok = False
    print()

    # ---- 用例 3：畸形输入不能崩 ----
    for label, payload in [
        ("非 JSON 字符串", None),
        ("空对象", {}),
        ("缺 tool_response", {"hook_event_name": "PostToolUse", "tool_name": "Bash"}),
        ("tool_response 是数字", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": 42}),
        ("不关注的工具", {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_response": {"content": "x" * 99999}}),
    ]:
        if payload is None:
            proc = subprocess.run(
                [sys.executable, HOOK], input="not json at all", capture_output=True, text=True, encoding="utf-8"
            )
            code, out = proc.returncode, proc.stdout.strip()
        else:
            out, code = run_hook(payload)
        status = "PASS" if code == 0 and out == "" else "FAIL"
        if status == "FAIL":
            ok = False
        print("[3] %-18s %s" % (label, status))

    # 3f（v0.6.1/S3）：孤立代理项（合法 JSON 转义）不得崩溃；
    # 允许裁剪或透传，但返回码必须为 0、有输出则结构合法。
    raw_sur = json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "Grep",
        "session_id": "s3-surrogate", "tool_response": "\ud800\n" * 300,
    }, ensure_ascii=True).encode("ascii")
    proc = subprocess.run([sys.executable, HOOK], input=raw_sur, capture_output=True)
    o = proc.stdout.strip()
    good = proc.returncode == 0
    if good and o:
        try:
            json.loads(o.decode("utf-8"))
        except Exception:
            good = False
    status = "PASS" if good else "FAIL"
    if status == "FAIL":
        ok = False
    print("[3] %-18s %s" % ("孤立代理项容错", status))

    # ---- 用例 4：记忆守卫（v0.3.6 起，SessionStart hook；v0.6.1 起用例自包含）----
    if not find_node():
        skip("[4]", "记忆守卫")
    else:
        # 自包含夹具（T1）：在临时 HOME 下构造 ~/.workbuddy/user-test-personal/MEMORY.md
        # （约 260 字符：>50 可触发 limit=50，<3500 对默认阈值静默），注入 USERPROFILE/HOME 运行。
        mem_home = tempfile.mkdtemp(prefix="wb-ops-mem-")
        try:
            mem_dir = os.path.join(mem_home, ".workbuddy", "user-test-personal")
            os.makedirs(mem_dir)
            fixture = ("# 记忆守卫测试夹具\n"
                       + "- 守卫用例自包含：固定内容填充行，不依赖本机真实记忆文件。\n" * 8)
            with open(os.path.join(mem_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
                f.write(fixture)

            # 4a: 默认阈值（3500）-> 夹具未超限 -> 静默
            out, code = run_guard(home=mem_home)
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[4a] 正常阈值静默       %s" % status)

            # 4b: 强触发阈值 -> 注入合法 JSON（默认 auto：含"自动"压缩指令）
            out, code = run_guard(limit=50, home=mem_home)
            try:
                d = json.loads(out)
                h = d["hookSpecificOutput"]
                good = (code == 0 and h["hookEventName"] == "SessionStart"
                        and "MEMORY.md" in h["additionalContext"]
                        and "自动" in h["additionalContext"])
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[4b] 超限自动压缩指令   %s" % status)

            # 4c: 非法阈值 -> 回退默认（夹具未超限，应静默）
            out, code = run_guard(limit="abc", home=mem_home)
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[4c] 非法阈值回退       %s" % status)

            # 4d: remind 模式 -> 建议式提醒（不含"自动执行"）
            out, code = run_guard(limit=50, mode="remind", home=mem_home)
            try:
                d = json.loads(out)
                ctx = d["hookSpecificOutput"]["additionalContext"]
                good = code == 0 and "建议" in ctx and "自动执行" not in ctx
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[4d] remind 模式提醒     %s" % status)

            # 4e: 非法模式 -> 回退 auto
            out, code = run_guard(limit=50, mode="weird", home=mem_home)
            status = "PASS" if code == 0 and "自动执行" in out else "FAIL"
            if status == "FAIL":
                ok = False
            print("[4e] 非法模式回退 auto   %s" % status)
        finally:
            shutil.rmtree(mem_home, ignore_errors=True)

    # ---- 用例 5：上下文守卫（v0.3.8 起，UserPromptSubmit hook）----
    if not find_node():
        skip("[5]", "上下文守卫")
    else:
        tmp = tempfile.mkdtemp(prefix="wb-ops-ctx-")
        try:
            def mk_tp(name, pt):
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"rawUsage": {"prompt_tokens": pt}}) + "\n")
                return p

            tp_big = mk_tp("big.jsonl", 210000)
            tp_small = mk_tp("small.jsonl", 500)
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "test1234abcd",
                "transcript_path": tp_big,
                "prompt": "hi",
                "is_internal_continuation": False,
            }
            sd = os.path.join(tmp, "st")

            # 5a: 超线 -> 注入合法 JSON（UserPromptSubmit + 含"上下文"）
            out, code = run_ctx_guard(payload, sd)
            try:
                h = json.loads(out)["hookSpecificOutput"]
                good = (code == 0 and h["hookEventName"] == "UserPromptSubmit"
                        and "上下文" in h["additionalContext"])
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5a] 超线注入提醒       %s" % status)

            # 5b: 同档重跑 -> 节流静默
            out, code = run_ctx_guard(payload, sd)
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5b] 同档节流           %s" % status)

            # 5c: 小上下文 -> 静默
            out, code = run_ctx_guard({**payload, "transcript_path": tp_small},
                                      os.path.join(tmp, "st2"))
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5c] 小上下文静默       %s" % status)

            # 5d: 内部延续 -> 静默
            out, code = run_ctx_guard({**payload, "is_internal_continuation": True},
                                      os.path.join(tmp, "st3"))
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5d] 内部延续静默       %s" % status)

            # 5e: 非法阈值 -> 回退默认（210K > 默认 200K，应触发）
            out, code = run_ctx_guard(payload, os.path.join(tmp, "st4"),
                                      {"WB_OPS_CTX_GUARD": "abc"})
            status = "PASS" if code == 0 and out != "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5e] 非法阈值回退       %s" % status)

            # 5f: 关闭开关 -> 静默
            out, code = run_ctx_guard(payload, os.path.join(tmp, "st5"),
                                      {"WB_OPS_CTX_GUARD": "off"})
            status = "PASS" if code == 0 and out == "" else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5f] 关闭开关静默       %s" % status)

            # 5g: hooks.json 四事件注册且语法合法
            try:
                with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
                    events = set(json.load(f).get("hooks", {}).keys())
                good = {"PostToolUse", "SessionStart", "UserPromptSubmit", "PreCompact"} <= events
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5g] hooks.json 事件注册 %s" % status)

            # ---- 5h~5n：v2 专项（v0.6.0 起：行为学习 / 双档提醒 / 压缩后检查 / pending 时序）----

            def wl(path, objs):
                with open(path, "w", encoding="utf-8") as f:
                    for o in objs:
                        f.write(json.dumps(o, ensure_ascii=False) + "\n")
                return path

            def ul(pt, rid="r", model=None):
                pd = {"conversationRequestId": rid, "rawUsage": {"prompt_tokens": pt}}
                if model:
                    pd["model"] = model
                return {"type": "message", "role": "assistant", "providerData": pd}

            def cl(seq, ctype, kind, tag):
                return {"id": "cmp_" + tag, "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "<cb_summary> test"}],
                        "providerData": {"compactionSeq": seq, "compactType": ctype, "compactionKind": kind}}

            def rstate(sd):
                with open(os.path.join(sd, "ctx-guard.json"), encoding="utf-8") as f:
                    return json.load(f)

            # 5h: auto 样本学习（152.8K）→ 学习线生效（强档≈130K），140K 触发强档；post 60K 无压缩后告警
            sd = os.path.join(tmp, "v2h"); os.makedirs(sd)
            tp = wl(os.path.join(tmp, "v2h.jsonl"), [
                ul(152800, "r1", "deepseek-v4.1-flash"), cl(1, "pre-message-auto", "auto", "h1"),
                ul(60000, "r2"), ul(140050, "r3"),
            ])
            out, code = run_ctx_guard({**payload, "session_id": "v2h00001", "transcript_path": tp}, sd)
            try:
                ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                st = rstate(sd)
                good = (code == 0 and "自动压缩点约 153K" in ctx and "压缩后" not in ctx
                        and st.get("models", {}).get("deepseek-v4.1-flash", {}).get("s") == [152800])
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5h] v2 学习+强档提醒   %s" % status)

            # 5i: 手动样本排除（user-command/manual 不学习）；50K 低于学习轻档线 → 静默
            sd = os.path.join(tmp, "v2i"); os.makedirs(sd)
            tp = wl(os.path.join(tmp, "v2i.jsonl"), [
                ul(152800, "r1", "deepseek-v4.1-flash"), cl(1, "pre-message-auto", "auto", "i1"),
                ul(60000, "r2"), ul(140050, "r3"), cl(2, "user-command", "manual", "i2"),
                ul(50000, "r4"),
            ])
            out, code = run_ctx_guard({**payload, "session_id": "v2i00001", "transcript_path": tp}, sd)
            try:
                st = rstate(sd)
                good = (code == 0 and out == ""
                        and st.get("models", {}).get("deepseek-v4.1-flash", {}).get("s") == [152800]
                        and st.get("sessions", {}).get("v2i00001", {}).get("ls") == 2)
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5i] v2 手动样本排除     %s" % status)

            # 5j: 压缩后水位 >100K → 告警（两步：先建会话，再补压缩记录）
            sd = os.path.join(tmp, "v2j"); os.makedirs(sd)
            tp = os.path.join(tmp, "v2j.jsonl")
            wl(tp, [ul(70000, "r1", "deepseek-v4.1-flash")])
            out1, code1 = run_ctx_guard({**payload, "session_id": "v2j00001", "transcript_path": tp}, sd)
            wl(tp, [ul(70000, "r1", "deepseek-v4.1-flash"),
                    cl(1, "pre-message-auto", "auto", "j1"), ul(110000, "r2")])
            out2, code2 = run_ctx_guard({**payload, "session_id": "v2j00001", "transcript_path": tp}, sd)
            try:
                ctx2 = json.loads(out2)["hookSpecificOutput"]["additionalContext"]
                good = (code1 == 0 and out1 == "" and code2 == 0
                        and "仍约 110K" in ctx2 and "超过 100K" in ctx2)
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5j] v2 压缩后>100K 告警 %s" % status)

            # 5k: 压缩后水位 ≤100K → 静默
            sd = os.path.join(tmp, "v2k"); os.makedirs(sd)
            tp = os.path.join(tmp, "v2k.jsonl")
            wl(tp, [ul(70000, "r1", "deepseek-v4.1-flash")])
            run_ctx_guard({**payload, "session_id": "v2k00001", "transcript_path": tp}, sd)
            wl(tp, [ul(70000, "r1", "deepseek-v4.1-flash"),
                    cl(1, "pre-message-auto", "auto", "k1"), ul(50000, "r2")])
            out, code = run_ctx_guard({**payload, "session_id": "v2k00001", "transcript_path": tp}, sd)
            status = "PASS" if (code == 0 and out == "") else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5k] v2 压缩后≤100K 静默 %s" % status)

            # 5l: pending 时序（压缩记录先到、其后请求未到 → 挂起；补齐后消解）
            sd = os.path.join(tmp, "v2l"); os.makedirs(sd)
            tp = os.path.join(tmp, "v2l.jsonl")
            wl(tp, [ul(70000, "r1", "deepseek-v4.1-flash")])
            run_ctx_guard({**payload, "session_id": "v2l00001", "transcript_path": tp}, sd)
            with open(tp, "a", encoding="utf-8") as f:
                f.write(json.dumps(cl(1, "pre-message-auto", "auto", "l1"), ensure_ascii=False) + "\n")
            out2, code2 = run_ctx_guard({**payload, "session_id": "v2l00001", "transcript_path": tp}, sd)
            p2 = rstate(sd).get("sessions", {}).get("v2l00001", {}).get("p")
            with open(tp, "a", encoding="utf-8") as f:
                f.write(json.dumps(ul(50000, "r2"), ensure_ascii=False) + "\n")
            out3, code3 = run_ctx_guard({**payload, "session_id": "v2l00001", "transcript_path": tp}, sd)
            s3 = rstate(sd).get("sessions", {}).get("v2l00001", {})
            good = (code2 == 0 and out2 == "" and p2 == 1
                    and code3 == 0 and out3 == "" and s3.get("p") == 0 and s3.get("ls") == 1)
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5l] v2 pending 时序      %s" % status)

            # 5m: env 覆盖轻档线（数字）→ 105K 触发轻档；状态文件为 v2 格式
            sd = os.path.join(tmp, "v2m"); os.makedirs(sd)
            tp = wl(os.path.join(tmp, "v2m.jsonl"), [ul(105000, "r1")])
            out, code = run_ctx_guard({**payload, "session_id": "v2m00001", "transcript_path": tp}, sd,
                                      {"WB_OPS_CTX_GUARD": "100000"})
            try:
                ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                good = code == 0 and "105K" in ctx and rstate(sd).get("v") == 2
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5m] v2 env 覆盖+状态格式 %s" % status)

            # 5o（v0.6.2）: 越过实测压缩点仍未触发 → band 3 强提醒（点明"自动压缩没触发"机理）
            #     v1.3：措辞改为「先答后劝」——先回答当前问题、只暂缓新方向不拒答
            sd = os.path.join(tmp, "v2o"); os.makedirs(sd)
            tp = wl(os.path.join(tmp, "v2o.jsonl"), [
                ul(152800, "r1", "deepseek-v4.1-flash"), cl(1, "pre-message-auto", "auto", "o1"),
                ul(60000, "r2"), ul(160000, "r3"),
            ])
            out, code = run_ctx_guard({**payload, "session_id": "v2o00001", "transcript_path": tp}, sd)
            try:
                ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                good = (code == 0 and "已越过" in ctx and "160K" in ctx
                        and "/compact" in ctx and "发送消息" in ctx
                        and "先照常回答" in ctx and "不要拒绝" in ctx)
            except Exception:
                good = False
            status = "PASS" if good else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5o] v2 越过压缩点强提醒 %s" % status)

            # 5n: 事件名不符 / 垃圾 stdin / 空 stdin / 空对象 → 静默且不崩
            checks = []
            out, code = run_ctx_guard({**payload, "hook_event_name": "SessionStart", "session_id": "v2n00001",
                                       "transcript_path": tp_big}, os.path.join(tmp, "v2n1"))
            checks.append(code == 0 and out == "")
            env_n = dict(os.environ)
            env_n.pop("WB_OPS_CTX_GUARD", None)
            env_n["WB_OPS_CTX_GUARD_DIR"] = os.path.join(tmp, "v2n2")
            for raw in ("not json at all", "", "{}"):
                proc = subprocess.run([find_node(), CTX_GUARD], input=raw, capture_output=True,
                                      text=True, encoding="utf-8", env=env_n)
                checks.append(proc.returncode == 0 and proc.stdout.strip() == "")
            status = "PASS" if all(checks) else "FAIL"
            if status == "FAIL":
                ok = False
            print("[5n] v2 垃圾输入静默      %s" % status)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- 用例 6：PreCompact hook（v0.6.0 起：压缩保留清单注入）----
    if not find_node():
        skip("[6]", "PreCompact")
    else:
        def run_precompact(raw):
            proc = subprocess.run(
                [find_node(), PRECOMPACT], input=raw, capture_output=True, text=True,
                encoding="utf-8", env=dict(os.environ),
            )
            return proc.stdout.strip(), proc.returncode

        # 6a: 正常输入 -> 输出保留清单（供压缩摘要使用）
        out, code = run_precompact(json.dumps({"hook_event_name": "PreCompact", "session_id": "x", "trigger": "auto"}))
        status = "PASS" if (code == 0 and "保留" in out and "任务" in out) else "FAIL"
        if status == "FAIL":
            ok = False
        print("[6a] 保留清单输出         %s" % status)

        # 6b: 空 stdin -> 容错，仍输出清单
        out, code = run_precompact("")
        status = "PASS" if (code == 0 and "保留" in out) else "FAIL"
        if status == "FAIL":
            ok = False
        print("[6b] 空 stdin 容错         %s" % status)

        # 6c: 垃圾输入 -> 不崩，仍输出清单
        out, code = run_precompact("not json at all")
        status = "PASS" if (code == 0 and "保留" in out) else "FAIL"
        if status == "FAIL":
            ok = False
        print("[6c] 垃圾输入容错         %s" % status)

    print()
    print("=" * 50)
    # v0.6.6/D3：缺 node 时不再静默全绿 —— 用退出码 2 与「有失败」区分
    if _SKIPPED[0]:
        print("⚠ %d 项因缺少 node 未执行，本次结果不代表全绿" % _SKIPPED[0])
    if not ok:
        print("有失败项 ✗")
        return 1
    if _SKIPPED[0]:
        print("已执行项全部通过，但有用例被跳过 △")
        return 2
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
