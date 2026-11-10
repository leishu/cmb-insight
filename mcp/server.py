#!/usr/bin/env python3
"""招行邮件洞察 MCP 服务端（stdio，零依赖）。

数据层是 SQLite（data/cmb.db），由 db.py 从 .eml 导入。
工具：
  get_overview        数据口径与总量
  aggregate           按维度聚合
  top_merchants       商户排行
  search_transactions 明细检索（FTS5 全文）
  run_sql             只读 SQL 查询
  render_chart        生成 PNG 柱状图
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

WEEK_ORDER = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_STATE: dict = {}


def log(msg: str) -> None:
    sys.stderr.write(f"[cmb-insight] {msg}\n")
    sys.stderr.flush()


def conn():
    """惰性打开数据库；库不存在时先从邮件目录导入一次。"""
    if "conn" in _STATE:
        return _STATE["conn"]
    if not os.path.exists(DB_PATH):
        log(f"数据库不存在，正在从 {MAIL_DIR} 导入…")
        db.import_dir(os.path.abspath(MAIL_DIR), DB_PATH, quiet=True)
    _STATE["conn"] = db.connect(DB_PATH)
    return _STATE["conn"]


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
        "cards": cards,
        "caveat": "金额为邮件提醒口径的净额（消费-退款）；还款来自自动还款通知，"
                  "与消费不完全同期，二者不可相减当欠款。",
    }


def tool_aggregate(dimension: str, since=None, until=None, kind=None,
                   card=None, merchant=None, limit=200) -> dict:
    rows = _rows(db.aggregate(conn(), dimension, since, until, kind=kind, card=card,
                              keyword=merchant, limit=limit))
    if dimension == "weekday":
        order = {w: i for i, w in enumerate(WEEK_ORDER)}
        rows.sort(key=lambda x: order.get(x["label"], 99))
    total = sum(r["count"] for r in rows)
    net = round(sum(r["amount"] for r in rows), 2)
    return {"dimension": dimension, "total": total, "net_spend": net,
            "items": rows[: max(1, min(limit, 200))]}


def tool_search(keyword=None, since=None, until=None, kind=None,
                min_amount=None, max_amount=None, limit=30) -> dict:
    rows = db.search_transactions(conn(), keyword, since, until, min_amount,
                                  max_amount, kind, limit)
    items = [{"date": r["tx_date"], "time": r["tx_time"], "amount": r["amount"],
              "sign": r["sign"], "kind": r["kind"], "channel": r["channel"],
              "merchant": r["merchant"], "card": r["card"],
              "mail_category": r["mail_category"]} for r in rows]
    return {"matched": len(items),
            "net_spend": round(sum(i["amount"] * i["sign"] for i in items), 2),
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
    "merchant, amount, sign, signed_amount, layout)；"
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
        "description": "按单一维度聚合交易。dimension 可选 year/month/weekday/hour/kind/channel/merchant/card/amount_bucket。返回每项笔数与净金额（消费-退款）。用于月度趋势、渠道占比、金额分布等。",
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
        "name": "search_transactions",
        "description": "按关键词/时间/金额检索单笔交易明细。关键词走 FTS5 全文索引（≥3 字走 trigram 子串匹配，2 字走 LIKE），匹配商户、渠道与邮件主题。",
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
        "name": "open_dashboard",
        "description": "在对话下方打开招行消费可视化面板（Vue3 + ECharts）：月度趋势下钻、类型占比、渠道/商户排行、星期×小时热力图、金额分布、明细表，支持联动筛选与按月播放。用户想看图表或说「打开面板/看板」时调用。",
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
                   f"{data['repayment_total']:,.2f} 元。")
        return {"content": [{"type": "text", "text": summary + "\n\n" + text_of(data)}],
                "structuredContent": data}
    if name == "aggregate":
        data = tool_aggregate(args.get("dimension", "month"), args.get("since"),
                              args.get("until"), args.get("kind"), args.get("card"),
                              args.get("merchant"), int(args.get("limit", 200)))
        lines = [f"{i['label']}\t{i['count']} 笔\t{i['amount']:,.2f} 元" for i in data["items"]]
        head = f"{data['dimension']} 聚合，共 {data['total']} 笔，净支出 {data['net_spend']:,.2f} 元"
        return {"content": [{"type": "text", "text": head + "\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "top_merchants":
        data = tool_aggregate("merchant", args.get("since"), args.get("until"),
                              limit=int(args.get("limit", 15)))
        lines = [f"{n + 1}. {i['label']}\t{i['count']} 笔\t{i['amount']:,.2f} 元"
                 for n, i in enumerate(data["items"])]
        return {"content": [{"type": "text", "text": "商户排行（净金额）\n" + "\n".join(lines)}],
                "structuredContent": data}
    if name == "search_transactions":
        data = tool_search(args.get("keyword"), args.get("since"), args.get("until"),
                           args.get("kind"), args.get("min_amount"),
                           args.get("max_amount"), int(args.get("limit", 30)))
        lines = [f"{i['date']} {i['time']}\t{i['amount']:>9,.2f}\t{i['merchant']}"
                 for i in data["items"]]
        head = f"命中 {data['matched']} 笔，合计 {data['net_spend']:,.2f} 元"
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
            "serverInfo": {"name": "cmb-insight", "version": "0.2.0"},
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
