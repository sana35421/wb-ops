# wb-ops 1.2.0 测试与改造报告

日期：2026-10-18 ｜ 宿主：WorkBuddy 5.8.x（Windows）｜ 上一版：1.1.0

> 文中路径均相对插件本体目录 `plugins/wb-ops/`（仓库根另有 `install_wb_ops.py` 与测试脚本）。

本版只有一件事：**把 Read 也接进默认裁剪，但只接数据类文件**。
1.1.0 之后剩下的最大浪费就是 Read —— 长日志、长 CSV、长 jsonl 被整段塞进上下文，
而 Read 在 1.1.0 里是完全不参与的（装机时 `--with-read` + 手动环境变量，实际上等于没人开）。

难点不在于裁，而在于**不能为源码文件白付进程开销**。这一版找到了宿主里那个能解决这个问题的开关。

---

## 一、为什么以前不敢默认开 Read

- Read 的典型用法是「读源码准备改」，截断会直接破坏编辑流程；
- 1.1.0 里 matcher 是 `Grep|Glob|WebFetch`，把 `Read` 加进去 = **每一次读文件**都 spawn 一次 hook
  （bash + exe 实测约 230–270ms/次），哪怕这个文件只有 20 行；
- 而真正需要裁的只是「日志/表格/清单」这类数据文件，源码文件价值密度高、不该动。

所以 1.1.0 只能做成「装时 `--with-read` + 运行时 `WB_OPS_READ_TRIM=1`」双开关 —— 双开关等于没人开。

## 二、关键发现：host hook 配置里的 `if` 字段（未文档化）

逆宿主 bundle（`cli/dist/codebuddy-lite-wb.mjs`）确认：

```js
filterHooksByIfRule(t, i) { … x$.matches(t.if, { toolName: n, toolInput: s }) … }
matches(…)  // Read/Write/Edit/MultiEdit → 取 tool_input.file_path（或 path）
            // → xN.N.matchFileRule(path, argPattern)
static matchFileRule(t, i) {
  return t = normalizePath(t), i = normalizeFilePathPattern(i),
         picomatch(t, i, { dot: true, nocase: true, matchBase: true });
}
```

四个要点决定了这条路可行：

1. **`if` 在 spawn 之前过滤** —— 不匹配的 hook 根本不会启动，源码文件是真正的零开销；
2. `matchBase: true` —— 模式只比文件名，`*.log` 能匹配任意目录下的 `a.log`；
3. `nocase: true` —— `.LOG` / `.Csv` 一样命中；
4. `normalizeFilePathPattern()` 对**以 `*` 开头**的模式不做 resolve
   （源码：`t.startsWith("*") || t.startsWith(".") || …` 才跳过 `path.resolve`），
   所以 `*.{log,jsonl,…}` 不会被错解析成「相对 cwd 的绝对路径」。

另外两个配套事实：`isValidHookConfig()` **不校验** `if`（不会被丢弃）；
`deduplicateHooks` 的去重键是 `sourceId|type|shell\0command\0if`，**含 `if`** ——
所以两组命令字面完全一样也不会被合并成一组。

## 三、改动清单

### 3.1 `hooks/hooks.json`：PostToolUse 拆两组

```json
"PostToolUse": [
  { "matcher": "Grep|Glob|WebFetch", "hooks": [ {…同一 command…} ] },
  { "matcher": "Read",
    "if": "Read(*.{log,jsonl,ndjson,csv,tsv,out,err,txt})",
    "hooks": [ {…同一 command…} ] }
]
```

两组共用同一条命令（`wb-ops-trim.exe`，退化到 `trim.py`），无新增脚本。

### 3.2 `hooks/trim.py` / `src/trim.cpp`：Read 分三态 + `min_lines` 参数化

| Read 目标 | 模式 | 门槛 | 捞回什么 |
|---|---|---|---|
| 数据类（`DATA_EXTS`） | 普通关键字 | **>200 行或超预算** | `error` / `warning` / `失败` / `异常` … |
| 源码类（且 `WB_OPS_READ_TRIM=1`） | 符号行 | **>1500 行** | `#include` / `def` / `class` / `struct` / `import` … |
| 其余 | — | — | 直接透传（不裁） |

- `trim(text, read_mode, min_lines)`：门槛从常量改成参数，两条判定分支保持原语义
  （普通档「行数 ≤ 门槛 **且** 字节 ≤ 预算」才放过；Read 档「行数 ≤ 门槛 **或** 字节 ≤ 预算」就放过）。
- `is_data_file(path)`：只看扩展名，反斜杠归一化、大小写不敏感；
  与 hooks.json 的 `if` 清单、`src/trim.cpp` 的 `DATA_EXTS` **三处同源**（`test_hook.py` 用例 1d 硬校验）。
- `tool_input` 的键 `file_path` 与 `path` 都认（不同工具键名不同）。

### 3.3 安装脚本 `install_wb_ops.py`：`--with-read` 语义收敛成单开关

以前：`--with-read` 只改 matcher，还得用户自己设 `WB_OPS_READ_TRIM=1`。
现在：`--with-read` **直接改落盘的 hooks.json** —— 去掉 Read 组的 `if`，并把
`export WB_OPS_READ_TRIM=1; ` 拼到该组命令前面。用户一个参数，不需要碰环境变量（幂等，不会重复拼）。

不加 `--with-read` 时：数据类文件照样裁，源码文件根本不会触发 hook。

## 四、测试结果

| 套件 | 用例 | 结果 |
|---|---|---|
| `test_parity.py`（双实现逐字节一致 + 记账 + 预算 + 输入链） | 72 | 全通过 |
| `test_hook.py`（端到端，含 Read 分档与 hooks.json 一致性） | 43 | 全通过 |
| `test_stability.py`（边界 / fuzz / 确定性 / 并发 / 长跑） | 275 | 全通过 |
| `test_report.py`（demo / 降级 / 清理 / 隔离 / 实时服务） | 115 | 全通过 |
| `test_mod.py`（归档的压缩督导） | 34 | 全通过 |
| **合计** | **539** | **全通过** |

本轮新增用例（都是新增能力，没有改旧语义）：

- `test_parity.py` 34 → 40 个对照用例，新增 6 条：`.log` 数据类裁剪、`.log` 未达 200 行透传、
  `.py` 默认透传、`.py` 开闸按符号行、`.LOG` 大写扩展名 + 反斜杠路径、`tool_input.path` 键。
  双实现输出 40 / 40 完全一致（含标记文案与行号区间）。
- `test_hook.py` 新增用例 1c（4 条：数据类裁剪 / 短文件透传 / 源码默认透传 / 源码开闸裁剪）
  与用例 1d（5 条：Read 组存在、带 `if`、`if` 清单 == `DATA_EXTS`、默认不带 `WB_OPS_READ_TRIM`、
  普通组 matcher 未含 Read）。1d 是**防漂移**的：三处清单一旦不一致，就会退化成「hook 不启动」或
  「启动了却按源码档处理」这种无声错配。
- 原用例 32 更名「Read 非数据类且无路径（透传）」，断言未变。

## 五、实测（本机，2026-10-18）

| 场景 | 原文 | 裁剪后 | 省 |
|---|---|---|---|
| `Read` 600 行 `.log`（中段 1 条 ERROR） | 24,509 B | 6,736 B | 72.5% |
| 同上，走安装副本的完整 hook 命令链 | 19,678 字符 | 7,495 字符 | 61.9% |
| `Read` 600 行 `.jsonl` | 24,509 B | 6,736 B | 72.5% |
| `Read` 2000 行 `.py`（未开 `--with-read`） | 10,579 B | 透传 | — |
| `Read` 100 行 `.log` | 4,099 B | 透传 | — |
| `Read` 2000 行 `.py`（`--with-read`） | 52,826 字符 | 6,781 字符 | 87% |

- 关键字行保留验证：`ERROR connect timeout` 在裁剪结果中仍存在，标记形如
  `… [wb-ops 省略 438 行（第 78–516 行），以下为捞回的 1 条关键字行] …` + 末尾段标记。
- **源码文件零开销**：`if` 不匹配 → hook 不 spawn，实测无进程启动（这是本版能默认开的关键）。

## 六、遗留与不做的事

- **Bash 裁剪**：仍等宿主把正文放进 `tool_response`（1.1.0 已实证不可行，见 `TEST-REPORT-1.1.0.md` §二）。
- **Read 源码默认裁**：不做。源码要的是完整内容，默认裁会破坏编辑流程；想要的人用 `--with-read`。
- **扩展 `DATA_EXTS`**：`if` 的 glob 与两处 `DATA_EXTS` 必须同步改，`test_hook.py` 用例 1d 会拦住遗漏。
- **`if` 字段的宿主兼容性**：属未文档化能力，宿主升级后若校验变严可能被丢弃。
  表现是「Read 不再裁剪」，不会出错也不会崩；届时数据类文件仍可用 `--with-read` 兜底。

## 七、升级方式

```bash
git pull                                  # 或在插件市场页面刷新市场
python install_wb_ops.py sync             # 脚本方式安装的用这句
# 想让源码文件也裁（可选）：
# python install_wb_ops.py sync --with-read
```

matcher / `if` 属于 hook 配置，**按会话进程粒度生效**：新开会话加载 1.2.0，旧会话保持旧版。
