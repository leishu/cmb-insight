---
name: cmb-consumption-insight
description: "招商银行信用卡消费分析。当用户问招行信用卡花了多少钱、某月/某年支出多少、钱花在哪些商户、什么时候刷卡最多、找某笔消费、要消费图表，或者要写 SQL 查账单时使用。关键词：招行、招商银行、信用卡账单、消费分析、支出统计、商户排行、账单面板、sqlite 查账单。"
version: 0.5.0
author: leishu
---

# 招行信用卡消费分析

数据来自本地导出的招行信用卡邮件（`.eml`），已由 `mcp/db.py` 导入 SQLite（`cmb-insight/data/cmb.db`）。
回答任何金额问题前，**先调 `get_overview` 对齐口径**，不要凭空估算。

## 可用工具

| 工具 | 用途 |
|---|---|
| `get_overview` | 数据口径：邮件数、交易笔数、时间跨度、净支出、退款、还款合计、卡号分布、**外币单列**、**数据体检**（缺月/低覆盖月） |
| `aggregate` | 单维度聚合，9 个维度：year/month/weekday/hour/kind/channel/merchant/card/amount_bucket；可再用 `since`/`until`/`kind`/`card`/`merchant` 过滤 |
| `top_merchants` | 商户排行（按净金额） |
| `compare` | **环比/同比**：本期 vs 上期或去年同期的差额与变化率，`by` 指定维度可直接给出「主要变动项」 |
| `subscriptions` | **周期扣款识别**：按间隔规律 + 金额稳定自动找订阅/固定支出，给出折合月成本、年成本、下次预计扣款 |
| `search_transactions` | 按关键词、时间、金额区间查明细（≥3 字走 FTS5，短词自动退化为 LIKE） |
| `bills` | **账本级**：信用额度、本期应还、最低还款、到期还款日、本期消费/退款、利息、与提醒流的对账差额、额度使用率 |
| `run_sql` | 对 SQLite 执行只读 SQL，用于聚合工具覆盖不到的复杂统计 |
| `render_chart` | 生成 PNG 柱状图并返回图片，可直接嵌在回答里 |
| `open_dashboard` | 打开 Vue3 + ECharts 可视化面板（趋势下钻/类型/渠道/商户/热力图/金额分布/账单与额度/明细，可交互） |

## 库结构

七张表：一封邮件 = `mail` 一行，明细/外币/额度/账单/预授权各自挂钩到 `mail_id`。

```
mail   (id, file, subject, mail_time, category, size_bytes,
        file_hash, tx_count, bill_amount, body_text, imported_at)
tx     (id, mail_id, card, tx_date, tx_time, kind, channel, merchant,
        amount, sign, layout, state)          -- 仅人民币
tx_fx  (id, mail_id, card, tx_date, tx_time, kind, channel, merchant,
        currency, amount, sign, layout, state) -- 外币原币，currency 如 HKD
hold   (id, mail_id, card, tx_date, tx_time, merchant, channel, amount, state, layout, currency)
bill   (id, mail_id, bill_month, cycle_start, cycle_end, due_date, credit_limit,
        due_amount, min_amount, prev_balance, payment, debits, credits, interest, layout)
credit (id, mail_id, dt, avail_credit, limit_credit, points, card)
meta   (key, value)   -- schema_version / *_count（含 tx_fx_count）/ last_sync / mail_dir
```

**生成列**（`pragma table_info` 里看不到，但可以直接查，永远别手动写、别 INSERT）：
`mail.mail_date`（= `substr(mail_time,1,10)`）；`tx.month`(YYYY-MM) / `tx.year` / `tx.dow`(0=周日) /
`tx.hour` / `tx.signed_amount`；`tx_fx` 只有 `month` / `signed_amount`。日期过滤优先用生成列，别在 `tx_date` 上做字符串函数。

```
视图：v_tx（tx + 邮件的 category/subject）/ v_monthly / v_merchant / v_channel
全文索引：tx_fts(merchant, channel, subject)，trigram 分词，rowid = tx.id
```

各表分工：

| 表 | 装什么 | 什么时候查 |
|---|---|---|
| `mail` | 邮件本身（主题、时间、分类、正文） | 追数据来源、查某封信解析是否正常 |
| `tx` | 真正入账的人民币交易明细 | 几乎所有消费统计 |
| `tx_fx` | 外币交易（HKD 等），**原币金额** | 问外币消费；不能与 `tx` 金额相加 |
| `hold` | 预授权冻结，只占额度、**不算支出** | 问「为什么可用额度少了但没扣钱」 |
| `bill` | 每期账单（额度、应还、最低、到期日、消费/退款/利息） | 账本级数字只能从这来 |
| `credit` | 额度快照（可用额度、信用额度、积分） | 额度曲线、使用率 |
| `meta` | 导入元信息 | 判断数据新旧、schema 版本 |

逐列的完整说明（类型、来源、空值语义、口径陷阱）见仓库根目录 `SCHEMA.md`。

`signed_amount = amount * sign`，退款 `sign = -1`。聚合金额一律用 `signed_amount`。

**预授权不在 `tx` 里**：占用额度的那笔在 `hold`，真正入账的「预授权完成」在 `tx` 且 `state='预授权完成'`、`kind='消费'`。算支出只用 `tx`，问「占用多少额度」才看 `hold`。

**`aggregate` 的返回与归一**：每行是 `name` / `tx_count` / `net_amount`（消费−退款）/ `gross_amount`（不冲减退款）。空值会被归一成标签——`kind` 空→`未分类`、`channel` 空→`未标注`、`card` 空→该行直接被过滤掉（不返回空名分组）。`amount_bucket` 固定 9 档（≤10 / 10-30 / 30-50 / 50-100 / 100-200 / 200-500 / 500-1k / 1k-3k / >3k），与面板一致。只有 `month`/`year`/`hour` 按名称升序，`amount_bucket` 按金额下限升序，其余按 `net_amount` 降序。

## 什么时候用 run_sql

`aggregate` 只能单维度。遇到这类问题写 SQL：

- 商户 × 月份的交叉统计（想看某家店月度消费走势）
- 窗口函数，如「每月消费环比」「每笔距上一笔的间隔」
- 多表联结，如「哪些交易来自『每日信用管家』邮件且金额 > 500」
- 百分位、去重计数、HAVING 过滤后的排名

示例：

```sql
-- 每年前三商户
SELECT year, merchant, total FROM (
  SELECT year, merchant, ROUND(SUM(signed_amount),2) total,
         ROW_NUMBER() OVER (PARTITION BY year ORDER BY SUM(signed_amount) DESC) rn
  FROM tx GROUP BY year, merchant
) WHERE rn <= 3 ORDER BY year, rn
```

只允许单条 SELECT/WITH，写操作会被拒绝。

## 口径（必须向用户交代）

1. **金额是净额**：退款按负数冲减，`net_spend = 消费 - 退款`。退货在邮件里写作 `CNY -xxx`，已按退款入库。
2. **交易来自两类提醒邮件**：早期「消费提醒」一封一笔，近年「每日信用管家」一封含多笔（已按版式分开解析）；只有被银行合并发送的极少数会偏少。
3. **预授权不算支出**：只占用额度，存在 `hold` 表；真正扣款由后续「预授权完成」体现。
4. **还款数据独立**：来自「自动还款扣款通知」，与消费不同期，不能直接相减当作欠款。
5. **账本级数只能来自 `bills`**：额度、应还、最低还款、到期日、利息都不是提醒流能算出来的。
6. **时间以邮件内交易时间为准**，不是邮件发送时间。
7. **`mail.tx_count` 不含 `hold`（但含 `tx_fx`）**：一封只发了预授权冻结的邮件 `tx_count = 0`，
   但它不是空邮件，查「有没有漏解析」时要把 `hold` 一起算进去。
8. **数据可能缺月**：`get_overview` 里的 `data_quality` 会标出低覆盖月（如某月只有 3 笔）
   和解析失败的邮件文件名（`parse_failed_files`，正常为空），
   回答趋势类问题时若用户问到的月份在名单里，要主动说明「该月邮件可能缺失」。
9. **外币原币单列，不进人民币总额**：邮件里没有人民币折算金额，HKD 等交易存 `tx_fx`。
   `get_overview` 的 `foreign_currency` 按币种单报；`aggregate`/`top_merchants`/面板只统计 `tx`（CNY）。
   `search_transactions` 会同时搜两张表，每条带 `currency`，外币净额在 `foreign_net` 里单列。
10. **2017–2019 的额度是反推值**：老版式模板的标签是图片，额度由「已用 + 可用」相加得出，
   取不到时宁可为空；与账单 `credit_limit` 不一致时以邮件自带值为准。

## 回答套路

- **总额类**：`get_overview` → 报净支出 + 时间跨度 + 笔数，并说明这是「提醒口径」。
- **额度/账单/到期日类**：`bills` → 报本期应还、最低还款、到期日、可用额度与使用率；
  注意 `due_amount` 是应还账单，不是当期消费额。
- **趋势类**：`aggregate(dimension="month")` → 只说关键拐点和极值，不要把 100 个月念一遍。
- **比较类**（「这个月比上个月多花多少」「为什么涨了」）：`compare(compare="mom", by="merchant")`
  → 先报差额与变化率，再点出 `movers` 里贡献最大的 1–2 项。不要自己写 SQL 算环比。
  跨年对比用 `compare="yoy"`；整年对比用 `granularity="year"`。
- **固定支出类**（「我有哪些订阅」「每月固定扣多少」）：`subscriptions` → 报活跃项数量、
  每月/每年合计，再列明细；`stability` 是「波动较大」的要说明它更像按量计费的账单（如话费）而非订阅。
- **结构类**：`top_merchants` 或 `aggregate(dimension="channel")` → 给前三和占比。
- **找单笔**：`search_transactions(keyword=...)` → 命中多就按金额降序给前几条。
- **数据可信度**：用户质疑数字时用 `bills` 的 `reconcile_diff`（账单消费 − 提醒流净支出）说明差在哪。
- **复杂问题**：`run_sql` → 先看一眼表结构再写，别猜列名。
- **要单张图**：`render_chart(dimension=...)` → 图片会直接进回答，配上一句结论即可，不要再抄一遍数字。
- **要交互分析**：`open_dashboard` → 面板渲染后**不要再重复念一遍数据**，只说「已打开，可按年份/类型筛选，点柱子能下钻到月」。

## 注意

- 金额单位是人民币元（`tx_fx` 除外，那是原币），展示时保留两位小数。
- 用户说「上次」「最近」时，先确认当前日期，再用 `since` 过滤，别默认全年。
- 面板数据是打开那一刻的快照；新增邮件后先跑 `python mcp/db.py 招商银行 data/cmb.db` 增量导入，再重新 `open_dashboard`。
