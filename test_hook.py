#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops / 裁剪 hook 的自测

把模拟的 PostToolUse 输入喂给 trim.py，检查三种情况：
    1. 大输出          -> 应该被裁剪，且中间的关键行被保留
    2. 小输出          -> 应该原样透传（hook 不输出任何东西）
    3. 畸形输入        -> 应该静默透传，不能崩
另检查记忆守卫（v0.3.6 起，SessionStart hook）：
    4. 正常阈值静默 / 超限自动压缩指令 / 非法阈值回退 / remind 模式 / 非法模式回退
另检查上下文守卫（v0.3.8 起 v1 / v0.6.0 起 v2，UserPromptSubmit hook）：
    5. 基础：超线注入 / 同档节流 / 小上下文静默 / 内部延续静默 / 非法阈值回退 / 关闭开关 / 事件注册（四事件）
       v2 专项：auto 样本学习 / 手动样本排除 / 学习线生效 / 压缩后 >100K 告警 / ≤100K 静默 /
                pending 时序 / env 覆盖轻档线 / 垃圾输入静默 / 状态文件 v2 格式
另检查 PreCompact hook（v0.6.0 起，压缩保留清单注入）：
    6. 保留清单输出 / 空 stdin 容错 / 垃圾输入容错

用法：python test_hook.py
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hooks", "trim.py")
GUARD = os.path.join(HERE, "hooks", "memory-guard.mjs")
CTX_GUARD = os.path.join(HERE, "hooks", "ctx-guard.mjs")
PRECOMPACT = os.path.join(HERE, "hooks", "precompact.mjs")
# v0.3.0 起：测试流量不写入真实记账数据（~/.workbuddy/wb-ops-data/）
os.environ["WB_OPS_STATS"] = "0"


def find_node():
    """定位 node 可执行文件（记忆守卫的运行时）。"""
    n = shutil.which("node")
    if n:
        return n
    # 兜底：WorkBuddy 托管 node（版本目录通配）+ 系统默认安装位置
    cands = glob.glob(os.path.join(os.path.expanduser("~"), ".workbuddy",
                                   "binaries", "node", "versions", "*", "node.exe"))
    cands.append(r"C:\Program Files\nodejs\node.exe")
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def run_guard(limit=None, mode=None):
    """跑记忆守卫脚本，返回 (stdout, 返回码)。limit/mode 覆盖对应环境变量。"""
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


def run_hook(payload):
    """把 payload 喂给 hook，返回 (stdout, 返回码)。"""
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip(), proc.returncode


def build_big_output():
    lines = ["$ make"]
    lines += ["  CC  src/parser.o" for _ in range(200)]
    lines.append("src/parser.cpp:87:14: error: 'node' was not declared in this scope")
    lines += ["  CC  src/tree.o" for _ in range(150)]
    lines.append("src/tree.cpp:203:5: warning: unused variable 'tmp'")
    lines += ["  CC  src/main.o" for _ in range(150)]
    lines.append("make: *** [Makefile:12: src/parser.o] Error 1")
    return "\n".join(lines)


def main():
    ok = True

    # ---- 用例 1：大输出应被裁剪且保留关键行 ----
    big = build_big_output()
    out, code = run_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "make"},
            "tool_response": {"stdout": big, "stderr": "", "exitCode": 2},
        }
    )
    if code != 0 or not out:
        print("[1] 大输出           FAIL  预期会输出裁剪结果，实际为空")
        ok = False
    else:
        payload = json.loads(out)
        trimmed = payload["hookSpecificOutput"]["updatedToolOutput"]
        ratio = 1 - len(trimmed) / max(len(big), 1)
        has_err = "was not declared in this scope" in trimmed
        has_warn = "unused variable" in trimmed
        has_tail = "Error 1" in trimmed
        has_marker = "wb-ops" in trimmed
        print("[1] 大输出           原 %d 字符 -> %d 字符，省 %.0f%%" % (len(big), len(trimmed), ratio * 100))
        print("    保留 error 行：%s   保留 warning 行：%s" % (has_err, has_warn))
        print("    保留结尾：%s       带省略标记：%s" % (has_tail, has_marker))
        if not (has_err and has_warn and has_tail and has_marker and ratio > 0.5):
            print("    FAIL  裁剪结果不符合预期")
            ok = False
        else:
            print("    PASS")
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

    # ---- 用例 4：记忆守卫（v0.3.6 起，SessionStart hook）----
    if not find_node():
        print("[4] 记忆守卫           SKIP（未找到 node）")
    else:
        # 4a: 正常阈值 -> 静默（当前记忆文件应远小于默认阈值 3500）
        out, code = run_guard()
        status = "PASS" if code == 0 and out == "" else "FAIL"
        if status == "FAIL":
            ok = False
        print("[4a] 正常阈值静默       %s" % status)

        # 4b: 强触发阈值 -> 注入合法 JSON（默认 auto：含"自动"压缩指令）
        out, code = run_guard(limit=50)
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

        # 4c: 非法阈值 -> 回退默认（当前文件小于默认阈值，应静默）
        out, code = run_guard(limit="abc")
        status = "PASS" if code == 0 and out == "" else "FAIL"
        if status == "FAIL":
            ok = False
        print("[4c] 非法阈值回退       %s" % status)

        # 4d: remind 模式 -> 建议式提醒（不含"自动执行"）
        out, code = run_guard(limit=50, mode="remind")
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
        out, code = run_guard(limit=50, mode="weird")
        status = "PASS" if code == 0 and "自动执行" in out else "FAIL"
        if status == "FAIL":
            ok = False
        print("[4e] 非法模式回退 auto   %s" % status)

    # ---- 用例 5：上下文守卫（v0.3.8 起，UserPromptSubmit hook）----
    if not find_node():
        print("[5] 上下文守卫         SKIP（未找到 node）")
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
                with open(os.path.join(HERE, "hooks", "hooks.json"), encoding="utf-8") as f:
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
        print("[6] PreCompact          SKIP（未找到 node）")
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
    print("全部通过 ✓" if ok else "有失败项 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
