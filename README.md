# cmb-insight

招商银行信用卡邮件洞察插件：把导出的 `.eml` 账单导入 **SQLite**，提供查询 / 只读 SQL / 出图工具，以及一个 **Vue 3 + ECharts** 的可交互消费面板（MCP App）。

> **仓库不含任何账单数据。** `data/`、`.cache/`、`dashboard-preview.html` 都含真实交易 / 商户 / 卡号尾号，已在 `.gitignore` 里排除。clone 后用自己的邮件导入一次即可（见「安装 → 3. 导入自己的邮件」）。

## 能力

| 工具 | 说明 |
|---|---|
| `get_overview` | 数据口径与总量 |
| `aggregate` | 按年月/星期/小时/类型/渠道/商户/卡号/金额区间聚合 |
| `top_merchants` | 商户排行 |
| `search_transactions` | 明细检索（FTS5 全文索引） |
| `run_sql` | 只读 SQL 查询（SELECT / WITH） |
| `render_chart` | 返回 PNG 柱状图，可直接嵌在回答中 |
| `open_dashboard` | 打开 Vue3 + ECharts 面板（可交互） |

## 安装

三步：**克隆 → 注册 → 重启**。不用打包、不用发布、不用手动起服务。

### 0. 环境要求

| | 要求 | 说明 |
|---|---|---|
| WorkBuddy | ≥ 5.6 | 需要插件市场与 MCP App（`ui://` 资源） |
| Python | ≥ 3.9，且 SQLite 带 FTS5 + trigram | 中文子串检索靠 trigram；`install.sh` 会自检 |
| Node | ≥ 18，**可选** | 只在改前端时需要，`dashboard/dist/index.html` 已随仓库提供 |

一条命令验 Python 和 SQLite：

```bash
python3 -c 'import sqlite3,sys;print(sys.version.split()[0], sqlite3.sqlite_version);sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize=trigram)")'
```

能打印版本号且不报错就没问题。macOS 自带的 `/usr/bin/python3` 通常满足；老系统或精简版 Python 可能缺 trigram。

### 1. 克隆成一个本地市场

WorkBuddy 只认「市场」下的插件：市场根目录里要有 `.codebuddy-plugin/marketplace.json`，它再把 `./cmb-insight` 指成插件目录。**所以不要随手 clone 到桌面**——先建市场目录，再把仓库 clone 成 `cmb-insight`：

```bash
mkdir -p ~/cmb-market
cd ~/cmb-market
git clone https://github.com/<your-name>/cmb-insight.git cmb-insight
```

```
~/cmb-market/                          ← 市场根（marketplace root）
├── .codebuddy-plugin/marketplace.json  ← 第 2 步生成
└── cmb-insight/                        ← 本仓库
    └── .codebuddy-plugin/plugin.json   ← 插件清单：MCP / skills / commands
```

目录名必须是 `cmb-insight`，因为市场清单里的 `source` 写的是 `./cmb-insight`。（换成别的名字也能跑，改 `marketplace.json` 里的 `source` 即可。）

### 2. 注册到 WorkBuddy

#### 方式 A：跑脚本（推荐）

```bash
cd ~/cmb-market/cmb-insight
./install.sh --dry-run    # 先看一眼会改哪些位置，不落盘
./install.sh              # 真正写入
```

脚本干的事全在下面「方式 B」里。它是幂等的，重复跑只会覆盖同样几个键；每个被改的文件都先备份成 `*.bak-<时间戳>`；最后打印 4 项自检结果。

#### 方式 B：手工改（4 个文件 + 1 处清理）

| # | 文件 | 改什么 | 漏了会怎样 |
|---|---|---|---|
| 1 | `<市场目录>/.codebuddy-plugin/marketplace.json` | 市场清单，声明本市场提供 `cmb-insight` | 插件目录不被识别 |
| 2 | `~/.workbuddy/plugins/known_marketplaces.json` | 登记市场 `cmb-local` | 市场不被扫描 |
| 3 | `~/.workbuddy/plugins/installed_plugins.json` | 登记安装 `cmb-insight@cmb-local` | 市场里能看到、但算「未安装」 |
| 4 | `~/.workbuddy/settings.json` 的 `enabledPlugins` | 加 `"cmb-insight@cmb-local": true` | **静默失效**：技能、命令、MCP 全都不加载，界面无任何提示 |
| 5 | `~/.workbuddy/mcp.json` | 删掉旧的同名 `cmb-insight` 服务 | 工具重复注册两份 |

第 2~5 项都在 `~/.workbuddy/` 下，属全局配置，**改前先备份**：

```bash
cd ~/.workbuddy
cp settings.json settings.json.bak-$(date +%Y%m%d)
cp plugins/known_marketplaces.json plugins/known_marketplaces.json.bak-$(date +%Y%m%d)
cp plugins/installed_plugins.json plugins/installed_plugins.json.bak-$(date +%Y%m%d)
cp mcp.json mcp.json.bak-$(date +%Y%m%d)
```

##### ① 市场清单 · `<市场目录>/.codebuddy-plugin/marketplace.json`

```json
{
  "name": "cmb-local",
  "description": "本地插件市场：插件直接用 clone 下来的源码目录，改代码重启即生效，无需打包。",
  "plugins": [
    {
      "name": "cmb-insight",
      "source": "./cmb-insight",
      "version": "0.2.0",
      "description": "招商银行信用卡邮件洞察：SQLite 入库、只读 SQL 查询、Vue3 + ECharts 可交互面板（MCP App）。"
    }
  ]
}
```

`source` 是**相对市场根**的路径，所以插件目录得叫 `cmb-insight`。

##### ② 登记市场 · `~/.workbuddy/plugins/known_marketplaces.json`

在**顶层对象**里加一个键，与 `experts`、`workbuddy-builtin` 同级：

```json
"cmb-local": {
  "manifestName": "cmb-local",
  "type": "directory",
  "source": { "source": "directory", "path": "/path/to/cmb-market" },
  "installLocation": "/path/to/cmb-market",
  "description": "Marketplace from /path/to/cmb-market",
  "lastUpdated": "2026-11-10T06:04:24.000Z",
  "autoUpdate": false
}
```

`path` 和 `installLocation` 都填**市场根**（即 `~/cmb-market`），不是插件目录。把 `/path/to/cmb-market` 换成本机实际路径。应用启动时会重写这个文件，但会保留手工加的条目。

##### ③ 登记安装 · `~/.workbuddy/plugins/installed_plugins.json`

在 `plugins` 对象里加：

```json
"cmb-insight@cmb-local": [
  {
    "scope": "user",
    "installPath": "/path/to/cmb-market/cmb-insight",
    "version": "0.2.0",
    "installedAt": "2026-11-10T06:04:24.000Z",
    "lastUpdated": "2026-11-10T06:04:24.000Z"
  }
]
```

`installPath` **直接指源码目录**，不复制到 `plugins/cache/`。这是刻意的：改完代码重启即生效，不用重新安装；官方市场的插件走 `plugins/cache/<市场>/<插件>/<版本>/`，那是快照语义，适合分发。

##### ④ 启用 · `~/.workbuddy/settings.json`

在 `enabledPlugins` 里加一行：

```json
"enabledPlugins": {
  "cmb-insight@cmb-local": true,
  "weixinpay@workbuddy-builtin": true
}
```

**这一步不能省，也是排障的第一现场。** ②③ 只是「登记」，缺了启用项，插件会被彻底跳过：不在技能列表里出现（连它带的 skill 也看不到）、「套件 → 我安装的」是空的、MCP 服务不会启动 —— 表现和「没装」一模一样，且**没有任何报错**。

##### ⑤ 清冲突 · `~/.workbuddy/mcp.json`

若之前按旧方式在 `mcp.json` 里注册过同名服务，**必须删掉那段**：

```json
{
  "mcpServers": {}
}
```

插件方式下这里什么都不需要。留着会让同一套工具注册两遍，模型看到重复工具。

### 3. 导入自己的邮件

仓库里**不带任何数据**：`data/`（SQLite 库）、`.cache/`（解析缓存）、`dashboard-preview.html` 都写进 `.gitignore` 了，它们含真实交易、商户和卡号尾号。装完自己导一次：

```bash
cd ~/cmb-market/cmb-insight
python3 mcp/db.py /path/to/招行邮件导出目录 data/cmb.db   # 首次全量（3000 封约 20 秒）
python3 mcp/db.py --stats data/cmb.db                    # 核对笔数
```

邮件目录默认取插件目录的上一级 `../招商银行`（`server.py` 里的兜底值）。放在别处两种办法：单次指定 `CMB_MAIL_DIR=/path/to/eml python3 mcp/db.py ...`；或长期生效，在 `plugin.json` 的 `env` 里加 `"CMB_MAIL_DIR": "/path/to/eml"`。库不存在时，首次调用工具会自动从 `CMB_MAIL_DIR` 导入一次。

### 4. 重启，然后点「信任」

**必须整个应用退出重开**（关窗口不算）：`enabledPlugins` 只在启动时读取，应用不监听这个文件，也没有热重载入口。

重启后第一次调用 MCP 时弹 **「首次连接此 MCP 服务需要您的信任确认」**，点「信任」。自定义 MCP 一律按第三方处理，跳不过去。

装好的三个标志：

- 「技能」列表里出现 **招商银行消费洞察**；
- 「MCP 服务管理 → 我的 MCP」里 `cmb-insight` 显示 **7/7 个工具已启用**；
- `~/.workbuddy/plugins/data/cmb-insight-cmb-local/` 被创建出来（应用注入的插件数据目录，也是确认「真的加载了」的硬证据）。

连不上时该页面有 **重连** 按钮，比重启应用快。

### 装完对一遍

```bash
python3 - <<'EOF'
import json, os
h = os.path.expanduser('~/.workbuddy')
m = json.load(open(f'{h}/plugins/known_marketplaces.json'))
i = json.load(open(f'{h}/plugins/installed_plugins.json'))
s = json.load(open(f'{h}/settings.json'))
c = json.load(open(f'{h}/mcp.json'))
print('1 market  :', 'cmb-local' in m)
print('2 install :', 'cmb-insight@cmb-local' in i['plugins'],
      i['plugins'].get('cmb-insight@cmb-local', [{}])[0].get('installPath'))
print('3 enabled :', s['enabledPlugins'].get('cmb-insight@cmb-local'))
print('4 no dup  :', 'cmb-insight' not in c['mcpServers'])
EOF
```

四项依次打印 `True`，且 `2 install` 后面是你 clone 的路径，就装好了。（`install.sh` 跑完会自动打印这四行。）

### 排错

| 症状 | 原因 |
|---|---|
| 「我安装的」空的，技能列表也没有 | `enabledPlugins` 没加 → 第 ④ 步 |
| 插件在，但工具调不出来 | 没点信任，或服务起不来 → 跑下面的「服务端自检」 |
| 工具出现两份 | `mcp.json` 里还留着旧注册 → 第 ⑤ 步 |
| 重启后条目不见了 | JSON 结构不对，被应用重写时丢弃 → 跑「装完对一遍」查哪项是 False |
| 面板打开是空白 | `dashboard/dist/index.html` 缺失 → `cd dashboard && npm install && npm run build` |
| 工具报「数据库不存在」或数据为 0 | 没导入 → 第 3 步 |
| 中文搜不到商户 | Python 的 SQLite 缺 trigram 分词 → 第 0 步自检 |

### 插件方式 vs 直接写 mcp.json

| | 插件方式（当前） | 只写 `mcp.json` |
|---|---|---|
| MCP 工具 | 由插件清单声明，随插件启停 | 由 `mcp.json` 声明，全局生效 |
| Skill | 随插件自动加载，模型知道什么时候该用 | 需单独放到 `~/.workbuddy/skills/` |
| `/招行面板` 命令 | 支持 | **不支持**——命令只从插件目录解析 |
| 出现在哪里 | 「技能 → 套件」，标「来自插件」 | 只在「MCP 服务管理」 |
| 卸载 | 套件里禁用即可，配置自动撤 | 手改 `mcp.json` |
| 分发 | 打包目录或 zip，加市场即可 | 让对方手抄一段 JSON |

驱动这次切换的关键是第三行：**命令文件的查找逻辑只会去插件根目录下的 `commands/` 里找**（`plugins/marketplaces/<市场>/…` 或 `marketplace.json` 里 `plugins[].source` 指向的目录），`mcp.json` 声明的服务拿不到 slash command。

两个附带差异：

- 插件运行时会注入 `CODEBUDDY_PLUGIN_ROOT`（代码目录）和 `CODEBUDDY_PLUGIN_DATA`（`~/.workbuddy/plugins/data/<插件>-<市场>/`，跨版本升级不丢）。现在 `CMB_DB` 指向 `data/cmb.db`（仓库内），好处是和命令行共用一份库、重装不丢；如果你希望数据跟代码彻底分离，改成 `"CMB_DB": "${CODEBUDDY_PLUGIN_DATA}/cmb.db"` 即可。
- 插件 `cwd` 默认是插件根目录，且会被校验必须在其内部。`CMB_MAIL_DIR` 的默认值 `../招商银行` 正是靠这一点才成立。

因为 `installPath` 指向源码目录而不是 `plugins/cache/`，**改完代码重启即生效**，不需要重新安装；`plugin.json` 里改了 `version`，记得同步改 `installed_plugins.json` 的 `version`。

卸载：删掉 ④ 那行 `enabledPlugins`（或改 `false`），想清干净再删 ②③ 的条目，重启。

之后在对话里直接说就行：

```
今年招行花了多少，按类型拆一下
打开招行面板
```
或输入 `/招行面板`。

### 面板怎么出现

`open_dashboard` 是一个 MCP App 工具，调用后右侧详情面板的 **MCP Apps** 区域才会渲染出这个页面——它不是常驻窗口，不调用就没有。打开前那块区域显示的是「等待 Agent 调用 MCP App 工具……」。

### 服务端自检

不确定服务能不能起来，先用这行命令验证（不依赖应用，直接按协议发一次握手）：

```bash
cd ~/cmb-market/cmb-insight
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
  | sh mcp/launch.sh 2>/dev/null | head -1
```

返回一段 `"serverInfo": {"name": "cmb-insight"` 的 JSON 就说明服务端没问题，剩下的是应用侧有没有读到配置。若什么都没返回，单独跑 `sh mcp/launch.sh --which` 看解释器探测结果。

### 完全不用 MCP 的用法

只想在终端查数，不装插件也能用：

```bash
python3 mcp/db.py --stats data/cmb.db
python3 mcp/db.py --sql data/cmb.db "SELECT month, net_amount FROM v_monthly ORDER BY month DESC LIMIT 6"
```

## 结构

```
cmb-insight/
├── .codebuddy-plugin/plugin.json   插件清单（声明 MCP / skills / commands）
├── install.sh                      一键注册到本地市场（macOS / Linux）
├── data/cmb.db                     SQLite 库（本地生成，不入库）
├── mcp/
│   ├── db.py                       建表 + 导入 + 查询层（核心）
│   ├── cmb_parse.py                .eml → 交易记录（版式 A/B 解析）
│   ├── server.py                   MCP 服务端（stdio，零依赖）
│   ├── launch.sh                   启动器：探测 Python 解释器后 exec（macOS/Linux）
│   ├── launch.cmd                  启动器（Windows）
│   └── chart_png.py                零依赖 PNG 柱状图
├── dashboard/                      Vue3 + ECharts 面板源码
│   ├── src/App.vue                 布局、筛选、联动、播放
│   ├── src/components/             BaseChart / Trend / KindBars / RankBar / HeatMap / Hist / TxTable
│   ├── src/logic/aggregate.js      前端聚合与格式化
│   └── dist/index.html             构建产物（单文件，随仓库提供；server 注入数据后用）
├── skills/cmb-consumption-insight/ 技能说明
└── commands/招行面板.md            /招行面板 快捷指令
```

## 一、把邮件导入 SQLite

```bash
# 首次全量导入（3177 封约 18 秒）
python3 mcp/db.py ../招商银行 data/cmb.db

# 增量导入：只处理新增或大小变化的邮件，其余跳过
python3 mcp/db.py ../招商银行 data/cmb.db

# 全量重建
python3 mcp/db.py ../招商银行 data/cmb.db --rebuild

# 看统计
python3 mcp/db.py --stats data/cmb.db

# 跑只读 SQL
python3 mcp/db.py --sql data/cmb.db "SELECT month, net_amount FROM v_monthly ORDER BY month DESC LIMIT 6"
```

### 表结构

```
mail(id, file, subject, mail_time, mail_date, category, size_bytes,
     tx_count, bill_amount, body_text)
tx(id, mail_id, card, tx_date, tx_time, month, year, dow, hour,
   kind, channel, merchant, amount, sign, signed_amount, layout)
```

- 生成列：`month` / `year` / `dow` / `hour` / `signed_amount` 全部由日期时间自动推算，写入端不用维护。
- `amount` 存绝对值，`sign` 为 1 或 -1，聚合一律用 `signed_amount`。
- 索引：日期、月份、年份、星期、商户、类型、渠道、卡号、mail_id 均单独建索引。
- 视图：`v_tx`（含中文星期）、`v_monthly`、`v_merchant`、`v_channel`。
- 全文索引：`tx_fts` 用 FTS5 + **trigram** 分词，中文子串可检索；`unicode61` 会把整句中文当一个 token，"星巴克" 搜不到 "星巴克咖啡（国贸店）"。

### 导入的两个处理

**商户名跨邮件归一**：银行在短信体里截断商户名，同一家店会裂成 "北京轨道交通路网管理有限" 和 "...有限公司"。导入末尾统一做一次前缀合并（短名 ≥ 8 字、是长名前缀、长度差 ≤ 6），本次合并了 1126 行。

**退款符号**：`parse_email` 只做抽取，`sign = -1` 由 db 层按 `kind == '退款'` 打上。漏了这步，净支出会等于原额。

## 二、Vue3 + ECharts 面板

### 开发

```bash
cd dashboard
npm install
npm run dev        # http://localhost:5273，数据走 data.dev.json
```

**`data.dev.json` 只有 `npm run dev` 需要**：它只是一个开发用的数据快照（不入库），由下面的命令从 SQLite 导出，vite 中间件会把它挂到 `/data.json`。缺了它，页面能打开但会提示「数据未注入，且找不到 data.json」。`npm run build` 和 MCP 运行时都**不读这个文件**——产物里的数据由 `server.py` 注入。

```bash
python3 -c "
import sys; sys.path.insert(0,'mcp')
import db, json
p = db.dashboard_payload(db.connect('data/cmb.db'))
json.dump(p, open('dashboard/data.dev.json','w',encoding='utf-8'), ensure_ascii=False, separators=(',',':'))
"
```

上面这条在插件根目录（`cmb-insight/`）下跑，别在 `dashboard/` 里跑，否则 `mcp` 和 `data/cmb.db` 的相对路径都会错。

### 构建

```bash
cd dashboard && npm run build
```

产物是**单个** `dist/index.html`（约 796 KB，gzip 275 KB）。因为 MCP App 的 `ui://` 资源只能返回一段 `text/html`，不能引用外部 js/css，所以用 `vite-plugin-singlefile` 把 Vue 运行时、ECharts 和样式全部内联。

### 数据注入

构建产物里留了一个占位：

```html
<script id="cmb-data" type="text/plain">__CMB_DATA_B64__</script>
```

`server.render_dashboard()` 读取该文件，把 `db.dashboard_payload()` 的结果 JSON → gzip → base64 后替换进去。前端用 `DecompressionStream` 解压。8809 笔交易压缩后约 200 KB，整页约 1 MB。

> 之所以不选明文 JSON：明文要 800 KB，gzip 后 200 KB，差值就是加载速度。

### 面板功能

- KPI 卡带数字滚动；净支出、笔数、笔均、商户数随筛选实时变化。
- 月度趋势：点柱子下钻到该月，再点取消；虚线为月均。
- 「按月播放」：逐月自动推进，其他图表跟着动，用来看结构随时间怎么变。
- 类型柱图 / 商户排行 / 渠道排行：点击即筛选。
- 星期 × 小时热力图：颜色深浅表示该时段净支出。
- 金额分布：笔数柱 + 合计折线双轴。
- 明细表：默认 40 条，可续加载，退款显示为绿色正号。

## 启动与解释器探测

MCP 客户端通过 `mcp/launch.sh` 拉起服务，而不是写死某个 Python 的绝对路径：

```bash
sh mcp/launch.sh            # 探测并启动
sh mcp/launch.sh --which    # 只打印选中的解释器（排查用）
CMB_PYTHON=/path/to/python3 sh mcp/launch.sh   # 显式指定，不合格直接报错
```

探测顺序：

1. `$CMB_PYTHON`（显式指定，不合格则报错退出，不会静默回退）
2. 插件内 `.venv/`、`venv/`
3. `~/.workbuddy/binaries/python/envs/default/bin/python`
4. `~/.workbuddy/binaries/python/versions/*/bin/python3`
5. `python3` / `python`（PATH）
6. `/usr/bin/python3`、`/usr/local/bin/python3`

每个候选都要同时满足三条才算可用：可执行、**Python ≥ 3.9**、**SQLite 支持 FTS5 trigram**。第三条最容易被忽略——少了 trigram 分词，`db.py` 建全文索引就会失败，但报错发生在半路，不如启动前就筛掉。

三个实现约束：

- **stdout 一律不碰**。MCP 用 stdio 通信，stdout 是协议通道，所有诊断信息走 stderr（`[cmb-insight]` 前缀）。
- **只用 shell 内建**（`cd`/`pwd`/`case`/`[ ]`），不依赖 `dirname`、`ls`、`sort`。MCP 客户端给的环境变量可能很精简，`env -i PATH=/nonexistent` 下也能正常探测。
- 探测完用 `exec` 替换进程，stdin/stdout 和信号原样透传给 `server.py`。

Windows 用 `mcp/launch.cmd`（`CMB_PYTHON` → `py -3` → `python`），但这份没有在实际 Windows 上验证过。

在 `plugin.json` 里这样声明：

```json
"mcpServers": {
  "cmb-insight": {
    "command": "/bin/sh",
    "args": ["${CODEBUDDY_PLUGIN_ROOT}/mcp/launch.sh"]
  }
}
```

## 数据来源

```bash
CMB_MAIL_DIR=/path/to/eml   # 邮件目录
CMB_DB=/path/to/cmb.db      # 数据库位置
```

## 已知口径限制

- 金额为「消费提醒」口径的净额（消费减退款），不是账单金额。
- 还款数据独立来自自动还款通知，与消费不同期，不能相减当欠款。
- 约 20 笔交易类型字段为空（银行描述未写），面板与 SQL 都归为「未分类」。
- 面板数据是打开时的快照；新增邮件后先增量导入，再重新打开面板。
