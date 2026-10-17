# wb-ops

**WorkBuddy 的上下文治理插件** —— 工具输出在进入模型上下文之前自动裁剪（省 token），
会话临近自动压缩点之前主动提醒（省心），记忆文件超限时自动收拾（防截断），
并自带一本"省了多少"的账与可视化报告后台。

当前版本 **1.0.0** ｜ 宿主 WorkBuddy 5.8.2（Windows 实测） ｜ [MIT License](LICENSE)

> **1.0.0 是首个稳定版**：功能与 0.6.9 一致，不引入新行为，只是把已验证的功能集冻结下来。
> 自本版起冻结对外契约——环境变量、省略标记格式、hook 事件集、数据文件格式、安装布局；
> 破坏性改动只会在 2.0 出现。装完想自检：`python test_hook.py`（注册与行为）、
> `python test_parity.py`（C++ 与 Python 双实现逐字节一致）。

---

## 它解决什么问题

Agent 会话的成本大头是**上下文随轮次累积**：一次重度搜索的完整输出会被后续每一轮请求重复携带。
wb-ops 在四个位置介入，把这个成本压下去：

| # | 链路 | 时机 | 做什么 |
|---|------|------|--------|
| 1 | **输出裁剪** | 每次 Grep / Glob / WebFetch / Bash 之后 | 长输出压缩到预算内再进上下文（默认 ≤ 8000 字节），关键行与结尾优先保留；**Bash 失败（退出码非 0）整段透传**，报错现场不动 |
| 2 | **上下文守卫 v2** | 每次提交消息时 | 学习各模型真实的自动压缩触发点，三档提醒（轻档 / 强档 / 越过压缩点仍未触发）收尾或换新会话 |
| 3 | **记忆守卫** | 新会话启动时 | 用户级记忆超限（默认 3500 字符）时自动压缩（带备份），防止被宿主静默截断 |
| 4 | **压缩保留清单** | 宿主执行自动压缩时 | 注入"什么必须保留"（任务目标 / 未决事项 / 关键决策…），让压缩摘要不失忆 |

另附一个**报告后台**：节省统计、积分消耗、上下文占用一页看清（`/wb-ops:report`）。

## 实测效果

| 指标 | 数据 | 口径 |
|---|---|---|
| 单次大输出裁剪 | **大输入约 80–95%** | 接近阈值的小输入可能不足 80%；50KB → ≤8KB（硬上界，实测约 4.8KB）；3000 行 → ≤8KB（实测约 1.9KB） |
| 重型会话输入重放 | **-81%** | 与"从未压缩"反事实对照（518 次请求的重型会话） |
| 截断开销 | 原生 exe 纯处理 **~2.4ms**（含进程启动 ~70ms） | 经 bash 包装后整体 ~0.3s/次（Grep/Glob/WebFetch/Bash） |
| 守卫开销 | 每消息 ~0.26s（尾部直读） | 未达阈值零输出、零注入 |
| 测试覆盖 | **520 项全绿** | 边界 / fuzz / 并发 / 长跑 / 双实现逐字节一致 |

> 裁剪不是"砍内容"：ERROR / warning 关键字行、结尾段、命中抽样会按规则优先保留，
> 并附带显式省略标记（`… [wb-ops 省略 N 行（第 X–Y 行），……] …`），带被省略段的**行号区间**，
> 模型可以直接用 `offset` 回查而不用重跑命令。

## 安装

**前置要求**

- WorkBuddy 桌面版（5.8.x；hook 协议与内部机制跟随此版本，见[兼容性](#兼容性与边界)）
- Python 3.8+（安装脚本 + Python 回退实现）
- Node.js 18+（两个 `.mjs` 守卫的运行时；系统无 node 时会探测 WorkBuddy 自带位置）

**方式一：通过 WorkBuddy 插件市场安装（推荐）**

本仓库同时就是一个标准的 WorkBuddy 插件市场（根目录 `.codebuddy-plugin/marketplace.json` +
`plugins/wb-ops/` 插件本体），直接添加即可：

1. 打开 WorkBuddy 的**插件页面**，进入其中的**插件市场**列表
2. 点击**添加市场**，输入 `sana35421/wb-ops`（支持 `owner/repo` 简写或完整 URL）
3. 在市场列表中找到 `wb-ops`，点击**安装**，按提示启用

**方式二：克隆 + 一键脚本**

```bash
git clone https://github.com/sana35421/wb-ops.git
cd wb-ops
python install_wb_ops.py
```

脚本会自动完成全部四步：注册本地插件市场 → 把插件本体（`plugins/wb-ops/`）铺进市场插件目录 →
生成安装快照 → 写入启用开关。完成后**重启 WorkBuddy（或执行 `/reload-plugins`）生效**，之后新开的会话自动加载。

**更新 / 卸载**

- 市场方式：在插件市场页面**刷新市场**即可拉取新版本；不再需要时在同一页面卸载
- 脚本方式：

```bash
git pull && python install_wb_ops.py sync   # 更新到最新版
python install_wb_ops.py uninstall          # 卸载（按字段移除本插件注册条目，只删本插件目录/快照）
python install_wb_ops.py status             # 查看注册状态
```

## 使用

### 自动部分（装完即生效，无需任何操作）

- **裁剪**：对 Grep / Glob / WebFetch / Bash 的长输出自动生效；短输出与 Bash 失败输出原样透传
- **守卫**：接近自动压缩点时，模型会在回复末尾顺带出现一句提醒（如"话题告一段落，可开新会话"），
  同档位每会话只提醒一次，不刷屏

### 报告后台

```bash
# 在仓库根目录执行；插件安装后，在插件安装目录里可直接跑 report.py
python plugins/wb-ops/report.py --serve     # 启动或安全复用 http://127.0.0.1:8787/，每30s局部更新
python plugins/wb-ops/report.py --status    # 校验已有服务身份、版本、配置
python plugins/wb-ops/report.py --snapshot  # 显式静态导出；Python 无参仍兼容导出 dashboard.html
python plugins/wb-ops/report.py --demo      # 合成示意快照，不占用真实后台端口
```

或在会话里用 `/wb-ops:report`：**默认打开固定地址的动态后台**，无需附加 `serve`；同版本同配置服务自动复用。
支持 `snapshot` / `demo` / `no-prune` / `out=<路径>`，其中 `snapshot`、`demo`、`out=` 显式导出静态文件。

页面移除搜索，按节省卡片、运行指标、趋势、上下文告警、明细分层展示；包含更新倒计时、隐藏暂停和失败保留旧数据。
端口被其他程序、旧版或不同配置服务占用时拒绝打开，不自动换端口、不终止占用进程；停止旧服务后重新启动即可。
升级后新会话加载新版命令。服务仅本机可访问，应用托管进程不保证退出后常驻；长期查看请在自己的终端运行 `--serve`。

报告包含三部分：**节省统计**（逐日、按工具）、**积分消耗**（按天/项目/会话）、
**上下文占用**（全量会话水位，≥75% 红色提示换新会话）。

### 上下文守卫的手动调节

学习模式是默认行为。若想手动控制：

```bash
WB_OPS_CTX_GUARD=120000   # 手动指定轻档线（同时禁用学习，适合固定用某个模型）
WB_OPS_CTX_GUARD=off      # 关闭守卫
```

## 配置（环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `WB_OPS_BUDGET` | `8000` | 裁剪字节预算（2000–131072）；非法值回退默认 |
| `WB_OPS_STATS` | 开 | `WB_OPS_STATS=0` 关闭节省记账 |
| `WB_OPS_STATS_DIR` | `~/.workbuddy/wb-ops-data` | 账本目录（测试隔离用） |
| `WB_OPS_CTX_GUARD` | 自动学习 | `0`/`off`/`false`/`no` 关闭；数字 = 手动轻档线 |
| `WB_OPS_CTX_GUARD_STEP` | `40000` | 轻档线 = 强档线 − 该档距（token） |
| `WB_OPS_POST_COMPACT_MAX` | `100000` | 压缩后水位告警线：高于它说明"底子过重" |
| `WB_OPS_CTX_GUARD_DIR` | `~/.workbuddy/wb-ops-data` | 守卫状态文件目录 |
| `WB_OPS_MEM_GUARD_LIMIT` | `3500` | 记忆字符阈值（超过即触发） |
| `WB_OPS_MEM_GUARD_MODE` | `auto` | `auto` 自动压缩 / `remind` 仅提醒 |
| `WB_OPS_READ_TRIM` | 关 | 置 `1` 开启 Read 按需裁剪（仅对 >1500 行的文件生效） |
| `WB_OPS_TRACE` | 关 | 置 `1`（或放 `bin/TRACE-ON`）打开 `bin/trace.log` 调试日志 |

## 工作原理

> 下图中路径均为插件包内相对路径（对应仓库的 `plugins/wb-ops/` 目录）。

```
【裁剪链路】每次 Grep / Glob / WebFetch / Bash
  工具调用 ──> PostToolUse hook（hooks/hooks.json）
                 │
                 ├─ bin/wb-ops-trim.exe          ← 原生实现（纯处理 ~2.4ms，静态编译）
                 │    └─ 不可用时回退 hooks/trim.py（Python，逐字节一致）
                 │
                 ├─ stdout: {"hookSpecificOutput":{"updatedToolOutput":"裁剪后输出"}}
                 │           → 模型只看到裁剪版
                 └─ 记账: ~/.workbuddy/wb-ops-data/saves-YYYYMM.jsonl
                          （仅尺寸/时间/会话前缀，不落任何内容）

【守卫链路】新会话启动 / 每次提交消息 / 宿主压缩时
  SessionStart ──> hooks/memory-guard.mjs   检查用户记忆字符数，超限注入压缩指令
  UserPromptSubmit ──> hooks/ctx-guard.mjs  尾部直读 transcript 得上下文水位；
                                            学习各模型压缩触发点 → 三档提醒 → 压缩后检查
  PreCompact ──> hooks/precompact.mjs       stdout 输出保留清单 → 注入压缩摘要过程

【报告链路】python report.py（或 /wb-ops:report）
  saves-*.jsonl        —— 精确节省口径（裁剪时同时看到前/后尺寸）
  workbuddy.db（只读）  —— 会话积分 / 上下文占用
  projects/*/*.jsonl   —— 单遍全量直读：按模型 token + 缓存命中（三格式归一）+ 上下文水位
        │
        ▼
  dashboard.html（单文件零依赖） ／ --serve 实时面板 http://127.0.0.1:8787/
```

## 兼容性与边界

- **宿主依赖**：本插件依据 WorkBuddy 5.8.x 的 hook 协议与 transcript 格式开发。
  宿主升级可能影响部分功能（尤其守卫与报告），届时欢迎提 issue；不承诺长期兼容。
- **平台**：Windows 已验证（含预编译 `plugins/wb-ops/bin/wb-ops-trim.exe`）。
  macOS / Linux 可用 Python 回退路径，但未实测——欢迎 PR 验证。
- **隐私**：零网络请求、零遥测；所有数据只在本机读写。
  账本只记录尺寸 / 时间 / 会话前缀；报告为本地单文件。
- **覆盖范围**：Grep / Glob / WebFetch / Bash 成功输出。Bash 退出码非 0 时整段透传（报错现场优先于省字节）。
  Read 默认不裁剪：设 `WB_OPS_READ_TRIM=1` 才对**超过 1500 行**的文件生效，保留首 200 / 尾 120 行并捞回
  `#include` / `def` / `class` / `struct` / `import` 等符号行。
- **已知边界**（设计使然，非缺陷）：守卫状态文件无锁（最坏情形为偶发重复或漏一次提醒）；
  守卫提醒依赖模型转述；每次会话首条消息恒静默。

## 目录结构

| 路径 | 作用 |
|---|---|
| `.codebuddy-plugin/marketplace.json` | 市场清单（官方插件市场入口，声明 wb-ops 插件） |
| `plugins/wb-ops/` | **插件本体**（安装时打包/复制的就是这个目录） |
| `plugins/wb-ops/src/trim.cpp` | 裁剪器原生实现（唯一需要编译的文件） |
| `plugins/wb-ops/bin/wb-ops-trim.exe` | 预编译二进制（静态链接，无 DLL 依赖） |
| `plugins/wb-ops/hooks/trim.py` | Python 回退实现（与 C++ 逐字节一致） |
| `plugins/wb-ops/hooks/hooks.json` | hook 注册（四事件：PostToolUse / SessionStart / UserPromptSubmit / PreCompact） |
| `plugins/wb-ops/hooks/ctx-guard.mjs` | 上下文守卫 v2（学习 / 三档提醒 / 压缩后检查） |
| `plugins/wb-ops/hooks/memory-guard.mjs` | 记忆守卫（SessionStart） |
| `plugins/wb-ops/hooks/precompact.mjs` | 压缩保留清单注入（PreCompact） |
| `plugins/wb-ops/report.py` | 报告生成器（纯标准库） |
| `plugins/wb-ops/commands/report.md` | `/wb-ops:report` 斜杠命令 |
| `install_wb_ops.py` | 一键安装 / 更新 / 卸载 / 状态 / 清理（脚本方式） |
| `build.sh` | 重新编译原生裁剪器（改 `src/trim.cpp` 后） |
| `test_*.py` | 测试：一致性 61 + hook 31 + 报告 115 + 稳定性 274 |
| `docs/` | 机制调研、设计文档与测试报告 |

## 开发

```bash
# 编译（改 plugins/wb-ops/src/trim.cpp 后；需要 MSYS2 MinGW-w64）
bash build.sh

# 测试（520 项）
python test_hook.py && python test_parity.py && python test_stability.py && python test_report.py
```

改动代码后的发布流程：升版本号（`plugins/wb-ops/.codebuddy-plugin/plugin.json`、根目录
`.codebuddy-plugin/marketplace.json`、`install_wb_ops.py` 里的 `VERSION` 三处保持一致）
→ `python install_wb_ops.py sync`。新版本按会话进程粒度生效，无需重启应用。

## 文档索引

- [`docs/AUTO-COMPACT-FEASIBILITY.md`](docs/AUTO-COMPACT-FEASIBILITY.md) —— 宿主自动压缩机制的完整调研（守卫 v2 的设计依据）
- [`docs/DASHBOARD-FEASIBILITY.md`](docs/DASHBOARD-FEASIBILITY.md) —— 报告后台的数据源证据链与设计
- [`docs/OPTIMIZATION-REVIEW.md`](docs/OPTIMIZATION-REVIEW.md) —— 裁剪算法的优化历程（0.2.2 → 0.3.x）
- [`docs/SAVING-REPORT.md`](docs/SAVING-REPORT.md) —— 节省效果的端到端实测

## License

[MIT](LICENSE)
