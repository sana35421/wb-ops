#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py 冒烟测试：demo 全流程 / 真实模式假账本降级 / 过期清理 / 隔离性 / 实时服务。

原则：全部用例在临时目录跑，绝不读写真实 ~/.workbuddy 数据。
用法：python test_report.py
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from contextlib import ExitStack
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


def case_serve():
    print("\n[5] --serve 实时服务：端口宣告 / 200+实时标记 / 404 / 缓存头")
    tmp = tempfile.mkdtemp(prefix="wbops-rep-serve-")
    proc = None
    try:
        with open(os.path.join(tmp, "saves-202610.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"t":%d,"s":"testsess00000000","tool":"Grep","b":50000,"a":6700}\n'
                    % int(time.time() * 1000))
        proc = subprocess.Popen(
            [PY, REPORT, "--serve", "--port", "0", "--no-prune",
             "--data-dir", tmp, "--db", os.path.join(tmp, "none.db"),
             "--projects", os.path.join(tmp, "noproj")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", env=ENV, cwd=HERE)
        # 读启动行拿端口（系统自动分配）
        port = None
        deadline = time.time() + 60
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if m:
                port = int(m.group(1))
                break
        check("服务启动并宣告端口", port is not None)
        if port is None:
            return
        url = "http://127.0.0.1:%d/" % port
        resp = body = None
        dl = time.time() + 60
        while time.time() < dl:  # 首次构建完成后才能连上，轮询等待
            try:
                resp = urllib.request.urlopen(url, timeout=20)
                body = resp.read().decode("utf-8")
                break
            except Exception:
                time.sleep(0.5)
        check("首页 200", resp is not None and resp.status == 200)
        if body is None:
            return
        check("实时标记（无脚本刷新兜底 + 局部更新）",
              '<noscript><meta http-equiv="refresh" content="30">' in body
              and 'data-served="true" data-refresh="30"' in body
              and 'id="refresh-status"' in body)
        check("假账本数据入页", "testsess" in body, body[:200])
        check("Cache-Control: no-store", resp.headers.get("Cache-Control") == "no-store")
        code = 0
        try:
            urllib.request.urlopen(url + "nope", timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            pass
        check("未知路径 404", code == 404)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


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
    check("保留搜索与滚动位置", "row.hidden" in live and "window.scrollTo" in live)
    check("无外部脚本或样式依赖", not re.search(r'<script[^>]+src=|<link[^>]+href=', static))
    check("元数据转义", "&lt;img src=x onerror=alert(1)&gt;" in static
          and "<img src=x" not in static)
    check("响应式与减弱动态效果", "@media (max-width:600px)" in static
          and "prefers-reduced-motion:reduce" in static)
    check("键盘与搜索可访问性", ":focus-visible" in static and 'type="search"' in static
          and 'class="skip-link"' in static and '<nav>' in static)

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
