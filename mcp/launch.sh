#!/bin/sh
# cmb-insight MCP 启动器：探测可用的 Python 解释器，然后 exec server.py。
#
# 为什么需要它：MCP 的 command 如果写死某个解释器的绝对路径，换一台机器就失效；
# 写死 python3 又可能在 PATH 里被指到不支持 FTS5 trigram 的低版本上。
#
# 约定：
#   * stdout 是 MCP 的协议通道，本脚本任何情况下都不往 stdout 写非协议内容；
#     诊断信息一律走 stderr。
#   * 探测只依赖 shell 内建（cd / pwd / case / [ ]），不依赖 dirname、ls、sort，
#     因为 MCP 客户端可能给出很精简的 PATH。
#   * 用 exec 替换进程，保证 stdin/stdout 与信号原样透传给 server.py。
#
# 用法：
#   sh launch.sh              # 探测并启动 MCP 服务
#   sh launch.sh --which      # 只打印选中的解释器路径（排查用）
#   CMB_PYTHON=/path/to/python3 sh launch.sh   # 显式指定，不合格则直接报错

set -u

MIN_MAJOR=3
MIN_MINOR=9

log() { printf '[cmb-insight] %s\n' "$*" >&2; }

# 解析脚本自身所在目录（不依赖 dirname）
case "$0" in
    */*) HERE=$(cd "${0%/*}" && pwd) ;;
    *)   HERE=$(pwd) ;;
esac

# 候选解释器，按优先级：插件内虚拟环境 → WorkBuddy 托管运行时 → PATH → 系统默认
candidates() {
    printf '%s\n' \
        "$HERE/../.venv/bin/python3" \
        "$HERE/../.venv/bin/python" \
        "$HERE/../venv/bin/python3" \
        "$HERE/../venv/bin/python" \
        "$HOME/.workbuddy/binaries/python/envs/default/bin/python"

    # 托管的多版本目录；glob 不匹配时保持字面量，下面 -d 会过滤掉
    for d in "$HOME"/.workbuddy/binaries/python/versions/*/; do
        [ -d "$d" ] || continue
        printf '%s\n' "${d}bin/python3" "${d}bin/python"
    done

    printf '%s\n' python3 python /usr/bin/python3 /usr/local/bin/python3
}

# 判断候选是否可用：可执行 + 版本达标 + SQLite 支持 FTS5 trigram（中文子串检索的前提）
probe() {
    py=$1
    [ -n "$py" ] || return 1
    if [ -x "$py" ]; then
        :
    elif command -v "$py" >/dev/null 2>&1; then
        :
    else
        return 1
    fi

    "$py" - "$MIN_MAJOR" "$MIN_MINOR" <<'PYCODE' >/dev/null 2>&1
import sqlite3
import sys

if len(sys.argv) > 2:
    if sys.version_info < (int(sys.argv[1]), int(sys.argv[2])):
        sys.exit(1)

con = sqlite3.connect(":memory:")
# 少了 trigram 分词，db.py 建 FTS 表就会失败，这里提前筛掉
con.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='trigram')")
con.close()
PYCODE
}

PY=""

# 显式指定优先，但指定了就必须合格——静默回退到别的解释器会让人误判
if [ -n "${CMB_PYTHON:-}" ]; then
    if probe "$CMB_PYTHON"; then
        PY=$CMB_PYTHON
    else
        log "CMB_PYTHON 指定的解释器不可用：$CMB_PYTHON"
        log "需要 Python >= ${MIN_MAJOR}.${MIN_MINOR} 且 SQLite 编译了 FTS5 trigram"
        exit 1
    fi
fi

if [ -z "$PY" ]; then
    # 用 heredoc 而不是管道喂循环：管道会让 while 跑在子 shell 里，PY 赋值传不出来
    while IFS= read -r c; do
        if probe "$c"; then
            PY=$c
            break
        fi
    done <<EOF
$(candidates)
EOF
fi

if [ -z "$PY" ]; then
    log "找不到可用的 Python 解释器（需要 >= ${MIN_MAJOR}.${MIN_MINOR}，且 SQLite 支持 FTS5 trigram）"
    log "已尝试："
    candidates | while IFS= read -r c; do log "  - $c"; done
    log "可用 CMB_PYTHON=/path/to/python3 显式指定"
    exit 1
fi

if [ "${1:-}" = "--which" ]; then
    printf '%s\n' "$PY"
    exit 0
fi

log "python=$PY ($("$PY" -V 2>&1))"
exec "$PY" "$HERE/server.py" "$@"
