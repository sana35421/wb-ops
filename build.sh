#!/usr/bin/env bash
# 重新编译原生裁剪器（改完 plugins/wb-ops/src/trim.cpp 后跑这个）
#
# 产物：plugins/wb-ops/bin/wb-ops-trim.exe —— 静态链接，无 DLL 依赖，可直接拷到任何 Windows 机器跑。
# 编译完建议再跑一次回归测试：python test_parity.py

set -e
cd "$(dirname "$0")"
PLUGIN="plugins/wb-ops"

# MSYS2 MinGW-w64 常见位置（不在 PATH 里时按需补充）
for GXX_BIN in "/c/msys64/mingw64/bin" "/d/tool/msys64/mingw64/bin"; do
    if [ -d "$GXX_BIN" ]; then
        export PATH="$GXX_BIN:$PATH"
        break
    fi
done

if ! command -v g++ >/dev/null 2>&1; then
    echo "找不到 g++。需要 MSYS2 MinGW-w64（可在本脚本开头补充你的安装路径）" >&2
    exit 1
fi

g++ -O2 -s -static -static-libgcc -static-libstdc++ -Wall -o "$PLUGIN/bin/wb-ops-trim.exe" "$PLUGIN/src/trim.cpp"
echo "编译完成：$PLUGIN/bin/wb-ops-trim.exe（$(stat -c%s "$PLUGIN/bin/wb-ops-trim.exe") 字节）"
