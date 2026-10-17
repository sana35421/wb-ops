#!/usr/bin/env node
/**
 * wb-ops 记忆守卫（SessionStart hook）
 *
 * 触发时机：每次新会话启动（宿主自动调用；本脚本不读 stdin）
 * 工作模式（环境变量 WB_OPS_MEM_GUARD_MODE）：
 *  - auto（默认）：超限时注入「自动压缩」指令——会话首轮执行：
 *                  备份 → 压缩（目标 ≤ 阈值×0.7）→ 汇报；压不动时列删除候选清单请用户勾选
 *  - remind      ：超限时仅注入建议式提醒
 * 阈值（环境变量 WB_OPS_MEM_GUARD_LIMIT，默认 3500 字符；宿主约 4000 截断）
 *
 * 设计原则：
 *  - 守卫本身只读不写：绝不直接改记忆文件；压缩与备份由会话内助手执行（可回滚）
 *  - 永远正常退出：任何异常都静默吞掉，绝不阻塞或干扰会话启动
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const HARD_LIMIT = 4000; // 宿主把记忆注入系统提示时的截断线：超出部分会丢失
const SOFT_LIMIT = (() => {
  const raw = parseInt(process.env.WB_OPS_MEM_GUARD_LIMIT || '', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : 3500; // 默认 3500：留 500 字符缓冲
})();
const MODE = (process.env.WB_OPS_MEM_GUARD_MODE || '').trim().toLowerCase() === 'remind'
  ? 'remind'
  : 'auto'; // 默认 auto；非法值一律回退 auto
const TARGET = Math.max(1200, Math.round(SOFT_LIMIT * 0.7)); // 压缩目标：留足缓冲，避免压完即触发

/** 找出所有超限的用户级记忆文件，返回 [{path, chars}] */
function findOversizedMemoryFiles() {
  // 2026-10-17 修复：原来只扫 ~/.workbuddy，而新宿主 WorkBuddyAI 的用户目录在
  // ~/.workbuddy-ai，导致守卫在那边永远扫不到东西（静默失效）。改为扫描所有存在的宿主根。
  const homes = [];
  for (const name of ['.workbuddy-ai', '.workbuddy']) {
    const h = path.join(os.homedir(), name);
    try {
      if (fs.statSync(h).isDirectory()) homes.push(h);
    } catch {
      /* 该宿主不存在：跳过 */
    }
  }

  const out = [];
  for (const base of homes) {
    let names = [];
    try {
      names = fs.readdirSync(base);
    } catch {
      continue;
    }
    for (const n of names) {
      if (!/^user-.*-personal$/.test(n)) continue;
      const p = path.join(base, n, 'MEMORY.md');
      try {
        const chars = fs.readFileSync(p, 'utf-8').length;
        if (chars > SOFT_LIMIT) out.push({ p, chars });
      } catch {
        /* 目录不存在 / 文件缺失：跳过 */
      }
    }
  }
  return out;
}

/** 生成注入到会话的 system-reminder 文本 */
function buildContext(over) {
  const lines = [
    '[wb-ops 记忆守卫] 用户级记忆文件已接近/超过宿主注入上限（约 ' + HARD_LIMIT +
      ' 字符，超出会被截断注入、内容丢失）：',
  ];
  for (const f of over) lines.push('- ' + f.p + ' — ' + f.chars + ' 字符');

  if (MODE === 'remind') {
    lines.push(
      '建议在本次会话内顺手处理：',
      '1. 先用一行向用户提示此事，不必打断用户当前任务；',
      '2. 精简原则：跨项目必备内容留在本文件；项目细节移到对应项目记忆（如 D:/wb-ops/.workbuddy/memory/）；删除过期与重复内容；',
      '3. 精简前先把原文备份到同目录 MEMORY.backup.md；',
      '4. 完成后向用户报告：旧/新字符数，以及内容去向。'
    );
    return lines.join('\n');
  }

  // auto（默认）：自动压缩流程 + 压不动的兜底
  lines.push(
    '请立即自动执行压缩（无需等待用户批准，本次会话首轮完成即可）：',
    '1. 先重新读取该文件确认当前大小（避免与并行会话重复压缩）；',
    '2. 把原文完整备份到同目录 MEMORY.backup-<YYYYMMDD-HHMMSS>.md；同目录只保留最近 3 份备份，更旧的删除；',
    '3. 压缩到 ' + TARGET + ' 字符以内（留足缓冲）：',
    '   · 保留：跨项目必备信息（用户环境、行为约定、关键路径指引）；',
    '   · 迁移：项目专属细节 → 对应项目记忆目录（如 D:/wb-ops/.workbuddy/memory/）；',
    '   · 删除：过期、重复、已失效内容；',
    '   · 原则：只重组与迁移，不丢有效信息；宁可保留，不要误删；',
    '4. 完成后用一行汇报：旧 → 新字符数 + 内容去向；',
    '5. 兜底：若内容已全是必需（再压缩会丢关键信息）或压缩后仍超过 ' + SOFT_LIMIT +
      ' 字符 → 不要暴力删减，改为向用户列出「建议删除候选」清单（逐条附一句理由）请用户勾选；用户表示维持现状则遵从。'
  );
  return lines.join('\n');
}

function main() {
  const over = findOversizedMemoryFiles();
  if (over.length === 0) return; // 静默：无输出 = 不注入任何内容

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'SessionStart',
      additionalContext: buildContext(over),
    },
  }));
}

try {
  main();
} catch {
  /* 静默失败：守卫的问题绝不影响会话启动 */
}
