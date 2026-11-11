#!/usr/bin/env python3
"""招行邮件洞察 MCP 服务端（stdio，零依赖）。

数据层是 SQLite（data/cmb.db），由 db.py 从 .eml 导入。
工具：
  get_overview        数据口径与总量
  aggregate           按维度聚合
  top_merchants       商户排行
  compare             环比/同比
  subscriptions       周期扣款（订阅）识别
  search_transactions 明细检索（FTS5 全文）
  run_sql             只读 SQL 查询
  render_chart        生成 PNG 柱状图
  bills               账单列表与对账
  open_dashboard      打开可视化面板（MCP App）
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
from chart_png import bar_chart  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
DB_PATH = os.environ.get("CMB_DB") or os.path.join(PLUGIN_ROOT, "data", "cmb.db")
MAIL_DIR = os.environ.get("CMB_MAIL_DIR") or os.path.join(PLUGIN_ROOT, "..", "招商银行")
DASHBOARD_HTML = os.path.join(PLUGIN_ROOT, "dashboard", "dist", "index.html")
DASHBOARD_URI = "ui://cmb-insight/dashboard"


def _plugin_version() -> str:
    """版本号以 plugin.json 为准：握手信息和清单各写一份迟早会不一致。"""
    try:
        with open(os.path.join(PLUGIN_ROOT, ".codebuddy-plugin", "plugin.json"),
                  encoding="utf-8") as f:
            return str(json.load(f).get("version") or "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


VERSION = _plugin_version()

WEEK_ORDER = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_STATE: dict = {}


def log(msg: str) -> None:
    sys.stderr.write(f"[cmb-insight] {msg}\n")
    sys.stderr.flush()


def conn():
    """惰性打开数据库；库不存在或结构落后时先从邮件目录导入一次。"""
    if "conn" in _STATE:
        return _STATE["conn"]
    if not os.path.exists(DB_PATH):
        log(f"数据库不存在，正在从 {MAIL_DIR} 导入…")
        db.import_dir(os.path.abspath(MAIL_DIR), DB_PATH, quiet=True)
    c = db.connect(DB_PATH)
    if db.ensure_schema(c):
        log(f"库结构已升级（schema v{db.SCHEMA_VERSION}），正在全量重建…")
        c.close()
        db.import_dir(os.path.abspath(MAIL_DIR), DB_PATH, rebuild=True, quiet=True)
        c = db.connect(DB_PATH)
    _STATE["conn"] = c
    return c


def _rows(agg: list[dict]) -> list[dict]:
    """db.aggregate 的列名 → 对外统一的 label/count/amount。"""
    return [{"label": r["name"], "count": r["tx_count"], "amount": r["net_amount"],
             "gross": r["gross_amount"]} for r in agg]


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #

def tool_overview() -> dict:
    c = conn()
    ov = db.overview(c)
    cats = {r[0]: r[1] for r in c.execute(
        "SELECT category, COUNT(*) FROM mail GROUP BY category ORDER BY 2 DESC")}
    refund = c.execute(
        "SELECT COUNT(*) n, ROUND(COALESCE(SUM(amount), 0), 2) v FROM tx WHERE sign = -1"
    ).fetchone()
    cards = {r[0] or "未知": r[1] for r in c.execute(
        "SELECT card, COUNT(*) FROM tx GROUP BY card ORDER BY 2 DESC")}
    return {
        "db_path": os.path.abspath(DB_PATH),
        "mail_dir": MAIL_DIR,
        "mail_count": ov["mails"],
        "mail_categories": cats,
        "transaction_count": ov["transactions"],
        "date_range": {"from": ov["tx_date_range"][0], "to": ov["tx_date_range"][1]},
        "net_spend": ov["net_spend"],
        "gross_spend": ov["gross_spend"],
        "refund_count": refund["n"],
        "refund_amount": refund["v"],
        "avg_amount": ov["avg_amount"],
        "merchant_count": ov["merchants"],
        "repayment_count": ov["repay_records"],
        "repayment_total": ov["repay_total"],
        "hold_count": ov["hold_records"],
        "hold_amount": ov["hold_amount"],
        "bill_count": ov["bill_records"],
        "bill_interest_total": ov["bill_interest"],
        "foreign_currency": ov["foreign_currency"],
        "data_quality": ov["coverage"],
        "cards": cards,
        "caveat": "金额为邮件提醒口径的净额（消费-退款），仅含人民币交易；"
                  "外币交易按原币存在 tx_fx 表、在 foreign_currency 单列，"
                  "邮件没有人民币折算金额，不能与 net_spend 相加。"
                  "预授权只是占用额度、不计入净支出，单独存在 hold 表。"
                  "还款来自自动还款通知，与消费不完全同期，"
                  "二者不可相减当欠款；账本级口径见 bills 工具。",
    }


def tool_bills(limit: int = 12, since=None) -> dict:
    c = conn()
    summary = db.bill_summary(c)
    rows = db.bills(c, limit=limit, since=since)
    usage = db.credit_usage(c, 24)
    if usage.get("latest"):
        summary["latest_avail_credit"] = usage["latest"]["avail"]
        summary["latest_used"] = usage["latest"]["used"]
        summary["latest_used_pct"] = usage["latest"]["pct"]
    return {
        "summary": summary,
        "interest_months": db.interest_months(c),
        "credit_usage": usage,
        "bills": rows,
    }


def tool_aggregate(dimension: str, since=None, until=None, kind=None,
                   card=None, merchant=None, limit=200) -> dict:
    c = conn()
    # 表头合计走全量口径：分组列表带 LIMIT，拿返回行求和会把截断当总量
    totals = db.aggregate_totals(c, since, until, kind=kind, card=card, keyword=merchant)
    rows = _rows(db.aggregate(c, dimension, since, until, kind=kind, card=card,
                              keyword=merchant))
    if dimension == "weekday":
        order = {w: i for i, w in enumerate(WEEK_ORDER)}
        rows.sort(key=lambda x: order.get(x["label"], 99))
    items = rows[: max(1, min(limit, 200))]
    return {"dimension": dimension, "total": totals["count"], "net_spend": totals["net"],
            "group_count": len(rows), "truncated": len(rows) > len(items),
            # 维度为空的交易不进分组（如 card 空），全量合计与分组之和的差在这里交代
            "unlabeled_count": totals["count"] - sum(r["count"] for r in rows),
            "items": items}


def tool_compare(granularity="month", current=None, compare="mom", by=None,
                 since=None, until=None, top=6) -> dict:
    """环比/同比：本期 vs 上一期或去年同期，并可给出拉高/拉低的类别。"""
    return db.compare_periods(conn(), granularity, current, compare, by=by,
                              since=since, until=until, top=top)


def tool_subscriptions(since=None, until=None, cadence=None, min_coverage=0.7,
                       include_inactive=True, limit=50) -> dict:
    """周期扣款/订阅识别。"""
    return db.detect_subscriptions(conn(), since, until, cadence, min_coverage,
                                   include_inactive, limit)


def tool_search(keyword=None, since=None, until=None, kind=None,
                min_amount=None, max_amount=None, limit=30) -> dict:
    c = conn()
    rows = db.search_transactions(c, keyword, since, until, min_amount,
                                  max_amount, kind, limit)
    # 命中数与合计走全量口径：明细被 limit 截断，不能拿返回行当总数
    summary = db.search_summary(c, keyword, since, until, min_amount, max_amount, kind)
    items = [{"date": r["tx_date"], "time": r["tx_time"], "amount": r["amount"],
              "sign": r["sign"], "kind": r["kind"], "channel": r["channel"],
              "merchant": r["merchant"], "card": r["card"],
              "currency": r.get("currency") or "CNY",
              "mail_category": r["mail_category"]} for r in rows]
    # 不同币种不能相加：net_spend 只算 CNY，外币按币种单列（均为全量命中口径）
    return {"matched": summary["matched"],
            "returned": len(items),
            "truncated": summary["matched"] > len(items),
            "net_spend": summary["net_spend"],
            "foreign_net": {f["currency"]: f["net_amount"] for f in summary["foreign"]},
            "items": items}


def tool_sql(sql: str, limit: int = 200) -> dict:
    result = db.run_sql(conn(), sql, limit)
    return result


def tool_render_chart(dimension="month", since=None, until=None, limit=12) -> dict:
    """生成 PNG 柱状图；点阵字模只有数字，故标签做一层映射。"""
    data = tool_aggregate(dimension, since, until, limit=max(limit, 36))
    items = data["items"]
    note = ""
    if dimension == "month":
        items = items[-max(1, min(limit, 36)):]
        pairs = [(f"{i['label'][2:4]}{i['label'][5:7]}", i["amount"]) for i in items]
        note = "横轴为 YYMM"
    elif dimension == "year":
        pairs = [(i["label"], i["amount"]) for i in items]
        note = "横轴为年份"
    elif dimension == "weekday":
        pairs = [(str(n + 1), i["amount"]) for n, i in enumerate(items)]
        note = "横轴 1=周一 … 7=周日"
    elif dimension == "hour":
        pairs = [(i["label"].zfill(2), i["amount"]) for i in items]
        note = "横轴为小时（0-23）"
    elif dimension == "amount_bucket":
        items = items[:9]
        pairs = [(str(n + 1), i["count"]) for n, i in enumerate(items)]
        note = ("横轴 1=≤10 2=10-30 3=30-50 4=50-100 5=100-200 6=200-500 "
                "7=500-1k 8=1k-3k 9=>3k，纵轴为笔数")
    else:
        pairs = [(str(n + 1), i["amount"]) for n, i in enumerate(items)]
        note = "横轴为排名，名称见下方清单"

    png = bar_chart(pairs)
    listing = "\n".join(f"{n + 1}. {i['label']}  {i['amount']:,.2f} 元（{i['count']} 笔）"
                        for n, i in enumerate(items[: max(1, min(limit, 20))]))
    caption = (f"{dimension} 柱状图，纵轴单位：元（amount_bucket 为笔数）。{note}。"
               f"区间 {since or '起'} ~ {until or '今'}，共 {data['total']} 笔。")
    return {
        "content": [
            {"type": "image", "data": base64.b64encode(png).decode("ascii"),
             "mimeType": "image/png"},
            {"type": "text", "text": caption + ("\n" + listing if listing else "")},
        ],
        "structuredContent": {"dimension": dimension, "items": items, "png_bytes": len(png)},
    }


def dashboard_payload() -> dict:
    return db.dashboard_payload(conn())


def _payload_uri() -> str:
    row = conn().execute(
        "SELECT COUNT(*) a, ROUND(COALESCE(SUM(signed_amount), 0), 2) b, MAX(tx_date) c FROM tx"
    ).fetchone()
    return f"{DASHBOARD_URI}?v={row['a']}-{int(row['b'])}-{row['c'] or ''}"


# --------------------------------------------------------------------------- #
# MCP 清单
# --------------------------------------------------------------------------- #

SCHEMA_HINT = (
    "库结构（SQLite）："
    "mail(id, file, subject, mail_time, mail_date, category, size_bytes, tx_count, "
    "bill_amount, body_text)；"
    "tx(id, mail_id, card, tx_date, tx_time, month, year, dow, hour, kind, channel, "
    "merchant, amount, sign, signed_amount, layout, state) 只含人民币交易；"
    "tx_fx(id, mail_id, card, tx_date, tx_time, month, kind, channel, merchant, "
    "currency, amount, sign, signed_amount, layout, state) 是外币交易（原币金额，"
    "currency 如 HKD/USD），不能与 tx 的金额相加；"
    "hold(id, mail_id, card, tx_date, tx_time, merchant, channel, amount, state, layout) "
    "是预授权占用额度，不是支出，不进 tx；"
    "bill(id, mail_id, bill_month, cycle_start, cycle_end, due_date, credit_limit, "
    "due_amount, min_amount, prev_balance, payment, debits, credits, interest, layout) "
    "是电子账单表头（账本级口径）；"
    "视图 v_tx / v_monthly / v_merchant / v_channel；"
    "全文索引 tx_fts(merchant, channel, subject)，trigram 分词。"
    "signed_amount = amount * sign，退款 sign=-1。"
)

TOOLS = [
    {
        "name": "get_overview",
        "description": "返回招行信用卡邮件数据集的口径与总量：邮件数、交易笔数、时间跨度、净支出、退款、还款合计、卡号分布。回答任何汇总类问题前先调它确认口径。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "aggregate",
        "description": "按单一维度聚合交易。dimension 可选 year/month/weekday/hour/kind/channel/merchant/card/amount_bucket。返回每项笔数与净金额（消费-退款）。用于月度趋势、渠道占比、金额分布等。total/net_spend 是筛选后的全量合计，不受 limit 影响；items 最多返回 limit 个分组，truncated=true 表示分组有截断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": ["year", "month", "weekday", "hour",
                                                         "kind", "channel", "merchant", "card",
                                                         "amount_bucket"]},
                "since": {"type": "string", "description": "起始日期 YYYY-MM-DD（含）"},
                "until": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
                "kind": {"type": "string", "description": "交易类型过滤，如 消费 / 退款"},
                "card": {"type": "string", "description": "卡号后四位"},
                "merchant": {"type": "string", "description": "商户名包含的片段"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["dimension"],
        },
    },
    {
        "name": "top_merchants",
        "description": "商户消费排行，按净金额降序。适合回答「钱主要花在哪」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "compare",
        "description": "环比/同比对比：本期净支出 vs 上一期（mom）或去年同期（yoy），返回差额、变化率，以及按维度拆出的「主要变动项」（哪些商户/类型把差额推上去或拉下来）。回答「这个月比上个月多花多少」「为什么涨了」用这个，不要自己写 SQL。当期未走完时会自动跳到上一个完整周期并说明。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "granularity": {"type": "string", "enum": ["month", "year"], "default": "month"},
                "current": {"type": "string",
                            "description": "本期标签，如 2026-10 或 2026；省略则取最近一个完整周期"},
                "compare": {"type": "string", "enum": ["mom", "yoy"], "default": "mom",
                            "description": "mom=环比（比上一期），yoy=同比（比去年同期）"},
                "by": {"type": "string", "enum": ["kind", "channel", "merchant", "card"],
                       "description": "差额按该维度拆解，建议默认传 merchant"},
                "top": {"type": "integer", "default": 6, "description": "返回几个变动项"},
                "since": {"type": "string"},
                "until": {"type": "string"},
            },
        },
    },
    {
        "name": "subscriptions",
        "description": "识别周期扣款/订阅：按「同一商户、扣款间隔规律、每期金额接近」自动发现，返回周期（月度/季度/年度）、单期金额、折合月成本与年成本、上次扣款与下次预计、是否疑似已停。用于回答「我有哪些订阅/固定支出」「每月固定开销多少」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "cadence": {"type": "string", "enum": ["monthly", "quarterly", "yearly"]},
                "min_coverage": {"type": "number", "default": 0.7,
                                 "description": "周期覆盖率下限，调低（如 0.5）能多找出不规律的固定支出"},
                "include_inactive": {"type": "boolean", "default": True,
                                     "description": "是否包含疑似已停止的扣款"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "search_transactions",
        "description": "按关键词/时间/金额检索单笔交易明细。关键词走 FTS5 全文索引（≥3 字走 trigram 子串匹配，2 字走 LIKE），匹配商户、渠道与邮件主题。matched/net_spend/foreign_net 为全部命中的合计口径；items 最多返回 limit 条明细（truncated=true 表示有截断），报总额时直接用合计字段，不要对 items 求和。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "kind": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "run_sql",
        "description": "对 SQLite 库执行只读 SQL（仅 SELECT / WITH），返回列名与行。用于 aggregate 覆盖不到的复杂问题，比如多表联结、窗口函数、按商户×月的交叉统计。" + SCHEMA_HINT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "单条只读 SQL，不要带分号"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "render_chart",
        "description": "生成一张 PNG 柱状图并直接返回图片，用于在回答里嵌入图表。dimension 同 aggregate；返回图片 + 文字说明（横轴含义、区间、金额清单）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": ["month", "year", "weekday", "hour",
                                                         "merchant", "channel",
                                                         "amount_bucket"]},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "default": 12},
            },
            "required": ["dimension"],
        },
    },
    {
        "name": "bills",
        "description": "账本级视图：电子账单的信用额度、本期应还、最低还款、到期还款日、本期消费/退款、利息，以及每期与提醒流的对账差额。回答「额度多少/这期该还多少/什么时候到期/有没有利息/提醒数据准不准」时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 12, "description": "返回最近多少期"},
                "since": {"type": "string", "description": "只看该日期之后的账单期 YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "open_dashboard",
        "description": "在对话下方打开招行消费可视化面板（Vue3 + ECharts）：顶部账单与额度（应还/到期日/可用额度/使用率/对账）、月度趋势下钻、类型占比、渠道/商户排行、星期×小时热力图、金额分布、明细表，支持联动筛选与按月播放。用户想看图表或说「打开面板/看板」时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"focus": {"type": "string",
                                     "enum": ["month", "merchant", "kind", "heatmap", "amount"]}},
        },
        "_meta": {"ui": {"resourceUri": DASHBOARD_URI}},
    },
]


# --------------------------------------------------------------------------- #
# 面板渲染
# --------------------------------------------------------------------------- #

def render_dashboard() -> str:
    """把构建好的 Vue 单文件读进来，注入 gzip+base64 的紧凑载荷。"""
    if not os.path.exists(DASHBOARD_HTML):
        raise RuntimeError(
            "面板尚未构建。请先执行：cd cmb-insight/dashboard && npm install && npm run build")
    with open(DASHBOARD_HTML, encoding="utf-8") as fh:
        tpl = fh.read()
    payload = dashboard_payload()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    return tpl.replace("__CMB_DATA_B64__", blob)


def text_of(obj) -> str:
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 调用分发
# --------------------------------------------------------------------------- #

def call_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name == "get_overview":
        data = tool_overview()
        summary = (f"{data['mail_count']} 封邮件、{data['transaction_count']} 笔交易，"
                   f"时间跨度 {data['date_range']['from']} ~ {data['date_range']['to']}，"
                   f"净支出 {data['net_spend']:,.2f} 元，退款 {data['refund_count']} 笔共 "
                   f"{data['refund_amount']:,.2f} 元，还款 {data['repayment_count']} 笔共 "
                   f"{data['repayment_total']:,.2f} 元；预授权 {data['hold_count']} 笔 "
                   f"{data['hold_amount']:,.2f} 元（占用额度，不计入净支出，单列）。")
        if data.get("foreign_currency"):
            fx_txt = "、".join(f"{i['currency']} {i['net_amount']:,.2f}（{i['count']} 笔）"
                              for i in data["foreign_currency"])
            summary += f"\n外币交易（原币单列，不计入净支出）：{fx_txt}。"
        q = data["data_quality"]
        if q["missing_months"]:
            summary += f"\n注意：{len(q['missing_months'])} 个月没有任何交易（{', '.join(q['missing_months'][:6])}）。"
        if q["low_months"]:
            low = "、".join(f"{i['month']}({i['tx']} 笔)" for i in q["low_months"][:6])
            summary += f"\n低覆盖月（疑似漏邮件）：{low}，月均中位数 {q['median_tx_per_month']} 笔。"
        return {"content": [{"type": "text", "text": summary + "\n\n" + text_of(data)}],
                "structuredContent": data}
    if name == "aggregate":
        data = tool_aggregate(args.get("dimension", "month"), args.get("since"),
                              args.get("until"), args.get("kind"), args.get("card"),
                              args.get("merchant"), int(args.get("limit", 200)))
        lines = [f"{i['label']}\t{i['count']} 笔\t{i['amount']:,.2f} 元" for i in data["items"]]
        head = f"{data['dimension']} 聚合，共 {data['total']} 笔，净支出 {data['net_spend']:,.2f} 元"
        if data.get("truncated"):
            head += (f"（共 {data['group_count']} 个分组，仅列出前 {len(data['items'])} 个；"
                     f"笔数与净支出为全量合计，不受显示截断影响）")
        if data.get("unlabeled_count"):
            head += f"（另有 {data['unlabeled_count']} 笔该维度为空，未列入分组）"
        return {"content": [{"type": "text", "text": head + "\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "top_merchants":
        data = tool_aggregate("merchant", args.get("since"), args.get("until"),
                              limit=int(args.get("limit", 15)))
        lines = [f"{n + 1}. {i['label']}\t{i['count']} 笔\t{i['amount']:,.2f} 元"
                 for n, i in enumerate(data["items"])]
        return {"content": [{"type": "text", "text": "商户排行（净金额）\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "compare":
        data = tool_compare(args.get("granularity", "month"), args.get("current"),
                            args.get("compare", "mom"), args.get("by"),
                            args.get("since"), args.get("until"),
                            int(args.get("top", 6)))
        if data.get("error"):
            return {"content": [{"type": "text", "text": data["error"]}], "isError": True}
        p, b = data["period"], data["baseline"]
        sign = "+" if data["delta"] >= 0 else ""
        pct = "—" if data["pct"] is None else f"{sign}{data['pct']}%"
        head = (f"{p['label']} 净支出 {p['net']:,.2f} 元（{p['count']} 笔），"
                f"{data['compare_label']} {b['label']} 的 {b['net']:,.2f} 元，"
                f"差额 {sign}{data['delta']:,.2f}（{pct}）。")
        if data.get("skipped_partial"):
            head += f"（{data['skipped_partial']} 尚未走完，已跳过）"
        if data["period"].get("missing"):
            head += "（该期没有任何交易）"
        lines = [f"{m['label']}\t{m['net']:>10,.2f}\t较基期 {m['delta']:+,.2f}"
                 + (f"\t{m['pct']:+.1f}%" if m["pct"] is not None else "")
                 for m in data["movers"]]
        if lines:
            head += "\n主要变动："
        return {"content": [{"type": "text", "text": head + ("\n" + "\n".join(lines) if lines else "")}],
                "structuredContent": data}
    if name == "subscriptions":
        data = tool_subscriptions(args.get("since"), args.get("until"),
                                  args.get("cadence"), float(args.get("min_coverage", 0.7)),
                                  bool(args.get("include_inactive", True)),
                                  int(args.get("limit", 50)))
        s = data["summary"]
        head = (f"识别到 {s['found']} 个周期扣款（活跃 {s['active']} 个），"
                f"合计每月 {s['monthly_total']:,.2f} 元、每年 {s['annual_total']:,.2f} 元。")
        lines = [f"{i['merchant']}\t{i['cadence_label']}\t单期 {i['amount']:,.2f}\t"
                 f"月均 {i['monthly_cost']:,.2f}\t上次 {i['last_date']}\t"
                 f"下次约 {i['next_expected']}\t{i['stability']}"
                 + ("" if i["active"] else "\t（疑似已停）")
                 for i in data["items"]]
        return {"content": [{"type": "text", "text": head + ("\n" + "\n".join(lines) if lines else "")}],
                "structuredContent": data}
    if name == "search_transactions":
        data = tool_search(args.get("keyword"), args.get("since"), args.get("until"),
                           args.get("kind"), args.get("min_amount"),
                           args.get("max_amount"), int(args.get("limit", 30)))
        lines = [f"{i['date']} {i['time']}\t{i['amount']:>9,.2f}"
                 + (f" {i['currency']}" if i["currency"] != "CNY" else "")
                 + f"\t{i['merchant']}" for i in data["items"]]
        head = f"命中 {data['matched']} 笔，人民币合计 {data['net_spend']:,.2f} 元"
        if data.get("foreign_net"):
            head += "；外币 " + "、".join(f"{v:,.2f} {k}" for k, v in data["foreign_net"].items())
        if data.get("truncated"):
            head += f"（笔数与金额为全部命中口径，以下仅列出最近 {data['returned']} 笔）"
        return {"content": [{"type": "text", "text": head + "\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "run_sql":
        sql = args.get("sql") or ""
        try:
            data = tool_sql(sql, int(args.get("limit", 200)))
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"SQL 执行失败：{exc}"}],
                    "isError": True}
        table = [("\t".join(data["columns"]))]
        table += ["\t".join("" if v is None else str(v) for v in r) for r in data["rows"]]
        return {"content": [{"type": "text", "text": "\n".join(table)}],
                "structuredContent": data}
    if name == "render_chart":
        return tool_render_chart(args.get("dimension", "month"), args.get("since"),
                                 args.get("until"), int(args.get("limit", 12)))
    if name == "bills":
        data = tool_bills(int(args.get("limit", 12)), args.get("since"))
        s = data["summary"]
        latest = s["latest"] or {}
        head = (f"账本级：{s['bill_count']} 期账单（{s['range'][0]} ~ {s['range'][1]}），"
                f"信用额度 {s['credit_limit'] or '-'}，累计利息 {s['interest_total']:,.2f} 元；"
                f"最新一期 {latest.get('bill_month', '-')} 应还 {latest.get('due_amount') or 0:,.2f}，"
                f"最低 {latest.get('min_amount') or 0:,.2f}，到期 {latest.get('due_date') or '-'}。")
        if s.get("latest_used_pct") is not None:
            head += (f"\n当前已用额度 {s['latest_used']:,.2f}（{s['latest_used_pct']}%），"
                     f"可用 {s['latest_avail_credit']:,.2f}。")
        lines = []
        for b in data["bills"]:
            diff = b.get("reconcile_diff")
            spend = b["debits"]
            spend_txt = f"{spend:>10,.2f}" if spend is not None else f"{'—':>10}"
            lines.append(
                f"{b['bill_month']}\t应还 {b['due_amount'] or 0:>10,.2f}\t"
                f"消费 {spend_txt}\t利息 {b['interest'] or 0:>7,.2f}\t到期 {b['due_date'] or '-'}"
                + (f"\t对账差 {diff:,.2f}" if diff is not None else ""))
        return {"content": [{"type": "text", "text": head + "\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "open_dashboard":
        data = dashboard_payload()
        return {
            "content": [{"type": "text",
                         "text": f"已打开招行消费面板：{len(data['txs'])} 笔交易、"
                                 f"{len(data['repays'])} 条还款记录，可在面板中按年份/类型/卡号/"
                                 f"渠道/商户筛选，点柱子下钻月份，或按「按月播放」看趋势演进。"}],
            "structuredContent": {"opened": True, "rows": len(data["txs"]),
                                  "focus": args.get("focus", "month")},
            "_meta": {"ui": {"resourceUri": _payload_uri()}},
        }
    raise ValueError(f"未知工具: {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        client_ver = params.get("protocolVersion") or "2024-11-05"
        return ok({
            "protocolVersion": client_ver,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False, "subscribe": False}},
            "serverInfo": {"name": "cmb-insight", "version": VERSION},
            "instructions": "招商银行信用卡邮件洞察，数据存放在 SQLite（data/cmb.db）。"
                            "查汇总先调 get_overview 对齐口径；复杂统计用 run_sql；"
                            "要图表调 open_dashboard。金额单位为人民币元。",
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        try:
            return ok(call_tool(name, params.get("arguments") or {}))
        except Exception as exc:
            log(traceback.format_exc())
            return ok({"content": [{"type": "text", "text": f"工具执行失败: {exc}"}],
                       "isError": True})
    if method == "resources/list":
        return ok({"resources": [{
            "uri": DASHBOARD_URI,
            "name": "招行消费面板",
            "description": "招行信用卡邮件的可交互消费可视化面板（Vue3 + ECharts）",
            "mimeType": "text/html;profile=mcp-app",
        }]})
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        if uri.startswith(DASHBOARD_URI):
            return ok({"contents": [{
                "uri": uri,
                "mimeType": "text/html;profile=mcp-app",
                "text": render_dashboard(),
            }]})
        return err(-32002, f"未知资源: {uri}")
    if method == "prompts/list":
        return ok({"prompts": []})
    return err(-32601, f"不支持的方法: {method}")


def main() -> int:
    log(f"start, db={os.path.abspath(DB_PATH)}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception as exc:
            log(traceback.format_exc())
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
