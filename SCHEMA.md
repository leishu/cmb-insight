# 数据库结构说明（schema v6）

`data/cmb.db` —— SQLite，零外部依赖。所有数据都从 `.eml` 解析而来，**没有不可再生的人工录入**，
所以库结构升级时直接全量重建，不会丢东西。

- 建表 SQL：`mcp/db.py` 里的 `SCHEMA` 常量（第 52 行起）
- 结构版本：`SCHEMA_VERSION = 6`（`db.py:50`），存在 `meta` 表里，**不是** `PRAGMA user_version`
- 写入者：`mcp/db.py`（导入层）、`mcp/cmb_parse.py`（抽取层，只产出 dict，不碰库）
- 读取者：`mcp/server.py` 的 10 个 MCP 工具、`dashboard/` 面板

> 文中的「现状」列来自作者本地库（约 3 千封邮件 / 近 9 千笔交易）的实测。为了不把个人账单带进公共
> 仓库，这里只写**比例和量级**、不写具体条数与金额——同一条结论在你自己的库上应当同样成立，
> 用来判断某个字段是「天然空」还是「解析漏了」。**空值语义不会变**。

---

## 0. 一张图看关系

```
                        ┌──────────┐
                        │   meta   │  元数据 key/value（版本、路径、行数）
                        └──────────┘

        ┌───────────────────────────────────────────┐
        │                  mail                      │  一封 .eml 一行（原文层）
        │  id / file / subject / mail_time / ...     │
        └────┬───────────┬───────────┬───────────┬───────────┬──┘
             │1:N        │1:N        │1:N        │1:1        │1:1
             ▼           ▼           ▼           ▼           ▼
        ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
        │   tx   │  │ tx_fx  │  │  hold  │  │  bill  │  │ credit │
        │CNY交易 │  │外币交易 │  │预授权占用│  │账单账本 │  │额度流水 │
        └───┬────┘  └────────┘  └────────┘  └────────┘  └────────┘
            │ 1:1（rowid = tx.id）
            ▼
        ┌────────┐
        │ tx_fts │  FTS5 trigram 全文索引（merchant/channel/subject）
        └────────┘
```

五张子表都挂了 `mail_id` 外键并 `ON DELETE CASCADE`。删一封邮件，它的交易（含外币）、预授权、
账单、额度记录一起没。

---

## 1. 表选型速查（先看这里）

| 你想问的问题 | 该查的表 / 视图 | 关键点 |
|---|---|---|
| 这个月花了多少钱 | `tx` / `v_monthly` | 一律 `SUM(signed_amount)`，别用 `amount` |
| 某商户花了多少、去过几次 | `tx` / `v_merchant` | `merchant` 被银行截断过，已做前缀合并 |
| 这笔消费走的是微信还是支付宝 | `tx.channel` | 早期邮件不写渠道，会有一批空值 |
| 周几、几点消费最多 | `tx.dow` / `tx.hour` / `v_tx.weekday` | 生成列，直接 group by |
| 某个关键词的消费记录 | `tx_fts` 或 `tx.merchant LIKE` | 查询串 ≥3 字走 FTS，否则 LIKE |
| 我这一期该还多少、最低还多少 | `bill` | 账本级，`due_amount` 是应还**不是**消费额 |
| 额度用掉几成、涨没涨 | `credit` | `avail_credit` 是每日流水，取时间序列 |
| 这笔酒店押金算支出了吗 | `hold` | **不算**，预授权只占额度 |
| 港币/美元等外币消费 | `tx_fx` | 原币入库，**不在人民币总额里**，按 `currency` 分组单报 |
| 邮件本身（原始正文、主题、大小） | `mail` | `body_text` 存了正文纯文本 |
| 库里现在多少行、什么时候同步的 | `meta` | `mail_count` / `last_sync` |

一句话：**`tx` 是消费分析的主力（仅人民币），`tx_fx` 是外币单列，`bill` 是账本对账，`credit` 是
额度曲线，`hold` 是坑，`mail` 是溯源。**

---

## 2. `mail` —— 邮件原文层

一行 = 一个 `.eml` 文件。任何 `tx`/`bill` 的数字都能通过 `mail_id` 追回原始邮件，这是核对的基础。

| 列 | 类型 | 含义 | 现状 / 注意 |
|---|---|---|---|
| `id` | INTEGER PK | 自增主键，无业务含义 | 被 `tx.mail_id` 等引用 |
| `file` | TEXT UNIQUE | `.eml` 文件名（`YYYYMMDD-HHMMSS_hash.eml`） | 去重键：同一文件不重复导入 |
| `subject` | TEXT | 邮件主题 | 分类判定的依据（见下） |
| `mail_time` | TEXT | 邮件发出时间 `YYYY-MM-DD HH:MM:SS` | 优先取文件名前缀，失败退回 `Date` 头 |
| `mail_date` | TEXT **生成列** | `substr(mail_time,1,10)` | 只读，别写 |
| `category` | TEXT | 邮件类型，见下方枚举 | 由主题关键词判定，5 类 |
| `size_bytes` | INTEGER | 文件字节数 | 与 `file_hash` 一起判断「内容变了」→ 删旧重插 |
| `file_hash` | TEXT | 文件内容 sha1 | v6 新增；同尺寸的内容变更也能检测到。解析失败行为空串 |
| `tx_count` | INTEGER | 这封邮件解析出的交易行数（**`tx` + `tx_fx`**） | **不含 `hold`**；账单/还款类是 0，正常 |
| `bill_amount` | REAL | 自动还款通知里的**实扣金额** | 只有「自动还款」这类邮件有值；账单类**不写这列**（走 `bill` 表） |
| `body_text` | TEXT | 正文纯文本（上限 20,000 字符） | HTML 去标签；供全文检索与人工核对 |
| `imported_at` | TEXT | 入库时间 | 默认 `datetime('now','localtime')` |

**`category` 的 5 个取值**（`cmb_parse.py:477`，按主题关键词顺序匹配）：

| 值 | 判定 | 相对量 | 有交易明细吗 |
|---|---|---|---|
| `每日信用管家` | 主题含「每日信用管家」 | 最多 | 有（版式 B） |
| `消费提醒` | 主题含「消费提醒」 | 次之 | 有（版式 A） |
| `电子账单` | 主题含「电子账单」 | 每月一封 | **没有**，数据在 `bill` |
| `自动还款` | 主题含「自动还款」 | 每月一封 | **没有**，只有 `bill_amount` |
| `营销/其他` | 以上都不匹配 | 少量 | 没有（广告、通知） |
| `解析失败` | 解析抛异常时落库（v6 起），`body_text` 存错误信息 | 正常为 0 | 没有；每次导入自动重试 |

> 主题里不含这四个词的邮件一律进「营销/其他」，**不是**「无法识别的内容」。
> 想确认广告邮件里有没有漏掉的真实消费，查 `category='营销/其他' and body_text not like '%活动%'`。

---

## 3. `tx` —— 交易明细（分析主力表，**仅人民币**）

一行 = 一笔人民币交易。所有 CNY 消费统计都应该从这里出；外币（HKD/USD 等）在 `tx_fx`，见 3.1。

| 列 | 类型 | 含义 | 现状 / 注意 |
|---|---|---|---|
| `id` | INTEGER PK | 自增 | 与 `tx_fts.rowid` 一一对应 |
| `mail_id` | INTEGER FK | 来源邮件 | `ON DELETE CASCADE` |
| `card` | TEXT | **卡号后四位** | 常见 2–3 个尾号；早期邮件不写尾号，这部分为空 |
| `tx_date` | TEXT | 交易日期 `YYYY-MM-DD` | 见下方「日期口径」 |
| `tx_time` | TEXT | 交易时间 `HH:MM:SS` | 实测无空值；缺失时写空串 |
| `month` | TEXT **生成列** | `substr(tx_date,1,7)` → `YYYY-MM` | 月度聚合用这个 |
| `year` | INTEGER **生成列** | `substr(tx_date,1,4)` | 年度聚合用 |
| `dow` | INTEGER **生成列** | 星期，0=周日 … 6=周六（`strftime('%w')`） | |
| `hour` | INTEGER **生成列** | `tx_time` 前两位；空则 0 | 分时段分析用 |
| `kind` | TEXT | 交易类型 | 实测只出现 `消费` / `退款` 两种；设计上还预留了 还款/取现/预授权/分期/转账/费用/利息 |
| `channel` | TEXT | 支付渠道 | `微信支付` / `支付宝` / `财付通` / `银联` / 空串（无渠道信息） |
| `merchant` | TEXT | 商户名 | **会被银行截断**（`…北京分公` 少个「司」），导入末尾做前缀合并，但裂条仍可能发生 |
| `amount` | REAL | 金额**绝对值** | 永远 ≥0，别用它算净支出 |
| `sign` | INTEGER | 方向：`1` 支出 / `-1` 退款 | 由 `kind` 决定 |
| `signed_amount` | REAL **生成列** | `round(amount*sign, 2)` | **所有金额聚合都用它** |
| `layout` | TEXT | 来源版式：`A`（少数，老版式）/ `B`（多数） | 排查解析问题用，见下方 |
| `state` | TEXT | 绝大多数为空串，少量 `预授权完成` | 非空表示这笔是预授权最终入账 |

**`layout` 的两个版式**（同一封邮件里 A 命中时丢弃 B，因为 A 的日期/卡号更准）：

| 值 | 结构 | 对应邮件 |
|---|---|---|
| `B` | 时间 → 金额 → `尾号XXXX` 描述 | 每日信用管家（新版，占多数） |
| `A` | 固定列位：日期 / 时间 / 币种 / 描述 / 金额 | 信用管家消费提醒（2017–2020 老版） |

**`tx_date` 的日期口径**：消费提醒里是**交易时间**，电子账单明细里是**记账日**。同一笔可能差 1 天。
做「按月对齐账单」时不要指望两边逐笔相等，正确做法是对月度汇总（现有 `coverage()` 的账单侧核对
就是这么做的，差 <1%）。

**不要直接写这张表**：`month` / `year` / `dow` / `hour` / `signed_amount` 是 STORED 生成列，
手写会报错。要改数据就改 `.eml` 然后重建。

### 3.1 `tx_fx` —— 外币交易（原币入库，**不进人民币总额**）

一行 = 一笔非人民币交易（实测目前只有 HKD，量级为个位数百分比都不到）。邮件里**没有人民币折算
金额**，所以不做折算、也不能与 `tx` 的金额相加；`get_overview` 的 `foreign_currency` 按币种单报。

结构与 `tx` 基本一致，差异如下：

| 列 | 与 `tx` 的差异 |
|---|---|
| `currency` | TEXT NOT NULL，ISO 币种码（`HKD` / `USD` …）。`tx` 没有这列（隐含 CNY） |
| `amount` / `signed_amount` | 单位是**原币**元，不是人民币 |
| `month` | 生成列，同 `tx` |
| `year` / `dow` / `hour` | **没有**这三列——行数太少，不值得为它维护生成列 |

其余列（`mail_id` / `card` / `tx_date` / `tx_time` / `kind` / `channel` / `merchant` / `sign` /
`layout` / `state`）语义与 `tx` 相同。索引：`tx_date` / `mail_id` / `currency`。

- **不进 FTS**：`tx_fts` 只索引 `tx`；`search_transactions` 对 `tx_fx` 用 LIKE 兜底（行数少，够快），
  返回的每条记录带 `currency` 字段，外币金额单独汇总在 `foreign_net`。
- 商户名前缀合并（`normalize_merchants`）覆盖 `tx` / `tx_fx` / `hold` 三张表，
  别名规则与解析层共用 `cmb_parse.merchant_aliases`。
- 聚合工具（`aggregate` / `top_merchants` / 面板）**只统计 `tx`**，外币要看 `get_overview` 或
  `search_transactions`，或自己写 SQL 查 `tx_fx`。

---

## 4. `hold` —— 预授权占用额度（**不是支出**）

酒店、租车的「预授权」会先冻结额度，之后才「预授权完成」真正扣款。两笔都算会把同一笔钱算两遍
（先占 2,000、完成再扣 2,000），所以占用那笔单独进这张表。

| 列 | 类型 | 含义 | 现状 / 注意 |
|---|---|---|---|
| `id` | INTEGER PK | 自增 | |
| `mail_id` | INTEGER FK | 来源邮件 | `ON DELETE CASCADE` |
| `card` | TEXT | 卡号后四位 | |
| `tx_date` | TEXT | 交易日期 | |
| `tx_time` | TEXT | 交易时间 | |
| `merchant` | TEXT | 商户 | 已剥掉「预授权」状态前缀（原实现会存成 `完成 某某租车`） |
| `channel` | TEXT | 支付渠道 | **实测全空**，预授权邮件不写渠道 |
| `amount` | REAL | 冻结金额（正数） | |
| `state` | TEXT | `预授权` / `撤销` | 实测全是 `预授权`，`撤销` 从未出现 |
| `layout` | TEXT | 来源版式 `A` / `B` | |
| `currency` | TEXT | 币种码，默认 `CNY` | v5 新增；外币预授权同样原币记录 |

**判定规则**：解析时按描述里的状态词分流——`预授权`/`撤销` → `hold`；`预授权完成` → `tx` 且
`state='预授权完成'`。两表**不会重复计入**（实测 `hold` 与 `tx` 无任何「同日期+同商户+同金额」重叠）。

> 分析时一般不用这张表。只有做「额度为什么突然少了一截」时才需要 `credit.avail_credit` 掉下去、
> `tx` 里却没有对应消费，那就是被 `hold` 占了。

---

## 5. `bill` —— 电子账单账本（账本级，**不是消费明细**）

一行 = 一期电子账单的表头。这是**银行口径的账本**：应还多少、最低还多少、本期消费多少。

| 列 | 类型 | 含义 | 现状 / 注意 |
|---|---|---|---|
| `id` | INTEGER PK | 自增 | |
| `mail_id` | INTEGER FK **UNIQUE** | 来源邮件，一封账单只能有一行 | `ON DELETE CASCADE` |
| `bill_month` | TEXT | 账期 `YYYY-MM` | = `cycle_end` 的年月，缺失时退回 `due_date` 的年月。实测与邮件月份 100% 一致 |
| `cycle_start` | TEXT | 账单周期起 | **只有新版式（`id`）有值**；老版式为 NULL |
| `cycle_end` | TEXT | 账单周期止 | 同上为 NULL |
| `due_date` | TEXT | 到期还款日 | **全部有值**（都在同一天） |
| `credit_limit` | REAL | 该期信用额度 | **只有新版式有**；老版式 NULL（要用 `credit.limit_credit` 补） |
| `due_amount` | REAL | **本期应还** | 全部有值。是账单总额，不是本期消费额 |
| `min_amount` | REAL | 最低还款额 | 全部有值 |
| `prev_balance` | REAL | 上期余额 | 仅新版式有 |
| `payment` | REAL | 本期还款 | 仅新版式有 |
| `debits` | REAL | 本期消费（含取现/费用） | 仅新版式有 |
| `credits` | REAL | 本期退款/贷记 | 仅新版式有；与 `tx` 侧退款额逐年差 <1% |
| `interest` | REAL | 本期利息 | 仅新版式有，且**实测全为 0.0**（不是「没利息数据」，是那几期确实为 0） |
| `layout` | TEXT | `id`（新版式）/ `pos`（老版式） | 见下 |

**两种版式**（`cmb_parse.py:121`）：

| `layout` | 年代 | 抽取方式 | 能拿到什么 |
|---|---|---|---|
| `id` | 近年新版式 | HTML 里有语义 id：`statementCycle` / `creditLimit` / `paymentDueDate` / `L1rmbLcurrBal` 等 | 全部 12 个字段 |
| `pos` | 早年老版式 | 只有「到期还款日 ￥应还 ＄应还 ￥最低 ＄最低」四个连续单元格，按位置取 | 只有 `due_date` / `due_amount` / `min_amount` |

**关键区分**：`bill.due_amount` ≠ 当月消费额。想算「这个月花了多少」查 `tx`；想算「这期要还多少」查
`bill`。两者差额来自上期余额、还款、退款、费用，属正常。`coverage()` 的账单侧核对就是拿
`bill.debits - bill.credits` 和 `tx` 月度汇总比的。

---

## 6. `credit` —— 额度流水

一行 = 一封「每日信用管家」或「消费提醒」里读到的额度快照。这是额度使用率曲线的唯一来源。

| 列 | 类型 | 含义 | 现状 / 注意 |
|---|---|---|---|
| `id` | INTEGER PK | 自增 | |
| `mail_id` | INTEGER FK **UNIQUE** | 来源邮件 | 一封一行，`ON DELETE CASCADE` |
| `dt` | TEXT | 快照日期 = `mail.mail_date` | |
| `avail_credit` | REAL | **可用额度** | 全部有值——这是本表存在的理由 |
| `limit_credit` | REAL | 该封邮件自带的**信用额度** | 约两成有值（2017–2019 图片式老模板反推出来的），其余 NULL |
| `points` | INTEGER | 积分余额 | 部分为 NULL（邮件里没写积分） |
| `card` | TEXT | 卡号后四位 | **全为空**，版式里拿不到卡号。多卡场景下这张表目前无法分卡，用 `mail.subject` 里的尾号代替 |

**`limit_credit` 为什么只有一部分有值**：2017–2019 的「信用管家消费提醒」把标签做成了图片，单元格里
只剩两个金额，例如 `￥xxxxx.xx`（已用）和 `￥yyyyy.yy`（可用）。用数百组相邻邮件的数字变化
比对后确认：**前一个是已用、后一个是可用**，两者相加正好是整千元的信用额度（含临时额度）。
合计不是整千元时视为没对上，宁可不取——所以有值的那部分都是「能可靠反推出来的」，不是随机采样。

**额度使用率怎么算**：`1 - avail_credit / limit`。`limit` 优先用 `credit.limit_credit`（有值时），
否则按时间就近取 `bill.credit_limit`。这条 fallback 链在 `db.py` 的额度曲线逻辑里，别自己写一套。

---

## 7. `meta` —— 元数据

小 key/value 表，不参与分析，但排查问题必看。

| key | 含义 | 示例 |
|---|---|---|
| `mail_dir` | 导入源目录 | `/path/to/招商银行` |
| `last_sync` | 上次导入完成时间 | `YYYY-MM-DD HH:MM:SS` |
| `mail_count` | 上次导入的邮件数 | 整数 |
| `tx_count` | 上次导入的交易数 | 整数 |
| `tx_fx_count` | 外币交易行数 | 整数 |
| `hold_count` | 预授权行数 | 整数 |
| `bill_count` | 账单期数 | 整数 |
| `credit_count` | 额度快照数 | 整数 |
| `schema_version` | 结构版本 | `4` |

> 这些计数是**上次导入时的快照**，不是实时值。要实时数字就 `SELECT COUNT(*)`；两者不一致说明库被
> 手工改过或导入中断。`schema_version < SCHEMA_VERSION` 时，下次导入会自动全量重建。

---

## 8. `tx_fts` —— 全文索引

FTS5 虚拟表，`tokenize='trigram'`。**不是独立数据**，只是 `tx` 的检索副本（**不含 `tx_fx`**，
外币行走 LIKE）。

| 列 | 来源 |
|---|---|
| `rowid` | = `tx.id`（一一对应，实测无缺口） |
| `merchant` | `tx.merchant` |
| `channel` | `tx.channel` |
| `subject` | 来源邮件的 `mail.subject` |

**为什么用 trigram 而不是默认的 `unicode61`**：后者会把整句中文当成一个 token，搜「星巴克」匹配
不到「星巴克咖啡（国贸店）」。trigram 支持任意中文子串，代价是**不足 3 个字符的查询串不进索引**。

**所以检索有两条路**（`server.py` / `db.py` 的 `search_transactions` 已封装，别绕开）：

1. 查询串 ≥3 字 → `tx_fts MATCH`，空结果再退一次 `LIKE`
2. 查询串 ≤2 字（美团、滴滴、瑞幸）→ 直接 `LIKE '%xx%'`

手工重建：`rebuild_fts(conn)`，或 `DELETE FROM tx_fts` 后重新导入。导入/删除邮件时索引会自动同步
（`_delete_mail` 里先删 FTS 再删 mail）。

---

## 9. 视图（只读，方便手写 SQL）

| 视图 | 粒度 | 列 |
|---|---|---|
| `v_tx` | 一笔交易一行 | `tx` 全字段 + 中文 `weekday` + 来源邮件的 `mail_category` / `mail_subject` / `mail_time` |
| `v_monthly` | 一个月一行 | `month`, `tx_count`, `net_amount`(=SUM signed), `gross_amount`(=SUM amount) |
| `v_merchant` | 一个商户一行 | `merchant`, `tx_count`, `net_amount`, `avg_amount`, `max_amount`（排除空商户） |
| `v_channel` | 一个渠道一行 | `channel`, `tx_count`, `net_amount` |

`v_tx` 干了三件手工 SQL 常忘的事：`dow` 转中文星期、`JOIN mail` 带上分类和主题。日常排查优先用它。

> 视图**没有**过滤「预授权完成」的 `state`，也没有排除 `kind='退款'`。要净支出就用 `net_amount` 或
> `SUM(signed_amount)`，两者已经含了退款负号。

---

## 10. 索引

| 表 | 索引列 | 用途 |
|---|---|---|
| `tx` | `tx_date`, `month`, `year`, `dow`, `merchant`, `kind`, `channel`, `card`, `mail_id` | 时间/维度筛选全覆盖 |
| `tx_fx` | `tx_date`, `mail_id`, `currency` | 外币按日期/币种查 |
| `mail` | `mail_date`, `category` | 按日期区间、按类型筛邮件 |
| `bill` | `bill_month` | 取某期账单 |
| `credit` | `dt` | 额度曲线按时间取 |
| `hold` | `tx_date` | 按日期查预授权 |

`tx.hour` 和 `tx.amount` **没有**独立索引——分时段、金额分档都是范围扫描，近万行全表扫也只有几
毫秒，不值得加。

---

## 11. 口径陷阱（新写查询前必读）

1. **金额永远用 `signed_amount`**。`amount` 是绝对值，`SUM(amount)` 会得到「消费+退款」的总额，
   净支出被虚高。差额正好等于退款总额（本库约占净支出 7%）。
2. **`hold` 不是支出**，不要 `UNION` 进 `tx`。同一笔预授权会在 `hold`（占用）和 `tx`（完成）各出现一次。
3. **`bill.due_amount` 不是消费额**。它是账单应还总额。月度消费查 `tx`。
4. **`bill` 的 12 个字段只有新版式是全的**，老账单只有应还/最低/到期日。
   写查询时对 `cycle_start` / `credits` / `interest` 这些列要么加 `IS NOT NULL`，要么按 `layout` 分支。
5. **`interest` 全 0 不等于「从来没付过利息」**，而是「新版式那几期确实为 0，老版式压根没这个字段」。
6. **`tx_count` 不含 `hold`，但含 `tx_fx`**。所以一封只有预授权的邮件 `tx_count=0`，但它并非空邮件。
7. **`merchant` 可能被截断**。银行短信体限长，`某某财产保险股份有限公司北京分公` 少个「司」，
   同一家可能裂成两行。跨商户聚合前先看 `v_merchant` 里有没有长得很像的两条。
8. **`mail.category='营销/其他'` 不是「解析失败」**。真正的解析失败是 `category='解析失败'`，实测 0 条。
9. **外币不能加进人民币总额**。`tx_fx.amount` 是原币（如 HKD），邮件里没有折算金额；
   `SUM(tx.signed_amount) + SUM(tx_fx.signed_amount)` 是无意义的数字。外币按 `currency` 分组单报。

---

## 12. 常用查询

```sql
-- 最近 6 个月净支出
SELECT month, net_amount FROM v_monthly ORDER BY month DESC LIMIT 6;

-- 某月 Top 10 商户（净额，已含退款）
SELECT merchant, COUNT(*) n, ROUND(SUM(signed_amount),2) amt
FROM tx WHERE month = '2026-10'
GROUP BY merchant ORDER BY amt DESC LIMIT 10;

-- 渠道占比（未标注渠道单独一档）
SELECT COALESCE(NULLIF(channel,''),'未标注') ch, COUNT(*) n,
       ROUND(SUM(signed_amount),2) amt
FROM tx WHERE year = 2026 GROUP BY 1 ORDER BY amt DESC;

-- 周内分布
SELECT weekday, COUNT(*) n, ROUND(SUM(signed_amount),2) amt
FROM v_tx WHERE year = 2026 GROUP BY weekday ORDER BY MIN(dow);

-- 账单 vs 明细对账（仅新版式；当期未走完会明显不等，属正常）
SELECT b.bill_month, b.debits, b.credits,
       (SELECT ROUND(SUM(signed_amount),2) FROM tx t
         WHERE t.month = b.bill_month) AS tx_net
FROM bill b WHERE b.layout = 'id' ORDER BY b.bill_month DESC LIMIT 6;

-- 额度曲线（优先用反推的限额，缺失时用最近一期账单）
SELECT dt, avail_credit, limit_credit FROM credit ORDER BY dt DESC LIMIT 10;

-- 查一户商户的所有交易（含来源邮件）
SELECT tx_date, merchant, amount, sign, channel, mail_subject
FROM v_tx WHERE merchant LIKE '%某某公司%' ORDER BY tx_date DESC;
```

走 MCP 工具时不用写这些：`get_overview` / `aggregate` / `top_merchants` / `search_transactions` /
`compare` / `subscriptions` / `bills` / `open_dashboard` 已经把口径封装好了，手写 SQL 只在工具没覆盖
的维度上才需要（`run_sql` 直接传 SQL）。

---

## 13. 重建与升级

```bash
# 增量导入（新邮件进库，旧的跳过）
python3 mcp/db.py ../招商银行 data/cmb.db

# 全量重建（结构升级、代码改动后必做）
python3 mcp/db.py ../招商银行 data/cmb.db --rebuild

# 核对行数
python3 mcp/db.py --stats data/cmb.db
```

- 改了 `SCHEMA` 或解析逻辑，把 `SCHEMA_VERSION` 加 1；`ensure_schema()` 检测到版本落后会在下次导入时
  自动全量重建。
- `ensure_schema()` 还会做幂等补列（例如给旧库补 `tx.state`），补列不改变版本号。
- 重建约 20 秒 / 3,000 封。数据全在 `.eml` 里，重建可以随时做，**不要**手工 `UPDATE` 关键字段——
  下次重建就被覆盖了。

---

*最后更新：2026-11-10 · schema v6（`mail.file_hash` 内容级变更检测；解析失败落库；增量清理已删邮件）*
