#!/usr/bin/env node
/**
 * wb-ops PreCompact hook —— 压缩指令注入
 *
 * 触发时机：会话进行压缩之前（手动 /compact、自动 pre-message、emergency 均会触发），
 *          宿主调用本 hook 并把 stdin 按通用 hook 协议传入 JSON
 *          （session_id / transcript_path / trigger 等；本脚本不依赖具体字段）。
 *
 * 契约（宿主源码实证，WorkBuddy 5.8.2）：
 *  - stdout 文本（trim 后）会作为「附加指令」以
 *    <system-reminder data-role="hook"> 注入本次压缩请求，指导摘要生成。
 *
 * 工作方式：输出一份「摘要保留清单」，让压缩摘要面向“继续工作”而非复述对话。
 * 设计原则：只读 stdin、只写 stdout；始终退出码 0；无状态、零依赖。
 */

import fs from 'node:fs';

const INSTRUCTIONS = [
    '压缩即将发生。生成摘要时请优先保留以下内容（按重要性排序），让摘要服务于“继续工作”而非复述对话：',
    '1. 当前任务与目标：用户要什么、进行到哪一步；',
    '2. 未完成事项与下一步计划（明确列出待办）；',
    '3. 关键决策与结论：选了什么、为什么（含被否决的方案与原因）；',
    '4. 落地细节：涉及的文件路径、命令、参数、接口与环境约定；',
    '5. 用户偏好与约束：用户明确表达过的偏好、纠正过的做法；',
    '6. 重要错误与教训：避免重复踩坑。',
    '可以丢弃：重复的调试过程、已完成的中间步骤细节、大段原始输出与日志。',
].join('\n');

try { fs.readFileSync(0, 'utf-8'); } catch { /* drain stdin */ }
try { process.stdout.write(INSTRUCTIONS + '\n'); } catch { /* ignore */ }
