#!/bin/sh
# cmb-insight —— 注册到 WorkBuddy 的「本地插件市场」
#
# 前提：本仓库 clone 在「市场目录」下并叫 cmb-insight
#   mkdir -p ~/cmb-market && cd ~/cmb-market
#   git clone <repo-url> cmb-insight
#
# 用法：
#   cd ~/cmb-market/cmb-insight && ./install.sh
#   ./install.sh --dry-run     # 只看会改什么，不落盘
#
# 会改动 4 个位置（都先备份 .bak-<时间戳>）：
#   <市场目录>/.codebuddy-plugin/marketplace.json      市场清单
#   ~/.workbuddy/plugins/known_marketplaces.json       登记市场
#   ~/.workbuddy/plugins/installed_plugins.json        登记安装
#   ~/.workbuddy/settings.json                         启用（enabledPlugins）
#   ~/.workbuddy/mcp.json                              清掉同名旧注册（不新增）
#
# 幂等：重复执行只会覆盖同样几个键。

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P)
PLUGIN_NAME=$(basename "$SCRIPT_DIR")
MARKET_ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P)
MARKET_NAME=cmb-local

say() { printf '%s\n' "$*"; }

if [ ! -f "$SCRIPT_DIR/.codebuddy-plugin/plugin.json" ]; then
    say "× 这里不是插件目录：找不到 .codebuddy-plugin/plugin.json"
    exit 1
fi

PY=
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || { say "× 需要 python3（用来安全地改 JSON 注册表）"; exit 1; }

DRY=0
case "${1:-}" in
    --dry-run) DRY=1 ;;
    "") ;;
    *) say "未知参数：$1（可用：--dry-run）"; exit 1 ;;
esac

say "插件目录 : $SCRIPT_DIR"
say "市场根   : $MARKET_ROOT"
say "市场名   : $MARKET_NAME"
say "插件名   : $PLUGIN_NAME"
if [ "$PLUGIN_NAME" != "cmb-insight" ]; then
    say "! 目录名不是 cmb-insight，市场清单里会写成 ./$PLUGIN_NAME，功能不受影响，但与文档不一致"
fi
say ""

CMB_DRY=$DRY "$PY" - "$SCRIPT_DIR" "$MARKET_ROOT" "$PLUGIN_NAME" "$MARKET_NAME" <<'PY'
import json, os, shutil, sqlite3, sys, time

plugin_dir, market_root, name, market = sys.argv[1:5]
dry = os.environ.get("CMB_DRY") == "1"
WB = os.path.join(os.path.expanduser("~"), ".workbuddy")
TS = time.strftime("%Y%m%d-%H%M%S")
fail = []


def rd(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        print(f"× {path}\n  不是合法 JSON（{e}），先修好再装")
        sys.exit(2)


def wr(path, data, label):
    if dry:
        print(f"[dry-run] {label} → {path}")
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{TS}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    print(f"✓ {label}\n  {path}")


# ---------- 0. 运行环境自检 ----------
print("环境自检：")
if sys.version_info < (3, 9):
    fail.append(f"Python {sys.version.split()[0]} < 3.9")
    print(f"  × Python {sys.version.split()[0]} 过低，需要 ≥ 3.9")
else:
    print(f"  ✓ Python {sys.version.split()[0]}")
try:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    con.close()
    print("  ✓ SQLite 支持 FTS5 trigram")
except Exception as e:
    fail.append("SQLite 缺 FTS5 trigram")
    print(f"  × SQLite 不支持 FTS5 trigram（{e}）\n    中文检索会失效，换个 Python（官方安装包/venv 一般自带）")

ver = "0.0.0"
try:
    with open(os.path.join(plugin_dir, ".codebuddy-plugin", "plugin.json"), encoding="utf-8") as f:
        ver = json.load(f).get("version", ver)
except Exception:
    pass
print(f"  · 插件版本 {ver}\n")

now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

# ---------- 1. 市场清单 ----------
manifest = {
    "name": market,
    "description": f"本地插件市场：插件直接用 clone 下来的源码目录，改代码重启即生效，无需打包。",
    "plugins": [
        {
            "name": name,
            "source": f"./{name}",
            "version": ver,
            "description": "招商银行信用卡邮件洞察：SQLite 入库、只读 SQL 查询、Vue3 + ECharts 可交互面板（MCP App）。",
        }
    ],
}
wr(os.path.join(market_root, ".codebuddy-plugin", "marketplace.json"), manifest, "市场清单")

# ---------- 2. 登记市场 ----------
km_path = os.path.join(WB, "plugins", "known_marketplaces.json")
km = rd(km_path, {})
km[market] = {
    "manifestName": market,
    "type": "directory",
    "source": {"source": "directory", "path": market_root},
    "installLocation": market_root,
    "description": f"Marketplace from {market_root}",
    "lastUpdated": now,
    "autoUpdate": False,
}
wr(km_path, km, "登记市场")

# ---------- 3. 登记安装 ----------
ip_path = os.path.join(WB, "plugins", "installed_plugins.json")
ip = rd(ip_path, {"version": 2, "plugins": {}})
ip.setdefault("version", 2)
ip.setdefault("plugins", {})
ip["plugins"][f"{name}@{market}"] = [
    {
        "scope": "user",
        "installPath": plugin_dir,
        "version": ver,
        "installedAt": now,
        "lastUpdated": now,
    }
]
wr(ip_path, ip, "登记安装")

# ---------- 4. 启用（关键：漏了插件会被静默跳过） ----------
st_path = os.path.join(WB, "settings.json")
st = rd(st_path, {})
en = st.setdefault("enabledPlugins", {})
en[f"{name}@{market}"] = True
wr(st_path, st, "启用插件")

# ---------- 5. 清掉 mcp.json 里的同名旧注册 ----------
mc_path = os.path.join(WB, "mcp.json")
mc = rd(mc_path, {"mcpServers": {}})
if name in mc.get("mcpServers", {}):
    del mc["mcpServers"][name]
    wr(mc_path, mc, "清理 mcp.json 里的同名服务")
else:
    print("✓ mcp.json 无同名服务，跳过")

# ---------- 汇总 ----------
print("\n" + ("自检（dry-run，读的是内存里的预期值，未落盘）：" if dry else "自检："))
print(f"  market    : {market in km}")
print(f"  installed : {f'{name}@{market}' in ip['plugins']}  {plugin_dir}")
print(f"  enabled   : {en.get(f'{name}@{market}')}")
print(f"  no dup    : {name not in mc.get('mcpServers', {})}")

if fail:
    print("\n× 有阻塞问题：" + "；".join(fail))
    sys.exit(3)
print("\n下一步：完全退出 WorkBuddy 再打开（enabledPlugins 只在启动时读），")
print("    首次调用会弹「信任此 MCP 服务」，点信任。")
PY

say ""
say "数据还没导入的话："
say "  python3 mcp/db.py /path/to/招行邮件 data/cmb.db"
say "  python3 mcp/db.py --stats data/cmb.db"
