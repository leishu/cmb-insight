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

AMOUNT_RE = re.compile(r"^(?:CNY|RMB|人民币|￥|¥)?\s*([0-9][0-9,]*\.?\d{0,2})$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
DATE8_RE = re.compile(r"^20\d{6}$")
CARD_TAIL_RE = re.compile(r"尾号\s*(\d{4})")
BILL_DATE_RE = re.compile(r"(20\d{2})[/-](\d{2})[/-](\d{2})\s*您的消费明细")

KINDS = ("消费", "退货", "退款", "还款", "取现", "预授权", "分期", "转账", "费用", "利息", "冲正")

KIND_ALIAS = {"退货": "退款", "冲正": "退款"}

CHANNEL_HINTS = (
    "云闪付扫码", "云闪付", "微信支付", "财付通", "支付宝", "银联", "京东支付",
    "美团支付", "抖音支付", "快捷支付", "手机支付", "网上支付", "Apple Pay",
)


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


def _to_amount(text: str) -> float | None:
    m = AMOUNT_RE.match(text.strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _money(text: str) -> float | None:
    """严格金额：必须带小数点，避免把 20170123 这类日期当金额。"""
    t = text.strip().rstrip("元").strip()
    if "." not in t:
        return None
    return _to_amount(t)


def _split_desc(desc: str) -> tuple[str, str, str]:
    """把描述拆成 (交易类型, 渠道, 商户)。"""
    text = re.sub(r"尾号\s*\d{4}", "", desc).strip()
    kind = ""
    for k in KINDS:
        if text.startswith(k):
            kind = KIND_ALIAS.get(k, k)
            text = text[len(k):].strip()
            break
    text = text.lstrip(":：-— ").strip()
    channel = ""
    for hint in CHANNEL_HINTS:
        if text.startswith(hint):
            channel = hint
            rest = text[len(hint):].lstrip(":：-— ").strip()
            return kind, channel, rest or text
    if ":" in text or "：" in text:
        head, tail = re.split(r"[:：]", text, maxsplit=1)
        if head.strip() and tail.strip():
            return kind, head.strip(), tail.strip()
    if "-" in text and len(text.split("-", 1)[0]) <= 8:
        head, tail = text.split("-", 1)
        return kind, head.strip(), tail.strip()
    return kind, channel, text


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
    bill_day = None
    for c in cells:
        bm = BILL_DATE_RE.search(c)
        if bm:
            try:
                bill_day = f"{bm.group(1)}-{bm.group(2)}-{bm.group(3)}"
            except Exception:
                pass
            break
    default_day = bill_day or (mail_dt.strftime("%Y-%m-%d") if mail_dt else "")

    txs: list[dict] = []
    txs_a: list[dict] = []
    n = len(cells)

    def push(bucket, seen, card, day, clock, kind, channel, merchant, amount, source):
        if amount is None or not merchant:
            return
        key = (card, day, clock, round(amount, 2), merchant[:16])
        if key in seen:
            return
        seen.add(key)
        bucket.append({
            "card": card, "date": day, "time": clock, "kind": kind,
            "channel": channel, "merchant": merchant, "amount": round(amount, 2),
            "layout": source,
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
        for j in range(i + 1, min(i + 7, n)):
            if amount is None:
                a = _to_amount(cells[j])
                if a is not None and ("," in cells[j] or "." in cells[j] or "CNY" in cells[j]):
                    amount = a
                    continue
            if desc is None and ("尾号" in cells[j] or any(k in cells[j] for k in KINDS)):
                if not _to_amount(cells[j]) and len(cells[j]) > 2:
                    desc = cells[j]
            if amount is not None and desc is not None:
                break
        if amount is None or desc is None:
            continue
        cm = CARD_TAIL_RE.search(desc)
        kind, channel, merchant = _split_desc(desc)
        push(txs_b, seen_b, cm.group(1) if cm else "", default_day, cell,
             kind, channel, merchant, amount, "B")

    # 版式 A：卡号 → 日期 → 时间 → 币种 → 描述 → 金额
    for i, cell in enumerate(cells):
        if not (len(cell) == 4 and cell.isdigit()):
            continue
        if i + 5 >= n:
            continue
        day, clock, currency, desc = cells[i + 1], cells[i + 2], cells[i + 3], cells[i + 4]
        if not DATE8_RE.match(day) or not TIME_RE.match(clock):
            continue
        amount = _to_amount(cells[i + 5])
        if amount is None or "币" not in currency:
            continue
        kind, channel, merchant = _split_desc(desc)
        push(txs_a, seen_a, cell, f"{day[:4]}-{day[4:6]}-{day[6:]}", clock,
             kind, channel, merchant, amount, "A")

    # 老版式邮件里，版式 B 也会误命中同一笔，故 A 命中时以 A 为准（日期与卡号更准）
    txs = txs_a if txs_a else txs_b

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

    return {
        "file": base,
        "subject": subject,
        "mail_time": mail_dt.strftime("%Y-%m-%d %H:%M:%S") if mail_dt else "",
        "category": kind_cat,
        "size": os.path.getsize(path),
        "tx_count": len(txs),
        "repay_amount": bill,
        "txs": txs,
    }


def _normalize(mails: list[dict]) -> None:
    """合并被截断的商户名，并给退款打负号。"""
    names = set()
    for m in mails:
        for t in m["txs"]:
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
                "repay_amount": None, "txs": [], "error": str(exc)[:120],
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
