---
description: 生成 wb-ops 运行报告（省 token 统计 + 积分与上下文分析）并在预览面板打开；支持 serve 实时服务模式
argument-hint: "可留空；支持 serve（实时服务） demo（示意数据） no-prune out=路径"
---

生成并展示 **wb-ops 后台报告**（本插件负责压缩工具输出省 token；报告含节省统计、积分消耗、上下文占用三部分）。

用户附加参数：$ARGUMENTS（若此处显示为 `$ARGUMENTS` 原文或为空，一律按"无参数"处理）

执行步骤：

1. 选一个可用 Python 解释器，按顺序尝试，用第一个存在的：
   - `~/.workbuddy/binaries/python/versions/` 下最新版本的 python.exe（WorkBuddy 托管解释器，优先）
   - `python`
   - `py -3`

2. 运行报告生成器。插件根目录即 `${CODEBUDDY_PLUGIN_ROOT}`（宿主会替换为实际安装目录；万一没有被替换，就改用 `~/.workbuddy/plugins/` 下本插件的安装目录（cache 快照中版本号最大的那个））：

   `"<python>" "${CODEBUDDY_PLUGIN_ROOT}/report.py"`

   按附加参数追加开关（无参数则不加）：含 `demo` → 追加 `--demo`；含 `no-prune` → 追加 `--no-prune`；含 `out=` → 追加 `--out <路径>`。

   **若参数含 `serve`：改走实时服务流程**——用 Bash 工具以 run_in_background 方式启动
   `"<python>" "${CODEBUDDY_PLUGIN_ROOT}/report.py" --serve`，等输出里出现
   `http://127.0.0.1:8787/` 后，用 present_files 打开该地址（传 URL 而不是文件），并告诉用户：
   页面每 30 秒自动刷新、数据即扫即新，日常查看无需再生成；该服务随本应用一起停止，
   想长期常驻就在自己的终端里运行同一命令。若输出提示端口被占用，追加 `--port 8788` 重试一次。
   serve 模式到此结束，不要再执行第 3、4 步。

3. 生成成功后用 present_files 打开报告文件，让它在预览面板直接展示（不要改用浏览器打开）：
   - 默认：`~/.workbuddy/wb-ops-data/dashboard.html`
   - 用 `--demo` 时：`~/.workbuddy/wb-ops-data/dashboard-demo.html`

4. 用 2~3 行向用户汇报关键数字：累计节省字节数（约合多少 token 量级）、账本覆盖天数、最近一次记账时间；再补充一条运行侧的：按模型统计（请求数/输入累计/缓存命中率）或上下文覆盖会话数。若报告里出现 ≥75% 的红色"建议换会话"标记，顺带指出是哪个会话。若脚本提示"节省区暂无数据"（新账本还没攒到数据），如实说明即可，这不是错误。

约束：账本与报告均为只读数据，不要删改 `~/.workbuddy/wb-ops-data/` 下的任何文件；过期清理由脚本自带的 3 个月滚动策略完成，无需手动干预。
