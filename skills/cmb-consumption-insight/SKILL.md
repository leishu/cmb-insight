---
name: cmb-consumption-insight
description: "招商银行信用卡消费分析。当用户问招行信用卡花了多少钱、某月/某年支出多少、钱花在哪些商户、什么时候刷卡最多、找某笔消费、要消费图表，或者要写 SQL 查账单时使用。关键词：招行、招商银行、信用卡账单、消费分析、支出统计、商户排行、账单面板、sqlite 查账单。"
version: 0.2.0
author: leishu
---

# 招行信用卡消费分析

数据来自本地导出的招行信用卡邮件（`.eml`），已由 `mcp/db.py` 导入 SQLite（`cmb-insight/data/cmb.db`）。
回答任何金额问题前，**先调 `get_overview` 对齐口径**，不要凭空估算。

## 可用工具

| 工具 | 用途 |
|---|---|
| `get_overview` | 数据口径：邮件数、交易笔数、时间跨度、净支出、退款、还款合计、卡号分布 |
| `aggregate` | 按 year/month/weekday/hour/kind/channel/merchant/card/amount_bucket 聚合 |
| `top_merchants` | 商户排行（按净金额） |
| `search_transactions` | 按关键词、时间、金额区间查明细（走 FTS5 全文索引） |
| `run_sql` | 对 SQLite 执行只读 SQL，用于聚合工具覆盖不到的复杂统计 |
| `render_chart` | 生成 PNG 柱状图并返回图片，可直接嵌在回答里 |
| `open_dashboard` | 打开 Vue3 + ECharts 可视化面板（趋势下钻/类型/渠道/商户/热力图/金额分布/明细，可交互） |

## 库结构

```
mail(id, file, subject, mail_time, mail_date, category, size_bytes,
     tx_count, bill_amount, body_text)
tx(id, mail_id, card, tx_date, tx_time, month, year, dow, hour,
   kind, channel, merchant, amount, sign, signed_amount, layout)
视图：v_tx / v_monthly / v_merchant / v_channel
全文索引：tx_fts(merchant, channel, subject)，trigram 分词
```

`signed_amount = amount * sign`，退款 `sign = -1`。聚合金额一律用 `signed_amount`。

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

1. **金额是净额**：退款按负数冲减，`net_spend = 消费 - 退款`。
2. **交易来自消费提醒邮件**，一笔提醒一笔交易；极少数被银行合并提醒的会偏少。
3. **还款数据独立**：来自「自动还款扣款通知」，与消费不同期，不能直接相减当作欠款。
4. **时间以邮件内交易时间为准**，不是邮件发送时间。
5. 早期（2017–2020）邮件只有单笔提醒，近年「每日信用管家」一封含多笔，解析已兼容。
6. 约 20 笔交易的类型字段为空（银行描述里没写），面板里归到「未分类」。

## 回答套路

- **总额类**：`get_overview` → 报净支出 + 时间跨度 + 笔数，并说明这是「提醒口径」。
- **趋势类**：`aggregate(dimension="month")` → 只说关键拐点和极值，不要把 100 个月念一遍。
- **结构类**：`top_merchants` 或 `aggregate(dimension="channel")` → 给前三和占比。
- **找单笔**：`search_transactions(keyword=...)` → 命中多就按金额降序给前几条。
- **复杂问题**：`run_sql` → 先看一眼表结构再写，别猜列名。
- **要单张图**：`render_chart(dimension=...)` → 图片会直接进回答，配上一句结论即可，不要再抄一遍数字。
- **要交互分析**：`open_dashboard` → 面板渲染后**不要再重复念一遍数据**，只说「已打开，可按年份/类型筛选，点柱子能下钻到月」。

## 注意

- 金额单位是人民币元，展示时保留两位小数。
- 用户说「上次」「最近」时，先确认当前日期，再用 `since` 过滤，别默认全年。
- 面板数据是打开那一刻的快照；新增邮件后先跑 `python mcp/db.py 招商银行 data/cmb.db` 增量导入，再重新 `open_dashboard`。
