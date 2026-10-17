#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-ops 节省统计与 Token 使用报告（v0.3.x 后台；命令行入口 /wb-ops:report）

读取三类数据。斜杠命令默认打开固定动态后台；Python 无参保留静态导出兼容。
`--serve` 启动或复用本地实时服务（5 秒短缓存、页面自动刷新），日常查看无需反复生成。
内联样式与 SVG，零依赖：
  1. ~/.workbuddy/wb-ops-data/saves-*.jsonl  —— wb-ops 裁剪记账（省了多少，决定性数据）
  2. ~/.workbuddy/workbuddy.db（只读）        —— 会话积分消耗 / 上下文占用窗口
  3. ~/.workbuddy/projects/*/*.jsonl          —— 单遍全量直读 transcript（v0.3.5 起一次遍历产出）：
       · 按模型 token 消耗 + 缓存命中（三格式归一，突破宿主 12 行窗口）
       · conversationRequestId -> 时间戳（积分按天归属）
       · 每会话末次请求 prompt_tokens（= 上下文占用精确值，全量会话覆盖）

保留策略：saves-*.jsonl 只保留最近 3 个月（滚动，生成报告时自动清理；--no-prune 跳过）。

用法：
  python report.py                  # 生成 ~/.workbuddy/wb-ops-data/dashboard.html
  python report.py --open           # 生成后用默认浏览器打开
  python report.py --out D:/x.html  # 指定输出路径
  python report.py --demo           # 用合成数据生成示意报告（不读真实数据）
  python report.py --no-prune       # 不清理过期记账文件
  python report.py --serve          # 本地实时服务：浏览器打开 http://127.0.0.1:8787/
                                    # （数据即扫即新，页面每 30s 自动刷新；Ctrl+C 停止）
"""

import argparse
import glob
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta

KEEP_MONTHS = 3          # 记账文件保留月数（含当前月）
DAYS_SHOWN = 14          # 趋势图天数
RECENT_ROWS = 25         # 明细表行数
CTX_WARN_RATIO = 0.75    # 上下文占用警示阈值（用户拍板 75%）
BYTES_PER_TOKEN = 3.5    # 字节 -> token 粗估系数（仅用于"感知量级"，非账单口径）

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def hb(n):
    """字节数 -> 人类可读。"""
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def est_tokens(nbytes):
    """字节 -> token 粗估（中英混合常见密度，仅供感知量级）。"""
    t = nbytes / BYTES_PER_TOKEN
    if t >= 10000:
        return "%.0f 万" % (t / 10000.0)
    return "%.0f" % t


def fmt_tok(n):
    """token 数 -> 人类可读（亿/万/千分位）。"""
    n = float(n)
    if n >= 1e8:
        return "%.2f 亿" % (n / 1e8)
    if n >= 1e4:
        return "%.1f 万" % (n / 1e4)
    return format(int(n), ",")


def esc(s):
    return html.escape(str(s if s is not None else ""))


def day_str(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")


def short_day(dstr):
    """2026-10-15 -> 10-15"""
    return dstr[5:] if len(dstr) >= 10 else dstr


def default_paths():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    wb = os.path.join(home, ".workbuddy")
    return {
        "data_dir": os.path.join(wb, "wb-ops-data"),
        "db": os.path.join(wb, "workbuddy.db"),
        "projects": os.path.join(wb, "projects"),
    }


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def prune_old(data_dir, keep_months=KEEP_MONTHS):
    """删除月份早于「当前月 - keep_months + 1」的 saves-*.jsonl。返回被删文件名列表。"""
    now = datetime.now()
    cur_idx = now.year * 12 + (now.month - 1)
    removed = []
    for fp in glob.glob(os.path.join(data_dir, "saves-*.jsonl")):
        m = re.match(r"saves-(\d{4})(\d{2})\.jsonl$", os.path.basename(fp))
        if not m:
            continue
        idx = int(m.group(1)) * 12 + (int(m.group(2)) - 1)
        if idx < cur_idx - (keep_months - 1):
            try:
                os.remove(fp)
                removed.append(os.path.basename(fp))
            except OSError:
                pass
    return removed


def load_saves(data_dir):
    """读全部 saves-*.jsonl。返回 (records, bad_lines, files)。"""
    records, bad, files = [], 0, 0
    for fp in sorted(glob.glob(os.path.join(data_dir, "saves-*.jsonl"))):
        files += 1
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                        t = o["t"]
                        b = o["b"]
                        a = o["a"]
                        if not (isinstance(t, (int, float)) and isinstance(b, (int, float))
                                and isinstance(a, (int, float))):
                            raise ValueError("bad field type")
                        # v0.6.1（S2）：时间戳区间校验（(0, 4102444800000) ≈ 公元 2100 年前），
                        # 越界值（半行/脏数据/手工改坏）计入 bad 丢弃，而不是把整份报告拖崩
                        if not (0 < t < 4102444800000):
                            raise ValueError("timestamp out of range")
                        records.append({"t": int(t), "s": str(o.get("s", "")),
                                        "tool": str(o.get("tool", "")),
                                        "b": int(b), "a": int(a)})
                    except Exception:
                        bad += 1
        except OSError:
            continue
    return records, bad, files


def load_host(db_path):
    """只读读取 workbuddy.db：sessions + session_usage。读失败返回空结构（降级）。

    v0.6.1（S7）：credit_json 解析失败逐条计数（credit_bad），供页面区分
    「本就没有积分数据」与「读库/解析失败导致的降级」。
    """
    out = {"sessions": [], "usage": [], "ok": False, "err": "", "credit_bad": 0}
    if not os.path.exists(db_path):
        out["err"] = "数据库不存在"
        return out
    try:
        uri = "file:%s?mode=ro" % db_path.replace("\\", "/").replace("?", "%3f")
        con = sqlite3.connect(uri, uri=True, timeout=3)
        cur = con.cursor()
        cur.execute("SELECT id, title, custom_title, cwd, model, updated_at "
                    "FROM sessions WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            out["sessions"].append({
                "id": r[0] or "", "title": (r[2] or r[1] or "").strip(),
                "cwd": r[3] or "", "model": r[4] or "", "updated_at": r[5] or 0,
            })
        cur.execute("SELECT session_id, used, size, updated_at, credit_json FROM session_usage")
        for r in cur.fetchall():
            credits = {}
            try:
                cj = json.loads(r[4]) if r[4] else {}
                if not isinstance(cj, dict):
                    raise ValueError("credit_json 不是对象")
                for k, v in cj.items():
                    if isinstance(v, (int, float)):
                        credits[str(k)] = float(v)
            except Exception:
                out["credit_bad"] += 1   # 解析失败计数（S7）：页面据此提示降级
            out["usage"].append({
                "sid": r[0] or "", "used": int(r[1] or 0), "size": int(r[2] or 0),
                "updated_at": int(r[3] or 0), "credits": credits,
            })
        con.close()
        out["ok"] = True
    except Exception as e:
        out["err"] = str(e)
    return out


def scan_transcripts(projects_dir):
    """v0.3.5 单遍扫描全部 transcript，合并原 scan_rid_ts + scan_model_usage + 上下文直读。

    纯字符串/正则提取（不逐行 json.loads，95MB 全扫约 0.2s），坏行自动跳过。
    返回 (models, rid_ts, ctx)：
      models: {model: {n_sessions, req, pin, pout, hit, first, last}}
              pin/pout 为计费口径（每请求含完整上下文）；hit 为缓存命中 tokens，
              三格式归一提炼：DeepSeek prompt_cache_hit_tokens /
              Anthropic cache_read_input_tokens / OpenAI cached_tokens。
      rid_ts: {conversationRequestId: 首见时间戳(ms)}，积分按天归属用（首见=最早）。
      ctx:    {sid: {used, ts, model}}，每会话末次请求的 prompt_tokens，
              即上下文占用精确值（与宿主 session_usage.used 实测逐字一致）。
    """
    models = {}
    rid_ts = {}
    ctx = {}

    rx_model = re.compile(r'"model"\s*:\s*"([^"]+)"')
    rx_pt = re.compile(r'"prompt_tokens"\s*:\s*(\d+)')
    rx_ct = re.compile(r'"completion_tokens"\s*:\s*(\d+)')
    rx_ts = re.compile(r'"timestamp"\s*:\s*(\d{13})')
    rx_hit_ds = re.compile(r'"prompt_cache_hit_tokens"\s*:\s*(\d+)')
    rx_hit_an = re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)')
    rx_hit_oa = re.compile(r'"cached_tokens"\s*:\s*(\d+)')

    for fp in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        sid = os.path.basename(fp)[:-6]
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    has_usage = '"prompt_tokens"' in line
                    k = line.find("conversation_request_id=")
                    if not has_usage and k < 0:
                        continue
                    # ---- RID 归属：首见时间戳（文件按时间追加，首见即最早）----
                    if k >= 0:
                        rid = line[k + 24:k + 56]
                        if len(rid) == 32 and rid not in rid_ts:
                            t = rx_ts.search(line)
                            if t:
                                rid_ts[rid] = int(t.group(1))
                    if not has_usage:
                        continue
                    # ---- 模型 token 聚合（同值双格式只取 rawUsage 一侧，不会双计）----
                    m = rx_model.search(line)
                    p = rx_pt.search(line)
                    if not m or not p:
                        continue
                    pt = int(p.group(1))
                    model = m.group(1)
                    t = rx_ts.search(line)
                    tv = int(t.group(1)) if t else 0
                    d = models.setdefault(model, {
                        "sessions": set(), "req": 0, "pin": 0, "pout": 0, "hit": 0,
                        "first": 0, "last": 0})
                    d["sessions"].add(sid)
                    d["req"] += 1
                    d["pin"] += pt
                    c = rx_ct.search(line)
                    if c:
                        d["pout"] += int(c.group(1))
                    h = (rx_hit_ds.search(line) or rx_hit_an.search(line)
                         or rx_hit_oa.search(line))
                    if h:
                        d["hit"] += int(h.group(1))
                    if tv:
                        if not d["first"] or tv < d["first"]:
                            d["first"] = tv
                        if tv > d["last"]:
                            d["last"] = tv
                    # ---- 上下文占用：逐行覆盖，扫完全文件后自然留下末次请求 ----
                    ctx[sid] = {"used": pt, "ts": tv, "model": model}
        except OSError:
            continue

    for d in models.values():
        d["n_sessions"] = len(d["sessions"])
        d.pop("sessions", None)
    return models, rid_ts, ctx


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def aggregate(records, host, rid_ts, models=None, ctx=None):
    """把原始数据聚合成报告需要的所有维度。"""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    days = [today - timedelta(days=i) for i in range(DAYS_SHOWN - 1, -1, -1)]

    # 会话索引：8 位前缀 -> session（saves 里只存了前缀）
    by8 = {}
    for s in host["sessions"]:
        if s["id"]:
            by8.setdefault(s["id"][:8], s)

    agg = {
        "total_b": 0, "total_a": 0, "count": 0,
        "today_saved": 0, "week_saved": 0,
        "per_day": {d.isoformat(): 0 for d in days},
        "per_day_cnt": {d.isoformat(): 0 for d in days},
        "per_tool": {}, "per_project": {},
        "recent": [], "models": models or {},
    }
    for r in records:
        saved = max(0, r["b"] - r["a"])
        agg["total_b"] += r["b"]
        agg["total_a"] += r["a"]
        agg["count"] += 1
        ds = day_str(r["t"])
        if ds in agg["per_day"]:
            agg["per_day"][ds] += saved
            agg["per_day_cnt"][ds] += 1
        if ds == today.isoformat():
            agg["today_saved"] += saved
        # v0.6.1（S6）：加当天上界，未来时间戳（时钟偏移/脏数据）不再虚增本周节省
        if monday.isoformat() <= ds <= today.isoformat():
            agg["week_saved"] += saved
        tl = agg["per_tool"].setdefault(r["tool"], {"saved": 0, "cnt": 0, "b": 0, "a": 0})
        tl["saved"] += saved
        tl["cnt"] += 1
        tl["b"] += r["b"]
        tl["a"] += r["a"]
        ses = by8.get(r["s"])
        proj = os.path.basename((ses["cwd"] if ses else "").replace("\\", "/").rstrip("/")) or "未知项目"
        pl = agg["per_project"].setdefault(proj, {"saved": 0, "cnt": 0})
        pl["saved"] += saved
        pl["cnt"] += 1
        agg["recent"].append(r)

    agg["recent"] = sorted(agg["recent"], key=lambda r: r["t"], reverse=True)[:RECENT_ROWS]

    # 上下文占用：宿主 session_usage 窗口 + transcript 全量直读（窗口之外会话）
    ctx = ctx or {}
    model_size = {}   # 模型 -> 已知最大上下文窗口（用于窗口外会话的占用分母）
    for u in host["usage"]:
        mdl = ((ctx.get(u["sid"]) or {}).get("model")
               or (by8.get(u["sid"][:8]) or {}).get("model") or "")
        if mdl and u["size"] and u["size"] > model_size.get(mdl, 0):
            model_size[mdl] = u["size"]

    # 积分：会话级（直接用 credit_json）+ 按天（借 rid_ts 归属）
    credit_total = 0.0
    per_day_credit = {d.isoformat(): 0.0 for d in days}
    per_day_credit_unknown = 0.0
    usage_rows = []
    seen_sids = set()
    for u in host["usage"]:
        sid = u["sid"]
        seen_sids.add(sid)
        csum = sum(u["credits"].values())
        credit_total += csum
        ses = by8.get(sid[:8])
        c2 = ctx.get(sid) or {}
        title = (ses["title"] if ses else "") or ("会话 " + sid[:6])
        cwd = ses["cwd"] if ses else ""
        mdl = c2.get("model") or (ses["model"] if ses else "")
        size = u["size"] or model_size.get(mdl, 0)
        used = u["used"] or c2.get("used") or 0
        usage_rows.append({
            "sid": sid, "title": title, "cwd": cwd, "model": mdl,
            "used": used, "size": size,
            "ratio": (used / size) if size else None,
            "credits": csum, "extra": False,
        })
        for rid, cv in u["credits"].items():
            ts = rid_ts.get(rid)
            if ts is None:
                per_day_credit_unknown += cv
                continue
            ds = day_str(ts)
            if ds in per_day_credit:
                per_day_credit[ds] += cv
            else:
                per_day_credit_unknown += cv

    # v0.3.5：宿主窗口之外（session_usage 只有最近 12 条）—— 凭 transcript 直读补齐
    for sid, c2 in ctx.items():
        if sid in seen_sids:
            continue
        ses = by8.get(sid[:8])
        title = (ses["title"] if ses else "") or ("会话 " + sid[:6])
        cwd = ses["cwd"] if ses else ""
        size = model_size.get(c2["model"], 0)
        usage_rows.append({
            "sid": sid, "title": title, "cwd": cwd, "model": c2["model"],
            "used": c2["used"], "size": size,
            "ratio": (c2["used"] / size) if size else None,
            "credits": None, "extra": True,
        })

    # 已知占比的按占比降序在前；未知占比的按绝对占用降序在后
    usage_rows.sort(key=lambda x: (x["ratio"] is None, -(x["ratio"] or 0.0), -x["used"]))
    by_credit = sorted(usage_rows, key=lambda x: (x["credits"] or 0.0), reverse=True)

    agg["credit_total"] = credit_total
    agg["per_day_credit"] = per_day_credit
    agg["credit_unknown"] = per_day_credit_unknown
    agg["usage_rows"] = usage_rows
    agg["usage_by_credit"] = by_credit
    agg["ctx_extra"] = sum(1 for r in usage_rows if r["extra"])
    agg["days"] = [d.isoformat() for d in days]
    return agg


# ---------------------------------------------------------------------------
# 渲染（深色单页 + 手写 SVG）
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme:dark; --bg:#070a0e; --surface:#0e131b; --surface-subtle:#121824;
  --border:rgba(174,203,227,.09); --border-hover:rgba(116,230,191,.35);
  --text:#edf4fa; --text-strong:#ffffff; --muted:#8e9fae; --mint:#74e6bf; --blue:#9cbcf2; }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; scroll-padding-top:92px; }
body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  letter-spacing:-.01em; -webkit-font-smoothing:antialiased; }
body::before { content:''; position:fixed; pointer-events:none; z-index:-1; inset:0;
  background:radial-gradient(ellipse 900px 500px at 85% -10%,rgba(116,230,191,.065),transparent 70%),
             radial-gradient(ellipse 700px 400px at 15% 100%,rgba(156,188,242,.04),transparent 70%),
             linear-gradient(rgba(255,255,255,.012) 1px,transparent 1px),
             linear-gradient(90deg,rgba(255,255,255,.012) 1px,transparent 1px);
  background-size:100% 100%,100% 100%,48px 48px,48px 48px; }
.hero-accent { color:var(--mint); margin-left:4px; text-shadow:0 0 20px rgba(116,230,191,.35); }
.card:first-child { background:linear-gradient(135deg,rgba(21,48,41,.92),rgba(14,24,28,.85) 60%,rgba(14,19,27,.8) 100%);
  border-color:rgba(116,230,191,.28); box-shadow:0 6px 24px -6px rgba(0,0,0,.5),0 0 16px -4px rgba(116,230,191,.15); }
.card:first-child:hover { border-color:rgba(116,230,191,.5); box-shadow:0 12px 32px -6px rgba(0,0,0,.6),0 0 24px -2px rgba(116,230,191,.25); }
.panel h2 { display:flex; align-items:center; gap:10px; }
.panel h2::before { content:''; width:3px; height:15px; border-radius:3px; background:var(--mint); opacity:.85;
  box-shadow:0 0 8px rgba(116,230,191,.5); }
.table-scroll { scrollbar-width:thin; scrollbar-color:rgba(116,230,191,.2) transparent; }
.runtime-item strong { color:#dbeafa; }

a { color:inherit; text-decoration:none; }
button,input { font:inherit; }
button { cursor:pointer; }
button:disabled { cursor:not-allowed; opacity:.55; }
:focus-visible { outline:2px solid var(--mint); outline-offset:4px; border-radius:5px; }
[hidden] { display:none !important; }
.skip-link { position:fixed; top:8px; left:240px; z-index:20; padding:10px 16px; background:var(--mint);
  color:#0b0d11; transform:translateY(-160%); border-radius:6px; }
.skip-link:focus { transform:none; }
.sidebar { position:fixed; inset:0 auto 0 0; width:220px; padding:30px 16px 20px;
  background:linear-gradient(180deg,#0a0f16,#070a0e); border-right:1px solid rgba(174,203,227,.07);
  display:flex; flex-direction:column; z-index:10; }
.brand { display:flex; align-items:center; gap:12px; padding:0 12px 32px; font-size:20px; font-weight:650; letter-spacing:-.6px; }
.brand svg { color:var(--mint); flex:none; filter:drop-shadow(0 0 10px rgba(116,230,191,.4)); }
.brand small { display:block; font-size:9px; color:var(--muted); letter-spacing:2px; font-weight:500; }
.nav-label { color:#627282; font-size:10px; letter-spacing:1.7px; padding:6px 14px 12px; }
.sidebar nav { display:grid; gap:7px; }
.nav-link { display:flex; align-items:center; gap:12px; padding:11px 14px; border:1px solid transparent;
  border-radius:8px; color:#95a3b3; font-size:13px; font-weight:500;
  transition:all .2s cubic-bezier(.16,1,.3,1); }
.nav-icon { width:18px; color:#6b7c7b; font:11px/1.5 ui-monospace,Consolas,monospace; transition:color .2s; }
.nav-link:hover { color:var(--text); background:rgba(255,255,255,.03); border-color:rgba(174,203,227,.12);
  transform:translateX(2px); }
.nav-link[aria-current] { color:var(--mint); background:linear-gradient(90deg,rgba(116,230,191,.09),rgba(116,230,191,.02));
  border-color:rgba(116,230,191,.22); box-shadow:inset 2px 0 0 var(--mint); }
.nav-link[aria-current] .nav-icon { color:var(--mint); }
.privacy { margin-top:auto; padding:24px 12px 0; border-top:1px solid var(--border); color:var(--muted); font-size:11px; }
.privacy strong { display:block; color:#9cb1a4; font-size:10px; letter-spacing:1.8px; margin-bottom:5px; }
.privacy svg { vertical-align:-2px; margin-right:5px; }
.workspace { margin-left:220px; min-width:0; }
.topbar { position:sticky; top:0; z-index:8; border-bottom:1px solid rgba(174,203,227,.07);
  background:rgba(7,10,14,.78); backdrop-filter:blur(16px) saturate(180%); -webkit-backdrop-filter:blur(16px); }
.topbar-inner { min-height:76px; max-width:1400px; margin:auto; padding:14px 40px;
  display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
.breadcrumb { font-size:12px; color:var(--muted); margin-right:auto; white-space:nowrap; }
.breadcrumb span { margin:0 12px; color:#3b4754; }
.breadcrumb b { color:#dae1eb; font-weight:550; }
.refresh-countdown { color:var(--muted); font:11px/1.5 ui-monospace,Consolas,monospace; min-width:90px; text-align:right; }
.refresh-button { display:inline-flex; align-items:center; gap:8px; background:rgba(255,255,255,.03); color:#d7dee8;
  border:1px solid rgba(255,255,255,.1); border-radius:8px; padding:8px 14px; font-size:12px; font-weight:500;
  box-shadow:0 1px 3px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.06);
  transition:all .2s cubic-bezier(.16,1,.3,1); }
.refresh-button:hover:not(:disabled) { border-color:rgba(116,230,191,.4); background:rgba(116,230,191,.08);
  color:#fff; transform:translateY(-1px); box-shadow:0 4px 12px -2px rgba(116,230,191,.2); }
.refresh-button:active:not(:disabled) { transform:translateY(0) scale(.98); }
.refresh-button.is-loading svg { animation:spin 1s linear infinite; }
.wrap { max-width:1400px; margin:0 auto; padding:44px 40px 54px; min-width:0; }
.hero { display:flex; justify-content:space-between; align-items:flex-start; gap:24px; margin-bottom:30px; }
.eyebrow { font:10px/1.6 ui-monospace,Consolas,monospace; color:var(--mint); letter-spacing:2.4px; margin:0 0 12px; }
h1 { font-size:clamp(25px,2.6vw,36px); line-height:1.35; font-weight:600; letter-spacing:-.9px; margin:0 0 12px; }
h2 { font-size:14px; margin:0 0 20px; font-weight:600; letter-spacing:.1px; }
.sub { color:var(--muted); font-size:12px; margin:0; }
.hero-meta { text-align:right; flex:none; padding-top:4px; }
.status { display:inline-flex; align-items:center; gap:8px; font-size:11px; color:var(--muted); }
.status-dot { width:6px; height:6px; border-radius:50%; background:#8993a3; flex:none; }
.status[data-state='live'] { color:var(--mint); }
.status[data-state='live'] .status-dot { background:var(--mint); animation:breathe 3s ease-in-out infinite; }
.status[data-state='error'] { color:#efb78c; }
.status[data-state='error'] .status-dot { background:#efb78c; }
.generation { display:block; margin-top:8px; color:#a5afbd; font:11px/1.8 ui-monospace,Consolas,monospace; }
.runtime-strip { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; margin:0 0 30px;
  background:rgba(174,203,227,.07); border:1px solid rgba(174,203,227,.1); border-radius:12px; overflow:hidden;
  box-shadow:0 4px 20px -4px rgba(0,0,0,.4); }
.runtime-item { padding:18px 22px; background:linear-gradient(180deg,#0f151e,#0b1017); min-width:0;
  transition:background .2s; }
.runtime-item:hover { background:linear-gradient(180deg,#121a24,#0d131c); }
.runtime-item span { display:block; font-size:11px; color:var(--muted); margin-bottom:6px; font-weight:500; }
.runtime-item strong { font-size:22px; font-weight:650; letter-spacing:-.5px; font-variant-numeric:tabular-nums; }
.runtime-item small { display:block; margin-top:4px; color:var(--muted); font-size:10px; }
.health-summary { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin:0 0 18px; }
.health-summary .tag { margin:0; padding:4px 9px; }
.metadata { margin:0 0 25px; color:var(--muted); font-size:12px; }
summary { cursor:pointer; width:fit-content; }
.metadata p { overflow-wrap:anywhere; padding:12px 16px; background:rgba(255,255,255,.02);
  border:1px solid var(--border); border-radius:8px; }
.cards { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin-bottom:24px; }
.card,.panel { min-width:0; background:linear-gradient(180deg,rgba(18,24,34,.7),rgba(14,19,27,.8));
  border:1px solid rgba(174,203,227,.1); border-radius:12px;
  box-shadow:0 4px 20px -2px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.05);
  animation:rise .4s cubic-bezier(.16,1,.3,1) both;
  transition:all .25s cubic-bezier(.16,1,.3,1); }
.card:hover,.panel:hover { border-color:rgba(174,203,227,.24);
  box-shadow:0 12px 32px -4px rgba(0,0,0,.5),0 0 16px -2px rgba(116,230,191,.08),inset 0 1px 0 rgba(255,255,255,.08);
  transform:translateY(-2px); }
.is-updated .card,.is-updated .panel,.is-updated .chart-line,.is-updated .chart-bar,.is-updated .bar i { animation:none; }
.card { padding:21px 21px 15px; position:relative; overflow:hidden; }
.card::before { content:''; position:absolute; top:0; left:15%; right:15%; height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.15),transparent); pointer-events:none; }
.card:nth-child(2) { animation-delay:.07s; } .card:nth-child(3) { animation-delay:.14s; } .card:nth-child(4) { animation-delay:.21s; }
.card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.card .k { color:#9cb0c1; font-size:12px; font-weight:500; }
.kpi-icon { color:#91b4a5; border:1px solid rgba(116,230,191,.2); background:rgba(116,230,191,.06); border-radius:7px;
  width:26px; height:26px; display:grid; place-items:center; font:10px ui-monospace,Consolas,monospace;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08); }
.card .v { margin:16px 0 4px; font-size:clamp(24px,2.4vw,32px); line-height:1.3; font-weight:650;
  letter-spacing:-1px; font-variant-numeric:tabular-nums; white-space:nowrap; }
  letter-spacing:-1px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.card-note { font-size:11px; color:var(--muted); min-height:20px; }
.sparkline { display:block; width:100%; height:35px; margin-top:18px; color:var(--mint); opacity:.75; }
.sparkline.blue { color:var(--blue); }
.spark-caption { color:#9ba6b5; font-size:10px; display:flex; justify-content:space-between; gap:8px; margin-top:5px; }
.section-label { display:flex; align-items:center; gap:14px; color:#8994a4; font-size:10px; letter-spacing:1.9px; margin:30px 0 15px; }
.section-label::after { content:''; height:1px; flex:1; background:var(--border); }
.panel { padding:24px; margin-bottom:20px; scroll-margin-top:96px; }
.grid2 { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; margin-bottom:20px; }
.grid2 .panel { margin-bottom:0; }
.grid2 .panel:nth-child(2) { animation-delay:.1s; }
.chart-summary { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:15px; color:var(--muted); font-size:11px; }
.chart-summary strong { color:var(--text); font-size:21px; font-weight:550; letter-spacing:-.5px; margin-right:6px; }
.chart-summary span:last-child { text-align:right; }
.chart { display:block; width:100%; overflow:visible; }
.chart text { fill:#9da8b8; font:10px -apple-system,'Segoe UI','Microsoft YaHei',sans-serif; }
.chart-grid { stroke:#ffffff09; stroke-dasharray:3 5; }
.chart-bar { transform-box:fill-box; transform-origin:center bottom; animation:grow .8s both; transition:opacity .2s; }
.chart-point { outline:none; }
.chart-point:hover .chart-bar,.chart-point:focus-visible .chart-bar { opacity:1; filter:brightness(1.2); }
.chart-point .chart-dot { opacity:0; transition:opacity .2s; }
.chart-point:hover .chart-dot,.chart-point:focus-visible .chart-dot { opacity:1; }
.chart-point:focus-visible .chart-hit { stroke:var(--mint); stroke-width:1; }
.chart-tip { opacity:0; pointer-events:none; transition:opacity .15s; }
.chart-point:hover .chart-tip,.chart-point:focus-visible .chart-tip { opacity:1; }
.chart-line { stroke-dasharray:1600; stroke-dashoffset:0; animation:draw 1.3s ease both; }
.table-scroll { width:100%; max-width:100%; overflow:auto; overscroll-behavior-x:contain; border-radius:5px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; color:#8ea0b2; font-weight:550; font-size:11px; padding:12px 14px;
  border-bottom:1px solid rgba(255,255,255,.07); background:rgba(255,255,255,.015); white-space:nowrap; letter-spacing:.02em; }
td { padding:14px; border-bottom:1px solid rgba(255,255,255,.04); vertical-align:middle; overflow-wrap:anywhere; min-width:88px; }
td:first-child { min-width:140px; }
tbody tr { transition:background .18s ease; }
tbody tr:hover { background:rgba(255,255,255,.025); }
tr:last-child td { border-bottom:none; }
.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; overflow-wrap:normal; }
.bar { height:6px; background:rgba(255,255,255,.06); border-radius:6px; overflow:hidden; min-width:80px; box-shadow:inset 0 1px 2px rgba(0,0,0,.4); }
.bar i { display:block; height:100%; border-radius:6px; transform-origin:left; animation:grow .9s .15s both;
  box-shadow:0 0 6px rgba(116,230,191,.4); }
.muted { color:var(--muted); }
.green { color:var(--mint); } .blue { color:var(--blue); } .red { color:#ef9999; } .gold { color:#dfbd88; }
.tag { display:inline-block; font-size:10px; font-weight:500; padding:2px 8px; border-radius:6px; border:1px solid var(--border);
  color:var(--muted); margin:3px 0 3px 6px; white-space:nowrap; letter-spacing:.02em; }
.tag.warn { color:#f0a897; border-color:rgba(240,168,151,.25); background:rgba(240,168,151,.08); box-shadow:0 0 8px -2px rgba(240,168,151,.2); }
.tag.ok { color:var(--mint); border-color:rgba(116,230,191,.25); background:rgba(116,230,191,.08); box-shadow:0 0 8px -2px rgba(116,230,191,.2); }
.empty { color:var(--muted); padding:34px 20px; margin:8px 0; text-align:center; font-size:12px;
  background:linear-gradient(120deg,#ffffff02,transparent); border:1px dashed #ffffff12; border-radius:8px; }
.empty::before { content:'—'; display:block; margin-bottom:8px; color:#74958a; font-size:24px; }
.foot { color:#8292a4; font-size:11px; line-height:1.95; margin-top:34px; border-top:1px solid var(--border); padding-top:22px; }
.foot b { color:#c5cedb; font-weight:550; }
.banner { background:rgba(223,189,136,.06); border:1px solid rgba(223,189,136,.22); color:#dfbd88;
  border-radius:10px; padding:12px 18px; font-size:12px; margin-bottom:24px; backdrop-filter:blur(8px); }
.banner-tip { background:rgba(116,230,191,.06); border-color:rgba(116,230,191,.25); color:var(--mint);
  box-shadow:0 2px 12px -2px rgba(116,230,191,.12); }
code { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.08); border-radius:5px;
  padding:2px 7px; font-size:11px; color:#bcd1ee; font-family:ui-monospace,Consolas,monospace; overflow-wrap:anywhere; }
@keyframes rise { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:none; } }
@keyframes grow { from { transform:scale(.02,.02); opacity:.1; } to { transform:scale(1,1); opacity:1; } }
@keyframes draw { from { stroke-dashoffset:1600; } to { stroke-dashoffset:0; } }
@keyframes breathe { 50% { box-shadow:0 0 0 4px #74e6bf12; opacity:.55; } }
@keyframes ambient { to { transform:translate(-100px,80px) scale(1.15); } }
@keyframes spin { to { transform:rotate(360deg); } }
@media (min-width:1600px) { .wrap { padding-top:52px; } .card { padding:25px 25px 18px; } }
@media (max-width:1200px) { .wrap { padding:32px 24px 48px; } .topbar-inner { padding:14px 24px; gap:14px; }
  .cards { grid-template-columns:repeat(2,minmax(0,1fr)); } .card .v { font-size:30px; }
  .panel { padding:20px; } .grid2 { gap:16px; } }
@media (max-width:900px) { html { scroll-padding-top:164px; } .sidebar { position:relative; inset:auto; width:auto; padding:14px 20px 0; }
  .brand { padding:0 0 13px; font-size:18px; } .brand small { display:none; } .brand svg { width:26px; height:26px; }
  .nav-label,.privacy { display:none; } .sidebar nav { display:flex; gap:4px; overflow-x:auto; padding:0 0 12px; }
  .nav-link { padding:8px 12px; white-space:nowrap; flex:none; font-size:12px; } .nav-icon { display:none; }
  .workspace { margin-left:0; } .topbar-inner { min-height:64px; padding:12px 24px; }
  .skip-link { left:16px; } .wrap { padding-top:32px; } .grid2 { grid-template-columns:1fr; }
  .panel { scroll-margin-top:90px; } .chart { max-height:270px; } }
@media (max-width:600px) { .sidebar { padding:16px 16px 0; } .topbar-inner { padding:10px 16px; gap:10px; }
  .breadcrumb { flex-basis:100%; font-size:11px; } .refresh-countdown { margin-left:auto; } .refresh-button { padding:8px 10px; }
  .wrap { padding:28px 16px 36px; } .hero { display:block; margin-bottom:22px; } h1 { font-size:27px; }
  .hero-meta { margin-top:18px; text-align:left; } .generation { display:inline-block; margin:0 0 0 12px; }
  .cards { grid-template-columns:1fr; gap:12px; } .card { padding:20px; } .card .v { font-size:32px; }
  .sparkline { height:32px; margin-top:12px; } .panel { padding:18px 14px; }
  .chart-summary { flex-wrap:wrap; } h2 { font-size:13px; } .section-label { margin-top:26px; }
  .metadata { font-size:11px; } .runtime-strip { grid-template-columns:repeat(2,minmax(0,1fr)); } .runtime-item { padding:15px; } }
@media (prefers-reduced-motion:reduce) { html { scroll-behavior:auto !important; }
  *,*::before,*::after { animation:none !important; transition:none !important; scroll-behavior:auto !important; } }
"""


JS = r"""
(() => {
  'use strict';
  const body = document.body;
  const countdown = document.getElementById('refresh-countdown');
  let nextRefreshAt = 0;
  const button = document.getElementById('refresh-button');
  const status = document.getElementById('refresh-status');
  const message = document.getElementById('refresh-message');
  const live = body.dataset.served === 'true' && /^https?:$/.test(location.protocol);
  const interval = Math.max(0, Number(body.dataset.refresh) || 0) * 1000;
  let timer = null;
  let controller = null;
  let busy = false;
  let failed = body.dataset.stale === 'true';

  function setStatus(state, text) {
    status.dataset.state = state;
    message.textContent = text;
  }
  function restingStatus() {
    if (failed) {
      setStatus('error', '更新失败 · 仍显示旧数据，可手动重试');
    } else if (!live) {
      setStatus('snapshot', '静态快照 · 不自动更新');
    } else if (document.hidden && interval) {
      setStatus('paused', '页面已隐藏 · 自动更新暂停');
    } else {
      setStatus('live', interval ? '本地服务 · 每 ' + interval / 1000 + ' 秒更新' : '本地服务 · 仅手动更新');
    }
  }
  function updateCountdown() {
    countdown.textContent = nextRefreshAt && !document.hidden && !busy
      ? '下次更新 ' + Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000)) + 's' : '';
  }
  function init(root) {
    root.querySelectorAll('.table-scroll').forEach((wrap, i) => {
      const title = wrap.closest('.panel').querySelector('h2');
      wrap.setAttribute('aria-label', (title ? title.textContent : '数据表 ' + (i + 1)) + '，可横向滚动');
    });
  }
  function schedule() {
    clearTimeout(timer);
    nextRefreshAt = 0;
    if (live && interval && !document.hidden && !busy) {
      nextRefreshAt = Date.now() + interval;
      timer = setTimeout(refresh, interval);
    }
    updateCountdown();
  }
  function focusState(root) {
    const active = document.activeElement;
    if (!root.contains(active)) return null;
    const section = active.closest('[id]');
    if (!section) return null;
    return {id: section.id, index: Array.from(section.querySelectorAll('a,button,input,summary,[tabindex]')).indexOf(active)};
  }
  async function refresh() {
    if (!live || busy) return;
    busy = true;
    clearTimeout(timer);
    nextRefreshAt = 0;
    updateCountdown();
    button.setAttribute('aria-disabled', 'true');
    button.classList.add('is-loading');
    button.setAttribute('aria-busy', 'true');
    setStatus('loading', '正在读取本地数据…');
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const url = new URL(location.href);
      url.hash = '';
      const response = await fetch(url, {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('读取报告失败');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const incoming = doc.getElementById('report-content');
      if (!incoming || !incoming.querySelector('#generated-at') || doc.body.dataset.served !== 'true' || doc.body.dataset.stale === 'true') {
        throw new Error('无效的报告');
      }
      const current = document.getElementById('report-content');
      const focus = focusState(current);
      const scroll = {x: window.scrollX, y: window.scrollY};
      const tables = Array.from(current.querySelectorAll('.table-scroll'), el => ({x: el.scrollLeft, y: el.scrollTop}));
      const details = Array.from(current.querySelectorAll('details[id]'), el => ({id: el.id, open: el.open}));
      const next = document.importNode(incoming, true);
      next.classList.add('is-updated');
      current.replaceWith(next);
      details.forEach(item => { const el = document.getElementById(item.id); if (el) el.open = item.open; });
      init(next);
      next.querySelectorAll('.table-scroll').forEach((el, i) => {
        if (tables[i]) { el.scrollLeft = tables[i].x; el.scrollTop = tables[i].y; }
      });
      if (focus) {
        const section = document.getElementById(focus.id);
        const target = section && (focus.index < 0 ? section : section.querySelectorAll('a,button,input,summary,[tabindex]')[focus.index]);
        if (target) target.focus({preventScroll: true});
      }
      window.scrollTo({left: scroll.x, top: scroll.y, behavior: 'instant'});
      failed = false;
    } catch (error) {
      if (!document.hidden || error.name !== 'AbortError') failed = true;
    } finally {
      clearTimeout(timeout);
      controller = null;
      busy = false;
      button.disabled = !live;
      button.classList.remove('is-loading');
      button.removeAttribute('aria-disabled');
      button.removeAttribute('aria-busy');
      restingStatus();
      schedule();
    }
  }
  function selectNav() {
    const hash = location.hash || '#overview';
    document.querySelectorAll('.nav-link').forEach(link => {
      if (link.getAttribute('href') === hash) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  document.addEventListener('click', event => {
    if (event.target.closest('#refresh-button')) refresh();
  });
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden && controller) controller.abort();
    if (!busy) restingStatus();
    schedule();
  });
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearInterval(countdownTimer); if (controller) controller.abort(); });
  window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); else { restingStatus(); schedule(); } });
  window.addEventListener('hashchange', selectNav);
  const countdownTimer = live && interval ? setInterval(updateCountdown, 1000) : null;
  button.disabled = !live;
  button.title = live ? '读取当前本地服务的最新数据' : '这是静态快照；使用 --serve 可手动和自动更新';
  init(document.getElementById('report-content'));
  selectNav();
  restingStatus();
  schedule();
})();
"""


def _smooth_path(coords):
    """水平控制点确保曲线不超出相邻真实数据的范围。"""
    path = ['M %.2f %.2f' % coords[0]]
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        mid = (x0 + x1) / 2
        path.append('C %.2f %.2f %.2f %.2f %.2f %.2f' % (mid, y0, mid, y1, x1, y1))
    return ' '.join(path)


def sparkline(points, label, blue=False):
    """迷你图仅展示传入的真实每日序列，不构造额外指标或增长率。"""
    values = [max(0, v) for _, v in points]
    if not values:
        return ''
    maximum = max(values) or 1
    coords = [(i * 240 / max(1, len(values) - 1), 30 - v / maximum * 26)
              for i, v in enumerate(values)]
    return ('<svg class="sparkline%s" viewBox="0 0 240 34" preserveAspectRatio="none" '
            'role="img" aria-label="%s"><title>%s</title>'
            '<path d="%s" fill="none" stroke="currentColor" stroke-width="1.6" '
            'vector-effect="non-scaling-stroke"/></svg>'
            % (' blue' if blue else '', esc(label), esc(label), _smooth_path(coords)))


def bar_svg(points, color, fmt, width=520, height=170, kind="bar"):
    """兼容原柱图接口；kind='area' 绘制平滑面积图，含可聚焦的数据点与提示。"""
    if not points:
        return '<div class="empty">暂无数据，等待第一条记录</div>'
    maximum = max((v for _, v in points), default=0)
    ceiling = (maximum or 1) * 1.12
    left, right, top, bottom = 62, 12, 18, 30
    iw, ih = width - left - right, height - top - bottom
    slot = iw / len(points)
    baseline = top + ih
    coords = [(left + slot * (i + .5), baseline - max(0, v) / ceiling * ih)
              for i, (_, v) in enumerate(points)]
    title = '每日节省趋势' if kind == 'area' else '每日积分消耗'
    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="group" aria-label="%s">'
             '<title>%s，每个数据点可通过 Tab 聚焦查看</title>' % (width, height, title, title)]
    for i in range(4):
        y = top + ih * i / 3
        parts.append('<path class="chart-grid" d="M %d %.2f H %d"/>'
                     '<text x="%d" y="%.2f" text-anchor="end">%s</text>'
                     % (left, y, width - right, left - 10, y + 3, esc(fmt(ceiling * (1 - i / 3)))))
    if kind == 'area':
        path = _smooth_path(coords)
        parts.append('<path d="%s L %.2f %.2f L %.2f %.2f Z" fill="%s" opacity=".09"/>'
                     '<path class="chart-line" d="%s" fill="none" stroke="%s" stroke-width="2"/>'
                     % (path, coords[-1][0], baseline, coords[0][0], baseline, esc(color), path, esc(color)))
    for i, ((label, value), (cx, y)) in enumerate(zip(points, coords)):
        tip = '%s：%s' % (label, fmt(value))
        parts.append('<g class="chart-point" tabindex="0" role="img" aria-label="%s"><title>%s</title>'
                     % (esc(tip), esc(tip)))
        if kind == 'area':
            parts.append('<circle class="chart-dot" cx="%.2f" cy="%.2f" r="4" fill="%s"/>' % (cx, y, esc(color)))
        elif value > 0:
            bw = min(slot * .52, 26)
            parts.append('<rect class="chart-bar" x="%.2f" y="%.2f" width="%.2f" height="%.2f" '
                         'rx="3" fill="%s" opacity=".72" style="animation-delay:%.2fs"/>'
                         % (cx - bw / 2, y, bw, baseline - y, esc(color), i * .025))
        else:
            parts.append('<path d="M %.2f %.2f h 4" stroke="%s" opacity=".25"/>' % (cx - 2, baseline, esc(color)))
        parts.append('<rect class="chart-hit" x="%.2f" y="%d" width="%.2f" height="%.2f" fill="transparent"/>'
                     % (cx - slot / 2, top - 4, slot, ih + 8))
        tip_w = min(iw, max(100, len(tip) * 7))
        tip_x = max(left, min(cx - tip_w / 2, width - right - tip_w))
        parts.append('<g class="chart-tip" aria-hidden="true"><rect x="%.2f" y="%.2f" width="%.2f" height="24" '
                     'rx="4" fill="#202832" stroke="#ffffff20"/><text x="%.2f" y="%.2f" '
                     'text-anchor="middle">%s</text></g></g>'
                     % (tip_x, top - 13, tip_w, tip_x + tip_w / 2, top + 3, esc(tip)))
        if len(points) <= 8 or i % 3 == 0 or i == len(points) - 1:
            parts.append('<text x="%.2f" y="%d" text-anchor="middle">%s</text>' % (cx, height - 8, esc(label)))
    parts.append('</svg>')
    return ''.join(parts)


def render_html(agg, meta):
    """渲染独立报告；served 区分本地服务（包括 refresh=0）与静态快照。"""
    p = []
    A = p.append
    refresh = max(0, int(meta.get("live_refresh") or 0))
    served = bool(meta.get("served", refresh > 0))
    stale = bool(meta.get("stale"))
    mode = '本地服务 · 每 %d 秒更新' % refresh if refresh else '本地服务 · 仅手动更新'
    if not served:
        mode = '静态快照 · 不自动更新'
    if stale:
        mode = '更新失败 · 仍显示旧数据，可手动重试'
    state = 'error' if stale else ('live' if served else 'snapshot')

    A('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width, initial-scale=1">')
    A('<meta name="report-mode" content="%s">' % ('served' if served else 'snapshot'))
    if served and refresh:
        A('<noscript><meta http-equiv="refresh" content="%d"></noscript>' % refresh)
    A('<title>wb-ops · 运行概览</title><style>%s</style></head>' % CSS)
    A('<body data-served="%s" data-refresh="%d" data-stale="%s">'
      % (str(served).lower(), refresh, str(stale).lower()))
    A('<a class="skip-link" href="#report-content">跳到主要内容</a>')
    A('''<aside class="sidebar" aria-label="报告导航">
      <a class="brand" href="#overview" aria-label="wb-ops 运行概览">
        <svg width="32" height="32" viewBox="0 0 32 32" role="img" aria-label="wb-ops 标志">
          <title>wb-ops</title><rect x="1" y="1" width="30" height="30" rx="9" fill="currentColor" opacity=".08"/>
          <path d="m7 11 4 11 5-8 5 8 4-11M13 9h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg><span>wb-ops<small>LOCAL OPERATIONS</small></span></a>
      <div class="nav-label">WORKSPACE</div><nav>
        <a class="nav-link" href="#overview" aria-current="location"><span class="nav-icon" aria-hidden="true">01</span>OVERVIEW 概览</a>
        <a class="nav-link" href="#context-health"><span class="nav-icon" aria-hidden="true">02</span>上下文健康</a>
        <a class="nav-link" href="#tools-projects"><span class="nav-icon" aria-hidden="true">03</span>工具与项目</a>
        <a class="nav-link" href="#model-analysis"><span class="nav-icon" aria-hidden="true">04</span>模型分析</a>
        <a class="nav-link" href="#recent-activity"><span class="nav-icon" aria-hidden="true">05</span>最近活动</a>
      </nav><div class="privacy"><strong>
        <svg width="12" height="13" viewBox="0 0 12 13" fill="none" aria-hidden="true"><path d="M6 1 1 3v4c0 2 3 4 5 5 2-1 5-3 5-5V3Z" stroke="currentColor"/><path d="m3.5 6 1.7 1.7 3.2-3.2" stroke="currentColor"/></svg>
        LOCAL ONLY</strong>数据留在本地，无外部资源请求。</div></aside>
      <div class="workspace"><header class="topbar"><div class="topbar-inner">
        <div class="breadcrumb">Workspace<span>/</span><b>运行概览</b></div>''')
    A('<span id="refresh-status" class="status" data-state="%s" role="status" aria-live="polite">'
      '<i class="status-dot" aria-hidden="true"></i><span id="refresh-message">%s</span></span>' % (state, esc(mode)))
    A('''<span class="refresh-countdown" id="refresh-countdown" aria-hidden="true"></span>
      <button class="refresh-button" id="refresh-button" type="button" disabled title="静态快照或 JavaScript 未启用时不可刷新">
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M16 7a6.5 6.5 0 1 0 .3 5M16 2v5h-5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>手动刷新</button>
      </div></header><main class="wrap" id="report-content" tabindex="-1">
      <section class="hero" id="overview" aria-labelledby="overview-title"><div>
        <p class="eyebrow">WB-OPS / LOCAL OBSERVABILITY</p><h1 id="overview-title">运行总览<span class="hero-accent">.</span></h1>
        <p class="sub">节省有迹可循，消耗一目了然。你的本地运行监控台。</p></div><div class="hero-meta">''')
    A('<span class="muted">%s · 数据生成时间</span><time class="generation" id="generated-at">%s</time></div></section>'
      % ('动态后台 · 5 秒短缓存' if served else '静态快照，生成后不再更新', esc(meta['generated_at'])))
    A('<details class="metadata" id="report-metadata"><summary>数据来源与生成信息</summary>'
      '<p>记账目录：<code>%s</code><br>标记：%s · 文件 %s 个 · 跳过损坏行 %s 条</p></details>'
      % (esc(meta['data_dir']), esc(meta.get('title_note') or '本地报告'), esc(meta.get('files', 0)), esc(meta.get('bad', 0))))
    A('<noscript><div class="banner">JavaScript 未启用：所有数据仍完整可读；手动刷新不可用。%s</div></noscript>'
      % ('本地服务将每 %d 秒整页刷新。' % refresh if served and refresh else '此页面不会自动刷新。'))
    if not served and not meta.get("demo"):
        A("<div class='banner banner-tip'>当前为静态导出，生成后不会自动更新。"
          "日常查看请打开固定动态后台（5 秒短缓存、每 30 秒局部更新）："
          "运行 <code>python report.py --serve</code> 或斜杠命令 <code>/wb-ops:report</code>，"
          "再打开 <code>http://127.0.0.1:8787/</code>。</div>")
    if meta.get("demo"):
        A("<div class='banner'>示意报告：以下为全合成演示数据，仅预览版式，不代表真实使用情况。</div>")
    else:
        # v0.6.1（S7）：宿主数据降级时给出明确提示（区分「无数据」与「读库/解析失败」）
        if meta.get("host_ok") is False:
            A("<div class='banner'>宿主数据库读取失败（%s）：积分与上下文区已降级为空显示。"
              "可检查 workbuddy.db 是否存在/是否被占用后重新生成。</div>"
              % esc(meta.get("host_err") or "未知原因"))
        if meta.get("credit_bad"):
            A("<div class='banner'>%d 条会话积分记录解析失败，相关积分按 0 显示（数据仍保留在宿主库中）。</div>"
              % int(meta["credit_bad"]))

    # ---- 概览卡：默认输出完整数值，无脚本也可读 ----
    saved = agg["total_b"] - agg["total_a"]
    rate = (saved / agg["total_b"] * 100.0) if agg["total_b"] else 0.0
    pts = [(short_day(d), agg["per_day"][d]) for d in agg["days"]]
    counts = [(short_day(d), agg["per_day_cnt"][d]) for d in agg["days"]]
    cards = [
        ('累计节省', hb(saved), '≈ %s tokens · 字节差粗估' % est_tokens(saved), pts, '每日节省字节趋势', False),
        ('今日节省', hb(agg['today_saved']), '今日裁剪输出的字节节省量', pts, '每日节省字节趋势', False),
        ('本周节省', hb(agg['week_saved']), '自本周一开始累计的字节节省量', pts, '每日节省字节趋势', False),
        ('平均节省率', '%.1f%%' % rate, '%d 次裁剪 · 总节省 / 原始字节' % agg['count'], counts, '每日裁剪次数趋势', True),
    ]
    A('<div class="cards" aria-label="节省指标">')
    for i, (label, value, note, series, spark_label, blue) in enumerate(cards, 1):
        A('<article class="card"><div class="card-head"><span class="k">%s</span>'
          '<span class="kpi-icon" aria-hidden="true">%02d</span></div><div class="v %s">%s</div>'
          '<div class="card-note">%s</div>%s<div class="spark-caption"><span>近 %d 天%s</span><span>%s — %s</span></div></article>'
          % (esc(label), i, 'blue' if blue else 'green', esc(value), esc(note),
             sparkline(series, spark_label, blue), DAYS_SHOWN,
             '每日裁剪次数' if blue else '每日节省', esc(short_day(agg['days'][0])) if agg['days'] else '—',
             esc(short_day(agg['days'][-1])) if agg['days'] else '—'))
    A('</div>')

    # ---- 运行侧：与节省统计分开，避免混淆积分与 token ----
    model_requests = sum(v.get('req', 0) for v in agg['models'].values())
    model_input = sum(v.get('pin', 0) for v in agg['models'].values())
    model_hits = sum(v.get('hit', 0) for v in agg['models'].values())
    cache_rate = '%.1f%%' % (model_hits / model_input * 100) if model_input else '—'
    runtime = [
        ('累计积分', '%.1f' % agg['credit_total'], '宿主记账 · 非金额'),
        ('模型请求', format(model_requests, ','), '%d 个模型 · 输入 %s tokens' % (len(agg['models']), fmt_tok(model_input))),
        ('缓存命中率', cache_rate, '按输入 tokens 加权 · 非请求占比'),
        ('上下文覆盖', str(len(agg['usage_rows'])), '会话 · 全量 transcript 直读'),
    ]
    A('<div class="runtime-strip" aria-label="运行指标">')
    for label, value, note in runtime:
        A('<div class="runtime-item"><span>%s</span><strong>%s</strong><small>%s</small></div>'
          % (esc(label), esc(value), esc(note)))
    A('</div>')

    # ---- 趋势（两栏）----
    A('<div class="section-label">01 / ACTIVITY TRENDS</div><div class="grid2">')
    A("<section class='panel' id='savings-trend'><h2>近 %d 天每日节省</h2>" % DAYS_SHOWN)
    A('<div class="chart-summary"><span><strong>%s</strong>区间合计</span><span>单日最高 %s</span></div>'
      % (hb(sum(v for _, v in pts)), hb(max((v for _, v in pts), default=0))))
    A(bar_svg(pts, "#74e6bf", hb, height=220, kind="area"))
    A("</section>")
    A("<section class='panel' id='credits-trend'><h2>近 %d 天积分消耗</h2>" % DAYS_SHOWN)
    pts2 = [(short_day(d), agg["per_day_credit"][d]) for d in agg["days"]]
    A('<div class="chart-summary"><span><strong>%.1f</strong>区间积分</span><span>按请求归属 · 宿主记账</span></div>'
      % sum(v for _, v in pts2))
    A(bar_svg(pts2, "#8faedd", lambda v: "%.1f" % v, height=220))
    if agg["credit_unknown"] > 0.05:
        A("<div class='muted' style='font-size:11.5px;margin-top:6px'>另有 %0.1f 积分未能归属到日期（会话记录缺失）</div>"
          % agg["credit_unknown"])
    A("</section></div>")

    # ---- 上下文健康（v0.3.5：全量直读，含宿主窗口之外的会话）----
    A('<div class="section-label">02 / CONTEXT HEALTH</div>')
    A("<div class='panel' id='context-health'><h2>上下文占用（全量会话，≥%.0f%% 建议换新会话）</h2>"
      % (CTX_WARN_RATIO * 100))
    warn_count = sum(1 for row in agg['usage_rows']
                     if row.get('ratio') is not None and row['ratio'] >= CTX_WARN_RATIO)
    A('<div class="health-summary"><span class="tag %s">%s</span>'
      '<span class="muted">优先展示占用最高的 10 个会话 · 占用为末次请求，非累计输入</span></div>'
      % ('warn' if warn_count else 'ok', '%d 个会话建议收尾' % warn_count if warn_count else '暂无高占用告警'))
    rows = agg["usage_rows"][:10]
    if not rows:
        A("<div class='empty'>暂无会话占用数据</div>")
    else:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>会话</th><th>项目</th><th class='num'>占用</th><th style='width:26%'>　</th><th class='num'>积分</th></tr></thead><tbody>")
        for r in rows:
            proj = os.path.basename(r["cwd"].replace("\\", "/").rstrip("/")) or "-"
            tip = format(int(r["used"]), ",") + \
                ((" / " + format(int(r["size"]), ",")) if r["size"] else "")
            if r["ratio"] is None:
                pct_cell = "<span class='muted'>%s</span>" % fmt_tok(r["used"])
                bar_cell, tag = "", ""
            else:
                pct = r["ratio"] * 100
                col = "#ef9999" if pct >= CTX_WARN_RATIO * 100 else ("#dfbd88" if pct >= 50 else "#74e6bf")
                tag = "<span class='tag warn'>建议换会话</span>" if pct >= CTX_WARN_RATIO * 100 else ""
                pct_cell = "%.0f%%" % pct
                bar_cell = "<div class='bar'><i style='width:%.0f%%;background:%s'></i></div>" \
                    % (min(pct, 100), col)
            ctxt = "%.1f" % r["credits"] if r["credits"] is not None else "—"
            A("<tr><td>%s%s</td><td class='muted'>%s</td><td class='num' title='%s'>%s</td>"
              "<td>%s</td><td class='num muted'>%s</td></tr>"
              % (esc(r["title"][:38]), tag, esc(proj), esc(tip), pct_cell, bar_cell, ctxt))
        A("</tbody></table></div>")
        A("<div class='muted' style='font-size:11.5px;margin-top:6px'>"
          "覆盖 %d 个会话（其中宿主窗口之外的 %d 个由 transcript 直读补齐）；"
          "占用 = 各会话末次请求的上下文 prompt_tokens（与宿主 used 一致）。</div>"
          % (len(agg["usage_rows"]), agg.get("ctx_extra", 0)))
    A("</div>")

    # ---- 维度 + 积分 Top ----
    A('<div class="section-label" id="tools-projects">03 / TOOLS &amp; PROJECTS</div>')
    A("<div class='grid2'>")
    A("<div class='panel' id='by-tool'><h2>按工具</h2>")
    if agg["per_tool"]:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>工具</th><th class='num'>次数</th><th class='num'>节省</th><th class='num'>节省率</th></tr></thead><tbody>")
        for tool, v in sorted(agg["per_tool"].items(), key=lambda kv: -kv[1]["saved"]):
            rt = (v["saved"] / v["b"] * 100.0) if v["b"] else 0
            A("<tr><td>%s</td><td class='num'>%d</td><td class='num green'>%s</td><td class='num'>%.0f%%</td></tr>"
              % (esc(tool), v["cnt"], hb(v["saved"]), rt))
        A("</tbody></table></div>")
    else:
        A("<div class='empty'>暂无数据</div>")
    A("</div>")
    A("<div class='panel' id='by-project'><h2>按项目</h2>")
    if agg["per_project"]:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>项目</th><th class='num'>次数</th><th class='num'>节省</th></tr></thead><tbody>")
        for proj, v in sorted(agg["per_project"].items(), key=lambda kv: -kv[1]["saved"])[:8]:
            A("<tr><td>%s</td><td class='num'>%d</td><td class='num green'>%s</td></tr>"
              % (esc(proj), v["cnt"], hb(v["saved"])))
        A("</tbody></table></div>")
    else:
        A("<div class='empty'>暂无数据</div>")
    A("</div></div>")

    # ---- 会话积分 Top ----
    A("<div class='panel' id='session-credits'><h2>会话积分消耗 Top</h2>")
    top = [r for r in agg["usage_by_credit"] if (r["credits"] or 0) > 0][:8]
    if top:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>会话</th><th class='muted'>模型</th><th class='num'>积分</th></tr></thead><tbody>")
        for r in top:
            A("<tr><td>%s</td><td class='muted'>%s</td><td class='num blue'>%.1f</td></tr>"
              % (esc(r["title"][:44]), esc(r["model"] or "-"), r["credits"]))
        A("</tbody></table></div>")
    else:
        A("<div class='empty'>暂无积分数据</div>")
    A("</div>")

    # ---- 按模型 Token 消耗（transcript 全量直读 + 缓存命中）----
    A('<div class="section-label">04 / MODEL ANALYSIS</div>')
    A("<div class='panel' id='model-analysis'><h2>按模型 Token 消耗（全量直读）</h2>")
    if agg["models"]:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>模型</th><th class='num'>会话</th><th class='num'>请求</th>"
          "<th class='num'>输入 tokens</th><th class='num'>输出 tokens</th>"
          "<th class='num'>缓存命中</th><th class='num'>命中率</th>"
          "<th class='num'>最近使用</th></tr></thead><tbody>")
        for name, v in sorted(agg["models"].items(), key=lambda kv: -kv[1]["pin"])[:12]:
            lt = (datetime.fromtimestamp(v["last"] / 1000.0).strftime("%m-%d %H:%M")
                  if v.get("last") else "-")
            pin = int(v.get("pin", 0))
            hit = int(v.get("hit", 0))
            rate = ("%.0f%%" % (hit / pin * 100.0)) if pin else "—"
            hit_cell = ("<span title='%s'>%s</span>" % (format(hit, ","), fmt_tok(hit))
                        if hit else "—")
            A("<tr><td>%s</td><td class='num'>%d</td><td class='num'>%d</td>"
              "<td class='num' title='%s'>%s</td><td class='num' title='%s'>%s</td>"
              "<td class='num'>%s</td><td class='num'>%s</td>"
              "<td class='num muted'>%s</td></tr>"
              % (esc(name), v.get("n_sessions", 0), v.get("req", 0),
                 format(pin, ","), fmt_tok(pin),
                 format(int(v.get("pout", 0)), ","), fmt_tok(v.get("pout", 0)),
                 hit_cell, rate, lt))
        A("</tbody></table></div>")
        A("<div class='muted' style='font-size:11.5px;margin-top:6px'>"
          "输入为每次请求的上下文累计（逐轮含完整历史，即计费口径）；"
          "缓存命中三格式归一提取（DeepSeek cache_hit / Anthropic cache_read / OpenAI cached_tokens）。"
          "直读全部会话 transcript，不受宿主会话窗口限制，含中转站/自定义模型。</div>")
    else:
        A("<div class='empty'>暂无会话记录</div>")
    A("</div>")

    # ---- 最近明细 ----
    A('<div class="section-label">05 / RECENT ACTIVITY</div>')
    A("<div class='panel' id='recent-activity'><h2>最近裁剪明细</h2>")
    if agg["recent"]:
        A("<div class='table-scroll' role='region' tabindex='0' aria-label='可横向滚动的数据表'><table><thead><tr><th>时间</th><th>会话</th><th>工具</th>"
          "<th class='num'>原始</th><th class='num'>裁剪后</th><th class='num'>节省</th></tr></thead><tbody>")
        for r in agg["recent"]:
            sid_label = r["s"][:8] if r["s"] else "-"
            A("<tr><td class='muted'>%s</td><td class='muted'>%s</td><td>%s</td>"
              "<td class='num'>%s</td><td class='num'>%s</td><td class='num green'>%s</td></tr>"
              % (esc(day_str(r["t"]) + " " + datetime.fromtimestamp(r["t"] / 1000.0).strftime("%H:%M")),
                 esc(sid_label), esc(r["tool"]), hb(r["b"]), hb(r["a"]), hb(max(0, r["b"] - r["a"]))))
        A("</tbody></table></div>")
    else:
        A("<div class='empty'>尚未产生记账数据。<br>"
          "每次 Grep/Glob 输出被裁剪时会自动记账；重新生成或从本地服务刷新即可查看。<br>"
          "记账目录可展开页头的“数据来源与生成信息”查看。</div>")
    A("</div>")

    # ---- 口径 ----
    A("<div class='foot'>")
    A("<b>口径说明</b>　· 节省字节为精确值（裁剪前/后字节差），token 数为粗估（≈%.1f 字节/token），仅供感知量级。"
      % BYTES_PER_TOKEN)
    A("　· 积分为宿主记账口径（conversationRequestId 维度），与 token 无固定换算。")
    A("<br><b>覆盖范围</b>　· 仅统计经 wb-ops 裁剪的工具输出（当前 Grep/Glob/WebFetch；Bash 输出宿主未下发给 hook，WebSearch 输出紧凑不裁）。")
    A("　· 裁剪预算默认 8 KB，可用 WB_OPS_BUDGET 环境变量覆盖（2000~131072 字节）。")
    A("　· 记账数据保留最近 %d 个月（滚动清理），含 %s 个文件%s。"
      % (KEEP_MONTHS, meta.get("files", 0),
         "，跳过 %d 个损坏行" % meta["bad"] if meta.get("bad") else ""))
    A("<br><b>按模型统计</b>　· 直读全部会话 transcript（含宿主会话窗口之外的），input/output 为请求计费口径累计值；不区分官方/中转站来源。")
    A("<br><b>上下文占用</b>　· 直读全部会话 transcript，占用为各会话末次请求的 prompt_tokens；")
    A("分母（上下文窗口）取自宿主同模型已知记录，未知时仅显示绝对数值。")
    A("<br><b>隐私</b>　· 记账只存尺寸、时间与会话前缀，不含任何输出内容；报告在本地生成。")
    A("</div>")

    A('</main></div><script>%s</script></body></html>' % JS)
    return "\n".join(p)


# ---------------------------------------------------------------------------
# demo 数据（--demo：全合成，不读真实数据）
# ---------------------------------------------------------------------------

def build_demo():
    now = datetime.now()
    today = date.today()
    recs = []
    import random
    rnd = random.Random(42)
    sess = ["3fa2c1d8", "7b04e9aa", "c91d2f31", "05e83ba7"]
    tools = ["Grep", "Grep", "Grep", "Glob"]
    for d in range(DAYS_SHOWN):
        day = today - timedelta(days=DAYS_SHOWN - 1 - d)
        if day.weekday() >= 5 and rnd.random() < 0.4:
            continue
        for _ in range(rnd.randint(2, 14)):
            b = rnd.randint(4000, 90000)
            a = int(b * rnd.uniform(0.04, 0.35))
            t = int(datetime(day.year, day.month, day.day,
                             rnd.randint(9, 22), rnd.randint(0, 59)).timestamp() * 1000)
            recs.append({"t": t, "s": rnd.choice(sess), "tool": rnd.choice(tools), "b": b, "a": a})

    host = {"ok": True, "err": "", "sessions": [
        {"id": "3fa2c1d8-1111-2222-3333-444455556666", "title": "wb-ops 记账与报告开发",
         "cwd": "d:/wb-ops", "model": "claude", "updated_at": 0},
        {"id": "7b04e9aa-1111-2222-3333-444455556666", "title": "直播智能体 PPT 制作",
         "cwd": "d:/直播智能体", "model": "claude", "updated_at": 0},
        {"id": "c91d2f31-1111-2222-3333-444455556666", "title": "C++ 线段树题目讨论",
         "cwd": "d:/oi", "model": "deepseek", "updated_at": 0},
        {"id": "05e83ba7-1111-2222-3333-444455556666", "title": "周报排版与公式校对",
         "cwd": "d:/docs", "model": "claude", "updated_at": 0},
    ], "usage": [
        {"sid": "3fa2c1d8-1111-2222-3333-444455556666", "used": 62000, "size": 300000,
         "updated_at": 0, "credits": {}},
        {"sid": "7b04e9aa-1111-2222-3333-444455556666", "used": 242000, "size": 300000,
         "updated_at": 0, "credits": {}},
        {"sid": "c91d2f31-1111-2222-3333-444455556666", "used": 88000, "size": 300000,
         "updated_at": 0, "credits": {}},
        {"sid": "05e83ba7-1111-2222-3333-444455556666", "used": 151000, "size": 300000,
         "updated_at": 0, "credits": {}},
    ]}
    # demo 积分：给每个会话造几条按天的 credit，并配对应的 rid_ts
    rid_ts = {}
    rn = 0
    for i, u in enumerate(host["usage"]):
        for d in range(3):
            rn += 1
            rid = ("%032x" % rn)
            u["credits"][rid] = round(rnd.uniform(1.5, 14.0), 2)
            t = int(datetime(today.year, today.month, today.day).timestamp() * 1000) - d * 86400000
            rid_ts[rid] = t
    # 按模型统计（合成，版式演示用；含缓存命中）
    t0 = int(datetime.now().timestamp() * 1000)
    models = {
        "deepseek-v4.1-flash": {"n_sessions": 14, "req": 820, "pin": 118400000,
                                "pout": 1460000, "hit": 112480000,
                                "first": t0 - 6 * 86400000, "last": t0},
        "gpt-6.1-sol": {"n_sessions": 9, "req": 388, "pin": 41300000,
                        "pout": 360000, "hit": 39028500,
                        "first": t0 - 7 * 86400000, "last": t0 - 3 * 3600000},
        "glm-5.3-flash": {"n_sessions": 3, "req": 21, "pin": 950000,
                          "pout": 44000, "hit": 912000,
                          "first": t0 - 9 * 86400000, "last": t0 - 5 * 86400000},
    }
    # v0.3.5 上下文全量化演示：追加几个"宿主窗口之外"的会话（仅 transcript 直读）
    host["sessions"] += [
        {"id": "aa01bb02-1111-2222-3333-444455556666", "title": "老会话：构建脚本重构",
         "cwd": "d:/old-proj", "model": "claude", "updated_at": 0},
        {"id": "bb22cc33-1111-2222-3333-444455556666", "title": "老会话：日志排查",
         "cwd": "d:/ops", "model": "deepseek", "updated_at": 0},
        {"id": "cc33dd44-1111-2222-3333-444455556666", "title": "老会话：中转站模型试验",
         "cwd": "d:/lab", "model": "relay-x1", "updated_at": 0},
    ]
    ctx = {
        "aa01bb02-1111-2222-3333-444455556666": {"used": 268000, "ts": t0 - 86400000,
                                                 "model": "claude"},
        "bb22cc33-1111-2222-3333-444455556666": {"used": 96000, "ts": t0 - 2 * 86400000,
                                                 "model": "deepseek"},
        "cc33dd44-1111-2222-3333-444455556666": {"used": 205000, "ts": t0 - 3 * 86400000,
                                                 "model": "relay-x1"},   # 未知窗口 -> 只显示数值
    }
    return recs, host, rid_ts, models, ctx


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build(args, live_refresh=0, served=False):
    """跑完整数据管线并渲染 HTML（一次生成）。

    返回 (html_txt, agg, info)；info 含 data_dir/out_path/note/records/bad/files/removed/host。
    served=True 或 live_refresh > 0 时输出为服务版；间隔为 0 时仍支持手动局部刷新。
    """
    paths = default_paths()
    data_dir = args.data_dir or paths["data_dir"]
    db_path = args.db or paths["db"]
    projects = args.projects or paths["projects"]

    removed = []
    if args.demo:
        records, host, rid_ts, models, ctx = build_demo()
        files, bad = 0, 0
        note = "（示意数据）"
    else:
        if not args.no_prune:
            removed = prune_old(data_dir)
        records, bad, files = load_saves(data_dir)
        host = load_host(db_path)
        models, rid_ts, ctx = scan_transcripts(projects)
        note = ""

    agg = aggregate(records, host, rid_ts, models, ctx)

    if args.out:
        out_path = args.out
    else:
        os.makedirs(data_dir, exist_ok=True)
        out_path = os.path.join(data_dir, "dashboard-demo.html" if args.demo else "dashboard.html")

    html_txt = render_html(agg, {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S" if served or live_refresh else "%Y-%m-%d %H:%M"),
        "data_dir": data_dir,
        "title_note": note,
        "demo": args.demo,
        "files": files,
        "bad": bad,
        "live_refresh": live_refresh,
        "served": served or live_refresh > 0,
        # v0.6.1（S7）：宿主降级信息进 meta —— 页面可区分「无数据」与「读库失败」
        "host_ok": host.get("ok"),
        "host_err": host.get("err") or "",
        "credit_bad": host.get("credit_bad", 0),
    })
    info = {
        "data_dir": data_dir, "out_path": out_path, "note": note,
        "records": records, "bad": bad, "files": files,
        "removed": removed, "host": host,
    }
    return html_txt, agg, info


def print_summary(html_txt, agg, info, args):
    """一次生成模式的终端摘要。"""
    print("报告已生成：%s（%d 字节）" % (info["out_path"], len(html_txt.encode("utf-8"))))
    print("  记账记录 %d 条 / 损坏行 %d / 文件 %d 个；积分合计 %.1f"
          % (len(info["records"]), info["bad"], info["files"], agg["credit_total"]))
    if agg["models"]:
        _mreq = sum(m.get("req", 0) for m in agg["models"].values())
        _mpin = sum(m.get("pin", 0) for m in agg["models"].values())
        _mhit = sum(m.get("hit", 0) for m in agg["models"].values())
        print("  按模型统计：%d 个模型 / %d 次请求 / 输入累计 %s tokens%s"
              % (len(agg["models"]), _mreq, fmt_tok(_mpin),
                 ("，缓存命中率 %.1f%%" % (_mhit / _mpin * 100.0)) if _mpin else ""))
    if agg["usage_rows"]:
        print("  上下文覆盖：%d 个会话（窗口之外直读补齐 %d 个）"
              % (len(agg["usage_rows"]), agg["ctx_extra"]))
    if info["removed"]:
        print("  已清理过期记账文件：%s" % ", ".join(info["removed"]))
    if not args.demo and not info["records"]:
        print("  提示：节省区暂无数据 —— 新会话的 Grep/Glob 裁剪会自动记账。")
    if not info["host"]["ok"] and not args.demo:
        print("  警告：workbuddy.db 读取失败（%s），积分/上下文区已降级为空。" % info["host"]["err"])


def serve(args):
    """仅监听本机固定端口；身份完全一致才复用，不换端口、不终止其他进程。"""
    import errno
    import http.server
    import socket
    import threading

    config = _service_config(args)
    args = argparse.Namespace(**vars(args))
    for key, value in config.items():
        setattr(args, key, value)
    identity = _service_identity(config)
    cache = {"html": None, "info": None, "t": 0.0, "stale": False}
    lock = threading.Lock()
    live = args.refresh

    def get_html():
        with lock:
            if cache["html"] is None or time.monotonic() - cache["t"] >= 5.0:
                try:
                    html_txt, _agg, info = build(args, live_refresh=live, served=True)
                    cache["html"] = html_txt
                    cache["info"] = info
                    cache["stale"] = False
                except Exception as e:  # 有旧结果就沿用，保证服务不中断
                    if cache["html"] is None:
                        raise
                    cache["stale"] = True
                    print("  构建失败（沿用上一份结果）：%s" % e, flush=True)
                cache["t"] = time.monotonic()
            result = cache["html"]
            if cache["stale"]:
                # 在返回副本标记失败，不改变缓存的完整报告或生成时间。
                result = result.replace('data-stale="false"', 'data-stale="true"', 1)
                result = result.replace('id="refresh-status" class="status" data-state="live"',
                                        'id="refresh-status" class="status" data-state="error"', 1)
                result = re.sub(r'(<span id="refresh-message">)[^<]*(</span>)',
                                r'\1更新失败 · 仍显示旧数据，可手动重试\2', result, count=1)
            return result

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "wb-ops-report"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/__wb_ops__/health":
                # 不获取构建锁、不扫描磁盘，也不返回数据源路径。
                body = json.dumps(dict(identity, ready=cache["html"] is not None),
                                  separators=(",", ":")).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif path in ("/", "/index.html", "/dashboard.html"):
                try:
                    body = get_html().encode("utf-8")
                except Exception as e:
                    # 异常详情不进入状态行，避免中文或换行污染响应头。
                    sys.stderr.write("构建失败：%r\n" % (e,))
                    self.send_error(500, "report build failed")
                    return
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404, "Not Found")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass

    class ExclusiveHTTPServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = False
        allow_reuse_port = False

        def server_bind(self):
            # Windows 的 SO_REUSEADDR 会允许第二个监听者抢占同端口。
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        srv = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        collision = e.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(
            e, "winerror", None) in (10013, 10048)
        if args.port and collision:
            # 同时启动时，先绑定者可能尚未来得及进入请求循环。
            deadline = time.monotonic() + 2.0
            while True:
                health, reason, retry = _probe_service(args.port, identity)
                if health is not None:
                    print("复用已有实时报告服务：http://127.0.0.1:%d/" % args.port, flush=True)
                    return 0
                if not retry or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            print("端口 %d 已被占用，拒绝复用：%s。请停止旧服务后，以当前版本和配置重新启动；"
                  "不会自动换端口或终止进程。" % (args.port, reason), file=sys.stderr)
        else:
            print("端口 %d 无法监听（%s）；不会自动换端口或终止进程。" % (args.port, e),
                  file=sys.stderr)
        return 1

    port = srv.server_address[1]
    print("实时报告服务已监听：http://127.0.0.1:%d/" % port, flush=True)
    print("  首次数据后台加载中；健康检查已可用；仅本机可访问；Ctrl+C 停止。", flush=True)
    print("  %s。" % (("页面每 %d 秒自动刷新" % live) if live > 0 else "自动刷新已关闭，可手动刷新"),
          flush=True)

    def warm_up():
        t0 = time.monotonic()
        try:
            get_html()
            n_rec = len((cache.get("info") or {}).get("records") or [])
            print("  首次数据已就绪（%.1fs%s）。"
                  % (time.monotonic() - t0, "，记账 %d 条" % n_rec if n_rec else ""), flush=True)
        except Exception as e:
            print("  首次加载失败（%s）—— 服务仍可访问，页面每次请求会重试。" % e, flush=True)

    try:
        threading.Thread(target=warm_up, daemon=True, name="wb-ops-report-warmup").start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        srv.server_close()
    return 0


SERVICE_ID = "wb-ops-report"
SERVICE_PROTOCOL = "wb-ops-report/1"
SERVICE_VERSION = "0.6.3"


def _service_config(args):
    paths = default_paths()
    config = {}
    for key in ("data_dir", "db", "projects"):
        value = getattr(args, key, None) or paths[key]
        config[key] = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(value))))
    config.update(demo=bool(args.demo), no_prune=bool(args.no_prune), refresh=max(0, args.refresh))
    return config


def _service_identity(config):
    import hashlib

    encoded = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {"service": SERVICE_ID, "protocol": SERVICE_PROTOCOL, "version": SERVICE_VERSION,
            "config_fingerprint": hashlib.sha256(encoded).hexdigest()}


def _probe_service(port, expected):
    """有限读取本机健康 JSON；禁用系统代理及重定向，绝不按 Server 响应头认身份。"""
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    url = "http://127.0.0.1:%d/__wb_ops__/health" % port
    try:
        with opener.open(url, timeout=0.4) as response:
            if response.status != 200:
                return None, "健康端点未返回 200", False
            body = response.read(4097)
        if len(body) > 4096:
            return None, "健康响应超过大小限制", False
        health = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.close()
        return None, "占用者没有兼容的健康端点", False
    except (urllib.error.URLError, OSError):
        return None, "占用者未及时响应健康检查", True
    except (http.client.HTTPException, ValueError, UnicodeError, RecursionError):
        return None, "占用者未返回有效的健康 JSON", False
    if not isinstance(health, dict) or any(health.get(key) != expected[key]
                                         for key in ("service", "protocol")):
        return None, "占用者不是兼容协议的 wb-ops 服务", False
    if health.get("version") != expected["version"]:
        return None, "版本不匹配（需要 wb-ops %s）" % expected["version"], False
    if health.get("config_fingerprint") != expected["config_fingerprint"]:
        return None, "配置不匹配（数据源或刷新/裁剪配置不同）", False
    return health, "", False


def main():
    ap = argparse.ArgumentParser(description="wb-ops 节省与 Token 报告生成器")
    ap.add_argument("--out", default=None, help="静态导出 HTML 路径")
    ap.add_argument("--data-dir", default=None, help="记账数据目录（默认 ~/.workbuddy/wb-ops-data）")
    ap.add_argument("--db", default=None, help="workbuddy.db 路径")
    ap.add_argument("--projects", default=None, help="transcript 目录（默认 ~/.workbuddy/projects）")
    ap.add_argument("--no-prune", action="store_true", help="不清理过期的记账文件")
    ap.add_argument("--open", action="store_true", help="静态导出后用默认浏览器打开")
    ap.add_argument("--demo", action="store_true", help="用合成数据生成示意报告")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="启动或复用配置一致的本机动态报告服务")
    mode.add_argument("--snapshot", action="store_true", help="显式静态导出（不启动服务；无参时也保留此行为）")
    mode.add_argument("--status", action="store_true", help="探测指定端口的服务身份（不扫描数据）")
    ap.add_argument("--port", type=int, default=8787, help="实时服务端口（默认固定 8787；0 = 自动分配）")
    ap.add_argument("--refresh", type=int, default=30, help="实时页面自动刷新间隔秒（0 = 关闭）")
    args = ap.parse_args()
    if not 0 <= args.port <= 65535:
        ap.error("--port 必须在 0 到 65535 之间")
    if args.refresh < 0:
        ap.error("--refresh 不能为负数")
    if args.status and args.port == 0:
        ap.error("--status 需要确定的非零端口")

    if args.serve:
        return serve(args)
    if args.status:
        health, reason, _retry = _probe_service(args.port, _service_identity(_service_config(args)))
        if health is None:
            print("服务探测失败：%s。请以当前版本和配置重启旧服务。" % reason, file=sys.stderr)
            return 1
        print("服务身份匹配，已监听：http://127.0.0.1:%d/（%s）" % (
            args.port, "数据已就绪" if health.get("ready") is True else "首次数据加载中"))
        return 0

    html_txt, agg, info = build(args)
    out_path = info["out_path"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_txt)
    print_summary(html_txt, agg, info, args)
    if args.open:
        try:
            os.startfile(out_path)  # noqa: S606 (Windows)
        except Exception as e:
            print("  打开浏览器失败：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
