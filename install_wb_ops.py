#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-ops 插件安装 / 卸载脚本

WorkBuddy 通过三个注册表文件管理插件，本脚本按它们的实际格式写入：
    ~/.workbuddy/plugins/known_marketplaces.json   市场注册
    ~/.workbuddy/plugins/installed_plugins.json    插件安装记录
    ~/.workbuddy/settings.json                     插件启用开关

所有被修改的文件都会先备份（后缀 .bak-2026-10-15-wbops）。

用法：
    python install_wb_ops.py            安装
    python install_wb_ops.py sync       只把插件目录（plugins/wb-ops）内容同步进市场副本 + 安装快照（日常改代码后用）
    python install_wb_ops.py uninstall  卸载（按字段移除注册条目；只删本插件目录与版本快照）
    python install_wb_ops.py status     查看当前状态
    python install_wb_ops.py gc         清理旧版本安装快照（默认只预演，加 --yes 执行）

可选参数：
    --marketplace NAME   自定义市场名（默认 local；卸载 / status / gc 须用同一名字）
    --with-read          把 Read 写进 PostToolUse matcher（默认不启用；仍需设 WB_OPS_READ_TRIM=1）
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")

# 宿主配置根目录：WorkBuddy 5.8.x 写 ~/.workbuddy，WorkBuddyAI 写 ~/.workbuddy-ai，两者互不相通。
# 装错一边等于没装——插件不会被扫描，hook 不加载、不报错、账本停更。
# 判定顺序：WB_OPS_HOST_HOME 显式指定 → plugins/.last_inuse_sweep 最新者 → 退回 ~/.workbuddy。
HOST_HOMES = [os.path.join(HOME, ".workbuddy-ai"), os.path.join(HOME, ".workbuddy")]


def detect_host_home():
    """返回 (宿主根目录, 判定依据)。"""
    env = os.environ.get("WB_OPS_HOST_HOME")
    if env:
        # 归一化：Git Bash 可能传 C://Users//x//.workbuddy，原样写进注册表会和宿主注入的插件根对不上
        return os.path.abspath(os.path.expanduser(env.strip())), "WB_OPS_HOST_HOME"
    best, best_t = None, -1.0
    for h in HOST_HOMES:
        if not os.path.isdir(h):
            continue
        marker = os.path.join(h, "plugins", ".last_inuse_sweep")
        t = os.path.getmtime(marker) if os.path.exists(marker) else 0.0
        if t > best_t:
            best, best_t = h, t
    if best:
        return best, "自动探测（.last_inuse_sweep 最新）"
    return os.path.join(HOME, ".workbuddy"), "默认"


WB, WB_SOURCE = detect_host_home()
PLUGINS = os.path.join(WB, "plugins")

MARKETPLACE = "local"   # 市场名；可用 --marketplace NAME 覆盖
PLUGIN = "wb-ops"
PLUGIN_ID = "%s@%s" % (PLUGIN, MARKETPLACE)
VERSION = "1.1.0"
BACKUP_SUFFIX = ".bak-2026-10-15-wbops"

# PostToolUse matcher（v1.1 收紧，详见 apply_read_matcher 的说明）
MATCHER_DEFAULT = "Grep|Glob|WebFetch"
MATCHER_WITH_READ = "Grep|Glob|WebFetch|Read"

MARKET_DIR = os.path.join(PLUGINS, "marketplaces", MARKETPLACE)
PLUGIN_DIR = os.path.join(MARKET_DIR, "plugins", PLUGIN)
# 宿主真正加载的是 cache 下的「安装快照」，路径形如
#   plugins/cache/<市场>/<插件>/<版本>/
# 只写 installed_plugins.json 而不落这份副本，插件会被跳过（扫描时静默 continue）。
CACHE_DIR = os.path.join(PLUGINS, "cache", MARKETPLACE, PLUGIN, VERSION)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # 仓库根（本脚本所在处）


def _locate_plugin_src():
    """定位插件内容目录。

    官方市场结构下插件本体位于 仓库根/plugins/wb-ops；
    兼容把本脚本直接拷进插件目录内单独运行的历史用法（此时本目录即插件本体）。
    """
    repo_layout = os.path.join(SCRIPT_DIR, "plugins", PLUGIN)
    if os.path.isdir(os.path.join(repo_layout, ".codebuddy-plugin")):
        return repo_layout
    if os.path.isdir(os.path.join(SCRIPT_DIR, ".codebuddy-plugin")):
        return SCRIPT_DIR
    return repo_layout


PLUGIN_SRC = _locate_plugin_src()  # 插件源目录：安装/同步都从这里复制

# 同步时排除：非插件内容 / 备份 / 日志
SYNC_IGNORE = shutil.ignore_patterns(
    ".workbuddy", ".git", "__pycache__", "install_wb_ops.py", "*.bak*", "trace.log",
    "backup", "_*.log")  # backup/ 是开发侧回滚快照；测试日志不随版本分发

KNOWN = os.path.join(PLUGINS, "known_marketplaces.json")
INSTALLED = os.path.join(PLUGINS, "installed_plugins.json")
SETTINGS = os.path.join(WB, "settings.json")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        "%03dZ" % (datetime.now(timezone.utc).microsecond // 1000)


def read_json(path):
    # 全新宿主可能还没有这些注册表文件，按空配置处理即可（首次安装的场景）
    if not os.path.isfile(path):
        print("  提示：%s 不存在，按空配置处理" % os.path.basename(path))
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data, backup=True):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if backup and os.path.exists(path):
        bak = path + BACKUP_SUFFIX
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            print("  已备份 -> %s" % os.path.basename(bak))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def want_read():
    """是否把 Read 装进 PostToolUse matcher（`--with-read`）。"""
    return "--with-read" in sys.argv


def apply_read_matcher(*roots):
    """v1.1：`--with-read` 时把已落盘的 hooks.json 的 PostToolUse matcher 扩到含 Read。

    默认 matcher 只有 Grep|Glob|WebFetch，理由：
      · Bash 摘除 —— 宿主对 Bash 的 tool_response 只给元数据（exitCode 等），
        正文不在载荷里，hook 拿不到任何可裁剪文本，命中一次只白付约 270ms 进程开销
        （实测 bash+exe 273ms/次 vs 裸 exe 64ms/次）。
      · Read 摘除 —— Read 裁剪默认关闭（要 WB_OPS_READ_TRIM=1 才生效），
        为默认关闭的功能在每次读文件时白付 270ms 不划算，改为安装期显式开启；
        开启后仍受 >1500 行门槛保护。
    """
    for root in roots:
        path = os.path.join(root, "hooks", "hooks.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            hit = 0
            for group in data.get("hooks", {}).get("PostToolUse", []):
                if group.get("matcher") == MATCHER_DEFAULT:
                    group["matcher"] = MATCHER_WITH_READ
                    hit += 1
            if not hit:
                print("  ! %s 的 matcher 不是默认值，未改动" % path)
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print("  + matcher -> %s（还需设 WB_OPS_READ_TRIM=1 才真正裁剪）" % MATCHER_WITH_READ)
        except Exception as e:
            print("  ! 修改 matcher 失败（%s）：%s" % (path, e))


def sync():
    """把插件源目录（PLUGIN_SRC）的当前内容同步进市场副本和安装快照。

    日常改完代码后跑这个：宿主真正加载的是 cache 快照，只改开发目录不生效。
    增量覆盖发布副本，不删除目录；同步成功后对齐注册表；幂等，可反复跑。
    """
    print("同步 开发目录 -> 市场副本 -> 安装快照")
    print()
    if not os.path.isdir(MARKET_DIR):
        print("! 市场目录不存在，请先运行 install：%s" % MARKET_DIR)
        return 1

    # 防版本漂移：本脚本 VERSION 必须与 plugin.json 一致，否则同步出「版本对不上」的快照
    try:
        pj = read_json(os.path.join(PLUGIN_SRC, ".codebuddy-plugin", "plugin.json"))
        if pj.get("version") != VERSION:
            print("! 版本不一致：plugin.json=%s / 本脚本 VERSION=%s，请先统一再同步"
                  % (pj.get("version"), VERSION))
            return 1
    except Exception as e:
        print("! 读取 plugin.json 失败：%s" % e)
        return 1

    # 发布只增量覆盖，不清空用户目录或旧快照。两处均从插件源目录（PLUGIN_SRC）复制，
    # 避免市场副本内的历史文件被带进新的版本快照。
    shutil.copytree(PLUGIN_SRC, PLUGIN_DIR, ignore=SYNC_IGNORE, dirs_exist_ok=True)
    print("  + 市场副本 %s" % PLUGIN_DIR)

    shutil.copytree(PLUGIN_SRC, CACHE_DIR, ignore=SYNC_IGNORE, dirs_exist_ok=True)
    if want_read():
        apply_read_matcher(PLUGIN_DIR, CACHE_DIR)
    if write_marketplace_manifest():
        print("  + 市场清单 %s" % os.path.join(MARKET_DIR, ".codebuddy-plugin", "marketplace.json"))
    print("  + 安装快照 %s" % CACHE_DIR)

    # 注册表一致性：宿主按 installed_plugins.json 的 installPath 加载插件。
    # 2026-10-15 教训：0.2.2 只跑过 sync 没更新注册表，注册表仍指 0.2.1，
    # 连续几个会话实际加载的都是 0.2.1 —— 版本发布后注册表必须一起更新。
    print("  · 检查注册表 installed_plugins.json")
    if not sync_registry():
        return 1

    print()
    print("完成。改动按会话进程粒度生效：新开的会话自动加载新版，旧会话保持旧版（无需重启应用）。")
    stale = stale_versions()
    if stale:
        print("提示：cache 下另有 %d 个旧版本快照（%s），可运行 python install_wb_ops.py gc 查看/清理。"
              % (len(stale), ", ".join(stale)))
    return 0


def write_marketplace_manifest():
    """把仓库根的市场清单写入市场目录（宿主按「清单 + plugins/」的官方结构校验市场）。

    主动铺一份 marketplace.json：较新宿主检查到已注册市场缺少本地快照时
    会告警并尝试从声明的源修复，主动补齐可避免这一状态。
    name 改写为本市场名（MARKETPLACE），与 known_marketplaces.json 保持一致；
    仅当脚本运行于仓库根（存在 .codebuddy-plugin/marketplace.json）时生效。
    """
    src = os.path.join(SCRIPT_DIR, ".codebuddy-plugin", "marketplace.json")
    if not os.path.isfile(src):
        return False
    try:
        mf = read_json(src)
    except Exception as e:
        print("  ! 读取市场清单失败：%s（跳过）" % e)
        return False
    mf["name"] = MARKETPLACE
    dst_dir = os.path.join(MARKET_DIR, ".codebuddy-plugin")
    os.makedirs(dst_dir, exist_ok=True)
    with open(os.path.join(dst_dir, "marketplace.json"), "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def sync_registry():
    """把 installed_plugins.json 的 wb-ops 条目对齐到当前 VERSION / CACHE_DIR。

    幂等：已对齐时不做任何写入。返回 False 表示更新失败（调用方应中止并提示）。
    """
    try:
        ip = read_json(INSTALLED)
        entries = ip.get("plugins", {}).get(PLUGIN_ID) or []
        entry = entries[0] if entries else {}
        same = (entry.get("version") == VERSION and
                os.path.normcase(os.path.normpath(entry.get("installPath") or "")) ==
                os.path.normcase(os.path.normpath(CACHE_DIR)))
        if same:
            print("  · 注册表已指向 %s（无需变更）" % VERSION)
            return True
        print("  ! 注册表版本是 %s，更新为 %s" % (entry.get("version") or "无", VERSION))
        ip.setdefault("plugins", {})[PLUGIN_ID] = [
            {
                "scope": "user",
                "installPath": CACHE_DIR,
                "version": VERSION,
                "resolvedVersion": VERSION,
                "installedAt": entry.get("installedAt") or now_iso(),
                "lastUpdated": now_iso(),
            }
        ]
        write_json(INSTALLED, ip)
        print("  + %s -> %s" % (PLUGIN_ID, CACHE_DIR))
        return True
    except Exception as e:
        print("  ! 注册表更新失败：%s（可单独运行 install 修复）" % e)
        return False


def install():
    print("安装 %s ..." % PLUGIN_ID)
    print()

    # 从 clone / 解压目录直接运行（插件源目录 ≠ 市场插件目录）时，先把插件本源铺进市场目录；
    # 用户 git clone 后直接 `python install_wb_ops.py` 即可完成安装。
    if os.path.normcase(os.path.abspath(PLUGIN_SRC)) != os.path.normcase(os.path.abspath(PLUGIN_DIR)):
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        shutil.copytree(PLUGIN_SRC, PLUGIN_DIR, ignore=SYNC_IGNORE, dirs_exist_ok=True)
        print("  + 市场插件目录 %s" % PLUGIN_DIR)

    if not os.path.isdir(PLUGIN_DIR):
        print("! 插件目录不存在：%s" % PLUGIN_DIR)
        return 1

    # ---- 1. 注册市场 ----
    print("[1/4] 注册市场 known_marketplaces.json")
    km = read_json(KNOWN)
    km[MARKETPLACE] = {
        "manifestName": MARKETPLACE,
        "type": "directory",
        "source": {"source": "directory", "path": MARKET_DIR},
        "installLocation": MARKET_DIR,
        "description": "本地自建插件市场",
        "lastUpdated": now_iso(),
        "autoUpdate": False,
    }
    write_json(KNOWN, km)
    if write_marketplace_manifest():
        print("  + 市场清单 .codebuddy-plugin/marketplace.json")
    print("  + %s -> %s" % (MARKETPLACE, MARKET_DIR))

    # ---- 2. 落安装快照到 cache/<市场>/<插件>/<版本>/ ----
    print("[2/4] 生成安装快照 cache/%s/%s/%s" % (MARKETPLACE, PLUGIN, VERSION))
    if os.path.isdir(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)
    shutil.copytree(PLUGIN_DIR, CACHE_DIR, dirs_exist_ok=True)
    if want_read():
        apply_read_matcher(PLUGIN_DIR, CACHE_DIR)
    print("  + %s" % CACHE_DIR)

    # ---- 3. 记录安装 ----
    print("[3/4] 记录安装 installed_plugins.json")
    ip = read_json(INSTALLED)
    ip.setdefault("plugins", {})
    ip["plugins"][PLUGIN_ID] = [
        {
            "scope": "user",
            "installPath": CACHE_DIR,
            "version": VERSION,
            "resolvedVersion": VERSION,
            "installedAt": now_iso(),
            "lastUpdated": now_iso(),
        }
    ]
    write_json(INSTALLED, ip)
    print("  + %s -> %s" % (PLUGIN_ID, CACHE_DIR))

    # ---- 4. 启用插件 ----
    print("[4/4] 启用插件 settings.json")
    st = read_json(SETTINGS)
    st.setdefault("enabledPlugins", {})
    st["enabledPlugins"][PLUGIN_ID] = True
    write_json(SETTINGS, st)
    print("  + enabledPlugins[\"%s\"] = true" % PLUGIN_ID)

    print()
    print("安装完成。需要重启 WorkBuddy（或执行 /reload-plugins）才会生效。")
    return 0


def uninstall():
    print("卸载 %s ..." % PLUGIN_ID)
    print()

    # v0.6.1（T3）：优先「按字段删除」——只移除本插件在注册表中的条目。
    # 旧的「整份还原备份」会把用户在安装 wb-ops 之后新增的插件注册一起回滚，
    # 故降级为：单文件字段删除失败时才对该文件回退整份还原。
    targets = (
        (KNOWN, "known_marketplaces.json", MARKETPLACE,
         lambda d: d.pop(MARKETPLACE, None)),
        (INSTALLED, "installed_plugins.json", PLUGIN_ID,
         lambda d: d.get("plugins", {}).pop(PLUGIN_ID, None)),
        (SETTINGS, "settings.json", PLUGIN_ID,
         lambda d: d.get("enabledPlugins", {}).pop(PLUGIN_ID, None)),
    )
    for path, name, entry_name, drop in targets:
        if not os.path.exists(path):
            print("  跳过 %s（文件不存在）" % name)
            continue
        try:
            d = read_json(path)
            if drop(d) is None:
                print("  %s 无本插件条目（无需变更）" % name)
                continue
            write_json(path, d, backup=False)
            print("  已从 %s 移除 %s" % (name, entry_name))
        except Exception as exc:
            bak = path + BACKUP_SUFFIX
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                print("  字段删除失败（%s），已回退为整份还原 %s" % (exc, name))
            else:
                print("  跳过 %s（字段删除失败：%s；且无备份可还原）" % (name, exc))

    # v0.6.1（T4）：只删本插件目录与版本快照；
    # 市场目录 / cache 市场目录仅在其为空时清理（同一市场名下可能还有其它插件）。
    if os.path.isdir(PLUGIN_DIR):
        shutil.rmtree(PLUGIN_DIR)
        print("  已删除插件目录 %s" % PLUGIN_DIR)
    cache_plugin = os.path.join(PLUGINS, "cache", MARKETPLACE, PLUGIN)
    if os.path.isdir(cache_plugin):
        shutil.rmtree(cache_plugin)
        print("  已删除安装快照 %s" % cache_plugin)
    for empty_dir in (os.path.join(MARKET_DIR, "plugins"), MARKET_DIR,
                      os.path.join(PLUGINS, "cache", MARKETPLACE)):
        try:
            os.rmdir(empty_dir)   # 仅当为空时才会成功
            print("  已清理空目录 %s" % empty_dir)
        except OSError:
            pass

    print()
    print("卸载完成。重启 WorkBuddy 后生效。")
    return 0


def status():
    print("当前状态")
    print("=" * 56)
    print("  %-8s %s  (%s)" % ("宿主根", WB, WB_SOURCE))
    for path, label in ((KNOWN, "市场注册"), (INSTALLED, "安装记录"), (SETTINGS, "启用开关")):
        try:
            d = read_json(path)
        except Exception as exc:
            print("  %-8s 读取失败 %s" % (label, exc))
            continue
        if label == "市场注册":
            ok = MARKETPLACE in d
        elif label == "安装记录":
            ok = PLUGIN_ID in d.get("plugins", {})
        else:
            ok = d.get("enabledPlugins", {}).get(PLUGIN_ID) is True
        print("  %-8s %s" % (label, "已注册 ✓" if ok else "未注册 ✗"))
    exists = os.path.isdir(PLUGIN_DIR)
    print("  %-8s %s" % ("源目录", "存在 ✓" if exists else "缺失 ✗"))
    print("  %-8s %s" % ("安装快照", "存在 ✓" if os.path.isdir(CACHE_DIR) else "缺失 ✗（插件会被宿主跳过）"))
    print("  源目录   %s" % PLUGIN_DIR)
    print("  安装快照 %s" % CACHE_DIR)
    return 0


def _cache_versions():
    """列出 cache 下形如 0.3.3 的版本目录名，按版本号升序。"""
    root = os.path.join(PLUGINS, "cache", MARKETPLACE, PLUGIN)
    out = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            if re.match(r"^\d+\.\d+\.\d+$", name) and \
                    os.path.isdir(os.path.join(root, name)):
                out.append(name)
    out.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
    return out


def stale_versions():
    """可清理的旧版本快照列表。

    保留：当前 VERSION、注册表指向的版本、版本号最近的 1 个旧版本
    （运行中的旧会话可能仍锁定某个旧版本）。
    """
    keep = {VERSION}
    try:
        entry = read_json(INSTALLED)["plugins"].get(PLUGIN_ID, [{}])[0]
        if entry.get("version"):
            keep.add(entry["version"])
    except Exception:
        pass
    versions = _cache_versions()
    for v in reversed(versions):
        if v not in keep:
            keep.add(v)
            break
    return [v for v in versions if v not in keep]


def gc(apply=False):
    """清理旧版本安装快照。默认只预演；确认后加 --yes 执行。"""
    root = os.path.join(PLUGINS, "cache", MARKETPLACE, PLUGIN)
    stale = stale_versions()
    print("安装快照目录：%s" % root)
    if not stale:
        print("  无旧版本可清理（保留：当前 %s + 注册表版本 + 最近的 1 个旧版本）。" % VERSION)
        return 0
    total = 0
    for v in stale:
        p = os.path.join(root, v)
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _, fs in os.walk(p) for f in fs)
        total += size
        print("  %s  %6d KB" % (v.ljust(8), size // 1024))
    print("  合计可回收约 %.1f MB" % (total / 1048576.0))
    if not apply:
        print("  注意：运行中的会话可能仍锁定其中某个版本（删除后该会话的裁剪会静默失效，"
              "输出原样透传）——确认无旧会话在用后再执行。")
        print("  （预演）执行删除：python install_wb_ops.py gc --yes")
        return 0
    for v in stale:
        shutil.rmtree(os.path.join(root, v))
        print("  已删除 %s" % v)
    return 0


def apply_marketplace(argv):
    """支持 --marketplace NAME：重算相关全局路径（须在任何安装逻辑之前调用）。

    v0.6.1（T2）：返回剥离 `--marketplace NAME` 参数对之后的位置参数列表，
    供入口按「首个非 - 开头 token」取子命令 —— 此前只看 argv[1]，
    `python install_wb_ops.py --marketplace X status` 会被误当作 install 实际执行。
    """
    global MARKETPLACE, PLUGIN_ID, MARKET_DIR, PLUGIN_DIR, CACHE_DIR
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--marketplace":
            if i + 1 >= len(argv):
                print("! --marketplace 需要一个名字，例如 --marketplace local")
                sys.exit(2)
            MARKETPLACE = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    PLUGIN_ID = "%s@%s" % (PLUGIN, MARKETPLACE)
    MARKET_DIR = os.path.join(PLUGINS, "marketplaces", MARKETPLACE)
    PLUGIN_DIR = os.path.join(MARKET_DIR, "plugins", PLUGIN)
    CACHE_DIR = os.path.join(PLUGINS, "cache", MARKETPLACE, PLUGIN, VERSION)
    return rest


if __name__ == "__main__":
    rest = apply_marketplace(sys.argv[1:])
    cmd = next((a for a in rest if not a.startswith("-")), "install")
    unknown = [a for a in rest if a.startswith("-") and a not in ("--yes", "--with-read")]
    if unknown:
        print("未知参数：%s（可用：--marketplace NAME / --yes / --with-read）" % " ".join(unknown))
        sys.exit(2)
    if cmd == "install":
        sys.exit(install())
    if cmd == "sync":
        sys.exit(sync())
    if cmd == "uninstall":
        sys.exit(uninstall())
    if cmd == "status":
        sys.exit(status())
    if cmd == "gc":
        sys.exit(gc("--yes" in rest))
    print("未知命令：%s（可用：install / sync / uninstall / status / gc）" % cmd)
    sys.exit(2)
