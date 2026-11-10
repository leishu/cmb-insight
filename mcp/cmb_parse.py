"""解析招商银行信用卡邮件，抽取交易明细。

两种版式：
  A. 早期「信用管家消费提醒」：卡号 → 日期 → 时间 → 币种 → 描述 → 金额
  B. 近年「每日信用管家」：  时间 → 金额(CNY) → 尾号XXXX 消费 渠道-商户

只依赖标准库。
"""

from __future__ import annotations

import email
import glob
import hashlib
import html
import json
import os
import re
from datetime import datetime
from email import policy

# 币种前缀捕获为 group(1)：CNY/RMB/￥/¥ 归一成 CNY，HKD/USD 等原样保留
AMOUNT_RE = re.compile(r"^([A-Z]{3}|人民币|￥|¥)?\s*(-?)\s*([0-9][0-9,]*\.?\d{0,2})$")
CURRENCY_ALIASES = {"CNY": "CNY", "RMB": "CNY", "人民币": "CNY", "￥": "CNY", "¥": "CNY", "": "CNY"}
# 版式 A 的「币别」列是中文；未知币别原样保留（宁可单列，不混进人民币）
CCY_CN = {"人民币": "CNY", "港币": "HKD", "美元": "USD", "日元": "JPY",
          "欧元": "EUR", "英镑": "GBP", "澳元": "AUD", "加元": "CAD", "新加坡元": "SGD"}
# 2017-2019 图片式模板里的金额格：只有「￥12,345.67」，没有文字标签；早年无小数
OLD_MONEY_RE = re.compile(r"^￥\s*([0-9][0-9,]*(?:\.\d{1,2})?)$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
DATE8_RE = re.compile(r"^20\d{6}$")
CARD_TAIL_RE = re.compile(r"尾号\s*(\d{4})")
BILL_DATE_RE = re.compile(r"(20\d{2})[/-](\d{2})[/-](\d{2})\s*您的消费明细")
# 2020–2021 年的每日信用管家写「2020/01/16 消费人民币 ￥xxx 明细如下」，
# 且发送比消费晚 1–2 天；不认这个措辞会把交易日期错记成发送日
SPEND_DATE_RE = re.compile(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s*消费")

KINDS = ("消费", "退货", "退款", "还款", "取现", "预授权", "分期", "转账", "费用", "利息",
         "冲正", "邮购")

KIND_ALIAS = {"退货": "退款", "冲正": "退款", "邮购": "消费"}

# 预授权/撤销等状态词：剥离出状态，避免污染商户名（"预授权完成 某某租车" → 某某租车）
STATUS_WORDS = ("完成", "撤销", "取消")

# 账单邮件里带语义 id 的字段（2023 年后版式）
BILL_ID_FIELDS = {
    "statementCycle": "cycle",
    "creditLimit": "credit_limit",
    "L1rmbLcurrBal": "due_amount",
    "L1rmbLdueAmt": "min_amount",
    "paymentDueDate": "due_date",
    "D1rmbLbegBal": "prev_balance",
    "D1rmbLpaymentAmt": "payment",
    "D1rmbLdebits": "debits",
    "D1rmbLcreditAmt": "credits",
    "D1rmbLinterest": "interest",
}

MONEY_CHARS = "&yen;￥¥＄$元 "


def _num(text: str) -> float | None:
    """账单金额：容忍 ￥/＄/&yen; 前缀、千分位、全角符号。"""
    t = html.unescape(text or "")
    t = t.replace(",", "").replace("，", "").strip()
    t = t.strip(MONEY_CHARS)
    m = re.match(r"^-?\d+(?:\.\d+)?$", t)
    if not m:
        return None
    try:
        return round(float(t), 2)
    except ValueError:
        return None

CHANNEL_HINTS = (
    "云闪付扫码", "云闪付", "微信支付", "财付通", "支付宝", "银联", "京东支付",
    "美团支付", "抖音支付", "快捷支付", "手机支付", "网上支付", "Apple Pay",
)


DATE_MD_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
DATE_YMD_RE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
CYCLE_RE = re.compile(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s*[-~至]\s*(20\d{2})[/-](\d{1,2})[/-](\d{1,2})")


def _html_of(msg) -> str:
    raw = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                raw.append(part.get_content())
            except Exception:
                continue
    return "\n".join(raw)


def _id_values(markup: str) -> dict:
    """按语义 id 取出账单字段原文（2023 年后版式才有）。"""
    out = {}
    for name, key in BILL_ID_FIELDS.items():
        m = re.search(r"id=['\"]?%s['\"]?[^>]*>(.{0,600}?)</DIV>" % name, markup, re.S | re.I)
        if not m:
            continue
        txt = re.sub(r"(?s)<[^>]+>", " ", m.group(1))
        txt = re.sub(r"\s+", " ", html.unescape(txt)).strip()
        if txt:
            out[key] = txt
    return out


def _norm_date(text: str, mail_dt) -> str | None:
    """账单日期归一：2026/11/23、2026-11-23、01/23（补年份）。"""
    t = (text or "").strip()
    m = DATE_YMD_RE.match(t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = DATE_MD_RE.match(t)
    if m and mail_dt:
        mon, day = int(m.group(1)), int(m.group(2))
        year = mail_dt.year
        # 12 月账单的到期日在次年 1 月
        if mon < mail_dt.month - 6:
            year += 1
        return f"{year:04d}-{mon:02d}-{day:02d}"
    return None


def _parse_bill(msg, cells: list[str], mail_dt) -> dict | None:
    """抽取电子账单的表头字段。

    两种版式：
      #id  2023 年后：带 statementCycle / creditLimit / paymentDueDate 等语义 id
      pos  2017-2022：只有「到期还款日 ￥应还 ＄应还 ￥最低 ＄最低」四个连续单元格
    """
    vals = _id_values(_html_of(msg))
    bill: dict = {}

    cycle = CYCLE_RE.search(vals.get("cycle", ""))
    if cycle:
        bill["cycle_start"] = f"{int(cycle.group(1)):04d}-{int(cycle.group(2)):02d}-{int(cycle.group(3)):02d}"
        bill["cycle_end"] = f"{int(cycle.group(4)):04d}-{int(cycle.group(5)):02d}-{int(cycle.group(6)):02d}"
    due = _norm_date(vals.get("due_date", ""), mail_dt)
    if due:
        bill["due_date"] = due
    for key in ("credit_limit", "due_amount", "min_amount", "prev_balance",
                "payment", "debits", "credits", "interest"):
        if key in vals:
            n = _num(vals[key])
            if n is not None:
                bill[key] = n

    if not bill.get("due_amount"):
        # 位置版式：找日期单元格，其后连续的人民币金额依次为应还、最低还款
        for i, c in enumerate(cells[:14]):
            if not (_norm_date(c, mail_dt) or DATE_MD_RE.match(c.strip())):
                continue
            cny = []
            for nxt in cells[i + 1: i + 6]:
                if "＄" in nxt or "$" in nxt:
                    continue
                n = _num(nxt)
                if n is not None:
                    cny.append(n)
            if cny:
                bill.setdefault("due_date", _norm_date(c, mail_dt))
                bill["due_amount"] = cny[0]
                if len(cny) > 1:
                    bill["min_amount"] = cny[1]
                break

    if not bill.get("due_amount") and not bill.get("credit_limit"):
        return None
    bill["layout"] = "id" if "credit_limit" in vals else "pos"
    return bill


AVAIL_RE = re.compile(r"可用额度\s*[：:]?\s*[￥¥]?\s*([\d,]+\.\d{2})")
POINTS_RE = re.compile(r"积分(?:余额)?\s*[：:]?\s*([\d,]+)")


def _parse_credit(cells: list[str]) -> dict | None:
    """每日信用管家/消费提醒里的可用额度与积分。

    三种版式：
      同行  「可用额度 ￥61,352.51」「可用额度： 16,591.79」
      值在前 「￥40,516.01」「22,583」「可用额度」「积分余额」
      标签在前 表格两行，标签行在上、数值行在下
    """
    avail = points = None
    for i, c in enumerate(cells[:12]):
        m = AVAIL_RE.search(c)
        if m:
            avail = _num(m.group(1))
            break
        if c.strip().rstrip("：:") == "可用额度":
            # 优先取带货币符号的邻居（值在前版式里，紧邻的纯数字其实是积分）
            window = [j for j in range(max(0, i - 3), min(len(cells), i + 5))]
            for j in window:
                if "￥" in cells[j] or "¥" in cells[j]:
                    avail = _num(cells[j])
                    if avail:
                        break
            if not avail:
                for j in window:
                    n = _num(cells[j])
                    if n is not None and n > 1000:
                        avail = n
                        break
            break

    for i, c in enumerate(cells[:16]):
        m = POINTS_RE.search(c)
        if m:
            try:
                points = int(m.group(1).replace(",", "")) or None
            except ValueError:
                pass
            break
        if c.strip() == "积分余额":
            for j in range(max(0, i - 3), i):
                t = cells[j].strip().replace(",", "")
                if t.isdigit():
                    points = int(t) or None
                    break
            break

    if not avail:
        # 2017-2019 图片式模板没有文字标签，只剩两个「￥金额」并排
        avail, limit = _old_credit_pair(cells)
        if not avail:
            return None
        return {"avail": avail, "points": points, "limit": limit}
    return {"avail": avail, "points": points}


def _old_credit_pair(cells: list[str]) -> tuple[float | None, float | None]:
    """老版式「信用管家消费提醒」的额度对，返回 (可用额度, 信用额度)。

    该模板把标签做成了图片，单元格里只有两个金额，例如
    「￥32731.22」「￥16268.78」。用 2017-2020 相邻邮件的数字变化与期间
    消费额比对 500 余次，规律是：**前一个是已用额度，后一个是可用额度**，
    且两者相加正好等于当期信用额度（29,000 / 49,000 / 55,000 / 125,000
    这类整千元，含临时额度）。合计不是整千元时视为没对上，宁可不取。
    """
    vals = []
    for cell in cells[:6]:
        m = OLD_MONEY_RE.match(cell.strip())
        if m:
            vals.append(float(m.group(1).replace(",", "")))
    if len(vals) != 2:
        return None, None
    used, avail = vals
    total = used + avail
    if used <= 0 or avail <= 0 or total % 1000:
        return None, None
    return avail, round(total, 2)


def _cells(msg) -> list[str]:
    """按文档顺序取出所有非空单元格文本。"""
    raw = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                raw.append(part.get_content())
            except Exception:
                continue
    joined = "\n".join(raw)
    out = []
    for cell in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", joined):
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", cell))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def _currency_amount(text: str) -> tuple[str, float] | None:
    """带币种与符号的金额：返回 (ISO 币种,  signed 金额)。

    无币种前缀默认 CNY；负数表示资金退回（退货/冲正）。
    HKD/USD 等外币原样返回，由 db 层单独入库，不混进人民币口径。
    """
    m = AMOUNT_RE.match(text.strip())
    if not m:
        return None
    token = m.group(1) or ""
    ccy = CURRENCY_ALIASES.get(token, token)
    try:
        value = float(m.group(3).replace(",", ""))
    except ValueError:
        return None
    return ccy, (-value if m.group(2) == "-" else value)


def _to_amount(text: str) -> float | None:
    """金额绝对值。

    退货/冲正在银行邮件里写作「CNY -201.50」，负号不参与绝对值，
    方向由 sign 字段承载；要判断方向用 _signed_amount。
    """
    r = _currency_amount(text)
    return abs(r[1]) if r else None


def _signed_amount(text: str) -> float | None:
    """带符号金额：负数表示资金退回（退货/冲正）。"""
    r = _currency_amount(text)
    return r[1] if r else None


def _money(text: str) -> float | None:
    """严格金额：必须带小数点，避免把 20170123 这类日期当金额。"""
    t = text.strip().rstrip("元").strip()
    if "." not in t:
        return None
    return _to_amount(t)


def _split_desc(desc: str) -> tuple[str, str, str, str]:
    """把描述拆成 (交易类型, 渠道, 商户, 状态)。

    状态用于预授权生命周期：预授权（占用额度）→ 完成（真正入账）/ 撤销。
    早期版式写作「预授权完成 某某租车」，状态词必须从商户名里剥掉。
    """
    text = re.sub(r"尾号\s*\d{4}", "", desc).strip()
    kind, state = "", ""
    for k in KINDS:
        if text.startswith(k):
            kind = KIND_ALIAS.get(k, k)
            text = text[len(k):].strip()
            if k == "预授权":
                for w in STATUS_WORDS:
                    if text.startswith(w):
                        state = w
                        text = text[len(w):].strip()
                        break
                else:
                    state = "预授权"
            break
    text = text.lstrip(":：-— ").strip()
    for w in STATUS_WORDS:  # 无「预授权」前缀时也可能带状态词
        if text.startswith(w) and len(text) > len(w):
            state = state or w
            text = text[len(w):].strip()
            break
    channel = ""
    for hint in CHANNEL_HINTS:
        if text.startswith(hint):
            channel = hint
            rest = text[len(hint):].lstrip(":：-— ").strip()
            return kind, channel, rest or text, state
    if kind == "消费" and desc.strip().startswith("邮购"):
        channel = "邮购"
    if ":" in text or "：" in text:
        head, tail = re.split(r"[:：]", text, maxsplit=1)
        if head.strip() and tail.strip():
            return kind, head.strip(), tail.strip(), state
    if "-" in text and len(text.split("-", 1)[0]) <= 8:
        head, tail = text.split("-", 1)
        return kind, head.strip(), tail.strip(), state
    return kind, channel, text, state


def parse_email(path: str) -> dict:
    with open(path, "rb") as fh:
        msg = email.message_from_bytes(fh.read(), policy=policy.default)

    subject = str(msg.get("Subject") or "")
    base = os.path.basename(path)
    m = re.match(r"^(\d{8})-(\d{6})_", base)
    mail_dt = None
    if m:
        try:
            mail_dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            mail_dt = None
    if mail_dt is None:
        try:
            mail_dt = datetime.strptime(str(msg.get("Date"))[:31], "%a, %d %b %Y %H:%M:%S %z")
        except Exception:
            mail_dt = None

    cells = _cells(msg)
    # 交易日期优先取正文：先认「您的消费明细」，再认「YYYY/MM/DD 消费」，
    # 都没有才退回邮件日期（发送可能比消费晚 1–2 天）
    bill_day = None
    spend_day = None
    for c in cells:
        bm = BILL_DATE_RE.search(c)
        if bm:
            bill_day = f"{bm.group(1)}-{bm.group(2)}-{bm.group(3)}"
            break
        if spend_day is None:
            sm = SPEND_DATE_RE.search(c)
            if sm:
                spend_day = f"{sm.group(1)}-{int(sm.group(2)):02d}-{int(sm.group(3)):02d}"
    default_day = bill_day or spend_day or (mail_dt.strftime("%Y-%m-%d") if mail_dt else "")

    txs: list[dict] = []
    txs_a: list[dict] = []
    n = len(cells)

    def push(bucket, seen, card, day, clock, kind, channel, merchant, amount, source,
             state="", neg=False, ccy="CNY"):
        if amount is None or not merchant:
            return
        amount = abs(amount)
        key = (card, day, clock, round(amount, 2), merchant[:16])
        if key in seen:
            return
        seen.add(key)
        bucket.append({
            "card": card, "date": day, "time": clock, "kind": kind,
            "channel": channel, "merchant": merchant, "amount": round(amount, 2),
            "layout": source, "state": state, "neg": neg, "currency": ccy,
        })

    seen_b: set[tuple] = set()
    seen_a: set[tuple] = set()
    txs_b: list[dict] = []

    # 版式 B：时间 → 金额 → 尾号XXXX 描述
    for i, cell in enumerate(cells):
        if not TIME_RE.match(cell):
            continue
        amount = None
        desc = None
        neg = False
        ccy = "CNY"
        for j in range(i + 1, min(i + 7, n)):
            if amount is None:
                parsed = _currency_amount(cells[j])
                # 裸整数（日期、积分）不当金额；带币种码的除外
                if parsed is not None and ("," in cells[j] or "." in cells[j]
                                           or "CNY" in cells[j] or parsed[0] != "CNY"):
                    ccy, amount = parsed
                    neg = amount < 0
                    continue
            if desc is None and ("尾号" in cells[j] or any(k in cells[j] for k in KINDS)):
                if not _to_amount(cells[j]) and len(cells[j]) > 2:
                    desc = cells[j]
            if amount is not None and desc is not None:
                break
        if amount is None or desc is None:
            continue
        cm = CARD_TAIL_RE.search(desc)
        kind, channel, merchant, state = _split_desc(desc)
        push(txs_b, seen_b, cm.group(1) if cm else "", default_day, cell,
             kind, channel, merchant, amount, "B", state, neg, ccy)

    # 版式 A：卡号 → 日期 → 时间 → 币种 → 描述 → 金额
    for i, cell in enumerate(cells):
        if not (len(cell) == 4 and cell.isdigit()):
            continue
        if i + 5 >= n:
            continue
        day, clock, currency, desc = cells[i + 1], cells[i + 2], cells[i + 3], cells[i + 4]
        if not DATE8_RE.match(day) or not TIME_RE.match(clock):
            continue
        amount = _signed_amount(cells[i + 5])
        if amount is None or "币" not in currency:
            continue
        # 币别列决定币种；未知币别原样保留，由 db 层单列，不混进人民币
        ccy = CCY_CN.get(currency.strip(), currency.strip())
        kind, channel, merchant, state = _split_desc(desc)
        push(txs_a, seen_a, cell, f"{day[:4]}-{day[4:6]}-{day[6:]}", clock,
             kind, channel, merchant, amount, "A", state, amount < 0, ccy)

    # 老版式邮件里，版式 B 也会误命中同一笔，故 A 命中时以 A 为准（日期与卡号更准）
    txs = txs_a if txs_a else txs_b

    # 预授权拆两路：占用额度（hold）不进消费，完成/撤销是状态而非支出
    holds, spend = [], []
    for t in txs:
        # 负金额代表资金退回；描述里没写「退货」时按退款处理，
        # 否则会变成一笔正向支出，把净支出抬高两倍
        if t.get("neg") and t["state"] not in ("预授权", "撤销") and t["kind"] in ("", "消费"):
            t["kind"] = "退款"
        if t["state"] == "预授权" or t["state"] == "撤销":
            holds.append(t)
        else:
            if t["state"] == "完成":
                t["kind"], t["state"] = "消费", "预授权完成"
            spend.append(t)
    txs = spend

    # 还款/账单金额：找到「实扣金额」「账单金额」标签后，向后取第一个金额单元格
    bill = None
    for i, c in enumerate(cells[:40]):
        if c in ("实扣金额", "应扣金额", "账单金额", "本期应还金额") or "本期应还" in c:
            for j in range(i + 1, min(i + 8, n)):
                a = _money(cells[j])
                if a is not None:
                    bill = round(a, 2)
                    break
            if bill is not None:
                break

    if "每日信用管家" in subject:
        kind_cat = "每日信用管家"
    elif "消费提醒" in subject:
        kind_cat = "消费提醒"
    elif "自动还款" in subject:
        kind_cat = "自动还款"
    elif "电子账单" in subject:
        kind_cat = "电子账单"
    else:
        kind_cat = "营销/其他"

    bill_info = _parse_bill(msg, cells, mail_dt) if kind_cat == "电子账单" else None
    credit = (_parse_credit(cells) if kind_cat in ("每日信用管家", "消费提醒") else None)

    return {
        "file": base,
        "subject": subject,
        "mail_time": mail_dt.strftime("%Y-%m-%d %H:%M:%S") if mail_dt else "",
        "category": kind_cat,
        "size": os.path.getsize(path),
        "tx_count": len(txs),
        "repay_amount": bill,
        "txs": txs,
        "holds": holds,
        "bill": bill_info,
        "credit": credit,
    }


def _normalize(mails: list[dict]) -> None:
    """合并被截断的商户名，并给退款打负号。"""
    names = set()
    for m in mails:
        for t in list(m["txs"]) + list(m.get("holds") or []):
            if t["merchant"]:
                names.add(t["merchant"])
    alias = {}
    ordered = sorted(names, key=len, reverse=True)
    for long_name in ordered:
        for short in names:
            if short == long_name or short in alias:
                continue
            if len(short) >= 8 and long_name.startswith(short) and len(long_name) - len(short) <= 6:
                alias[short] = long_name
    for m in mails:
        for t in m["txs"]:
            t["merchant"] = alias.get(t["merchant"], t["merchant"])
            t["sign"] = -1 if t["kind"] == "退款" else 1
        for t in (m.get("holds") or []):
            t["merchant"] = alias.get(t["merchant"], t["merchant"])


def build_index(mail_dir: str, cache_path: str | None = None) -> dict:
    files = sorted(glob.glob(os.path.join(mail_dir, "*.eml")))
    mtimes = {}
    for f in files:
        try:
            mtimes[os.path.basename(f)] = os.path.getmtime(f)
        except OSError:
            pass
    sig = hashlib.sha1(
        json.dumps([len(files), sorted(mtimes.items())[-5:] if mtimes else []], sort_keys=True).encode()
    ).hexdigest()

    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig:
                cached["from_cache"] = True
                return cached
        except Exception:
            pass

    mails = []
    for path in files:
        try:
            mails.append(parse_email(path))
        except Exception as exc:  # 单封失败不影响整体
            mails.append({
                "file": os.path.basename(path), "subject": "", "mail_time": "",
                "category": "解析失败", "size": 0, "tx_count": 0,
                "repay_amount": None, "txs": [], "holds": [], "bill": None,
                "credit": None, "error": str(exc)[:120],
            })

    index = {
        "signature": sig,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mail_dir": mail_dir,
        "mail_count": len(mails),
        "from_cache": False,
        "mails": mails,
    }
    _normalize(mails)
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False)
        os.replace(tmp, cache_path)
    return index


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "招商银行")
    out = sys.argv[2] if len(sys.argv) > 2 else ""
    idx = build_index(src, out or None)
    txs = [t for m in idx["mails"] for t in m["txs"]]
    print(f"邮件 {idx['mail_count']} 封，抽出交易 {len(txs)} 笔")
    print("邮件类型:", {c: sum(1 for m in idx['mails'] if m['category'] == c)
                       for c in {m['category'] for m in idx['mails']}})
    for t in txs[:5]:
        print(t)
