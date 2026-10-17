---
description: 打开固定地址的 wb-ops 动态后台（节省统计、积分与上下文分析）；自动启动或复用本地服务
argument-hint: "默认动态后台；兼容 serve；支持 snapshot（静态导出） demo（示意快照） no-prune out=路径"
---

打开 **wb-ops 动态后台**，默认始终使用 `http://127.0.0.1:8787/`，不要每次生成静态文件。

用户附加参数：$ARGUMENTS（原文或为空时按无参数处理）。

执行步骤：

1. 优先选用 `~/.workbuddy/binaries/python/versions/` 下可用的最新托管 Python 版本（Windows 为 `python.exe`，其他平台为 `bin/python3`），不存在再尝试 `python`、`py -3`。
   插件根目录使用 `${CODEBUDDY_PLUGIN_ROOT}`；未替换时，定位 `~/.workbuddy/plugins/` 下本插件的实际安装目录（cache 快照选版本号最大的目录，不固定市场名称），确认其中存在 `report.py`，后续命令均使用该目录。

2. **默认动态模式（无参数或含 serve；若含 snapshot、demo、out=，优先跳到第 3 步）**：
   - 用 Bash 的 `run_in_background` 启动 `"<python>" "${CODEBUDDY_PLUGIN_ROOT}/report.py" --serve`。含 `no-prune` 时追加 `--no-prune`。
   - 脚本在固定端口启动新服务，或校验已有服务的协议、版本、配置后复用并退出 0。复用成功不必再启动。
   - 出现监听/复用提示后，用相同参数执行 `--status`（替换 `--serve`），确认服务身份匹配；必要时读取首页确认 HTTP 200。首次数据可能仍在加载，健康端点不扫描数据。
   - 若端口是其他程序、旧版服务或配置不同，明确报告冲突；不打开未知网页、不自动换成 8788、不终止占用进程。由用户停止旧服务后再重试。
   - 就绪后通过 `present_files` 打开 `http://127.0.0.1:8787/`（必须传 URL，不传 dashboard.html，也不要另开系统浏览器）。
   - 简要说明：固定地址、页面每 30 秒局部更新，隐藏时暂停，日常无需重新生成；服务由当前应用托管，不承诺退出后常驻，需长期运行可在自己的终端执行同一命令。

3. **仅显式导出时生成文件**：参数含 `snapshot`、`demo` 或 `out=` 时走静态导出，不启动服务。
   - 执行 `"<python>" "${CODEBUDDY_PLUGIN_ROOT}/report.py" --snapshot`，按需追加 `--demo`、`--no-prune`、`--out "<路径>"`。
   - 生成成功后用 `present_files` 打开实际输出文件。默认是 `~/.workbuddy/wb-ops-data/dashboard.html`；demo 是 `dashboard-demo.html`。
   - `demo` 仅预览合成数据，不能让示意服务占用真实后台的固定地址。
   - 简要汇报统计；明确这是静态导出，不自动更新。

约束：不要手动修改或清理账本、宿主库和 transcript。过期清理仅由报告脚本自带策略完成。仅监听 127.0.0.1，不发布到公网。动态模式不落盘 dashboard.html。
