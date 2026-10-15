# wb-ops 自动压缩上下文 —— 可行性评估与多模型时机策略

> 调研日期：2026-10-15（多模型修订版）· 对象：WorkBuddy 5.8.2（workbuddy-desktop）
> 证据来源：宿主 CLI 源码逆写（`resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs`）、产品配置缓存、daemon/renderer 日志、真实会话 transcript 实测

---

## 一、结论先行

1. **可行——宿主已内置完整的自动压缩系统，当前配置下已开启并已实际工作。**
2. **但触发点因模型而异**（本轮核心修正）：官方目录模型按「窗口 × 50%」触发（deepseek 300K → **150K**）；自定义模型（models.json 引入，如 gpt-6.1-sol）没有窗口定义，走 fallback 路径 → **固定 200K 触发，且不受百分比设置影响**。
3. **实测对照**（两条会话铁证）：
   - `deepseek-v4.1-flash`（官方目录 300K）：149K 触发 pre-message-auto ✓（会话 sess-a，共 10 次压缩）
   - `gpt-6.1-sol`（自定义无窗口）：122K 未触发、201.1K 才被 emergency 保命压缩 ✓（会话 sess-d / sess-c）
4. **wb-ops 应做的两件事**（对所有模型通用）：
   - **提升压缩质量**：PreCompact hook 注入结构化压缩指令（保留任务状态/文件路径/未完成事项）；
   - **修正提醒水线**：ctx-guard 当前固定 200K，对 deepseek 太高（宿主 150K 已压）、对 gpt 又贴着宿主阈值——应**按模型动态计算**。
5. 介入通道（按可行性排序）：

   | 通道 | 能做什么 | 可行性 |
   |---|---|---|
   | PreCompact hook | 给压缩过程注入"保留什么"的指令（所有模型通用） | ✅ 立即可做 |
   | `/autocompact <n>` 设置 | 自定义模型：**n 直接=触发点**；官方模型：min(n, 窗口)×pct | ✅ 用户可用（手动） |
   | 桌面 RPC `configSetPreMessageCompactPct` | 官方模型有效；**对自定义模型无效**（见 §2.2） | ⚠️ 可达性待验证 |
   | mods（function hooks）`session.compact()` | 任意时刻程序化主动压缩 | 🔶 需开 env 开关（当前关闭） |
   | UserPromptSubmit hook 触发压缩 | — | ❌ 不可行（输出契约无此字段） |
   | 改写 transcript jsonl | — | ❌ 不可行（破坏宿主内部状态一致性） |

---

## 二、宿主压缩机制全貌（源码 + 配置实证）

### 2.1 三档触发类型

源码枚举 `CompactType`（`compactType` 字段写入会话 transcript）：

| 类型 | 值 | 触发者 |
|---|---|---|
| 手动 | `user-command` | 用户输入 `/compact [指令]` |
| 消息前自动 | `pre-message-auto` | 用户提交消息时，token 越线自动压缩 |
| 紧急自动 | `emergency-auto` | 请求超限/内存压力时的保命压缩（漂移的最后防线） |

### 2.2 触发点解析链（多模型差异的根源）

```
有效预算 budget = resolveEffectiveContextBudget(
    overrideContextWindow,        // ① 会话级覆盖（UI "Context" 选择器，官方模型可选 300K/600K/1M）
    model.contextWindow,          // ② 模型目录定义（官方模型有 defaultLength + supportedLengths）
    model.maxInputTokens          // ③ 模型输入上限
)
解析优先级：① override（且合法）→ ② defaultLength（且合法）→ ② supportedLengths 最小值（≥2 个时）
           → ③ maxInputTokens → **全无则 undefined**

触发点 P = budget 存在 ? min(autoCompactWindow设置值, budget) × pct
                     : clamp(autoCompactWindow设置值 ?? 200_000, 100_000, 1_000_000)
                     //  ↑ 注意：此分支**不乘 pct**，直接把窗口值当触发点
```

- `pct` 优先级：会话级 `meta.preMessageCompactPct`(0=禁用) → env `CODEBUDDY_PRE_MESSAGE_COMPACT_PCT` → 产品配置 `inputTokens.preMessage`（**实测 0.5**）→ 0.8
- `autoCompactWindow`：`/autocompact 400k` 或设置项（clamp 100K–1M）；env `CODEBUDDY_AUTO_COMPACT_WINDOW` 可覆盖

**产品配置**（`~/.workbuddy/cache/acc-product-config-v3.json`）：

```json
{
  "enablePreMessageCompact": true,
  "tokenUsageThresholds": {
    "inputTokens": { "warning": 0.6, "critical": 0.7, "emergency": 0.9, "preMessage": 0.5 },
    "compact": { "emergency": 0.5, "modelOverrides": { "deepseek": 0.5 } }
  }
}
```

### 2.3 多模型触发点对照（本轮实测校准）

| 模型 | 类型 / 来源 | budget | 自动触发点 | 实测验证 |
|---|---|---|---|---|
| `deepseek-v4.1-flash` | 官方目录（300K defaultLength） | 300K | **150K**（300K×0.5） | sess-a：149K 触发 pre-message-auto ✓ |
| `custom-local:gpt-6.1-sol` | models.json 自定义（无窗口字段） | undefined | **200K**（默认，不乘 0.5） | sess-d：122K 未触发 ✓；sess-c：201.1K 由 emergency 兜底 ✓ |
| `custom-local:deepseek-v4.1-flash` | models.json 自定义（同一模型另一入口） | undefined | 200K | sessions 表 window=NULL 佐证 |
| 官方 300K 系列（glm-5.3-flash / kimi-k3-1 / step-5-preview 等） | 官方目录 | 300K | 150K | 同类推定 |
| 官方大窗口系列（hy4-preview：300K/600K/960K 可选；glm/kimi：可达 1M） | 官方目录 | 视会话选择 | 所选窗口×0.5 | supportedLengths 佐证 |
| 内置默认档（fast/balanced-model） | 内置目录（maxInputTokens=200K） | 200K | 100K | 目录定义佐证 |

> ⚠️ **注意同名双份模型**：`models.json` 里自定义的 `deepseek-v4.1-flash` 与官方目录同名；UI 中前者显示为 `custom-local:` 前缀。**两者触发点不同**（300K×0.5=150K vs 200K 固定）。历史会话两种都出现过。

### 2.4 检查时机与「漂移」问题

`pre-message-auto` 只在**用户提交消息那一刻**检查：

- 两条用户消息之间穿插多轮工具调用，token 会涨过触发点而无人察觉；
- 直到下一条消息提交才压缩 → **实测漂移：deepseek 会话 149K–226K**（gpt 会话因阈值固定，漂移表现为「撞 emergency」）；
- 压缩在用户消息前**同步阻塞**（`compacting before user message`）→ 用户感知"这条消息特别慢"。

源码里还有一条 `cache-expired` 路径（闲置 ≥1h 缓存过期 + 水位 ≥50% 触发点），但**仅在产品未启用阈值压缩（S=false）时生效**；当前配置 `enablePreMessageCompact=true`，该路径不会走。「趁缓存过期再压」只对**主动压缩方案（mods）**有参考价值。

### 2.5 两级压缩：先用免费的，不够才花钱

`doPreMessageCompactionCore` 流程：

1. **工程压缩（纯本地、零模型成本）**：`HistoryEngineeringCompactor.compact()` 把历史规则化压缩为 `<cb_summary>` 文本；
2. **充分性检查**：压缩结果 ≤ 预算 × 15%（`CODEBUDDY_ENGINEERING_COMPACT_SUFFICIENCY_PCT` 可调）→ **跳过 LLM 摘要**；
3. 否则才调 LLM 摘要（兜底）。

压缩保留结构：全历史摘要（`cb_summary`，含 `<omitted />` 标记）+ 最近 5 轮原文（`takeRecentRounds(5)`）。

### 2.6 实测压缩效果（会话 sess-a，deepseek）

| 序次 | 压缩前 tokens | 压缩后 tokens |
|---|---|---|
| 1 | 183.4K | 48.6K |
| 2 | 152.8K | 60.0K |
| 3 | 186.2K | 81.0K |
| 4 | 161.0K | 88.3K |
| 5 | 226.4K | 36.7K |
| 6 | 151.2K | 48.2K |
| 7 | 184.5K | 66.8K |
| 8 | 164.5K | 78.3K |
| 9 | 202.7K | — |
| 10 | 148.8K | — |

统计口径：6 × pre-message-auto + 4 × user-command（逐条类型未对应）；**单次回收约 60K–190K，均值 ≈100K tokens**。

---

## 三、介入通道详解

### 通道 1：PreCompact hook（推荐立即实施）

- **契约**（源码实证）：stdin 传入 `{session_id, transcript_path, cwd, trigger:"manual"|"auto", custom_instructions}`；hook 的 **stdout 内容成为 `preCompactInstructions` 注入压缩过程**。
- **能力边界**：不能阻止/触发压缩，只能给压缩过程"下指令"。
- **价值**：直接提升压缩质量（性能维度），**对所有模型一视同仁**，零风险。

### 通道 2：触发时机的用户级杠杆（按模型区分——本轮核心修正）

| 杠杆 | 官方目录模型 | 自定义模型（custom-local） |
|---|---|---|
| `/autocompact <n>` | min(n, budget) × pct（n≥budget 时无效） | **触发点 = n 本身**（不乘 pct）✓ |
| 会话 pct（桌面 RPC / UI） | 有效（150K 可调） | **无效**（fallback 分支不读 pct） |
| env `CODEBUDDY_PRE_MESSAGE_COMPACT_PCT` | 有效 | 无效（同上） |
| env `CODEBUDDY_AUTO_COMPACT_WINDOW` | 有效（窗口上限） | 有效（=触发点） |
| UI "Context" 选择器（会话覆盖 600K/1M） | 有效（budget 变大 → 触发点变大） | 不适用（无 supportedLengths） |
| models.json 补 `maxInputTokens` 字段 | — | **可激活标准 budget 路径**（字段全量透传，待实测；补后走 min(设置值, 字段值)×pct） |

**给 gpt-6.1-sol 的具体建议**：它走 d1api.xin 中转，真实窗口未知。宿主保守假定 200K。若中转实际模型窗口更大（如 400K），现在等于**白白浪费一半窗口**——可用 `/autocompact 400k` 提高触发点；反之担心性能则 `/autocompact 150k` 提前压。这是对自定义模型最直接、立即生效的调节手段。

### 通道 3：桌面 RPC / mods（同前版结论）

- 桌面 RPC `configSetPreMessageCompactPct`：桌面端例行配置同步在用（daemon.log 每会话 3–6 条），外部可达性待专项验证；且**仅对官方目录模型有实际效果**。
- mods `session.compact()`：需 `CBC_ENABLE_FUNCTION_HOOKS=1`（当前关闭），是消除漂移的唯一主动方案；与模型无关。
- UserPromptSubmit hook 无法触发压缩（输出契约只有 `additionalContext`）；改写 transcript 会破坏宿主一致性——均不可行。

---

## 四、时机策略：速度 × 性能 × 价格（多模型版）

### 4.1 成本模型（不变）

| 维度 | 压缩的代价 | 压缩的收益 |
|---|---|---|
| **价格** | ①LLM 摘要（多被工程压缩挡住，≈0）；②前缀缓存失效，首轮全量 prefill | 之后每轮输入从 C 降到 ~1/4C，轮数越多省越多 |
| **速度** | ①同步阻塞（用户等待）；②首轮 cache miss（TTFT 上升） | 之后每轮 prefill 变小 |
| **性能** | 信息损失；压得越晚内容越多，越可能触发 LLM 摘要、丢细节越多 | 近处内容注意力更集中 |

### 4.2 甜点水位（按模型换算）

**通用原则：甜点区 = 自动触发点的 80%–100% 区间内的任务边界**——宿主反正会在触发点自动压，wb-ops 的作用是在触发**之前**提醒用户在任务边界主动压（压得更干净、避免漂移撞线 + 同步阻塞）。

| 模型类型 | 自动触发点 | 建议主动压缩窗口 | wb-ops 提醒线建议 |
|---|---|---|---|
| 官方 300K（deepseek-v4.1-flash 等） | 150K | **120K–150K** | ≈120K（触发点×0.8） |
| 官方 600K/1M（会话选大窗口） | 300K/500K | 240K–500K | 触发点×0.8 |
| 自定义（gpt-6.1-sol） | 200K（默认） | **160K–200K** 或先用 `/autocompact` 重设触发点 | ≈160K |
| 内置默认档（200K） | 100K | 80K–100K | ≈80K |

四条时机规则（跨模型通用）：

1. **任务边界优先**：刚交付成果、待办清空、话题切换——此时未完成状态最少，压缩损失最小。
2. **频率约束**：两次压缩间隔 ≥100K 增量（≈15 轮），避免反复重建缓存。
3. **剩余 <3–5 轮不压**：固定成本收不回来。
4. **不要等漂移**：漂移意味着压缩时历史更长（更贵、丢更多）+ 用户消息前同步阻塞（体感差）。

### 4.3 反模式清单

- ❌ 水满再压（226K）：体感最差，且很可能触发 LLM 摘要（花钱）。
- ❌ 刚压完又压：缓存反复重建，成本净增。
- ❌ 工具链中途压：丢中间状态，任务恢复成本高。
- ❌ 对自定义模型设会话 pct：**根本不生效**（源码 fallback 分支不读 pct）。
- ❌ 无视模型差异用同一个水位提醒：对 deepseek 太晚、对 gpt 太早。

---

## 五、推荐路线（对 wb-ops）

**阶段 1（✅ 已于 0.6.0 落地，2026-10-15）**
- ✅ 新增 `PreCompact` hook（hooks/precompact.mjs）：stdout 输出结构化压缩指令（保留任务状态/关键决策/文件路径/未完成事项）——所有模型通用。
- ✅ **ctx-guard v2（hooks/ctx-guard.mjs 重写）**：行为学习（min auto 样本×0.85=强档线）+ 双档提醒 + 压缩后水位检查（>100K → 建议新建会话）。设计细节见 §5.1。

### 5.1 ctx-guard 阀门设计：不依赖模型识别的最优解

> 问题：如果不假定插件"认识"当前模型（新模型/自定义模型无法穷举窗口），提醒阀门怎么设最合理？

**事实先行**：插件其实"能知道"当前模型——transcript 每条记录都带 `model` 字段，切模型后可立即跟上；产品配置缓存有官方窗口表；压缩事件（`compactionSeq`/`compactType`）和水位轨迹（`prompt_tokens`）也全在 transcript 里。**但鲁棒的设计不应依赖识别**（模型名→窗口映射要维护、同模型可选不同窗口、自定义模型窗口未知），而应依赖**可观察的行为**。

**三层优先级（从最优到兜底）**：

```
提醒线 = ① 该模型自动压缩点的学习值 × 0.85
      ?? ② 模型目录窗口 × 0.5 × 0.85（能在目录里查到时）
      ?? ③ 130K（一无所知时的数学最优单值）
```

**① 行为学习（推荐主路径，零模型知识、零维护）**
- 学习信号：transcript 尾部出现「水位骤降 ≥30%」或新 `compactionId`/`compactionSeq` → 判定发生压缩；
- 样本过滤：只采 `compactType ∈ {pre-message-auto, emergency-auto}`，**排除 `user-command`**（手动压缩的水位不是触发点证据）；
- 触发点估计：取各样本中较低值（首次触发值最接近真实触发点，漂移只会向上）；
- 实测验证该算法收敛：
  - deepseek 样本 [149K, 151K, 152K, ...226K] → 估计 ≈149K → 提醒线 ≈127K ✓
  - gpt-6.1-sol 样本 [201.1K] → 估计 ≈201K → 提醒线 ≈171K ✓
  - 与手算甜点（各自触发点 −20K~30K）吻合。

**② 查表辅助（可选增强）**：`acc-product-config-v3.json` 的 `models[].contextWindow.defaultLength` 自动更新，新官方模型无需改代码。

> **自定义模型的"窗口"可知性（10-15 补测实证）**：`custom-local:*` 的真实窗口**磁盘上不存在**——sessions.context_window 官方模型写 300000，5 个自定义模型**全为 NULL**；models.json 条目无窗口字段（全集：id/name/vendor/url/apiKey/supports*/reasoning）；transcript 亦无窗口字段。**宿主自己也不知道**，它只用管理值兜底：`clamp(settings.autoCompactWindow ?? 200K)`（settings.json 未设该键 → 所有自定义模型统一 200K，与实测 201K 吻合）。好在**管理值是全局单值且可读**——插件对 custom-local:* 无需查表：直接读该键、或默认 200K；且不乘 pct、不随模型变，反而比官方模型的逐模型查表更简单。

**③ 冷启动兜底 130K 的数学推导**（"真的一无所知"时的单值最优）：
- 约束：提醒需早于自动压缩（行动余量 ≥10K）、又不宜过早（提前 >70K 会被忽视）；
- 对 deepseek（触发 150K）：V ∈ [80K, 140K]；对 gpt（触发 200K）：V ∈ [130K, 190K]；
- 交集 = **[130K, 140K]**，取下沿 130K（更保险：宿主检查时还会叠加 trailing tool tokens，实际触发略早于名义值）。

**双档设计（配合 STEP=40K）**：130K 轻提醒 → 170K 强提醒 → 每 40K 递进。
- 对 deepseek：130K 提前 20K（≈3–4 轮）提醒一次即可；压缩后水位回落，后续档位自然沉寂；
- 对 gpt：130K 是"预通知"，170K 是提前 30K 的黄金强提醒；
- 文案分档：低档 =「任务边界可考虑压缩/切换」；高档 =「建议开新会话收尾」。

**反模式**：单值一刀切、且只提醒"开新会话"（现状 200K）——对 deepseek 提醒时宿主已压完（无意义），对 gpt 又紧贴其触发点（来不及行动）。

**阶段 2（专项调研后）**
- WBBridge RPC 可达性 → 动态 `preMessageCompactPct`（**仅官方模型有效**）。

**阶段 3（实验性）**
- `CBC_ENABLE_FUNCTION_HOOKS=1` + mods `session.compact()`：工具轮间隙主动压缩，消除漂移；与模型无关，是唯一能"提前于宿主触发点"的主动方案。**对 gpt 这类自定义模型价值最大**（宿主兜底触发太晚/太机械）。

---

## 附录：证据索引

| 证据 | 位置 |
|---|---|
| 触发解析链 / 预算 fallback / 默认常量（tI=2e5, tO=1e5, tx=1e6, n7=1h, n9=0.5） | `codebuddy-lite-wb.mjs`：`resolveEffectiveContextBudget`、`resolveCompactTriggerAt`、`getAutoCompactWindow` |
| pre-message 拦截器全文（L/q 双路径、检查时机、缓存过期条件） | 同文件 `PreMessageCompactInterceptor`（源偏移 ~7742449） |
| 两级压缩 / 充分性 15% | `doPreMessageCompactionCore`、`getEngineeringCompactSufficiencyRatio` |
| 产品真实配置（pct=0.5、modelOverrides） | `~/.workbuddy/cache/acc-product-config-v3.json` |
| 自定义模型字段全量透传 | 同文件 `CustomModelsProductProvider`（spread `{disabled:!1,...t}`） |
| 同名双份模型证据 | `~/.workbuddy/models.json`（2 个自定义）+ `sessions` 表（window=300000 vs NULL） |
| deepseek 实测（10 次压缩、149K 触发、漂移 149–226K） | `~/.workbuddy/projects/<项目>/sess-a-*.jsonl` |
| gpt 实测（122K 未触发；201.1K emergency；0 次 pre-message） | `~/.workbuddy/projects/*/sess-d-*.jsonl`、`sess-c-*.jsonl`（另一项目） |
| RPC 调用记录 | `~/.workbuddy/logs/daemon.log`（`configSetPreMessageCompactPct`） |
| PreCompact hook 契约 / mods | `codebuddy-lite-wb.mjs`：`executePreCompactHooks`、`bootstrapFunctionHookMods` |
