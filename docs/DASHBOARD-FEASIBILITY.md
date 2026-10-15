# wb-ops 节省统计与 Token 分析后台 —— 可行性评估

日期：2026-10-15 ｜ 评估对象：为 wb-ops 增加"节省总结 + token 使用分析"后台

**结论：高度可行，无技术阻塞。** 核心数据链路已逐项实测验证；建议分三阶段落地，MVP（采集 + 静态报告）约 1.5 个开发会话。

---

## 一、结论摘要

1. **wb-ops 恰好站在唯一的"精确节省数据"采集点上**：只有它在裁剪时同时看到原始大小与裁剪后大小。宿主 audit-log 和 transcript 都只记录裁剪后的结果，无法反推"省了多少"。
2. **hook 输入自带关联键**：已在宿主 CLI 代码（`cli/dist/codebuddy-lite-wb.mjs` 的 `convertToSdkInput`）中确认，PostToolUse 输入含 `session_id`、`tool_use_id`、`cwd`、`transcript_path` —— 记账天然可关联到会话与项目，无需猜测。
3. **宿主侧已有现成 token/积分数据**：`workbuddy.db` 的 `session_usage.credit_json` 按 `conversationRequestId` 记录积分消耗（key 语义已在 transcript 中验证闭环）；`sessions` 表提供标题/项目/模型维度。
4. **可实现为零第三方依赖**：C++ 标准库（记账）+ Python 标准库（聚合、报告），与项目现有哲学一致。

---

## 二、数据源盘点（均已实测验证）

| # | 数据源 | 内容 | 能回答的问题 | 状态 |
|---|--------|------|--------------|------|
| A | wb-ops hook 记账（**待建**） | 每次裁剪：时间、session_id、tool、裁剪前/后字节 | 省了多少、省在哪 | 采集点已就位，缺结构化记账 |
| B | `~/.workbuddy/workbuddy.db`（只读可访问） | `session_usage`(used/size/credit_json)、`sessions`(cwd/title/model) | 积分消耗、上下文占用 | 13 条 usage / 66 个会话 |
| C | transcript JSONL `~/.workbuddy/projects/<slug>/<sessionId>.jsonl` | 每行含 timestamp / type / role / sessionId / cwd / requestId | 把积分归属到天/项目、对话体积构成 | 可读，格式已确认 |
| D | audit-log JSONL（次要） | 命令安全审计（commandPreview 等） | Bash 调用频率（Bash 无 hook 的补位参考） | 可读 |

### 关键证据

- **hook 输入结构**（宿主 CLI `convertToSdkInput`）：
  `{hook_event_name, session_id, transcript_path, cwd, permission_mode?, tool_use_id?, hook_specific_output?}` + `tool_name / tool_input / tool_response`。
- **credit_json 的 key = `conversationRequestId`**：如 `0f1e2d3c4b5a...`，在对应会话 transcript 中作为 `providerData.conversationRequestId` 出现（实测 185 处引用），该值伴随 timestamp，可把积分精确归属到"天/项目/会话"。
- **`session_usage` 样本**：`used=41390 / size=300000`；`credit_json` 为 `{requestId: credits}`，单请求 0.42 ~ 16.07 不等。
- **会话归属**：`session_usage.session_id` = transcript 文件名 = hook 输入 `session_id`，三方对齐，join 无歧义。

---

## 三、推荐架构（三段式，零依赖）

```
[采集] trim.cpp / trim.py（+约 50 行）
    裁剪成功时 → append 一行 JSONL（约 150 B）
    落点：~/.workbuddy/wb-ops-data/saves-YYYYMM.jsonl
        ↓
[聚合] report.py（新增，纯标准库）
    JSONL + workbuddy.db（只读）+ sessions 表 join
        ↓
[呈现] dashboard.html（单文件，内联 CSS/SVG，无 CDN）
    可选 --serve：localhost 常驻面板（http.server 轮询）
```

### 记账行设计（最小化落盘）

```json
{"t":1792000000000,"s":"a1b2c3d4","tool":"Grep","b":123456,"a":4567}
```

- `t` 毫秒时间戳；`s` session_id 前 8 位（碰撞概率可忽略）；`b/a` 裁剪前/后字节。
- **cwd 不落盘**：报告层用 session_id join `sessions` 表获得项目维度 —— 省空间、免冗余、不泄漏路径。
- **失败静默**：任何记账异常不得影响裁剪主流程（与现有 hook "exit 0 恒成立"哲学一致）。
- `WB_OPS_STATS=0` 环境变量可整关统计。

---

## 四、v1 功能清单（报告页内容）

1. **节省总览**：今日 / 本周 / 累计节省字节、节省率、覆盖调用数
2. **趋势图**：近 14 天每日节省（柱状）
3. **维度拆解**：按工具（Grep/Glob）、按项目（cwd）、按会话 Top N
4. **积分分析**：按天 / 项目 / 会话的 credit 消耗与分布
5. **上下文健康**：`used/size` Top 会话，>60% 高亮 —— "该换会话"从经验变成数据
6. **明细表**：最近 N 笔裁剪记录（时间/会话/工具/前→后字节）
7. **口径脚注**：覆盖范围声明 + 估算说明（字节→token 按 3.5 B/token 估算，可配置）

---

## 五、关键设计决策

1. **数据目录必须放插件外**（`~/.workbuddy/wb-ops-data/`）：插件经历"开发目录 → 市场副本 → cache 快照"三级复制，sync / 版本升级都会覆盖或分裂目录（trace.log 已被 SYNC_IGNORE 排除，stats 数据同理绝不能留在插件内）。
2. **双实现一致性**：trim.cpp 与 trim.py 都要实现记账，输出格式必须逐字节一致；`test_parity.py` 增加记账路径用例。
3. **口径诚实**：积分 ≠ token，官方无换算公式。报告并列"字节口径"（精确、可加总）与"积分口径"（官方记账），不做伪精确换算。
4. **并发写兜底**：单行单次 write + append 模式；解析侧跳过坏行（多 hook 进程并行时容错）。
5. **覆盖面显式声明**：当前仅 Grep/Glob（宿主不下发 Bash 输出正文）；audit-log 可统计 Bash 频次作参考；WebFetch/WebSearch 探针若判定可行，接入后自动纳入统计。

---

## 六、风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| hook 热路径 I/O 开销 | 低 | 单行 append 约 1ms，相对端到端 55-60ms 可忽略；可开关 |
| 多进程并发写交错 | 低 | 单行单 write + 解析跳过坏行 |
| 宿主字段属实现细节，版本升级可能变 | 中 | 字段缺失时全量降级（只记时间 + 尺寸）；不硬依赖 transcript_path |
| sqlite 读写失败 / 锁 | 低 | `mode=ro` 只读 + busy_timeout；失败降级为只读 JSONL 出报告 |
| 数据增长 | 低 | 按月分文件，几百次/天 ≈ 几十 KB/月 |
| 隐私 | — | 不落任何输出内容，仅尺寸 / 时间 / 短 id |

---

## 七、工作量与路线

| 阶段 | 内容 | 预估 |
|------|------|------|
| P1 | 采集：trim.cpp + trim.py 记账、parity/单测扩展、编译 | 0.5 会话 |
| P2 | 聚合 + 报告：report.py + dashboard.html | 1 会话 |
| P3（可选） | `--serve` 实时面板 + "该换会话"提醒联动 | 0.5 会话 |

---

## 八、默认决策

1. 记账**默认开**还是默认关？（建议默认开，留一个可关开关）
2. 呈现形态：静态报告 / 常驻面板 / 两者兼有？（建议先静态，`--serve` 作为开关）
3. 数据保留：按月轮转 + 永久保留？（体量极小，建议保留）
4. 是否顺带做"上下文占用 >60% 提醒"？（可与现有会话卫生习惯联动）
