#!/usr/bin/env node
/**
 * wb-ops 上下文守卫 v2（UserPromptSubmit hook）
 *
 * 触发时机：用户每次提交消息时（宿主自动调用，stdin 传入 JSON，含 session_id / transcript_path）
 * 工作方式（三层职责）：
 *  1) 行为学习：尾部直读 transcript，从压缩记录（providerData.compactionSeq /
 *     compactType / compactionKind）采集「自动压缩发生时的水位」样本（仅 auto 类型，
 *     排除手动 /compact），取最小值作为该模型真实触发点 T 的估计。
 *  2) 三档提醒（v0.6.2 起）：强档线 = T×0.85（T 未知时 170K），轻档线 = 强档线 − STEP（未知时 130K）；
 *     水位越过实测压缩点 T 仍未触发压缩时升至第三档（band 3，点明"宿主只在发送消息时检查"
 *     的漂移机理）。跨档时向本轮注入 system-reminder；band 2/3 要求模型在回复【开头】提醒，
 *     band 1 仅末尾顺带提一句。发生一次压缩即开启新纪元，各档允许重新提醒一次。
 *     v1.3：band 3 措辞改为「先答后劝、落盘+摘要」——只暂缓展开新方向，不拒答；记忆路径按本机
 *     既有约定给出，不写死（.workbuddy/memory 与 .workbuddy-ai/memory 都有在用）。
 *  3) 压缩后检查：压缩发生后若水位仍 > POST_MAX（默认 100K），说明会话“底子”过重、
 *     后续将频繁压缩——注入提醒，建议用户尽快收尾并新建会话。
 *
 * 环境变量：
 *  - WB_OPS_CTX_GUARD        0/off/false/no 关闭守卫；数字则手动指定轻档线（覆盖学习值）
 *  - WB_OPS_CTX_GUARD_STEP   档距（token，默认 40000）
 *  - WB_OPS_POST_COMPACT_MAX 压缩后水位阈值（token，默认 100000）
 *  - WB_OPS_CTX_GUARD_DIR    状态目录覆盖（默认 ~/.workbuddy/wb-ops-data，测试隔离用）
 *
 * 设计原则：
 *  - 只读 transcript / 产品配置，只写自身状态文件；绝不改动会话数据
 *  - 永远正常退出：任何异常都静默吞掉，绝不阻塞用户消息提交
 *  - 静默优先：没有要说的就零输出
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const STEP_DEFAULT = 40000;
const FALLBACK_LIGHT = 130000;   // T 未知时的轻档线
const FALLBACK_STRONG = 170000;  // T 未知时的强档线
const POST_MAX_DEFAULT = 100000; // 压缩后水位阈值
const WIN_FAST = 262144;         // 快路径：尾部 256KB
const WIN_FULL = 16777216;       // 首见会话 / 未决压缩时：尾部 16MB（覆盖大多数历史压缩记录）
const SAMPLES_CAP = 20;          // 每模型学习样本上限

/** 解析正整数，非法一律回退默认（容忍空白） */
function parsePositive(raw, fallback) {
    const s = String(raw == null ? '' : raw).trim();
    if (!/^\d+$/.test(s)) return fallback;
    const v = parseInt(s, 10);
    return v > 0 ? v : fallback;
}

const guardEnv = (process.env.WB_OPS_CTX_GUARD || '').trim().toLowerCase();
const OFF = guardEnv === '0' || guardEnv === 'off' || guardEnv === 'false' || guardEnv === 'no';
const LIGHT_OVERRIDE = /^\d+$/.test(guardEnv) && parseInt(guardEnv, 10) > 0
    ? parseInt(guardEnv, 10) : 0;
const STEP = parsePositive(process.env.WB_OPS_CTX_GUARD_STEP, STEP_DEFAULT);
const POST_MAX = parsePositive(process.env.WB_OPS_POST_COMPACT_MAX, POST_MAX_DEFAULT);
/**
 * 宿主根探测（v0.6.5）
 *
 * 本机并存两套宿主，配置根互不相通：WorkBuddy → ~/.workbuddy，WorkBuddyAI → ~/.workbuddy-ai，
 * 产品配置（模型表 / 上下文窗口）与 settings 各存一份。原实现硬编码 ~/.workbuddy，
 * 在新宿主下会读到另一套宿主那份可能过期的模型表 → 查表落空 → 退到 130K/170K 兜底，
 * 守卫不会失效，但水线精度下降。这里改为按当前宿主解析。
 *
 * 顺序：WB_OPS_HOST_HOME 显式指定 → 从脚本自身位置推导（快照形如 <HOST>/plugins/cache/…）
 *      → 默认 ~/.workbuddy。开发目录下直接运行时走默认，与历史行为一致。
 */
const HOST_HOME = (() => {
    const explicit = (process.env.WB_OPS_HOST_HOME || '').trim();
    if (explicit) return explicit;
    try {
        const segs = path.dirname(fileURLToPath(import.meta.url)).split(path.sep);
        const idx = segs.lastIndexOf('plugins');
        if (idx > 0) {
            const cand = segs.slice(0, idx).join(path.sep);
            if (fs.statSync(path.join(cand, 'plugins')).isDirectory()) return cand;
        }
    } catch {
        /* 开发目录 / 路径异常：回退默认 */
    }
    return path.join(os.homedir(), '.workbuddy');
})();

// 状态目录刻意保持共用一份（不随宿主变化）：两个宿主的会话前缀互不冲突，
// 共用可避免切换宿主时节流状态丢失、同档重复提醒。记账同理由 trim 侧固定。
const STATE_DIR = process.env.WB_OPS_CTX_GUARD_DIR ||
    path.join(os.homedir(), '.workbuddy', 'wb-ops-data');
const STATE_FILE = path.join(STATE_DIR, 'ctx-guard.json');
// v1.3.0：动态预算旁路目录。默认按 USERPROFILE 落（trim 侧就是这么找的）；
// 测试用 WB_OPS_CTX_GUARD_DIR / WB_OPS_BUDGET_DIR 覆盖时跟着走，别污染真实数据目录。
const BUDGET_DIR = (process.env.WB_OPS_BUDGET_DIR || '').trim() ||
    (process.env.WB_OPS_CTX_GUARD_DIR
        ? path.join(STATE_DIR, 'budget')
        : path.join(os.homedir(), '.workbuddy', 'wb-ops-data', 'budget'));
const BUDGET_LEVELS = [8192, 6144, 4096];   // 低 / 中 / 高三档（字节）
const BUDGET_BAND = [0.75, 0.90];           // 越 75%T 收紧一档，越 90%T 收到最紧
const BUDGET_TTL_MS = 7 * 24 * 3600 * 1000; // 旁路文件保留 7 天（trim 侧 24h 后即不认）
const SETTINGS_FILE = path.join(HOST_HOME, 'settings.json');
const PRODUCT_CFG_FILE = path.join(HOST_HOME, 'cache', 'acc-product-config-v3.json');

/** 同步读取 stdin 全部内容（hook 协议：宿主把 JSON 写在 stdin） */
function readStdin() {
    try {
        return fs.readFileSync(0, 'utf-8');
    } catch {
        return '';
    }
}

/** 从文件尾部读取 want 字节（不足则全读）；失败返回空串 */
function readTail(fp, want) {
    if (!fp) return '';
    let fd = 0;
    try {
        fd = fs.openSync(fp, 'r');
        const size = fs.fstatSync(fd).size;
        if (size <= 0) return '';
        const start = Math.max(0, size - want);
        const len = size - start;
        const buf = Buffer.alloc(len);
        fs.readSync(fd, buf, 0, len, start);
        return buf.toString('utf-8');
    } catch {
        return '';
    } finally {
        if (fd) {
            try { fs.closeSync(fd); } catch { /* ignore */ }
        }
    }
}

/**
 * 解析 transcript 尾部文本：
 *  - usages: 水位轨迹（每行最后一次 prompt_tokens）
 *  - comps:  压缩记录（compactionSeq / compactType / compactionKind）
 *  - model:  最近一次出现的模型名
 */
function parseTail(text) {
    const usages = [];
    const comps = [];
    let model = '';
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.indexOf('"prompt_tokens"') >= 0) {
            const m = /"prompt_tokens"\s*:\s*(\d+)/.exec(l);
            if (m) usages.push({ i, v: parseInt(m[1], 10) });
        }
        if (l.indexOf('"compactionSeq"') >= 0) {
            const s = /"compactionSeq"\s*:\s*(\d+)/.exec(l);
            if (s) {
                const t = /"compactType"\s*:\s*"([^"]*)"/.exec(l);
                const kd = /"compactionKind"\s*:\s*"([^"]*)"/.exec(l);
                comps.push({
                    i,
                    seq: parseInt(s[1], 10),
                    type: t ? t[1] : '',
                    kind: kd ? kd[1] : '',
                });
            }
        }
    }
    if (text.indexOf('"model"') >= 0) {
        const mm = [...text.matchAll(/"model"\s*:\s*"([^"]+)"/g)];
        if (mm.length) model = mm[mm.length - 1][1];
    }
    return { usages, comps, model };
}

function loadState() {
    try {
        const d = JSON.parse(fs.readFileSync(STATE_FILE, 'utf-8'));
        if (d && d.v === 2 && d.sessions && d.models) return d;
    } catch { /* ignore */ }
    return { v: 2, sessions: {}, models: {} };
}

function saveState(st) {
    try {
        const se = Object.entries(st.sessions || {});
        if (se.length > 200) {
            se.sort((a, b) => (b[1].u || 0) - (a[1].u || 0));
            st.sessions = Object.fromEntries(se.slice(0, 150));
        }
        const me = Object.entries(st.models || {});
        if (me.length > 60) {
            me.sort((a, b) => (b[1].u || 0) - (a[1].u || 0));
            st.models = Object.fromEntries(me.slice(0, 40));
        }
        for (const key of Object.keys(st.models || {})) {
            const r = st.models[key];
            if (r && Array.isArray(r.s) && r.s.length > SAMPLES_CAP) {
                r.s = r.s.slice(-SAMPLES_CAP);
            }
        }
        fs.mkdirSync(STATE_DIR, { recursive: true });
        // v1.3.0：临时文件 + rename 原子替换。原先直写会在多会话并发时互相截断，
        // 那一刻读到的半截 JSON 会让 loadState() 退回空状态（档位重复提醒 / 学习样本丢失）。
        const tmp = STATE_FILE + '.' + process.pid + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(st));
        fs.renameSync(tmp, STATE_FILE);
    } catch { /* ignore */ }
}

function addSample(st, key, pre) {
    st.models = st.models || {};
    const r = (st.models[key] = st.models[key] || { s: [] });
    r.u = Date.now();
    if (!Array.isArray(r.s)) r.s = [];
    if (!r.s.includes(pre)) r.s.push(pre);
}

/**
 * 查表预判触发点：
 *  - 官方目录条目（有 contextWindow.defaultLength）→ defaultLength × pct（配置里读，默认 0.5）
 *  - 自定义条目（id 为 custom-local:*，无窗口定义）→ 管理值 clamp(autoCompactWindow ?? 200K)
 *  - 查不到 → -1
 */
function lookupTrigger(modelKey) {
    try {
        const cfg = JSON.parse(fs.readFileSync(PRODUCT_CFG_FILE, 'utf-8'));
        const ms = Array.isArray(cfg && cfg.models) ? cfg.models : [];
        const isCustomKey = modelKey.startsWith('custom-local:');
        const name = isCustomKey ? modelKey.slice('custom-local:'.length) : modelKey;
        let ent = null;
        if (isCustomKey) {
            ent = ms.find((m) => m && m.id === 'custom-local:' + name) || null;
        } else {
            ent = ms.find((m) => m && m.id === name) ||
                ms.find((m) => m && m.id === 'custom-local:' + name) || null;
        }
        if (!ent) return -1;
        const cw = ent.contextWindow || {};
        if (typeof cw.defaultLength === 'number' && cw.defaultLength > 0) {
            const pct = (cfg.tokenUsageThresholds && cfg.tokenUsageThresholds.inputTokens &&
                typeof cfg.tokenUsageThresholds.inputTokens.preMessage === 'number')
                ? cfg.tokenUsageThresholds.inputTokens.preMessage : 0.5;
            return Math.round(cw.defaultLength * pct);
        }
        if (String(ent.id || '').startsWith('custom-local:')) {
            let win = 0;
            try {
                const s2 = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf-8'));
                win = Number(s2 && s2.autoCompactWindow) || 0;
            } catch { /* ignore */ }
            if (!(win > 0)) win = 200000;
            return Math.max(100000, Math.min(1000000, win));
        }
        return -1;
    } catch {
        return -1;
    }
}

/** 解析当前生效的轻/强档线（轻档 ← 强档 − STEP；强档 ← 学习值×0.85 → 查表 → 兜底） */
function resolveLines(st, modelKey) {
    if (LIGHT_OVERRIDE) return { light: LIGHT_OVERRIDE, strong: LIGHT_OVERRIDE + STEP, T: 0 };
    let T = 0;
    const rec = modelKey ? st.models[modelKey] : null;
    if (rec && Array.isArray(rec.s) && rec.s.length) {
        T = Math.min.apply(null, rec.s);
        T = Math.max(100000, Math.min(2000000, T)); // 样本保护区间 [100K, 2M]
    } else if (rec && typeof rec.T === 'number' && rec.T > 0) {
        T = rec.T;
    } else if (modelKey) {
        const t = lookupTrigger(modelKey);
        st.models = st.models || {};
        const r = (st.models[modelKey] = st.models[modelKey] || { s: [] });
        // 查不到也落 -1：避免每次消息都重复解析大配置文件
        if (!(typeof r.T === 'number' && r.T > 0)) r.T = t > 0 ? t : -1;
        if (t > 0) T = t;
    }
    if (T > 0) {
        const strong = Math.round(T * 0.85);
        return { light: Math.max(60000, strong - STEP), strong, T };
    }
    return { light: FALLBACK_LIGHT, strong: FALLBACK_STRONG, T: 0 };
}

const K = (v) => Math.round(v / 1000);

/** 分档提醒文案（band 1 = 轻档，band 2 = 强档，band 3 = 越过压缩点仍未触发；均为请模型转述给用户的指令） */
function bandText(band, level, T) {
    const head = '[wb-ops 上下文守卫] 本会话上下文已约 ' + K(level) + 'K tokens' +
        (T > 0 ? '（该模型实测自动压缩点约 ' + K(T) + 'K）' : '') + '。';
    if (band >= 3) {
        // v0.6.2：越过实测压缩点却未见宿主压缩——正是"自动压缩没触发"的漂移场景。
        // v1.3 按反馈重写措辞：① 先回答用户当前问题，再劝收尾——告警不能盖过任务；
        // ② 只"暂缓展开新方向"，用户坚持继续则继续，不能变成拒答；
        // ③ 记忆位置交给本机既有约定（.workbuddy/memory 与 .workbuddy-ai/memory 并存），不写死路径。
        return head + '已越过该模型实测自动压缩点，但宿主尚未触发压缩——宿主自动压缩只在「发送消息」那一刻检查水位，' +
            '代理循环中途不会压缩，因此水位会持续漂高。请先照常回答用户当前的问题，在此基础上优先收尾、暂缓展开新方向' +
            '（用户要求继续就继续，不要拒绝或拖延）；收尾时把关键结论落盘到记忆并写一条简短摘要，' +
            '记忆位置按本机既有约定（如 .workbuddy/memory/ 或 .workbuddy-ai/memory/），不新建目录。' +
            '另外在回复【开头】用一两句话提醒用户：建议收尾后手动 /compact 或开新会话继续；' +
            '紧接着照常回答，别让提醒挤占正文。';
    }
    if (band === 2) {
        return head + '已接近自动压缩点：被动压缩会损耗细节、成本随轮数持续放大。' +
            '请务必在本次回复【开头】用醒目的一段话提醒用户：为接下来的新任务开新会话（切换前把关键结论写入记忆即可衔接）；' +
            '当前任务如需收尾，先完成再切换。不要中断正在进行的任务。';
    }
    return head + '请在本次回复末尾用一句话顺带提醒用户（不要打断任务）：' +
        '若当前话题已告一段落，可考虑压缩（/compact）或开新会话继续新工作；如任务正处关键阶段则继续即可。';
}

/** 压缩后水位仍然偏高的提醒文案 */
function postText(seq, post) {
    return '[wb-ops 上下文守卫] 检测到本会话刚发生一次压缩（第 ' + seq + ' 次），' +
        '但压缩后上下文仍约 ' + K(post) + 'K（超过 ' + K(POST_MAX) + 'K 阈值）——' +
        '会话“底子”已偏重，继续下去会频繁压缩、细节持续损耗。' +
        '请在本次回复末尾明确建议用户：尽快收尾当前任务并新建会话继续（先写入记忆衔接）。不要中断正在进行的任务。';
}

/**
 * v1.3.0：按水位算裁剪预算（字节）。
 * T 未知时用 FALLBACK_STRONG 当 T——与提醒档位复用同一个兜底，不另拍一个数。
 * 刚压缩过（水位已回落）时按最松一档，等下一轮重新评估。
 */
function budgetFor(level, T, afterCompaction) {
    if (afterCompaction) return BUDGET_LEVELS[0];
    const ref = T > 0 ? T : FALLBACK_STRONG;
    if (level >= ref * BUDGET_BAND[1]) return BUDGET_LEVELS[2];
    if (level >= ref * BUDGET_BAND[0]) return BUDGET_LEVELS[1];
    return BUDGET_LEVELS[0];
}

/**
 * 落纯整数旁路文件 <sid8>.num：trim 侧（py 与 exe）只做「读一行 → atoi → 范围校验」。
 * 写临时文件再 rename：多会话并发时不会互相截断（原 ctx-guard.json 的 writeFileSync 会）。
 */
function writeBudget(sid, bytes) {
    try {
        fs.mkdirSync(BUDGET_DIR, { recursive: true });
        const fp = path.join(BUDGET_DIR, sid + '.num');
        const tmp = fp + '.' + process.pid + '.tmp';
        fs.writeFileSync(tmp, String(bytes));
        fs.renameSync(tmp, fp);
        pruneBudget();
    } catch { /* ignore */ }
}

/** 清理 7 天前的旁路文件（目录大小有界；单次最多删 20 个，别让钩子变慢） */
function pruneBudget() {
    try {
        const now = Date.now();
        const names = fs.readdirSync(BUDGET_DIR);
        let n = 0;
        for (const name of names) {
            if (n >= 20) break;
            if (!name.endsWith('.num') && !name.endsWith('.tmp')) continue;
            const fp = path.join(BUDGET_DIR, name);
            try {
                if (now - fs.statSync(fp).mtimeMs > BUDGET_TTL_MS) {
                    fs.unlinkSync(fp);
                    n++;
                }
            } catch { /* ignore */ }
        }
    } catch { /* ignore */ }
}

function main() {
    if (OFF) return;
    const raw = readStdin();
    let d = null;
    try { d = JSON.parse(raw); } catch { return; }
    if (!d || d.hook_event_name !== 'UserPromptSubmit') return;
    if (d.is_internal_continuation === true) return; // 内部延续（含子 agent）不提醒
    const sid = typeof d.session_id === 'string' ? d.session_id.slice(0, 8) : '';
    if (!sid) return;
    const tp = typeof d.transcript_path === 'string' ? d.transcript_path : '';
    if (!tp) return;

    const st = loadState();
    const sess0 = st.sessions[sid] && typeof st.sessions[sid] === 'object' ? st.sessions[sid] : null;

    // 快路径 256KB；首见会话 / 有未决压缩 / 没读到水位时改读 3MB
    let text = readTail(tp, WIN_FAST);
    let P = parseTail(text);
    if (!P.usages.length || !sess0 || (sess0 && Number(sess0.p) > 0)) {
        const t2 = readTail(tp, WIN_FULL);
        if (t2.length > text.length) {
            text = t2;
            P = parseTail(t2);
        }
    }
    if (!P.usages.length) return;
    const L = P.usages[P.usages.length - 1].v;
    if (!(L > 0)) return;

    const firstSeen = !sess0;
    const s = sess0 || { m: '', ls: 0, b: 0, p: 0 };
    const modelKey = P.model || (typeof s.m === 'string' ? s.m : '');
    if (P.model) s.m = P.model;

    const msgs = [];
    const ls0 = Number(s.ls) || 0;
    const newComps = P.comps.filter((c) => c.seq > ls0).sort((a, b) => a.seq - b.seq);

    let lastSeq = ls0;
    let pendingSeq = 0;
    let warn = null;
    for (let idx = 0; idx < newComps.length; idx++) {
        const c = newComps[idx];
        let pre = 0;
        for (let j = P.usages.length - 1; j >= 0; j--) {
            if (P.usages[j].i < c.i) { pre = P.usages[j].v; break; }
        }
        let post = 0;
        for (let j = 0; j < P.usages.length; j++) {
            if (P.usages[j].i > c.i) { post = P.usages[j].v; break; }
        }
        if (!post) {
            // 压缩后还没有任何请求记录（本轮刚压缩）：留到下次处理；首见（历史）则直接跳过
            if (firstSeen) lastSeq = Math.max(lastSeq, c.seq);
            else pendingSeq = c.seq;
            break;
        }
        const isAuto = c.kind === 'auto' || /auto$/.test(c.type);
        if (isAuto && pre > 0 && modelKey) addSample(st, modelKey, pre);
        lastSeq = Math.max(lastSeq, c.seq);
        if (!firstSeen && idx === newComps.length - 1 && post > POST_MAX) {
            warn = { seq: c.seq, post };
        }
    }
    s.ls = lastSeq;
    s.p = pendingSeq;
    if (newComps.length) s.b = 0; // 发生压缩 → 新纪元，两档允许重新提醒

    if (warn) msgs.push(postText(warn.seq, warn.post));

    let linesT = 0;
    if (!pendingSeq) {
        // 存在未决压缩时跳过档位判断（此时尾部的“最近水位”可能还是压缩前的旧值）
        const lines = resolveLines(st, modelKey);
        linesT = lines.T;
        let band = 0;
        if (lines.T > 0 && L >= lines.T) {
            // v0.6.2：越过实测自动压缩点却仍未压缩——宿主只会在发送消息时检查，
            // 这是"自动压缩没触发"的典型漂移场景，单独提一档做强提醒。
            band = 3;
        } else if (L >= lines.strong) {
            band = 2;
        } else if (L >= lines.light) {
            band = 1;
        }
        const prevBand = Number(s.b) || 0;
        if (band > prevBand) {
            s.b = band;
            if (!warn) msgs.push(bandText(band, L, lines.T)); // 压缩后已有强提醒则不重复
        }
    }

    // v1.3.0：按水位决定下一轮工具输出的裁剪预算（低/中/高三档），落纯整数旁路文件
    writeBudget(sid, budgetFor(L, linesT, !!pendingSeq));

    s.u = Date.now();
    st.sessions[sid] = s;
    saveState(st);

    if (msgs.length) {
        process.stdout.write(JSON.stringify({
            hookSpecificOutput: {
                hookEventName: 'UserPromptSubmit',
                additionalContext: msgs.join('\n\n'),
            },
        }));
    }
}

try {
    main();
} catch {
    /* 静默失败：守卫的问题绝不影响用户消息提交 */
}
