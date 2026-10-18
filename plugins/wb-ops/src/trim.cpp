// wb-ops / PostToolUse 输出裁剪 hook —— 原生实现
//
// 与 ../hooks/trim.py 行为逐条对齐，但把每次调用的启动开销
// 从 ~300 ms（bash + Python）降到 ~5 ms（单个静态 exe）。
//
// 安全原则（与 Python 版一致）：
//   1. 认不出来就原样透传 —— 绝不猜测、绝不破坏结构化输出
//   2. 输出不长就不动 —— 而不是无脑截断
//   3. 被丢掉的行里，命中 error/warning 等关键词的会捞回来（含头/尾段被预算挤掉的）
//   4. 任何异常都静默退出（不输出 = 宿主保持原样），绝不让 hook 挂住会话
//
// 输入：stdin 收到 JSON，含 hook_event_name / tool_name / tool_input / tool_response
// 输出：stdout 写 JSON，用 hookSpecificOutput.updatedToolOutput 替换工具结果
//
// v0.6.1：捞回域扩展至头/尾被挤行 + 标记文案按实际命中措辞（S1）；stdin 读取时即设
// 8MB 上限（X1）；记账 sid/tool 走 JSON 转义、sid 按字节截断（S4/X2）；退出码数值
// 先做 isfinite + 范围判断再转整型（X3/X5）；退化输入不比原文小就透传（S9）。
//
// 编译（MSYS2 MinGW-w64）：
//   ./build.sh                      ← 推荐：顺带写 bin/wb-ops-trim.build.json 构建戳
//   或手动：
//   export PATH="/d/tool/msys64/mingw64/bin:$PATH"
//   g++ -O2 -s -static -static-libgcc -static-libstdc++ -o bin/wb-ops-trim.exe src/trim.cpp

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <chrono>
#include <string>
#include <utility>
#include <vector>

#include <direct.h>
#include <fcntl.h>
#include <io.h>
#include <windows.h>

// ---------------------------------------------------------------------------
// 阈值：与 hooks/trim.py 保持一致，改这里也要同步改那边
//
// v0.2.2 起预算一律按「字节」计（UTF-8 字节数）：同样字节量的中英文，
// 上下文成本量级一致；按码点计会被中文内容绕过上界（1 个汉字 ≈ 3 字节 ≈ 1 token）。
// ---------------------------------------------------------------------------
static const int MAX_LINES = 120;      // 行数超过这个才裁（触发条件）
static const int MAX_BYTES = 8000;     // 默认预算：字节数超过这个才裁（触发条件），也是结果硬上界
static const int MAX_KEY_LINES = 40;   // 中间区域最多捞回多少条关键行（超出则均匀抽样）
static const int MIN_LINE_BYTES = 60;  // 截断展示时单行至少保留的字节数，低于此值整行丢弃
static const int MAX_LINE_BYTES = 1000;// 单行最多展示的字节数（防 minified 超长行吃掉预算）
static const int MARKER_RESERVE = 300; // 给省略标记预留的字节数

// ---- v0.3.5：预算可配置（WB_OPS_BUDGET，单位字节）----
static const int BUDGET_MIN = 2000;    // 允许的最小预算（再窄内容只剩骨架）
static const int BUDGET_CAP = 131072;  // 允许的最大预算（再大等于放弃裁剪）
static const size_t MAX_INPUT = 8 * 1024 * 1024;  // v0.6.1：stdin 读取上限（读取时即生效）
static const int MAX_FILL_ROUNDS = 4;  // v0.6.1（S1）：捞回候选迭代填充轮数上限（与 trim.py 一致）

// ---- v1.1：头尾行数按「平均行长」自适应，并向预算扩张（与 trim.py 逐项一致）----
static const int HEAD_PCT = 30;        // 头段初始按预算的这个百分比估行数
static const int TAIL_PCT = 22;        // 尾段初始按预算的这个百分比估行数
static const int HEAD_MIN = 8, HEAD_MAX = 400;          // 普通工具头段行数夹紧区间
static const int TAIL_MIN = 5, TAIL_MAX = 300;          // 普通工具尾段行数夹紧区间
static const int READ_HEAD_MIN = 20, READ_HEAD_MAX = 400;  // Read 模式头段
static const int READ_TAIL_MIN = 10, READ_TAIL_MAX = 300;  // Read 模式尾段
static const int GROW_ROUNDS = 6;      // 预算扩张轮数上限（每轮头尾各 ×1.25）
static const int FILL_TARGET_PCT = 85; // 预算利用率目标：低于这个就放大头尾，达到就停

// 幂等护栏：输出里已带本插件标记，说明这段文本裁剪过，不再二次裁剪
static const char *MARK_OMIT = "[wb-ops 省略";
static const char *MARK_TAIL = "[wb-ops 以下为原文末尾";

// 解析 WB_OPS_BUDGET，得到本次裁剪的字节预算；默认 MAX_BYTES。
// 纯 ASCII 数字（与 hooks/trim.py 的 resolve_budget 完全一致，parity 有专项用例）：
// 空值/含非数字 -> 回退默认；有效值收敛到 [BUDGET_MIN, BUDGET_CAP]。
static int resolve_budget() {
    const char *env = std::getenv("WB_OPS_BUDGET");
    if (!env) return MAX_BYTES;
    std::string s(env);
    const std::string ws = " \t\r\n\v\f";
    size_t a = 0, b = s.size();
    while (a < b && ws.find(s[a]) != std::string::npos) a++;
    while (b > a && ws.find(s[b - 1]) != std::string::npos) b--;
    if (a == b) return MAX_BYTES;
    for (size_t k = a; k < b; k++)
        if (s[k] < '0' || s[k] > '9') return MAX_BYTES;
    if (b - a > 9) return BUDGET_CAP;  // 位数过多，必然越上界
    long v = std::strtol(s.substr(a, b - a).c_str(), NULL, 10);
    if (v < BUDGET_MIN) return BUDGET_MIN;
    if (v > BUDGET_CAP) return BUDGET_CAP;
    return (int)v;
}

// ---------------------------------------------------------------------------
// 关键行特征：等价于 trim.py 里的 KEY_PATTERN 正则
// （全是字面量，用子串匹配即可精确复现）
// ---------------------------------------------------------------------------
static const char *KEYWORDS[] = {
    "error", "Error", "ERROR",
    "fail", "Fail", "FAIL",
    "warn", "Warn", "WARN",
    "fatal", "Fatal",
    "Traceback", "Exception",
    "cannot", "Cannot",          // 常见错误句式（cannot find / Cannot open）
    "denied", "refused", "panic",  // 权限拒绝 / 连接拒绝 / 崩溃
    "timeout", "timed out",      // 超时
    "not found", "Not Found", "No such file",  // 未找到
    "\xe9\x94\x99\xe8\xaf\xaf",  // 错误
    "\xe5\xa4\xb1\xe8\xb4\xa5",  // 失败
    "\xe8\xad\xa6\xe5\x91\x8a",  // 警告
    "\xe5\xbc\x82\xe5\xb8\xb8",  // 异常
    "\xe6\x9c\xaa\xe8\x83\xbd",  // 未能
    "\xe6\x97\xa0\xe6\xb3\x95",  // 无法
};
static const size_t KEYWORD_COUNT = sizeof(KEYWORDS) / sizeof(KEYWORDS[0]);

// 只处理这几类高频大输出工具
static const char *TRIM_TOOLS[] = {"Bash", "Grep", "Glob", "Read", "WebFetch"};
static const size_t TRIM_TOOL_COUNT = sizeof(TRIM_TOOLS) / sizeof(TRIM_TOOLS[0]);

// ---- v0.6.9 ----
// Read 分两档（v1.2）：
//   数据类（.log/.jsonl/.csv…）—— 低门槛 + 关键字行，hooks.json 的 if 规则直接放行；
//   源码类 —— 仍然只有显式 WB_OPS_READ_TRIM=1 且文件足够长时才走「头 + 尾 + 符号行」。
static const int READ_MIN_LINES = 1500;   // 源码类 Read 小于这么多行一律不裁
static const int READ_DATA_MIN_LINES = 200;  // 数据类 Read 门槛（>200 行或超预算就裁）
// v1.2.2：结构类（.json）单独一档，门槛 800 行——读 json 多半是为了接着 Edit，
// 中段裁掉后剩下的片段虽仍合法、嵌套结构却断了，补读成本高于省下的 token。
static const int READ_STRUCT_MIN_LINES = 800;
// 硬阀：行数不多但体量巨大的（压缩成几行的 dump），不管门槛直接裁
static const size_t READ_STRUCT_HARD_BYTES = 40000;
static const int READ_HEAD_LINES = 200;   // Read 头段行数
static const int READ_TAIL_LINES = 120;   // Read 尾段行数

// v1.2：数据类扩展名（与 hooks/trim.py 的 DATA_EXTS、hooks.json 的 if 规则同一份清单）
static const char *DATA_EXTS[] = {
    ".log", ".jsonl", ".ndjson", ".csv", ".tsv", ".out", ".err", ".txt", ".json",
};
static const size_t DATA_EXT_COUNT = sizeof(DATA_EXTS) / sizeof(DATA_EXTS[0]);

// v1.2.2：结构类（DATA_EXTS 的子集，走更高门槛）
static const char *STRUCT_EXTS[] = {".json"};
static const size_t STRUCT_EXT_COUNT = sizeof(STRUCT_EXTS) / sizeof(STRUCT_EXTS[0]);

// 判定只看扩展名（大小写不敏感），反斜杠先归一化
static bool is_data_file(const std::string &path) {
    std::string p = path;
    for (size_t i = 0; i < p.size(); i++) {
        if (p[i] == '\\') p[i] = '/';
    }
    const std::string trim_ws = " \t\r\n\v\f";
    size_t a = 0, b = p.size();
    while (a < b && trim_ws.find(p[a]) != std::string::npos) a++;
    while (b > a && trim_ws.find(p[b - 1]) != std::string::npos) b--;
    p = p.substr(a, b - a);
    for (size_t i = 0; i < p.size(); i++) {
        if (p[i] >= 'A' && p[i] <= 'Z') p[i] = (char)(p[i] - 'A' + 'a');
    }
    for (size_t i = 0; i < DATA_EXT_COUNT; i++) {
        const size_t n = strlen(DATA_EXTS[i]);
        if (p.size() >= n && p.compare(p.size() - n, n, DATA_EXTS[i]) == 0) return true;
    }
    return false;
}

// v1.2.2：结构类（.json）—— 属于数据类，但门槛更高
static bool is_struct_file(const std::string &path) {
    std::string p = path;
    for (size_t i = 0; i < p.size(); i++) {
        if (p[i] == '\\') p[i] = '/';
    }
    const std::string trim_ws = " \t\r\n\v\f";
    size_t a = 0, b = p.size();
    while (a < b && trim_ws.find(p[a]) != std::string::npos) a++;
    while (b > a && trim_ws.find(p[b - 1]) != std::string::npos) b--;
    p = p.substr(a, b - a);
    for (size_t i = 0; i < p.size(); i++) {
        if (p[i] >= 'A' && p[i] <= 'Z') p[i] = (char)(p[i] - 'A' + 'a');
    }
    for (size_t i = 0; i < STRUCT_EXT_COUNT; i++) {
        const size_t n = strlen(STRUCT_EXTS[i]);
        if (p.size() >= n && p.compare(p.size() - n, n, STRUCT_EXTS[i]) == 0) return true;
    }
    return false;
}

// Read 模式下的「符号行」：函数 / 类 / 结构 / 导入等骨架行（全是字面量，子串匹配，
// 与 hooks/trim.py 的 SYMBOLS 逐项一致，保 parity）
static const char *SYMBOLS[] = {
    "#include", "function ", "def ", "class ", "struct ", "namespace ",
    "impl ", "interface ", "pub fn", "export ", "typedef ", "import ",
};
static const size_t SYMBOL_COUNT = sizeof(SYMBOLS) / sizeof(SYMBOLS[0]);

static bool is_symbol_line(const std::string &line) {
    for (size_t i = 0; i < SYMBOL_COUNT; i++) {
        if (line.find(SYMBOLS[i]) != std::string::npos) return true;
    }
    return false;
}

// 环境变量取 "1"（容忍两侧空白），与 trim.py 的 env_flag 一致
static bool env_flag(const char *name) {
    const char *env = std::getenv(name);
    if (!env) return false;
    std::string s(env);
    const std::string ws = " \t\r\n\v\f";
    size_t a = 0, b = s.size();
    while (a < b && ws.find(s[a]) != std::string::npos) a++;
    while (b > a && ws.find(s[b - 1]) != std::string::npos) b--;
    return b - a == 1 && s[a] == '1';
}

// 文本形态的失败信号：Bash 纯文本里的「Exit Code: N」且 N != 0。
// 命令失败时整段输出都可能有用（编译器错误上下文），不做裁剪。
static bool text_failed(const std::string &text) {
    const std::string key = "Exit Code:";
    size_t p = text.rfind(key);
    if (p == std::string::npos) return false;
    size_t i = p + key.size();
    while (i < text.size() && (text[i] == ' ' || text[i] == '\t')) i++;
    bool neg = false;
    if (i < text.size() && text[i] == '-') { neg = true; i++; }
    long long v = 0;
    bool any = false;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9') {
        v = v * 10 + (text[i] - '0');
        any = true;
        i++;
        if (v > 2000000000LL) break;   // 防溢出：真实退出码远小于此
    }
    if (!any) return false;
    return neg ? v != 0 : v != 0;
}

// v1.1（P0-2）：结构化失败判定 —— 宿主在 tool_response 里给了权威字段，不能再靠猜正文。
//   is_error / isError 为真            —— 宿主自己打的失败标
//   exitCode / exit_code 非 0          —— Bash / 命令类工具
//   tool_error_code 不是 "0"/"Success" —— 宿主工具错误码（"0" 表示成功）
//   error 非空字符串                    —— 兜底
// 与 trim.py 的 resp_failed 同构；失败一律整段透传。
static bool num_nonzero(double dn) {
    // 非 0 且有限（NaN/inf 不算失败信号，与 Python 侧一致）
    if (!std::isfinite(dn)) return false;
    return dn != 0;
}

// 粗判原文是 JSON 结构（两端括号）。与 trim.py 的 looks_json 同构，不做真正的解析。
static bool looks_json(const std::string &text) {
    const std::string ws = " \t\r\n";
    size_t a = 0, b = text.size();
    while (a < b && ws.find(text[a]) != std::string::npos) a++;
    while (b > a && ws.find(text[b - 1]) != std::string::npos) b--;
    if (b - a < 2) return false;
    char f = text[a], l = text[b - 1];
    return (f == '{' && l == '}') || (f == '[' && l == ']');
}

static bool is_key_line(const std::string &line) {
    for (size_t i = 0; i < KEYWORD_COUNT; i++) {
        if (line.find(KEYWORDS[i]) != std::string::npos) return true;
    }
    return false;
}

static bool is_trim_tool(const std::string &name) {
    if (name.empty()) return false;
    for (size_t i = 0; i < TRIM_TOOL_COUNT; i++) {
        if (name == TRIM_TOOLS[i]) return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// trace：仅在设置了 WB_OPS_TRACE 时写文件，平时零开销
// ---------------------------------------------------------------------------
static std::string exe_dir() {
    char buf[1024];
    DWORD n = GetModuleFileNameA(NULL, buf, sizeof(buf));
    if (n == 0 || n >= sizeof(buf)) return std::string(".");
    std::string path(buf, n);
    size_t slash = path.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string(".") : path.substr(0, slash);
}

static void trace(const std::string &msg) {
    if (!getenv("WB_OPS_TRACE")) return;
    try {
        std::string path = exe_dir() + "\\trace.log";
        FILE *f = fopen(path.c_str(), "rb");
        if (f) {
            fseek(f, 0, SEEK_END);
            long size = ftell(f);
            fclose(f);
            if (size > 256 * 1024) remove(path.c_str());
        }
        f = fopen(path.c_str(), "ab");
        if (!f) return;

        time_t now = time(NULL);
        struct tm *lt = localtime(&now);
        char stamp[16] = "??:??:??";
        if (lt) strftime(stamp, sizeof(stamp), "%H:%M:%S", lt);

        std::string body = msg.size() > 400 ? msg.substr(0, 400) : msg;
        std::string line = std::string(stamp) + " | exe | " + body + "\n";
        fwrite(line.data(), 1, line.size(), f);
        fclose(f);
    } catch (...) {
        // 日志失败绝不能影响主流程
    }
}

// ---------------------------------------------------------------------------
// v0.3.0 节省记账：每次裁剪成功后 append 一行 JSONL（默认开启，失败静默）
//
// 落点：%USERPROFILE%\.workbuddy\wb-ops-data\saves-YYYYMM.jsonl
//   （WB_OPS_STATS_DIR 覆盖数据目录；WB_OPS_STATS=0 整关）
// 行格式与 hooks/trim.py 的 stats() 逐字节对齐（时间戳除外）：
//   {"t":毫秒,"s":会话id前8字节,"tool":"Grep","b":原始字节,"a":裁剪后字节}
// 只记尺寸与时间，不落任何输出内容。
// v0.6.1（S4/X2）：sid/tool 经 dump_str JSON 转义（含引号/反斜杠也不产出非法 JSONL）；
// sid 按 UTF-8 字节安全截到前 8 字节（与 Python 侧 u8_prefix 一致，不切断多字节字符）。
// ---------------------------------------------------------------------------
static std::string u8_prefix(const std::string &s, size_t n);   // 前置声明（定义见下方）
static void dump_str(const std::string &s, std::string &out);   // 前置声明（定义见下方）

static void ensure_dirs(const std::string &path) {
    for (size_t i = 1; i <= path.size(); i++) {
        if (i == path.size() || path[i] == '\\' || path[i] == '/') {
            std::string sub = path.substr(0, i);
            if (sub.size() > 3) _mkdir(sub.c_str());  // 已存在/失败一律忽略
        }
    }
}

static void stats(const std::string &tool, const std::string &sid,
                  long long before, long long after) {
    const char *off = getenv("WB_OPS_STATS");
    if (off && strcmp(off, "0") == 0) return;  // 显式关闭
    // v1.1（P2）：只记真实会话，测试/基准探头（无 session_id 或带探针前缀）不入账本
    if (sid.empty()) return;
    static const char *FAKE_PREFIX[] = {"test", "bench", "probe", "demo", "sampl"};
    for (size_t k = 0; k < 5; k++) {
        if (sid.compare(0, 5, FAKE_PREFIX[k]) == 0) return;
    }
    try {
        const char *dir = getenv("WB_OPS_STATS_DIR");
        std::string base = (dir && *dir) ? dir : "";
        if (base.empty()) {
            const char *home = getenv("USERPROFILE");
            if (!home || !*home) return;
            base = std::string(home) + "\\.workbuddy\\wb-ops-data";
        }
        ensure_dirs(base);

        long long now_ms = (long long)std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::system_clock::now().time_since_epoch()).count();
        char month[8] = "000000";
        time_t tt = time(NULL);
        struct tm *lt = localtime(&tt);
        if (lt) strftime(month, sizeof(month), "%Y%m", lt);

        std::string path = base + "\\saves-" + month + ".jsonl";
        FILE *f = fopen(path.c_str(), "ab");
        if (!f) return;
        std::string sid8 = u8_prefix(sid, 8);   // 按字节截断（X2，与 Python 侧一致）
        std::string line = "{\"t\":" + std::to_string(now_ms) + ",\"s\":";
        dump_str(sid8, line);                   // JSON 转义（S4）
        line += ",\"tool\":";
        dump_str(tool, line);                   // JSON 转义（S4）
        line += ",\"b\":" + std::to_string(before) +
                ",\"a\":" + std::to_string(after) + "}\n";
        fwrite(line.data(), 1, line.size(), f);
        fclose(f);
    } catch (...) {
        // 记账失败绝不能影响主流程
    }
}

// ---------------------------------------------------------------------------
// 极简 JSON 解析器（够用即可：对象 / 数组 / 字符串 / 数字 / bool / null）
// ---------------------------------------------------------------------------
struct JVal {
    enum Type { NUL, BOOL, NUM, STR, ARR, OBJ } t;
    bool b;
    double num;
    std::string s;
    std::vector<JVal> arr;
    std::vector<std::pair<std::string, JVal> > obj;

    JVal() : t(NUL), b(false), num(0) {}

    const JVal *get(const char *key) const {
        if (t != OBJ) return NULL;
        for (size_t i = 0; i < obj.size(); i++) {
            if (obj[i].first == key) return &obj[i].second;
        }
        return NULL;
    }
};

// v1.1（P0-2）：结构化失败判定 —— 宿主在 tool_response 里给了权威字段，不能再靠猜正文。
//   is_error / isError 为真            —— 宿主自己打的失败标
//   exitCode / exit_code 非 0          —— Bash / 命令类工具
//   tool_error_code 不是 "0"/"Success" —— 宿主工具错误码（"0" 表示成功）
//   error 非空字符串                    —— 兜底
// 与 trim.py 的 resp_failed 同构；失败一律整段透传。
static bool resp_failed(const JVal *resp) {
    if (!resp || resp->t != JVal::OBJ) return false;
    static const char *FLAGS[] = {"is_error", "isError"};
    for (size_t k = 0; k < 2; k++) {
        const JVal *v = resp->get(FLAGS[k]);
        if (!v) continue;
        if (v->t == JVal::BOOL && v->b) return true;
        if (v->t == JVal::NUM && num_nonzero(v->num)) return true;
    }
    const JVal *code = resp->get("exitCode");
    if (!code) code = resp->get("exit_code");
    if (code && code->t == JVal::NUM && num_nonzero(code->num)) return true;
    const JVal *tec = resp->get("tool_error_code");
    if (!tec) tec = resp->get("toolErrorCode");
    if (tec && tec->t == JVal::STR) {
        std::string s = tec->s;
        const std::string ws = " \t\r\n\v\f";
        size_t a = 0, b = s.size();
        while (a < b && ws.find(s[a]) != std::string::npos) a++;
        while (b > a && ws.find(s[b - 1]) != std::string::npos) b--;
        s = s.substr(a, b - a);
        if (!s.empty() && s != "0" && s != "Success" && s != "success") return true;
    }
    const JVal *err = resp->get("error");
    if (err && err->t == JVal::STR && !err->s.empty()) return true;
    return false;
}

static void append_utf8(std::string &o, unsigned cp) {
    if (cp < 0x80) {
        o += (char)cp;
    } else if (cp < 0x800) {
        o += (char)(0xC0 | (cp >> 6));
        o += (char)(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        o += (char)(0xE0 | (cp >> 12));
        o += (char)(0x80 | ((cp >> 6) & 0x3F));
        o += (char)(0x80 | (cp & 0x3F));
    } else {
        o += (char)(0xF0 | (cp >> 18));
        o += (char)(0x80 | ((cp >> 12) & 0x3F));
        o += (char)(0x80 | ((cp >> 6) & 0x3F));
        o += (char)(0x80 | (cp & 0x3F));
    }
}

struct Parser {
    const std::string &s;
    size_t i;
    int depth;
    bool ok;

    Parser(const std::string &in) : s(in), i(0), depth(0), ok(true) {}

    void ws() {
        while (i < s.size()) {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
            else break;
        }
    }

    bool hex4(unsigned &v) {
        if (i + 4 > s.size()) return false;
        v = 0;
        for (int k = 0; k < 4; k++) {
            char c = s[i++];
            int d;
            if (c >= '0' && c <= '9') d = c - '0';
            else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
            else return false;
            v = (v << 4) | (unsigned)d;
        }
        return true;
    }

    bool parse_string(std::string &out) {
        if (i >= s.size() || s[i] != '"') return false;
        i++;
        out.clear();
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return true;
            if (c != '\\') {
                out += c;
                continue;
            }
            if (i >= s.size()) return false;
            char e = s[i++];
            switch (e) {
                case '"':  out += '"';  break;
                case '\\': out += '\\'; break;
                case '/':  out += '/';  break;
                case 'b':  out += '\b'; break;
                case 'f':  out += '\f'; break;
                case 'n':  out += '\n'; break;
                case 'r':  out += '\r'; break;
                case 't':  out += '\t'; break;
                case 'u': {
                    unsigned cp;
                    if (!hex4(cp)) return false;
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        // 高代理项：尝试合并后面的低代理项
                        if (i + 5 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                            size_t save = i;
                            i += 2;
                            unsigned lo;
                            if (hex4(lo) && lo >= 0xDC00 && lo <= 0xDFFF) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            } else {
                                i = save;
                                cp = 0xFFFD;
                            }
                        } else {
                            cp = 0xFFFD;
                        }
                    } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                        cp = 0xFFFD;  // 孤立低代理项
                    }
                    append_utf8(out, cp);
                    break;
                }
                default:
                    return false;
            }
        }
        return false;
    }

    bool parse_number(JVal &v) {
        size_t st = i;
        if (i < s.size() && s[i] == '-') i++;
        while (i < s.size() && s[i] >= '0' && s[i] <= '9') i++;
        if (i < s.size() && s[i] == '.') {
            i++;
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') i++;
        }
        if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
            i++;
            if (i < s.size() && (s[i] == '+' || s[i] == '-')) i++;
            while (i < s.size() && s[i] >= '0' && s[i] <= '9') i++;
        }
        if (i == st) return false;
        v.t = JVal::NUM;
        v.num = strtod(s.substr(st, i - st).c_str(), NULL);
        return true;
    }

    // 递归入口：统一在这里维护 depth，保证任何返回路径都平衡
    bool parse(JVal &v) {
        if (++depth > 100) return false;  // 防御性深度限制
        bool r = parse_value(v);
        depth--;
        return r;
    }

    bool parse_value(JVal &v) {
        ws();
        if (i >= s.size()) return false;

        char c = s[i];
        if (c == '{') {
            v.t = JVal::OBJ;
            i++;
            ws();
            if (i < s.size() && s[i] == '}') {
                i++;
                return true;
            }
            while (true) {
                ws();
                std::string key;
                if (!parse_string(key)) return false;
                ws();
                if (i >= s.size() || s[i] != ':') return false;
                i++;
                JVal child;
                if (!parse(child)) return false;
                v.obj.push_back(std::make_pair(key, child));
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == '}') { i++; break; }
                return false;
            }
            return true;
        }
        if (c == '[') {
            v.t = JVal::ARR;
            i++;
            ws();
            if (i < s.size() && s[i] == ']') {
                i++;
                return true;
            }
            while (true) {
                JVal child;
                if (!parse(child)) return false;
                v.arr.push_back(child);
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == ']') { i++; break; }
                return false;
            }
            return true;
        }
        if (c == '"') {
            v.t = JVal::STR;
            return parse_string(v.s);
        }
        if (c == 't' && s.compare(i, 4, "true") == 0) {
            i += 4; v.t = JVal::BOOL; v.b = true;
            return true;
        }
        if (c == 'f' && s.compare(i, 5, "false") == 0) {
            i += 5; v.t = JVal::BOOL; v.b = false;
            return true;
        }
        if (c == 'n' && s.compare(i, 4, "null") == 0) {
            i += 4; v.t = JVal::NUL;
            return true;
        }
        if (c == '-' || (c >= '0' && c <= '9')) {
            return parse_number(v);
        }
        return false;
    }
};

// ---------------------------------------------------------------------------
// UTF-8 安全的字节级操作（预算按字节计，故以字节为单位切，且不切断多字节序列）
// ---------------------------------------------------------------------------
// 取前 n 个字节（若切到字符中间则回退到最后一个完整字符）
static std::string u8_prefix(const std::string &s, size_t n) {
    if (s.size() <= n) return s;
    size_t k = n;
    while (k > 0 && ((unsigned char)s[k] & 0xC0) == 0x80) k--;
    return s.substr(0, k);
}

// 取后 n 个字节（若切到字符中间则前进到下一个完整字符）
static std::string u8_suffix(const std::string &s, size_t n) {
    if (s.size() <= n) return s;
    size_t st = s.size() - n;
    while (st < s.size() && ((unsigned char)s[st] & 0xC0) == 0x80) st++;
    return s.substr(st);
}

// ---------------------------------------------------------------------------
// 段内填充（对应 trim.py 的 fill_lines）：
//   优先整行；单行超过 MAX_LINE_BYTES 先自截到上限再算；
//   放不下而剩余额度 ≥ MIN_LINE_BYTES+3 时，末行截断后结束本段；
//   连截断都放不下则丢弃该行、结束本段。整段消耗不超过 cap（used 为累计额度）。
// ---------------------------------------------------------------------------
static std::vector<std::string> fill_lines(const std::vector<std::string> &cands,
                                           size_t cap, size_t &used) {
    std::vector<std::string> chosen;
    for (size_t k = 0; k < cands.size(); k++) {
        const std::string &line = cands[k];
        std::string disp;
        size_t want;
        if (line.size() > (size_t)MAX_LINE_BYTES) {
            disp = u8_prefix(line, (size_t)MAX_LINE_BYTES) + "\xe2\x80\xa6";
            want = (size_t)MAX_LINE_BYTES + 3;
        } else {
            disp = line;
            want = line.size();
        }
        if (used + want + 1 <= cap) {
            chosen.push_back(disp);
            used += want + 1;
        } else {
            size_t rem = (cap > used + 1) ? (cap - used - 1) : 0;
            if (rem >= (size_t)MIN_LINE_BYTES + 3) {
                chosen.push_back(u8_prefix(line, rem - 3) + "\xe2\x80\xa6");
                used = cap;
            }
            break;
        }
    }
    return chosen;
}

// ---------------------------------------------------------------------------
// tool_response -> 文本   （对应 trim.py 的 extract_text）
// ---------------------------------------------------------------------------
static bool extract_text(const JVal *resp, std::string &out) {
    if (!resp) return false;

    if (resp->t == JVal::STR) {
        out = resp->s;
        return true;
    }

    if (resp->t == JVal::ARR) {
        // 两种真实形状：
        //   1. MCP 风格 [{"type":"text","text":"..."}]
        //   2. Grep/Glob 这类直接给字符串数组，每个元素是一行
        std::vector<std::string> parts;
        for (size_t k = 0; k < resp->arr.size(); k++) {
            const JVal &item = resp->arr[k];
            if (item.t == JVal::OBJ) {
                const JVal *tx = item.get("text");
                if (tx && tx->t == JVal::STR) parts.push_back(tx->s);
            } else if (item.t == JVal::STR) {
                parts.push_back(item.s);
            }
        }
        if (parts.empty()) return false;
        out.clear();
        for (size_t k = 0; k < parts.size(); k++) {
            if (k) out += '\n';
            out += parts[k];
        }
        return true;
    }

    if (resp->t == JVal::OBJ) {
        static const char *TEXT_KEYS[] = {"stdout", "stderr", "output", "text", "content"};
        std::vector<std::string> parts;
        for (size_t k = 0; k < 5; k++) {
            const JVal *v = resp->get(TEXT_KEYS[k]);
            if (v && v->t == JVal::STR && !v->s.empty()) parts.push_back(v->s);
        }
        if (parts.empty()) return false;

        out.clear();
        for (size_t k = 0; k < parts.size(); k++) {
            if (k) out += '\n';
            out += parts[k];
        }

        // 把非零退出码带上，避免丢失信息。
        // v0.6.1（X3/X5）：先判 isfinite + long long 范围再转整型（NaN/inf/越界不转，杜绝 UB），
        // 且只认整数值（与 Python 侧「非 bool 的整数值且非 0」语义对齐）。
        const JVal *code = resp->get("exitCode");
        if (!code) code = resp->get("exit_code");
        if (code && code->t == JVal::NUM) {
            double dn = code->num;
            if (std::isfinite(dn) && dn >= -9223372036854775808.0 && dn < 9223372036854775808.0) {
                long long ic = (long long)dn;
                if ((double)ic == dn && ic != 0) {
                    char buf[64];
                    snprintf(buf, sizeof(buf), "[exit code %lld]", ic);
                    out += '\n';
                    out += buf;
                }
            }
        }
        return true;
    }

    return false;
}

// ---------------------------------------------------------------------------
// 裁剪   （对应 trim.py 的 trim；返回 false 表示不需要裁剪）
//
// 输出结构：开头行 → [省略标记] → 捞回的关键行 → [末尾标记] → 结尾行
// 预算按字节计：整体 MAX_BYTES，扣掉标记预留后按段使用；
// 关键字段与结尾段各有保底额度，前面的段用不完的额度留给后面的段。
//
// v0.6.1（S1）：捞回候选 = 中段全部命中行 + 头/尾段「未被本段填中」的命中行
// （保持原文顺序）；候选集依赖上一轮填充的省略集，故迭代填充至候选稳定
// （上限 MAX_FILL_ROUNDS 轮；与 trim.py 使用相同循环，保 parity）。
// 标记三态（与 trim.py 逐字节一致）：捞回非空 → “以下为捞回的 K 条关键字行”
// （K < 全文命中数时补“共命中 M 条”）；全文无关键字 → “未发现…”；
// 有关键字但全在显示区 → “关键字行已包含在显示区域”。
// ---------------------------------------------------------------------------
// v0.6.9：read_mode = true 时按「源码骨架」策略（头 200 + 尾 120 + 符号行），
// 并把行号区间写进标记（模型可据此 offset 定向回查）。
// ---------------------------------------------------------------------------
struct FillPass {
    size_t used;
    bool sampled;
    size_t pool_n;
    std::vector<std::string> head_shown, key_shown, tail_shown;
    std::vector<size_t> cand;
    std::vector<char> shown;
    FillPass() : used(0), sampled(false), pool_n(0) {}
};

// 单次填充（对应 trim.py 里 GROW 循环内的填充段）：给定头尾行数，
// 迭代捞回关键字行直到候选稳定。used 越大说明这一轮吃掉的预算越多。
static FillPass fill_pass(const std::vector<std::string> &lines, size_t total,
                          size_t head_n, size_t tail_n, size_t budget,
                          const std::vector<char> &is_key) {
    FillPass r;
    r.shown.assign(total, 0);
    std::vector<std::string> head(lines.begin(), lines.begin() + head_n);
    std::vector<std::string> tail(lines.end() - tail_n, lines.end());
    const size_t mid_lo = head_n;
    const size_t mid_hi = total - tail_n;

    std::vector<size_t> key_idx;
    for (size_t i = mid_lo; i < mid_hi; i++) {
        if (is_key[i]) key_idx.push_back(i);
    }

    for (int round = 0; round < MAX_FILL_ROUNDS; round++) {
        // 候选超过捞出上限时按固定步长均匀抽样（覆盖全局，而非只看前 40 条）
        r.pool_n = key_idx.size();
        r.sampled = r.pool_n > (size_t)MAX_KEY_LINES;
        r.cand.clear();
        if (r.sampled) {
            size_t step = (r.pool_n + (size_t)MAX_KEY_LINES - 1) / (size_t)MAX_KEY_LINES;
            for (size_t k = 0; k < r.pool_n; k += step) r.cand.push_back(key_idx[k]);
        } else {
            r.cand = key_idx;
        }

        // ---- 填预算 ----
        size_t used = 0;
        r.head_shown = fill_lines(head, budget * 40 / 100, used);
        std::vector<std::string> key_lines;
        key_lines.reserve(r.cand.size());
        for (size_t k = 0; k < r.cand.size(); k++) key_lines.push_back(lines[r.cand[k]]);
        r.key_shown = fill_lines(key_lines, budget * 70 / 100, used);
        std::vector<std::string> tail_rev = fill_lines(
            std::vector<std::string>(tail.rbegin(), tail.rend()), budget, used);
        r.tail_shown = std::vector<std::string>(tail_rev.rbegin(), tail_rev.rend());
        r.used = used;

        // 本轮的显示行索引集合
        std::fill(r.shown.begin(), r.shown.end(), 0);
        for (size_t k = 0; k < r.head_shown.size(); k++) r.shown[k] = 1;
        for (size_t k = 0; k < r.key_shown.size() && k < r.cand.size(); k++) r.shown[r.cand[k]] = 1;
        for (size_t k = 0; k < r.tail_shown.size(); k++) r.shown[total - 1 - k] = 1;

        // 新一轮候选：中段全部命中行 + 头/尾段「未被本段填中」的命中行（原文顺序）
        std::vector<size_t> new_idx;
        for (size_t i = 0; i < total; i++) {
            if (!is_key[i]) continue;
            if ((i >= mid_lo && i < mid_hi) || !r.shown[i]) new_idx.push_back(i);
        }
        if (new_idx == key_idx) break;   // 候选已稳定：本轮填充即最终结果
        key_idx.swap(new_idx);
    }
    return r;
}

static bool trim_text(const std::string &text, bool read_mode, int min_lines,
                      bool strict_gate, size_t hard_bytes, std::string &out) {
    std::vector<std::string> lines;
    {
        size_t st = 0;
        while (true) {
            size_t p = text.find('\n', st);
            if (p == std::string::npos) {
                lines.push_back(text.substr(st));
                break;
            }
            lines.push_back(text.substr(st, p - st));
            st = p + 1;
        }
    }

    const size_t total = lines.size();
    const size_t tbytes = text.size();
    const size_t budget_bytes = (size_t)resolve_budget();  // v0.3.5：可用 WB_OPS_BUDGET 覆盖

    // v1.1：已带本插件标记的文本不再二次裁剪（幂等，免得越裁越碎）
    if (text.find(MARK_OMIT) != std::string::npos ||
        text.find(MARK_TAIL) != std::string::npos) {
        return false;
    }

    // 触发条件：普通工具「行数 <= min_lines 且字节不超预算」直接放过；
    // 源码 Read 只要「行数 <= min_lines」或「字节不超预算」就放过（两项都过才裁）；
    // v1.2.2 结构类 Read 同源码档，但另有体量硬阀
    if (strict_gate) {
        // 行数与字节「两项都过线」才裁；但体量超硬阀时行数不再是门槛
        // （否则 30 行的 60KB dump 会因行数不达标而整段透传，硬阀形同虚设）
        if (tbytes < hard_bytes &&
            ((int)total <= min_lines || tbytes <= budget_bytes)) return false;
    } else if (!read_mode) {
        if ((int)total <= min_lines && tbytes <= budget_bytes) return false;  // 不长，不动
    } else {
        if (total <= (size_t)min_lines || tbytes <= budget_bytes) return false;
    }

    // v1.1：头尾行数按平均行长估，再在下面按预算扩张
    size_t avg = total ? tbytes / total : 1;
    if (avg < 1) avg = 1;
    const int head_lo = read_mode ? READ_HEAD_MIN : HEAD_MIN;
    const int head_hi = read_mode ? READ_HEAD_MAX : HEAD_MAX;
    const int tail_lo = read_mode ? READ_TAIL_MIN : TAIL_MIN;
    const int tail_hi = read_mode ? READ_TAIL_MAX : TAIL_MAX;
    long long base_bytes = (long long)budget_bytes - (long long)MARKER_RESERVE;
    if (base_bytes < 0) base_bytes = 0;
    long long hn = base_bytes * HEAD_PCT / 100 / (long long)avg;
    long long tn = base_bytes * TAIL_PCT / 100 / (long long)avg;
    if (hn < head_lo) hn = head_lo;
    if (hn > head_hi) hn = head_hi;
    if (tn < tail_lo) tn = tail_lo;
    if (tn > tail_hi) tn = tail_hi;
    // 头尾不能盖满全文：留出至少 6 行当「中段」
    long long side_max = ((long long)total - 6) / 2;
    if (side_max < 1) side_max = 1;
    if (hn > side_max) hn = side_max;
    if (tn > side_max) tn = side_max;
    size_t head_n = (size_t)hn;
    size_t tail_n = (size_t)tn;

    if ((int)total <= (int)(head_n + tail_n + 5)) {
        // 行数少但字节多（超长单行），按字节截首尾（额度随预算等比缩放；默认 8000 -> 5000/2400）
        std::string head = u8_prefix(text, budget_bytes * 5 / 8);
        std::string tail = u8_suffix(text, budget_bytes * 3 / 10);
        long long omitted = (long long)tbytes - (long long)head.size() - (long long)tail.size();
        char buf[128];
        snprintf(buf, sizeof(buf), "\n… [wb-ops 省略 %lld 字节] …", omitted);
        std::string marker = buf;
        if (looks_json(text)) {
            marker += "\n… [wb-ops 注：原文为 JSON 结构，截断后已非合法 JSON] …";
        }
        out = head + marker + "\n" + tail;
        return true;
    }

    // 命中统计（全文一次预计算，供候选集与标记文案复用）
    // 普通模式认 error/warning 等关键字；Read 模式认函数/类/import 等符号行
    std::vector<char> is_key(total, 0);
    size_t hit_total = 0;
    for (size_t i = 0; i < total; i++) {
        if (read_mode ? is_symbol_line(lines[i]) : is_key_line(lines[i])) {
            is_key[i] = 1;
            hit_total++;
        }
    }

    const size_t budget = budget_bytes - (size_t)MARKER_RESERVE;

    // ---- v1.1：预算扩张。头尾行数按平均行长估出来后，
    // 若填充没用到 FILL_TARGET_PCT 预算，就放大头尾再来一轮（最多 GROW_ROUNDS 轮）；
    // 取「用掉预算最多」的那一轮。与 trim.py 同一循环。
    FillPass best;
    bool have_best = false;
    for (int grow = 0; grow <= GROW_ROUNDS; grow++) {
        FillPass cur = fill_pass(lines, total, head_n, tail_n, budget, is_key);
        if (!have_best || cur.used > best.used) {
            best = cur;
            have_best = true;
        }
        if (cur.used * 100 >= budget * FILL_TARGET_PCT) break;   // 预算用够了，收工
        size_t next_head = std::min<size_t>(head_n * 5 / 4 + 1, (size_t)side_max);
        size_t next_tail = std::min<size_t>(tail_n * 5 / 4 + 1, (size_t)side_max);
        if (next_head == head_n && next_tail == tail_n) break;   // 顶到上限，再放大也没用
        head_n = next_head;
        tail_n = next_tail;
    }

    std::vector<std::string> head_shown = best.head_shown;
    std::vector<std::string> key_shown = best.key_shown;
    std::vector<std::string> tail_shown = best.tail_shown;
    const bool sampled = best.sampled;
    const size_t pool_n = best.pool_n;
    std::vector<char> &shown = best.shown;

    // ---- 组装（与 trim.py 逐字节一致）----
    const long long shown_n = (long long)head_shown.size() + (long long)key_shown.size() +
                              (long long)tail_shown.size();
    const long long silent = (long long)total - shown_n;

    // v0.6.9：未显示区间的首末行号（1 基），写进标记供模型 offset 定向回查
    long long rng_lo = 0, rng_hi = 0;
    for (size_t i = 0; i < total; i++) {
        if (!shown[i]) {
            if (!rng_lo) rng_lo = (long long)i + 1;
            rng_hi = (long long)i + 1;
        }
    }

    std::vector<std::string> parts(head_shown);
    char mbuf[320];
    if (!key_shown.empty()) {
        if (sampled) {
            snprintf(mbuf, sizeof(mbuf),
                     "… [wb-ops 省略 %lld 行（第 %lld–%lld 行），%s的 %d 条中均匀抽取 %d 条如下] …",
                     silent, rng_lo, rng_hi, read_mode ? "命中符号" : "命中关键字",
                     (int)pool_n, (int)key_shown.size());
            parts.push_back(mbuf);
        } else {
            snprintf(mbuf, sizeof(mbuf),
                     "… [wb-ops 省略 %lld 行（第 %lld–%lld 行），以下为捞回的 %d 条%s",
                     silent, rng_lo, rng_hi, (int)key_shown.size(),
                     read_mode ? "符号行" : "关键字行");
            std::string marker = mbuf;
            if (key_shown.size() < hit_total) {
                snprintf(mbuf, sizeof(mbuf), "（共命中 %d 条）", (int)hit_total);
                marker += mbuf;
            }
            marker += read_mode ? "，可加 offset 回查] …" : "] …";
            parts.push_back(marker);
        }
    } else if (hit_total) {
        snprintf(mbuf, sizeof(mbuf),
                 "… [wb-ops 省略 %lld 行（第 %lld–%lld 行，%s已包含在显示区域）] …",
                 silent, rng_lo, rng_hi, read_mode ? "符号行" : "关键字行");
        parts.push_back(mbuf);
    } else {
        snprintf(mbuf, sizeof(mbuf),
                 "… [wb-ops 省略 %lld 行（第 %lld–%lld 行，未发现 %s）] …",
                 silent, rng_lo, rng_hi,
                 read_mode ? "函数/类/import 行" : "error/warning 关键字");
        parts.push_back(mbuf);
    }
    parts.insert(parts.end(), key_shown.begin(), key_shown.end());
    if (looks_json(text)) {
        // P1-2：行导向裁剪后必然不是合法 JSON，明说一句，省得模型拿去 json.loads 白跑
        parts.push_back("… [wb-ops 注：原文为 JSON 结构，截断后已非合法 JSON] …");
    }
    if (!tail_shown.empty()) {
        snprintf(mbuf, sizeof(mbuf), "… [wb-ops 以下为原文末尾 %d 行（第 %lld–%lld 行）] …",
                 (int)tail_shown.size(), (long long)(total - tail_shown.size()) + 1,
                 (long long)total);
        parts.push_back(mbuf);
        parts.insert(parts.end(), tail_shown.begin(), tail_shown.end());
    }

    std::string res;
    for (size_t k = 0; k < parts.size(); k++) {
        if (k) res += '\n';
        res += parts[k];
    }

    // v0.6.1（S9）：退化输入（如大量空行）可能越裁越大 —— 不比原文小就直接透传
    if (res.size() >= text.size()) return false;

    // 兜底硬上界（正常不会触发）
    if (res.size() > budget_bytes)
        res = u8_prefix(res, budget_bytes - 3) + "\xe2\x80\xa6";

    out = res;
    return true;
}

// ---------------------------------------------------------------------------
// 序列化：JSON 字符串转义
// ---------------------------------------------------------------------------
static void dump_str(const std::string &s, std::string &out) {
    out += '"';
    for (size_t k = 0; k < s.size(); k++) {
        unsigned char c = (unsigned char)s[k];
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
}

static std::string to_str(long long v) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld", v);
    return std::string(buf);
}

// 诊断用：把 tool_response 的类型/键/长度压成一行（仅在 WB_OPS_TRACE 时经 trace 写出）
static std::string brief(const JVal *v) {
    if (!v) return "null";
    char buf[64];
    if (v->t == JVal::STR) {
        snprintf(buf, sizeof(buf), "str(%d)", (int)v->s.size());
        return buf;
    }
    if (v->t == JVal::ARR) {
        snprintf(buf, sizeof(buf), "arr(%d)", (int)v->arr.size());
        return buf;
    }
    if (v->t == JVal::NUM) return "num";
    if (v->t == JVal::BOOL) return "bool";
    if (v->t == JVal::OBJ) {
        std::string r = "obj{";
        for (size_t k = 0; k < v->obj.size() && k < 10; k++) {
            if (k) r += ", ";
            r += v->obj[k].first + "=";
            const JVal &x = v->obj[k].second;
            if (x.t == JVal::STR) {
                snprintf(buf, sizeof(buf), "str(%d)", (int)x.s.size());
                r += buf;
            } else if (x.t == JVal::ARR) {
                snprintf(buf, sizeof(buf), "arr(%d)", (int)x.arr.size());
                r += buf;
            } else if (x.t == JVal::NUM) r += "num";
            else if (x.t == JVal::BOOL) r += "bool";
            else if (x.t == JVal::OBJ) r += "obj";
            else r += "null";
        }
        r += "}";
        return r;
    }
    return "null";
}

// ---------------------------------------------------------------------------
// v1.3：--dump-exts 打印数据类扩展名清单（JSON 数组），供 test_hook.py 校验
// 「hooks.json 的 if 规则 / hooks/trim.py 的 DATA_EXTS / 本二进制」三处同源。
// 生产走的是这个 exe，只比对源码等于没比对——改完清单忘了重编译，自报清单才对得上。
static void dump_exts() {
    _setmode(_fileno(stdout), _O_BINARY);
    std::string out = "[";
    for (size_t k = 0; k < DATA_EXT_COUNT; k++) {
        if (k) out += ",";
        out += "\"";
        out += DATA_EXTS[k];
        out += "\"";
    }
    out += "]\n";
    fwrite(out.data(), 1, out.size(), stdout);
    fflush(stdout);
}

int main(int argc, char **argv) {
    // 版本自检通道：不读 stdin、不碰 hook 协议，只回一份清单
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--dump-exts") == 0) {
            dump_exts();
            return 0;
        }
    }

    // 二进制模式：避免 \r\n 转换与 CTRL-Z 截断破坏 JSON
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);

    std::string raw;
    {
        char buf[65536];
        size_t n;
        while ((n = fread(buf, 1, sizeof(buf), stdin)) > 0) {
            raw.append(buf, n);
            // v0.6.1（X1）：读取时即设上限（宿主一般远小于此），超限直接透传
            if (raw.size() > MAX_INPUT) {
                trace("输入超过 8MB，直接透传");
                return 0;
            }
        }
    }
    if (raw.empty()) return 0;

    // 去掉可能的前导 BOM
    if (raw.size() >= 3 && (unsigned char)raw[0] == 0xEF &&
        (unsigned char)raw[1] == 0xBB && (unsigned char)raw[2] == 0xBF) {
        raw.erase(0, 3);
    }

    JVal root;
    {
        Parser p(raw);
        if (!p.parse(root) || root.t != JVal::OBJ) {
            if (getenv("WB_OPS_TRACE")) {
                char info[128];
                snprintf(info, sizeof(info), "parse fail @ i=%d / len=%d / depth=%d",
                         (int)p.i, (int)raw.size(), (int)p.depth);
                size_t st = p.i > 60 ? p.i - 60 : 0;
                std::string ctx = raw.substr(st, 120);
                std::string safe;
                for (size_t k = 0; k < ctx.size(); k++) {
                    unsigned char u = (unsigned char)ctx[k];
                    safe += (u >= 0x20 && u < 0x7F) ? (char)u : '.';
                }
                trace(std::string(info) + " | ctx=" + safe);
            }
            trace("stdin \xe4\xb8\x8d\xe6\x98\xaf\xe5\x90\x88\xe6\xb3\x95 JSON \xe5\xaf\xb9\xe8\xb1\xa1\xef\xbc\x8c\xe9\x80\x8f\xe4\xbc\xa0");
            return 0;
        }
    }

    const JVal *ev = root.get("hook_event_name");
    if (!ev || ev->t != JVal::STR || ev->s != "PostToolUse") {
        trace("\xe4\xba\x8b\xe4\xbb\xb6\xe4\xb8\x8d\xe6\x98\xaf PostToolUse\xef\xbc\x8c\xe9\x80\x8f\xe4\xbc\xa0");
        return 0;
    }

    const JVal *tn = root.get("tool_name");
    std::string tool = (tn && tn->t == JVal::STR) ? tn->s : "";
    if (!is_trim_tool(tool)) {
        trace(std::string("工具 ") + tool + " 不在裁剪名单，透传 | " +
              brief(root.get("tool_response")));
        return 0;
    }

    std::string text;
    if (!extract_text(root.get("tool_response"), text) || text.empty()) {
        trace(std::string("工具 ") + tool + " 输出无法解析为文本，透传 | " +
              brief(root.get("tool_response")));
        return 0;
    }

    std::string trimmed;
    // v1.2：Read 按文件类型分档（与 hooks/trim.py 的 main() 同构）
    //   数据类（日志/表格/清单）—— 普通关键字模式，200 行门槛（.json 结构类 800 行）；
    //   源码类 —— 符号行模式，1500 行门槛，需 WB_OPS_READ_TRIM=1（由 --with-read 写入命令）；
    //   其余 —— 直接透传。
    bool read_mode = false;
    bool strict_gate = false;   // v1.2.2：结构类用「两项都过线」的严格门槛
    size_t hard_bytes = 0;      // v1.2.2：结构类体量硬阀
    int min_lines = MAX_LINES;
    if (tool == "Read") {
        std::string path;
        const JVal *ti = root.get("tool_input");
        if (ti && ti->t == JVal::OBJ) {
            static const char *PKEYS[] = {"file_path", "path"};
            for (size_t k = 0; k < 2 && path.empty(); k++) {
                const JVal *v = ti->get(PKEYS[k]);
                if (v && v->t == JVal::STR && !v->s.empty()) path = v->s;
            }
        }
        if (is_data_file(path)) {
            // 结构类 .json 门槛更高（读它多半是为了接着改），且要两项都过线才裁
            if (is_struct_file(path)) {
                min_lines = READ_STRUCT_MIN_LINES;
                strict_gate = true;
                hard_bytes = READ_STRUCT_HARD_BYTES;
            } else {
                min_lines = READ_DATA_MIN_LINES;
            }
        } else if (env_flag("WB_OPS_READ_TRIM")) {
            read_mode = true;
            min_lines = READ_MIN_LINES;
        } else {
            trace(std::string("Read 非数据文件且未开启 WB_OPS_READ_TRIM，透传 | ") + path.substr(0, 200));
            return 0;
        }
    }
    if (resp_failed(root.get("tool_response")) || text_failed(text)) {
        trace(std::string("工具 ") + tool + " 失败（结构化或文本信号），保留完整输出，透传");
        return 0;
    }
    if (!trim_text(text, read_mode, min_lines, strict_gate, hard_bytes, trimmed)) {
        trace(std::string("\xe5\xb7\xa5\xe5\x85\xb7 ") + tool +
              " \xe8\xbe\x93\xe5\x87\xba\xe4\xb8\x8d\xe9\x95\xbf\xef\xbc\x8c\xe4\xb8\x8d\xe8\xa3\x81");
        return 0;
    }

    trace(std::string("工具 ") + tool + " 已裁剪：" + to_str((long long)text.size()) +
          " -> " + to_str((long long)trimmed.size()) + " 字节");

    // v0.3.0 节省记账（默认开启；失败静默，绝不影响裁剪输出）
    {
        const JVal *sidv = root.get("session_id");
        stats(tool, (sidv && sidv->t == JVal::STR) ? sidv->s : std::string(""),
              (long long)text.size(), (long long)trimmed.size());
    }

    std::string out = "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"updatedToolOutput\":";
    dump_str(trimmed, out);
    out += "},\"suppressOutput\":true}";

    fwrite(out.data(), 1, out.size(), stdout);
    fflush(stdout);
    return 0;
}
