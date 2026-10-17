#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py 冒烟测试：demo 全流程 / 真实模式假账本降级 / 过期清理 / 隔离性 / 实时服务。

原则：全部用例在临时目录跑，绝不读写真实 ~/.workbuddy 数据。
用法：python test_report.py
"""

import argparse
import errno
import http.server
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))  # 仓库根
PLUGIN = os.path.join(HERE, "plugins", "wb-ops")  # 插件本体（官方市场结构）
sys.path.insert(0, PLUGIN)  # 让下方 `import report` 命中插件内的 report.py
import report  # noqa: E402  （须在 sys.path 调整之后导入）
REPORT = os.path.join(PLUGIN, "report.py")
PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] %s" % name)
    else:
        failed += 1
        print("  [FAIL] %s %s" % (name, str(extra)[:220]))


def run(args):
    return subprocess.run([PY, REPORT] + args, capture_output=True,
                          text=True, encoding="utf-8", env=ENV, timeout=180)


def case_demo():
    print("\n[1] demo 模式：完整区块 + 不碰真实数据目录")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-demo-")
    try:
        out = os.path.join(tmp, "d.html")
        r = run(["--demo", "--out", out, "--data-dir", tmp])
        check("退出码 0", r.returncode == 0, r.stderr)
        html = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        check("HTML 已生成", len(html) > 2000)
        for sec in ("示意数据", "今日节省", "每日节省", "按工具", "按项目",
                    "按模型 Token 消耗", "上下文占用", "全量会话", "缓存命中",
                    "命中率", "最近裁剪明细"):
            check("区块存在: %s" % sec, sec in html)
        args = argparse.Namespace(demo=True, data_dir=tmp, out=out,
                                  db=os.path.join(tmp, "none.db"),
                                  projects=os.path.join(tmp, "noproj"), no_prune=False)
        with ExitStack() as stack:
            for name in ("load_saves", "load_host", "scan_transcripts", "prune_old"):
                stack.enter_context(patch.object(report, name,
                                    side_effect=AssertionError("demo 不应调用 " + name)))
            isolated, _, _ = report.build(args)
        check("demo 不触碰真实数据加载或清理", "示意数据" in isolated)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_real_fake_ledger():
    print("\n[2] 真实模式：假账本聚合 + 坏行计数 + db 缺失降级")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-real-")
    try:
        now_ms = int(time.time() * 1000)
        fp = os.path.join(tmp, "saves-%s.jsonl" % datetime.now().strftime("%Y%m"))
        with open(fp, "w", encoding="utf-8") as f:
            f.write('{"t":%d,"s":"abcdef12","tool":"Grep","b":50000,"a":6000}\n' % now_ms)
            f.write('{"t":%d,"s":"abcdef12","tool":"Glob","b":20000,"a":4000}\n' % (now_ms + 100))
            f.write("这不是 JSON 的损坏行\n")
        out = os.path.join(tmp, "r.html")
        r = run(["--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
                 "--projects", os.path.join(tmp, "noproj"), "--out", out])
        check("退出码 0", r.returncode == 0, r.stderr)
        check("记账计数正确（2 条 / 坏 1）",
              "记账记录 2 条 / 损坏行 1" in r.stdout, r.stdout[-300:])
        check("db 缺失已降级并提示", "workbuddy.db 读取失败" in r.stdout, r.stdout[-300:])
        html = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        # 总节省 = (50000-6000) + (20000-4000) = 60000 字节 -> hb() 口径 "58.6 KB"
        check("节省数字入报告", "58.6 KB" in html)
        check("工具名入报告", "Grep" in html and "Glob" in html)
        # v0.6.1（S7）：宿主库缺失 -> 页面顶部出现降级 banner（区分「无数据」与「读库失败」）
        check("宿主降级 banner 入页", "宿主数据库读取失败" in html)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_prune():
    print("\n[3] 过期清理：默认删 3 个月前的账本，--no-prune 保留")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-prune-")
    try:
        now = datetime.now()
        idx = now.year * 12 + (now.month - 1) - 5          # 5 个月前 → 应删除
        old_fp = os.path.join(tmp, "saves-%04d%02d.jsonl" % (idx // 12, idx % 12 + 1))
        cur_fp = os.path.join(tmp, "saves-%s.jsonl" % now.strftime("%Y%m"))
        out = os.path.join(tmp, "p.html")
        base = ["--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
                "--projects", os.path.join(tmp, "noproj"), "--out", out]
        open(old_fp, "w").close()
        open(cur_fp, "w").close()
        r = run(base)
        check("默认清理旧账本", (not os.path.exists(old_fp)) and os.path.exists(cur_fp))
        check("清理有提示", "已清理过期记账文件" in r.stdout, r.stdout[-260:])
        open(old_fp, "w").close()
        run(base + ["--no-prune"])
        check("--no-prune 保留旧账本", os.path.exists(old_fp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_scan_full():
    print("\n[4] v0.3.5 单遍扫描：全量上下文 + 缓存命中 + RID 归属 + 窗口外补齐")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-scan-")
    try:
        proj = os.path.join(tmp, "projects", "d-demo")
        os.makedirs(proj)
        t = int(time.time() * 1000)
        rid1 = "abcdef0123456789abcdef0123456789"
        rid2 = "def0123456789abcdef0123456789abc"
        # 两个会话的 transcript（scan 为子串提取，整体无需合法 JSON）
        with open(os.path.join(proj, "aaaa1111-2222-3333-4444-555566667777.jsonl"),
                  "w", encoding="utf-8") as f:
            f.write('{"timestamp":%d,"providerData":{"model":"test-ma",'
                    '"conversation_request_id=%s","rawUsage":{"prompt_tokens":2000,'
                    '"completion_tokens":50,"prompt_cache_hit_tokens":1700}}}\n'
                    % (t - 5000, rid1))
            f.write('{"timestamp":%d,"providerData":{"model":"test-ma",'
                    '"conversation_request_id=%s","rawUsage":{"prompt_tokens":240000,'
                    '"completion_tokens":80,"prompt_cache_hit_tokens":228000}}}\n'
                    % (t - 1000, rid2))
        with open(os.path.join(proj, "bbbb2222-2222-3333-4444-555566667777.jsonl"),
                  "w", encoding="utf-8") as f:
            f.write('{"timestamp":%d,"providerData":{"model":"test-mb",'
                    '"rawUsage":{"prompt_tokens":8000,"completion_tokens":20,'
                    '"prompt_cache_hit_tokens":6800}}}\n' % (t - 3000))
        # 假宿主库：s1 在窗口内（有窗口大小），s2 只存在于 transcript
        db = os.path.join(tmp, "fake.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sessions(id TEXT, title TEXT, custom_title TEXT,"
                    " cwd TEXT, model TEXT, updated_at INTEGER, deleted_at INTEGER)")
        con.execute("CREATE TABLE session_usage(session_id TEXT, used INTEGER,"
                    " size INTEGER, updated_at INTEGER, credit_json TEXT)")
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,NULL)",
                    ("aaaa1111-2222-3333-4444-555566667777", "窗口内会话", None,
                     "d:/demo", "test-ma", 0))
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,NULL)",
                    ("bbbb2222-2222-3333-4444-555566667777", "窗口外会话", None,
                     "d:/demo2", "test-mb", 0))
        con.execute("INSERT INTO session_usage VALUES(?,?,?,?,?)",
                    ("aaaa1111-2222-3333-4444-555566667777", 240000, 300000, 0,
                     '{"%s": 5.0}' % rid1))
        con.commit()
        con.close()

        out = os.path.join(tmp, "s.html")
        r = run(["--data-dir", tmp, "--db", db, "--projects",
                 os.path.join(tmp, "projects"), "--out", out])
        check("退出码 0", r.returncode == 0, r.stderr)
        check("窗口外补齐计数", "窗口之外直读补齐 1 个" in r.stdout, r.stdout[-400:])
        check("缓存命中率聚合 94.6%", "94.6%" in r.stdout, r.stdout[-400:])
        check("积分按 RID 归属（合计 5.0）", "积分合计 5.0" in r.stdout, r.stdout[-400:])
        html = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        check("窗口外会话标题入表", "窗口外会话" in html)
        check("窗口内占比 80% + 警示", "80%" in html and "建议换会话" in html)
        check("test-mb 缓存命中率 85%", "85%" in html)
        check("窗口外无分母时显示数值", "8,000" in html)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def _running_report(arguments, setup=""):
    if setup:
        code = "import sys; sys.path.insert(0, %r); import report\n" % os.path.dirname(REPORT)
        code += setup + "\nraise SystemExit(report.main())"
        command = [PY, "-u", "-c", code] + arguments
    else:
        command = [PY, "-u", REPORT] + arguments
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", env=ENV, cwd=HERE)
    output = queue.Queue()

    def read_lines():
        for line in proc.stdout:
            output.put(line)
        output.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    lines = []
    try:
        deadline = time.monotonic() + 15
        port = None
        while time.monotonic() < deadline:
            try:
                line = output.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            lines.append(line)
            match = re.search(r"127\.0\.0\.1:(\d+)", line)
            if match:
                port = int(match.group(1))
                break
        if port is None:
            raise AssertionError("服务未宣告监听地址：" + "".join(lines))
        yield port, lines
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        reader.join(timeout=2)
        proc.stdout.close()


def _local_get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return response.status, response.headers, response.read().decode("utf-8")


@contextmanager
def _fake_health_server(payload=None, status=200):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "wb-ops-report"

        def do_GET(self):
            body = b"ordinary HTTP service" if payload is None else payload
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "/somewhere-else")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        allow_reuse_address = False
        allow_reuse_port = False

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    with Server(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            worker.join(timeout=5)


def case_serve():
    print("\n[5] --serve：健康协议 / 固定地址复用 / 配置隔离 / 动态页面")
    with tempfile.TemporaryDirectory(prefix="wbops-rep-serve-") as tmp:
        with open(os.path.join(tmp, "saves-202610.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"t":%d,"s":"testsess00000000","tool":"Grep","b":50000,"a":6700}\n'
                    % int(time.time() * 1000))
        config_args = ["--no-prune", "--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
                       "--projects", os.path.join(tmp, "noproj")]
        with _running_report(["--serve", "--port", "0"] + config_args) as (port, lines):
            url = "http://127.0.0.1:%d/" % port
            check("监听地址与数据就绪提示分离", "已监听" in "".join(lines))
            code, headers, raw = _local_get(url + "__wb_ops__/health")
            health = json.loads(raw)
            check("健康端点 200 且禁缓存", code == 200 and headers.get("Cache-Control") == "no-store")
            check("健康端点为 JSON", headers.get("Content-Type", "").startswith("application/json"))
            expected = report._service_identity(report._service_config(argparse.Namespace(
                data_dir=tmp, db=os.path.join(tmp, "none.db"), projects=os.path.join(tmp, "noproj"),
                demo=False, no_prune=True, refresh=30)))
            check("健康协议/版本/配置指纹精确匹配", all(health.get(k) == v for k, v in expected.items())
                  and health.get("version") == "0.6.3" and isinstance(health.get("ready"), bool))
            check("健康端点不泄露路径", set(health) == set(expected) | {"ready"}
                  and tmp not in raw and "none.db" not in raw and re.fullmatch(
                      r"[0-9a-f]{64}", health.get("config_fingerprint", "")) is not None)
            code, headers, body = _local_get(url)
            check("首页 200", code == 200)
            check("实时标记（无脚本刷新兜底 + 局部更新）",
                  '<noscript><meta http-equiv="refresh" content="30">' in body
                  and 'data-served="true" data-refresh="30"' in body
                  and 'id="refresh-status"' in body)
            check("假账本数据入页", "testsess" in body, body[:200])
            check("首页 Cache-Control: no-store", headers.get("Cache-Control") == "no-store")
            health = json.loads(_local_get(url + "__wb_ops__/health")[2])
            check("构建完成后健康端点报告就绪", health["ready"] is True)
            check("服务不写静态 dashboard", not os.path.exists(os.path.join(tmp, "dashboard.html")))
            code = 0
            try:
                _local_get(url + "nope")
            except urllib.error.HTTPError as e:
                code = e.code
            check("未知路径 404", code == 404)
            reused = run(["--serve", "--port", str(port)] + config_args)
            check("重复启动退出 0 并复用同一 URL", reused.returncode == 0
                  and "复用已有" in reused.stdout and url in reused.stdout, reused.stdout + reused.stderr)
            with patch.dict(ENV, HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1",
                            ALL_PROXY="http://127.0.0.1:1", NO_PROXY=""):
                status = run(["--status", "--port", str(port)] + config_args)
            check("身份状态探测禁用系统代理", status.returncode == 0 and url in status.stdout, status.stderr)
            normalized = config_args[:]
            normalized[normalized.index("--data-dir") + 1] = os.path.join(tmp, ".")
            reused = run(["--serve", "--port", str(port)] + normalized)
            check("规范化相同路径可复用", reused.returncode == 0 and url in reused.stdout, reused.stderr)
            changes = (("data_dir", ["--data-dir", os.path.join(tmp, "other-data")]),
                       ("db", ["--db", os.path.join(tmp, "other.db")]),
                       ("projects", ["--projects", os.path.join(tmp, "other-projects")]),
                       ("demo", ["--demo"]), ("refresh", ["--refresh", "0"]))
            for name, extra in changes:
                mismatch = run(["--serve", "--port", str(port)] + config_args + extra)
                check("不同 %s 拒绝复用且提示重启" % name, mismatch.returncode != 0
                      and "配置不匹配" in mismatch.stderr and "重新启动" in mismatch.stderr,
                      mismatch.stdout + mismatch.stderr)
            mismatch = run(["--serve", "--port", str(port)] + [
                a for a in config_args if a != "--no-prune"])
            check("不同 no_prune 拒绝复用", mismatch.returncode != 0 and "配置不匹配" in mismatch.stderr)
            check("拒绝复用未杀原服务", _local_get(url + "__wb_ops__/health")[0] == 200)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    second.bind(("127.0.0.1", port))
                    second.listen()
                    exclusive = False
                except OSError:
                    exclusive = True
            check("禁止第二监听者（包括 SO_REUSEADDR）", exclusive)
        with _running_report(["--serve", "--port", "0", "--refresh", "0"] + config_args) as (port, _):
            body = _local_get("http://127.0.0.1:%d/" % port)[2]
            check("refresh=0 保持动态模式且关闭自动刷新", 'data-served="true" data-refresh="0"' in body
                  and '<noscript><meta http-equiv="refresh"' not in body)
            check("refresh=0 服务仍不写 dashboard", not os.path.exists(os.path.join(tmp, "dashboard.html")))
    case_service_identity()
    case_service_startup()
    case_service_cli()


def case_service_identity():
    print("\n[5a] 占用者身份拒绝 / 有限探测 / 指纹覆盖")
    with tempfile.TemporaryDirectory(prefix="wbops-rep-identity-") as tmp:
        args = argparse.Namespace(data_dir=tmp, db=os.path.join(tmp, "none.db"),
                                  projects=os.path.join(tmp, "noproj"), demo=False, no_prune=True,
                                  refresh=30, out=None, port=0)
        config_args = ["--data-dir", args.data_dir, "--db", args.db, "--projects", args.projects, "--no-prune"]
        identity = report._service_identity(report._service_config(args))
        cases = [("普通 HTTP 服务即使伪装 Server 头也拒绝", None, 200, "JSON"),
                 ("旧版本拒绝并提示版本不匹配", json.dumps(dict(identity, version="0.6.2")).encode(),
                  200, "版本不匹配"),
                 ("错误协议拒绝", json.dumps(dict(identity, protocol="other/1")).encode(),
                  200, "兼容协议"),
                 ("错误服务标识拒绝", json.dumps(dict(identity, service="other-service")).encode(),
                  200, "兼容协议"),
                 ("非对象 JSON 拒绝", b"[]", 200, "兼容协议"),
                 ("过深 JSON 安全拒绝", b"[" * 1500 + b"]" * 1500, 200, "拒绝复用"),
                 ("缺失配置指纹拒绝", json.dumps({k: v for k, v in identity.items()
                                           if k != "config_fingerprint"}).encode(), 200, "配置不匹配"),
                 ("超大健康响应拒绝", b" " * 4097, 200, "大小限制"),
                 ("健康端点重定向拒绝", b"", 302, "健康端点"),
                 ("不存在的健康端点拒绝", b"", 404, "健康端点")]
        for name, payload, status, reason in cases:
            with _fake_health_server(payload, status) as port:
                result = run(["--serve", "--port", str(port)] + config_args)
                check(name, result.returncode != 0 and reason in result.stderr
                      and "不会自动换端口或终止进程" in result.stderr, result.stdout + result.stderr)
        with patch.object(report, "__file__", os.path.join(tmp, "another-install", "report.py")):
            relocated = report._service_identity(report._service_config(args))
        check("身份不依赖安装路径", identity == relocated)
        default = report.default_paths()
        implicit = argparse.Namespace(**dict(vars(args), data_dir=None, db=None, projects=None))
        explicit = argparse.Namespace(**dict(vars(args), **default))
        check("默认数据源与显式数据源身份相同", report._service_identity(report._service_config(implicit))
              == report._service_identity(report._service_config(explicit)))
        for key, value in (("data_dir", os.path.join(tmp, "other")), ("db", os.path.join(tmp, "x.db")),
                           ("projects", os.path.join(tmp, "xproj")), ("demo", True),
                           ("no_prune", False), ("refresh", 0)):
            changed = argparse.Namespace(**dict(vars(args), **{key: value}))
            check("配置指纹覆盖 " + key, identity != report._service_identity(report._service_config(changed)))
        with patch("urllib.request.build_opener", side_effect=AssertionError("不应探测非碰撞错误")), \
                patch("http.server.ThreadingHTTPServer.__init__", side_effect=OSError("非端口碰撞")):
            check("非端口碰撞失败不做复用探测", report.serve(args) != 0)
        args.port = 12345
        with patch("http.server.ThreadingHTTPServer.__init__", side_effect=OSError(errno.EADDRINUSE, "碰撞")), \
                patch.object(report, "_probe_service", side_effect=[
                    (None, "启动中", True), (identity, "", False)]) as probe, \
                patch.object(report.time, "sleep"):
            result = report.serve(args)
        check("仅启动碰撞时短重试并复用", result == 0 and probe.call_count == 2)
        with patch("http.server.ThreadingHTTPServer.__init__", side_effect=OSError(errno.EADDRINUSE, "碰撞")), \
                patch.object(report, "_probe_service", return_value=(None, "版本不匹配", False)) as probe:
            result = report.serve(args)
        check("明确身份不匹配不重试", result != 0 and probe.call_count == 1)
        with patch("http.server.ThreadingHTTPServer.__init__", side_effect=OSError(errno.EADDRINUSE, "碰撞")), \
                patch.object(report, "_probe_service", return_value=(None, "无响应", True)) as probe, \
                patch.object(report.time, "monotonic", side_effect=[0.0, 1.0, 2.1]), \
                patch.object(report.time, "sleep"):
            result = report.serve(args)
        check("碰撞就绪探测重试有明确上限", result != 0 and probe.call_count == 2)


def case_service_startup():
    print("\n[5b] 后台加载 / 健康无扫描 / 并发启动 / monotonic 缓存")
    with tempfile.TemporaryDirectory(prefix="wbops-rep-startup-") as tmp:
        config = ["--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
                  "--projects", os.path.join(tmp, "noproj"), "--no-prune"]
        slow = ("import threading\n"
                "def slow_build(*a, **k):\n"
                "    threading.Event().wait(60)\n"
                "    raise AssertionError('不应等待扫描')\n"
                "report.build = slow_build")
        with _running_report(["--serve", "--port", "0"] + config, slow) as (port, _):
            start = time.monotonic()
            health = json.loads(_local_get("http://127.0.0.1:%d/__wb_ops__/health" % port)[2])
            check("扫描阻塞时健康端点仍立即响应且未就绪", health["ready"] is False
                  and time.monotonic() - start < 3)
            results = []
            workers = [threading.Thread(target=lambda: results.append(
                run(["--serve", "--port", str(port)] + config))) for _ in range(3)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
            check("并发重复启动均复用未就绪服务", len(results) == 3 and all(
                result.returncode == 0 and "复用已有" in result.stdout for result in results))
            check("健康与复用不写静态报告", not os.path.exists(os.path.join(tmp, "dashboard.html")))
        expire = os.path.join(tmp, "expire-cache")
        cached = ("import types, os\n"
                  "real_clock = report.time.monotonic\n"
                  "count = [0]\n"
                  "expire_file = %r\n"
                  "def wall_clock():\n"
                  "    raise AssertionError('缓存不应使用墙上时钟')\n"
                  "def monotonic_clock():\n"
                  "    return real_clock() + (10 if os.path.exists(expire_file) else 0)\n"
                  "def build_once(*a, **k):\n"
                  "    count[0] += 1\n"
                  "    return '<html>build-%%d</html>' %% count[0], {}, {'records': []}\n"
                  "report.time = types.SimpleNamespace(monotonic=monotonic_clock, time=wall_clock)\n"
                  "report.build = build_once") % expire
        with _running_report(["--serve", "--port", "0"] + config, cached) as (port, _):
            url = "http://127.0.0.1:%d/" % port
            first = _local_get(url)[2]
            second = _local_get(url)[2]
            check("5 秒缓存使用 monotonic 且复用构建结果", first == second == "<html>build-1</html>")
            with open(expire, "w", encoding="utf-8") as f:
                f.write("expire")
            check("单调时钟超过缓存期限后重新构建", _local_get(url)[2] == "<html>build-2</html>")


def case_service_cli():
    print("\n[5c] CLI 默认固定端口 / 参数互斥 / 静态兼容")
    with patch.object(sys, "argv", [REPORT, "--serve"]), patch.object(report, "serve", return_value=0) as service:
        result = report.main()
    check("serve 默认固定端口 8787", result == 0 and service.call_args.args[0].port == 8787)
    for modes in (("--serve", "--snapshot"), ("--serve", "--status"), ("--snapshot", "--status")):
        result = run(list(modes))
        check("模式参数互斥 " + "/".join(modes), result.returncode == 2)
    for options in (("--serve", "--port", "-1"), ("--serve", "--port", "65536"),
                    ("--status", "--port", "0"), ("--serve", "--refresh", "-1")):
        result = run(list(options))
        check("无效服务参数拒绝 " + " ".join(options), result.returncode == 2)
    with tempfile.TemporaryDirectory(prefix="wbops-rep-snapshot-") as tmp:
        defaults = dict(data_dir=tmp, db=os.path.join(tmp, "none.db"), projects=os.path.join(tmp, "noproj"))
        with patch.object(sys, "argv", [REPORT]), patch.object(report, "default_paths", return_value=defaults):
            result = report.main()
        check("Python 无参仍静态导出", result == 0 and os.path.exists(os.path.join(tmp, "dashboard.html")))
        out = os.path.join(tmp, "snapshot.html")
        result = run(["--snapshot", "--demo", "--out", out, "--data-dir", tmp,
                      "--db", defaults["db"], "--projects", defaults["projects"]])
        body = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        check("显式 snapshot 静态导出", result.returncode == 0 and 'data-served="false" data-refresh="0"' in body,
              result.stderr)


def case_dashboard_ui():
    print("\n[6] 控制台 UI：模式 / 无外部资源 / 空态 / 转义 / 可访问性")
    recs, host, rid_ts, models, ctx = report.build_demo()
    agg = report.aggregate(recs, host, rid_ts, models, ctx)
    meta = {"generated_at": "2026-10-15 23:00", "data_dir": "<img src=x onerror=alert(1)>",
            "title_note": "示意数据", "demo": True, "live_refresh": 0}
    static = report.render_html(agg, meta)
    live = report.render_html(agg, dict(meta, served=True, live_refresh=30))
    manual = report.render_html(agg, dict(meta, served=True))
    check("静态页面不启用轮询", 'data-served="false" data-refresh="0"' in static)
    check("实时页面启用 30 秒更新", 'data-served="true" data-refresh="30"' in live)
    check("关闭自动更新仍支持服务模式", 'data-served="true" data-refresh="0"' in manual)
    check("局部更新与隐藏暂停逻辑", "current.replaceWith(next)" in live
          and "visibilitychange" in live and "AbortController" in live)
    check("移除搜索但保留滚动位置", "table-search" not in live and "row.hidden" not in live
          and "search-empty" not in live and "window.scrollTo" in live)
    check("无外部脚本或样式依赖", not re.search(r'<script[^>]+src=|<link[^>]+href=', static))
    check("元数据转义", "&lt;img src=x onerror=alert(1)&gt;" in static
          and "<img src=x" not in static)
    check("响应式与减弱动态效果", "@media (max-width:600px)" in static
          and "prefers-reduced-motion:reduce" in static)
    check("键盘与导航可访问性", ":focus-visible" in static and 'type="search"' not in static
          and 'class="skip-link"' in static and '<nav>' in static)
    check("运行指标分层展示", 'class="runtime-strip"' in live and 'aria-label="运行指标"' in live
          and '缓存命中率' in live and '累计积分' in live)
    check("刷新倒计时与生命周期清理", 'id="refresh-countdown"' in live and 'nextRefreshAt' in live
          and 'clearInterval(countdownTimer)' in live)
    check("上下文告警摘要", 'class="health-summary"' in live and '个会话建议收尾' in live)
    check("搜索界面与脚本彻底移除", not any(x in live for x in (
        '搜索', 'search-count', '.search', 'rowText', 'input.disabled')))
    check("窄屏运行指标两列", '.runtime-strip { grid-template-columns:repeat(2' in live)

    class Markup(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
        def handle_starttag(self, tag, attrs):
            attr = dict(attrs)
            if "id" in attr:
                self.ids.append(attr["id"])

    parsed = Markup()
    parsed.feed(static)
    check("页面锚点唯一且齐全", len(parsed.ids) == len(set(parsed.ids))
          and all(x in parsed.ids for x in ("overview", "context-health", "tools-projects",
                                            "model-analysis", "recent-activity", "report-content")))
    empty = report.aggregate([], {"sessions": [], "usage": []}, {})
    empty_html = report.render_html(empty, meta)
    check("无数据时可完整渲染", "暂无会话占用数据" in empty_html and "0.0%" in empty_html)
    agg["usage_rows"][0]["title"] = "<script>alert(7)</script>"
    escaped = report.render_html(agg, meta)
    check("会话标题无法注入脚本", "<script>alert(7)</script>" not in escaped
          and "&lt;script&gt;alert(7)&lt;/script&gt;" in escaped)
    stale = report.render_html(agg, dict(meta, served=True, stale=True))
    check("旧数据明确提示更新失败", 'data-stale="true"' in stale and "仍显示旧数据" in stale)
    check("图表提示文字转义", "&lt;unsafe&gt;" in report.bar_svg([("<unsafe>", 10)], "#74e6bf", str))


def case_bad_timestamp():
    print("\n[7] S2：越界时间戳计入损坏行，不再拖崩报告")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-badts-")
    try:
        fp = os.path.join(tmp, "saves-%s.jsonl" % datetime.now().strftime("%Y%m"))
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": 1750000000000, "s": "aaaa1111", "tool": "Grep",
                                "b": 9000, "a": 500}) + "\n")
            f.write(json.dumps({"t": 99999999999999999999, "s": "aaaa1111", "tool": "Grep",
                                "b": 9000, "a": 500}) + "\n")
            f.write(json.dumps({"t": -5, "s": "aaaa1111", "tool": "Grep",
                                "b": 9000, "a": 500}) + "\n")
        out = os.path.join(tmp, "bt.html")
        r = run(["--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
                 "--projects", os.path.join(tmp, "noproj"), "--out", out, "--no-prune"])
        check("退出码 0（不再崩溃）", r.returncode == 0, r.stderr[-200:])
        check("报告已生成", os.path.exists(out))
        check("越界时间戳计入损坏行（1 好 / 2 坏）",
              "记账记录 1 条 / 损坏行 2" in r.stdout, r.stdout[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("report.py 冒烟测试")
    print("=" * 56)
    if not os.path.exists(REPORT):
        print("找不到 report.py：%s" % REPORT)
        return 1
    case_demo()
    case_real_fake_ledger()
    case_prune()
    case_scan_full()
    case_serve()
    case_dashboard_ui()
    case_bad_timestamp()
    print()
    print("通过 %d / 失败 %d" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
