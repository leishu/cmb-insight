"""把招商银行信用卡邮件（.eml）导入 SQLite。

设计要点
--------
* 两张主表：``mail``（每封邮件一行，含正文纯文本）与 ``tx``（每笔交易一行）。
* 生成列：``month`` / ``year`` / ``dow`` / ``hour`` / ``signed_amount`` 由
  ``tx_date``、``tx_time``、``amount``、``sign`` 自动推算，写入端不必维护。
* ``tx_fts``：FTS5 + trigram 分词，支持中文子串检索（"星巴克"能命中
  "星巴克咖啡（国贸店）"）。unicode61 对中文整段当 token，不适用。
* 增量导入：按文件名 + 大小 + 内容哈希判断是否需要重导；目录里已删除的邮件
  会自动从库里清掉（仅限同一导入目录）；解析失败的邮件落 ``category='解析失败'``
  行，体检指标不再空转。给出 ``--rebuild`` 可全量重建。
* 商户别名归一放在导入末尾统一做一次（跨邮件合并被截断的商户名），
  保证增量导入与全量导入结果一致。

命令行
------
    python db.py <邮件目录> [数据库路径]          # 增量导入
    python db.py <邮件目录> [数据库路径] --rebuild  # 全量重建
    python db.py <邮件目录> [数据库路径] --json     # 导入统计输出为 JSON
    python db.py --stats <数据库路径>              # 看统计
    python db.py --sql <数据库路径> "SELECT ..."    # 跑只读 SQL
"""

from __future__ import annotations

import glob
import hashlib
import html
import json
import os
import re
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta

# 允许作为脚本直接运行（python db.py ...）时同目录导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cmb_parse import parse_email, merchant_aliases  # noqa: E402

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PLUGIN_ROOT, "data", "cmb.db")

# 库结构版本：变更表结构时 +1，导入时会自动全量重建（数据全部可由 .eml 复现）
# v5: 交易日期改取正文「YYYY/MM/DD 消费」措辞（修复 2020–2021 发送日错位）；
#     外币交易原币入库到 tx_fx，不再静默丢弃
# v6: mail 增加 file_hash（内容级变更检测）；解析失败落库；增量清理已删邮件
SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail (
    id          INTEGER PRIMARY KEY,
    file        TEXT NOT NULL UNIQUE,
    subject     TEXT NOT NULL DEFAULT '',
    mail_time   TEXT NOT NULL DEFAULT '',          -- YYYY-MM-DD HH:MM:SS
    mail_date   TEXT GENERATED ALWAYS AS (substr(mail_time, 1, 10)) STORED,
    category    TEXT NOT NULL DEFAULT '',          -- 消费提醒 / 每日信用管家 / 自动还款 / 电子账单
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    file_hash   TEXT NOT NULL DEFAULT '',          -- 文件内容 sha1，增量导入检测同尺寸变更
    tx_count    INTEGER NOT NULL DEFAULT 0,
    bill_amount REAL,                              -- 自动还款通知的实扣金额（账单类走 bill 表）
    body_text   TEXT NOT NULL DEFAULT '',          -- 正文纯文本，供全文检索
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tx (
    id            INTEGER PRIMARY KEY,
    mail_id       INTEGER NOT NULL REFERENCES mail(id) ON DELETE CASCADE,
    card          TEXT NOT NULL DEFAULT '',        -- 卡号后四位
    tx_date       TEXT NOT NULL,                   -- YYYY-MM-DD
    tx_time       TEXT NOT NULL DEFAULT '',        -- HH:MM:SS
    month         TEXT GENERATED ALWAYS AS (substr(tx_date, 1, 7)) STORED,
    year          INTEGER GENERATED ALWAYS AS (CAST(substr(tx_date, 1, 4) AS INTEGER)) STORED,
    dow           INTEGER GENERATED ALWAYS AS (CAST(strftime('%w', tx_date) AS INTEGER)) STORED,
    hour          INTEGER GENERATED ALWAYS AS (CAST(COALESCE(NULLIF(substr(tx_time, 1, 2), ''), '0') AS INTEGER)) STORED,
    kind          TEXT NOT NULL DEFAULT '',        -- 消费 / 退款 / 还款 / 取现 ...
    channel       TEXT NOT NULL DEFAULT '',        -- 微信支付 / 支付宝 / 银联 ...
    merchant      TEXT NOT NULL DEFAULT '',
    amount        REAL NOT NULL,                   -- 绝对值
    sign          INTEGER NOT NULL DEFAULT 1,      -- 1 支出，-1 退款
    signed_amount REAL GENERATED ALWAYS AS (round(amount * sign, 2)) STORED,
    layout        TEXT NOT NULL DEFAULT '',        -- 来源版式 A/B，便于排查解析问题
    state         TEXT NOT NULL DEFAULT ''         -- 空 / 预授权完成
);

-- 外币交易：原币单列，不混进人民币口径（tx / 各聚合视图只含 CNY）。
-- 邮件里没有人民币折算金额，不能直接折算；汇总时按 currency 分组单报。
CREATE TABLE IF NOT EXISTS tx_fx (
    id            INTEGER PRIMARY KEY,
    mail_id       INTEGER NOT NULL REFERENCES mail(id) ON DELETE CASCADE,
    card          TEXT NOT NULL DEFAULT '',
    tx_date       TEXT NOT NULL,
    tx_time       TEXT NOT NULL DEFAULT '',
    month         TEXT GENERATED ALWAYS AS (substr(tx_date, 1, 7)) STORED,
    kind          TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL DEFAULT '',
    merchant      TEXT NOT NULL DEFAULT '',
    currency      TEXT NOT NULL,                   -- ISO 币种码，如 HKD / USD
    amount        REAL NOT NULL,                   -- 原币绝对值
    sign          INTEGER NOT NULL DEFAULT 1,
    signed_amount REAL GENERATED ALWAYS AS (round(amount * sign, 2)) STORED,
    layout        TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tx_fx_date ON tx_fx(tx_date);
CREATE INDEX IF NOT EXISTS idx_tx_fx_mail ON tx_fx(mail_id);
CREATE INDEX IF NOT EXISTS idx_tx_fx_ccy  ON tx_fx(currency);

-- 预授权占用额度，不是支出：单独存，避免虚增净支出
CREATE TABLE IF NOT EXISTS hold (
    id       INTEGER PRIMARY KEY,
    mail_id  INTEGER NOT NULL REFERENCES mail(id) ON DELETE CASCADE,
    card     TEXT NOT NULL DEFAULT '',
    tx_date  TEXT NOT NULL,
    tx_time  TEXT NOT NULL DEFAULT '',
    merchant TEXT NOT NULL DEFAULT '',
    channel  TEXT NOT NULL DEFAULT '',
    amount   REAL NOT NULL,
    state    TEXT NOT NULL DEFAULT '预授权',       -- 预授权 / 撤销
    layout   TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY'
);

-- 电子账单表头（账本级口径）
CREATE TABLE IF NOT EXISTS bill (
    id           INTEGER PRIMARY KEY,
    mail_id      INTEGER NOT NULL UNIQUE REFERENCES mail(id) ON DELETE CASCADE,
    bill_month   TEXT NOT NULL DEFAULT '',         -- YYYY-MM，取账单周期结束月
    cycle_start  TEXT,
    cycle_end    TEXT,
    due_date     TEXT,
    credit_limit REAL,
    due_amount   REAL,                             -- 本期应还
    min_amount   REAL,                             -- 最低还款
    prev_balance REAL,                             -- 上期余额
    payment      REAL,                             -- 本期还款
    debits       REAL,                             -- 本期消费
    credits      REAL,                             -- 本期退款/贷记
    interest     REAL,                             -- 本期利息
    layout       TEXT NOT NULL DEFAULT ''          -- id / pos
);

CREATE INDEX IF NOT EXISTS idx_hold_date   ON hold(tx_date);
CREATE INDEX IF NOT EXISTS idx_bill_month  ON bill(bill_month);

-- 每日信用管家给出的可用额度/积分（额度使用率的原始数据）
CREATE TABLE IF NOT EXISTS credit (
    id          INTEGER PRIMARY KEY,
    mail_id     INTEGER NOT NULL UNIQUE REFERENCES mail(id) ON DELETE CASCADE,
    dt          TEXT NOT NULL,                    -- mail_date
    avail_credit REAL,                            -- 可用额度
    limit_credit REAL,                            -- 该封邮件自带的信用额度（老版式可反推）
    points      INTEGER,                          -- 积分余额
    card        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_credit_dt ON credit(dt);

CREATE INDEX IF NOT EXISTS idx_tx_date     ON tx(tx_date);
CREATE INDEX IF NOT EXISTS idx_tx_month    ON tx(month);
CREATE INDEX IF NOT EXISTS idx_tx_year     ON tx(year);
CREATE INDEX IF NOT EXISTS idx_tx_dow      ON tx(dow);
CREATE INDEX IF NOT EXISTS idx_tx_merchant ON tx(merchant);
CREATE INDEX IF NOT EXISTS idx_tx_kind     ON tx(kind);
CREATE INDEX IF NOT EXISTS idx_tx_channel  ON tx(channel);
CREATE INDEX IF NOT EXISTS idx_tx_card     ON tx(card);
CREATE INDEX IF NOT EXISTS idx_tx_mail     ON tx(mail_id);
CREATE INDEX IF NOT EXISTS idx_mail_date   ON mail(mail_date);
CREATE INDEX IF NOT EXISTS idx_mail_cat    ON mail(category);

-- 中文子串检索：trigram 分词，查询串需 >= 3 字符
CREATE VIRTUAL TABLE IF NOT EXISTS tx_fts USING fts5(
    merchant, channel, subject,
    tokenize = 'trigram'
);

CREATE VIEW IF NOT EXISTS v_tx AS
SELECT t.id,
       t.tx_date,
       t.tx_time,
       t.month,
       t.year,
       t.dow,
       CASE t.dow WHEN 0 THEN '周日' WHEN 1 THEN '周一' WHEN 2 THEN '周二'
                  WHEN 3 THEN '周三' WHEN 4 THEN '周四' WHEN 5 THEN '周五'
                  ELSE '周六' END AS weekday,
       t.hour,
       t.card,
       t.kind,
       t.channel,
       t.merchant,
       t.amount,
       t.sign,
       t.signed_amount,
       t.state,
       m.category AS mail_category,
       m.subject  AS mail_subject,
       m.mail_time
FROM tx t
JOIN mail m ON m.id = t.mail_id;

CREATE VIEW IF NOT EXISTS v_monthly AS
SELECT month,
       COUNT(*)                              AS tx_count,
       ROUND(SUM(signed_amount), 2)          AS net_amount,
       ROUND(SUM(amount), 2)                 AS gross_amount
FROM tx
GROUP BY month;

CREATE VIEW IF NOT EXISTS v_merchant AS
SELECT merchant,
       COUNT(*)                              AS tx_count,
       ROUND(SUM(signed_amount), 2)          AS net_amount,
       ROUND(AVG(amount), 2)                 AS avg_amount,
       ROUND(MAX(amount), 2)                 AS max_amount
FROM tx
WHERE merchant <> ''
GROUP BY merchant;

CREATE VIEW IF NOT EXISTS v_channel AS
SELECT channel,
       COUNT(*)                              AS tx_count,
       ROUND(SUM(signed_amount), 2)          AS net_amount
FROM tx
GROUP BY channel;
"""

DIMENSIONS = {
    "month": "t.month",
    "year": "CAST(t.year AS TEXT)",
    "weekday": ("CASE t.dow WHEN 0 THEN '周日' WHEN 1 THEN '周一' WHEN 2 THEN '周二'"
                " WHEN 3 THEN '周三' WHEN 4 THEN '周四' WHEN 5 THEN '周五' ELSE '周六' END"),
    "hour": "CAST(t.hour AS TEXT)",
    "kind": "COALESCE(NULLIF(t.kind, ''), '未分类')",
    "channel": "COALESCE(NULLIF(t.channel, ''), '未标注')",
    "merchant": "t.merchant",
    "card": "NULLIF(t.card, '')",
    # 与前端 BUCKETS 保持一致，共 9 档
    "amount_bucket": (
        "CASE WHEN t.amount < 10 THEN '≤10' WHEN t.amount < 30 THEN '10-30'"
        " WHEN t.amount < 50 THEN '30-50' WHEN t.amount < 100 THEN '50-100'"
        " WHEN t.amount < 200 THEN '100-200' WHEN t.amount < 500 THEN '200-500'"
        " WHEN t.amount < 1000 THEN '500-1k' WHEN t.amount < 3000 THEN '1k-3k'"
        " ELSE '>3k' END"
    ),
}


# --------------------------------------------------------------------------- #
# 连接与建表
# --------------------------------------------------------------------------- #

def db_label(path: str) -> str:
    """插件内的库显示成 data/cmb.db，外部路径原样返回。"""
    if not path:
        return ""
    rel = os.path.relpath(path, PLUGIN_ROOT)
    return path if rel.startswith("..") else rel


def connect(db_path: str) -> sqlite3.Connection:
    """打开（并按需创建）数据库，返回已配置好的连接。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError):
        return 0


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """幂等补列；返回 True 表示库结构落后，需要全量重建才能拿到新数据。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tx)")}
    if cols and "state" not in cols:
        conn.execute("ALTER TABLE tx ADD COLUMN state TEXT NOT NULL DEFAULT ''")
        conn.commit()
    return _schema_version(conn) < SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 正文提取
# --------------------------------------------------------------------------- #

def body_text_of(path: str, limit: int = 20000) -> str:
    """提取邮件正文纯文本：优先 text/plain，其次 text/html 去标签。"""
    import email
    from email import policy

    try:
        with open(path, "rb") as fh:
            msg = email.message_from_bytes(fh.read(), policy=policy.default)
    except Exception:
        return ""

    plain, rich = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                plain.append(part.get_content())
            except Exception:
                pass
        elif ctype == "text/html":
            try:
                rich.append(part.get_content())
            except Exception:
                pass

    if plain:
        text = "\n".join(plain)
    else:
        text = "\n".join(rich)
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)

    text = re.sub(r"[ \t\u3000\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #

def _existing_mails(conn: sqlite3.Connection) -> dict[str, tuple]:
    """file → (size_bytes, file_hash, category)，供增量导入判断跳过/重导。"""
    return {r["file"]: (r["size_bytes"], r["file_hash"], r["category"])
            for r in conn.execute("SELECT file, size_bytes, file_hash, category FROM mail")}


def _file_hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return ""


def _insert_failed_mail(conn: sqlite3.Connection, base: str, size: int,
                        error: str) -> None:
    """解析失败也落一行 mail：体检能数到、能溯源，下次导入会自动重试。"""
    mail_time = ""
    try:
        mail_time = datetime.strptime(base[:15], "%Y%m%d-%H%M%S").strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    conn.execute(
        """INSERT INTO mail (file, subject, mail_time, category, size_bytes,
                             tx_count, bill_amount, body_text)
           VALUES (?, '', ?, '解析失败', ?, 0, NULL, ?)""",
        (base, mail_time, size, f"解析失败：{error}"[:2000]),
    )


def _delete_mail(conn: sqlite3.Connection, mail_id: int) -> None:
    conn.execute("DELETE FROM tx_fts WHERE rowid IN (SELECT id FROM tx WHERE mail_id = ?)",
                 (mail_id,))
    conn.execute("DELETE FROM mail WHERE id = ?", (mail_id,))


def _insert_bill(conn: sqlite3.Connection, mail_id: int, b: dict | None) -> None:
    if not b:
        return
    month = (b.get("cycle_end") or b.get("due_date") or "")[:7]
    conn.execute(
        """INSERT INTO bill (mail_id, bill_month, cycle_start, cycle_end, due_date,
                             credit_limit, due_amount, min_amount, prev_balance,
                             payment, debits, credits, interest, layout)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mail_id, month, b.get("cycle_start"), b.get("cycle_end"), b.get("due_date"),
         b.get("credit_limit"), b.get("due_amount"), b.get("min_amount"),
         b.get("prev_balance"), b.get("payment"), b.get("debits"),
         b.get("credits"), b.get("interest"), b.get("layout", "")),
    )


def _insert_credit(conn: sqlite3.Connection, mail_id: int, rec: dict) -> None:
    c = rec.get("credit")
    if not c:
        return
    conn.execute(
        "INSERT OR REPLACE INTO credit (mail_id, dt, avail_credit, limit_credit, points, card) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mail_id, rec["mail_time"][:10], c.get("avail"), c.get("limit"), c.get("points"), ""),
    )


def _insert_mail(conn: sqlite3.Connection, rec: dict, path: str,
                 file_hash: str = "") -> int:
    body = body_text_of(path)
    cur = conn.execute(
        """INSERT INTO mail (file, subject, mail_time, category, size_bytes,
                             file_hash, tx_count, bill_amount, body_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rec["file"], rec["subject"], rec["mail_time"], rec["category"],
         rec["size"], file_hash, rec["tx_count"], rec["repay_amount"], body),
    )
    mail_id = cur.lastrowid
    rows, fx_rows, fts = [], [], []
    for t in rec["txs"]:
        # parse_email 只做抽取，退款打负号这一步在 db 层完成
        sign = t.get("sign") or (-1 if t["kind"] == "退款" else 1)
        row = (mail_id, t["card"], t["date"], t["time"], t["kind"],
               t["channel"], t["merchant"], t["amount"], sign,
               t.get("layout", ""), t.get("state", ""))
        ccy = t.get("currency") or "CNY"
        if ccy == "CNY":
            rows.append(row)
            fts.append((t["merchant"], t["channel"], rec["subject"]))
        else:
            fx_rows.append(row + (ccy,))
    conn.executemany(
        """INSERT INTO tx (mail_id, card, tx_date, tx_time, kind, channel,
                           merchant, amount, sign, layout, state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    if fx_rows:
        conn.executemany(
            """INSERT INTO tx_fx (mail_id, card, tx_date, tx_time, kind, channel,
                                  merchant, amount, sign, layout, state, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fx_rows,
        )
    holds = rec.get("holds") or []
    if holds:
        conn.executemany(
            """INSERT INTO hold (mail_id, card, tx_date, tx_time, merchant,
                                 channel, amount, state, layout, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(mail_id, h["card"], h["date"], h["time"], h["merchant"],
              h["channel"], h["amount"], h.get("state") or "预授权",
              h.get("layout", ""), h.get("currency") or "CNY") for h in holds],
        )
    _insert_bill(conn, mail_id, rec.get("bill"))
    _insert_credit(conn, mail_id, rec)
    # 用最近插入的 tx 行 id 与 fts 行对齐（fts 只含 CNY 交易，与 rows 一一对应）
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM tx WHERE mail_id = ? ORDER BY id", (mail_id,))]
    conn.executemany(
        "INSERT INTO tx_fts (rowid, merchant, channel, subject) VALUES (?, ?, ?, ?)",
        [(i, m, c, s) for i, (m, c, s) in zip(ids, fts)],
    )
    return mail_id


def normalize_merchants(conn: sqlite3.Connection) -> int:
    """跨邮件合并被上游截断的商户名（如「北京轨道交通路网管理有限」→ 全称）。

    返回被改写的行数（tx / tx_fx / hold 三张表）。别名规则与解析层共用
    cmb_parse.merchant_aliases：短名 >= 8 字且是长名前缀，长度差 <= 6。
    hold 也要覆盖：解析层只归一同批邮件，跨批次的截断要靠这里兜底。
    """
    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT merchant FROM tx WHERE merchant <> '' "
        "UNION SELECT DISTINCT merchant FROM tx_fx WHERE merchant <> '' "
        "UNION SELECT DISTINCT merchant FROM hold WHERE merchant <> ''")]
    if not names:
        return 0
    alias = merchant_aliases(names)

    changed = 0
    for short, final in alias.items():
        for table in ("tx", "tx_fx", "hold"):
            cur = conn.execute(
                f"UPDATE {table} SET merchant = ? WHERE merchant = ?", (final, short))
            changed += cur.rowcount
    return changed


def rebuild_fts(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM tx_fts")
    conn.execute(
        """INSERT INTO tx_fts (rowid, merchant, channel, subject)
           SELECT t.id, t.merchant, t.channel, m.subject
           FROM tx t JOIN mail m ON m.id = t.mail_id"""
    )
    return conn.execute("SELECT COUNT(*) FROM tx_fts").fetchone()[0]


def import_dir(mail_dir: str, db_path: str = DEFAULT_DB, rebuild: bool = False,
               quiet: bool = False) -> dict:
    """把目录下所有 .eml 导入数据库，返回统计信息。"""
    files = sorted(glob.glob(os.path.join(mail_dir, "*.eml")))
    if not files:
        raise SystemExit(f"目录里没有 .eml：{mail_dir}")

    conn = connect(db_path)
    had_mail = conn.execute("SELECT COUNT(*) FROM mail").fetchone()[0]
    if not rebuild and ensure_schema(conn):
        rebuild = True
        if not quiet and had_mail:
            print("库结构已升级，正在全量重建…", file=sys.stderr)
    if rebuild:
        conn.executescript("DROP VIEW IF EXISTS v_tx; DROP VIEW IF EXISTS v_monthly;"
                           " DROP VIEW IF EXISTS v_merchant; DROP VIEW IF EXISTS v_channel;"
                           " DROP TABLE IF EXISTS tx_fts; DROP TABLE IF EXISTS tx;"
                           " DROP TABLE IF EXISTS tx_fx;"
                           " DROP TABLE IF EXISTS hold; DROP TABLE IF EXISTS bill;"
                           " DROP TABLE IF EXISTS credit;"
                           " DROP TABLE IF EXISTS mail; DROP TABLE IF EXISTS meta;")
        conn.commit()
        init_schema(conn)

    existing = _existing_mails(conn)
    added = updated = skipped = failed = removed = 0
    started = datetime.now()

    for path in files:
        base = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0  # 文件可能刚被删/无权限，后面 parse 会失败并落「解析失败」行

        digest = ""
        prev = existing.get(base)
        if prev is not None and not rebuild:
            prev_size, prev_hash, prev_cat = prev
            # 解析失败行永远重试（解析器修好后能自动恢复）；其余按 大小+内容哈希 判断
            if prev_size == size and prev_cat != "解析失败":
                digest = _file_hash(path)
                if digest and digest == prev_hash:
                    skipped += 1
                    continue
            row = conn.execute("SELECT id FROM mail WHERE file = ?", (base,)).fetchone()
            if row:
                _delete_mail(conn, row[0])
            updated += 1
        else:
            added += 1

        try:
            rec = parse_email(path)
        except Exception as exc:
            failed += 1
            if not quiet:
                print(f"  解析失败 {base}: {exc}", file=sys.stderr)
            _insert_failed_mail(conn, base, size, str(exc))
            continue
        _insert_mail(conn, rec, path, digest or _file_hash(path))

    # 清理目录里已删除的邮件：只在「导入目录 == 上次记录的目录」时做，
    # 避免换个目录导入把旧目录的数据误删。全量重建后不存在陈旧行。
    if not rebuild:
        prev_dir = conn.execute(
            "SELECT value FROM meta WHERE key = 'mail_dir'").fetchone()
        if prev_dir and prev_dir[0] == os.path.abspath(mail_dir):
            present = {os.path.basename(p) for p in files}
            for r in conn.execute("SELECT id, file FROM mail").fetchall():
                if r["file"] not in present:
                    _delete_mail(conn, r["id"])
                    removed += 1

    conn.commit()
    changed = normalize_merchants(conn)
    if changed:
        rebuild_fts(conn)
    conn.commit()

    total_tx = conn.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    total_fx = conn.execute("SELECT COUNT(*) FROM tx_fx").fetchone()[0]
    total_mail = conn.execute("SELECT COUNT(*) FROM mail").fetchone()[0]
    total_hold = conn.execute("SELECT COUNT(*) FROM hold").fetchone()[0]
    total_bill = conn.execute("SELECT COUNT(*) FROM bill").fetchone()[0]
    total_credit = conn.execute("SELECT COUNT(*) FROM credit").fetchone()[0]
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [("mail_dir", os.path.abspath(mail_dir)),
         ("last_sync", started.strftime("%Y-%m-%d %H:%M:%S")),
         ("mail_count", str(total_mail)),
         ("tx_count", str(total_tx)),
         ("tx_fx_count", str(total_fx)),
         ("hold_count", str(total_hold)),
         ("bill_count", str(total_bill)),
         ("credit_count", str(total_credit)),
         ("schema_version", str(SCHEMA_VERSION))],
    )
    conn.commit()

    # WAL 里的数据还没落主文件时 getsize 会严重偏小（首导入常见 4096 字节），
    # 先 checkpoint 再量，报告的才是真实库体积
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    info = {
        "db": os.path.abspath(db_path),
        "db_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "mail_total": total_mail,
        "tx_total": total_tx,
        "tx_fx_total": total_fx,
        "hold_total": total_hold,
        "bill_total": total_bill,
        "credit_total": total_credit,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "removed": removed,
        "merchant_merged": changed,
        "elapsed": round((datetime.now() - started).total_seconds(), 2),
    }
    conn.close()
    if not quiet:
        print(f"数据库：{info['db']}（{info['db_bytes'] / 1024 / 1024:.1f} MB）")
        print(f"邮件 {total_mail} 封 · 交易 {total_tx} 笔 · 外币 {total_fx} 笔 · 预授权 {total_hold} 笔 · 账单 {total_bill} 期")
        print(f"新增 {added} · 更新 {updated} · 跳过 {skipped} · 失败 {failed} · 清理 {removed} "
              f"· 商户名合并 {changed} 行 · 耗时 {info['elapsed']}s")
    return info


# --------------------------------------------------------------------------- #
# 查询层（供 MCP 复用）
# --------------------------------------------------------------------------- #

def _filters(since=None, until=None, kind=None, channel=None, card=None,
             keyword=None, merchant=None) -> tuple[str, list]:
    where, params = ["1=1"], []
    if since:
        where.append("t.tx_date >= ?")
        params.append(since)
    if until:
        where.append("t.tx_date <= ?")
        params.append(until)
    if kind:
        where.append("t.kind = ?")
        params.append(kind)
    if channel:
        where.append("t.channel = ?")
        params.append(channel)
    if card:
        where.append("t.card = ?")
        params.append(card)
    if merchant:
        where.append("t.merchant LIKE ?")
        params.append(f"%{merchant}%")
    if keyword:
        where.append("(t.merchant LIKE ? OR t.channel LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    return " AND ".join(where), params


def overview(conn: sqlite3.Connection) -> dict:
    base = conn.execute(
        """SELECT COUNT(*) tx, ROUND(SUM(signed_amount), 2) net, ROUND(SUM(amount), 2) gross,
                  MIN(tx_date) first_day, MAX(tx_date) last_day,
                  COUNT(DISTINCT merchant) merchants, COUNT(DISTINCT card) cards,
                  ROUND(AVG(amount), 2) avg_amount
           FROM tx""").fetchone()
    repay = conn.execute(
        "SELECT COUNT(*) n, ROUND(SUM(bill_amount), 2) total FROM mail "
        "WHERE bill_amount IS NOT NULL").fetchone()
    mails = conn.execute(
        "SELECT COUNT(*) n, MIN(mail_time) first_dt, MAX(mail_time) last_dt FROM mail").fetchone()
    hold = conn.execute(
        "SELECT COUNT(*) n, ROUND(COALESCE(SUM(amount), 0), 2) v FROM hold").fetchone()
    bill = conn.execute(
        "SELECT COUNT(*) n, ROUND(COALESCE(SUM(interest), 0), 2) interest "
        "FROM bill").fetchone()
    fx = [{"currency": r["currency"], "count": r["n"], "net_amount": r["net"]}
          for r in conn.execute(
              "SELECT currency, COUNT(*) n, ROUND(SUM(signed_amount), 2) net "
              "FROM tx_fx GROUP BY currency ORDER BY currency")]
    return {
        "mails": mails["n"],
        "mail_range": [mails["first_dt"], mails["last_dt"]],
        "transactions": base["tx"],
        "net_spend": base["net"],
        "gross_spend": base["gross"],
        "tx_date_range": [base["first_day"], base["last_day"]],
        "avg_amount": base["avg_amount"],
        "merchants": base["merchants"],
        "cards": base["cards"],
        "repay_records": repay["n"],
        "repay_total": repay["total"],
        "hold_records": hold["n"],
        "hold_amount": hold["v"],
        "bill_records": bill["n"],
        "bill_interest": bill["interest"],
        # 外币原币单列：邮件没有人民币折算，不能混进 net_spend
        "foreign_currency": fx,
        "coverage": coverage(conn),
    }


def _month_range(first: str, last: str) -> list[str]:
    """['2017-01', ..., '2026-11'] 的连续月份列表。"""
    out = []
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def coverage(conn: sqlite3.Connection) -> dict:
    """数据体检：月份缺口、低覆盖月、账单覆盖、解析失败、预授权占用。

    提醒流靠邮件推送，漏收一封就是一个月少几十笔，而面板上看不出任何异常。
    这里给出可判定的信号，供 ``get_overview`` 直接暴露。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT month, COUNT(*) n FROM tx GROUP BY month ORDER BY month")]
    counts = sorted(r["n"] for r in rows)
    median = counts[len(counts) // 2] if counts else 0
    failed = conn.execute(
        "SELECT COUNT(*) FROM mail WHERE category = '解析失败'").fetchone()[0]
    failed_files = [r[0] for r in conn.execute(
        "SELECT file FROM mail WHERE category = '解析失败' ORDER BY file LIMIT 20")]
    if not rows:
        return {"months": 0, "missing_months": [], "low_months": [],
                "parse_failed": failed, "parse_failed_files": failed_files}
    seen = {r["month"]: r["n"] for r in rows}
    all_months = _month_range(rows[0]["month"], rows[-1]["month"])
    threshold = max(3, int(median * 0.3))
    latest = all_months[-1]  # 当月未过完，笔数天然偏低，不算异常
    low = [{"month": m, "tx": n} for m, n in seen.items()
           if n < threshold and m != latest]
    missing = [m for m in all_months if m not in seen]
    bill_months = {r[0] for r in conn.execute(
        "SELECT DISTINCT bill_month FROM bill WHERE bill_month <> ''")}
    bill_missing = [m for m in all_months if m not in bill_months]
    no_tx = {r[0]: r[1] for r in conn.execute(
        "SELECT category, COUNT(*) FROM mail WHERE tx_count = 0 GROUP BY category")}
    return {
        "months": len(all_months),
        "partial_month": latest,
        "median_tx_per_month": median,
        "low_threshold": threshold,
        "missing_months": missing,
        "low_months": sorted(low, key=lambda x: x["month"]),
        "bill_months": len(bill_months),
        "bill_missing_months": bill_missing,
        "parse_failed": failed,
        "parse_failed_files": failed_files,
        "mails_without_tx": no_tx,
    }


def bills(conn: sqlite3.Connection, limit: int = 24, since=None) -> list[dict]:
    """账单期列表（新的在前），带提醒流的对账差额。

    ``reconcile_diff`` = 账单「本期消费-退款」 - 同期提醒流净支出。
    差值主要来自记账日与交易日的错位、以及账单里含的分期/费用，属于正常范围；
    差值过大说明提醒流缺邮件。
    """
    sql = ("""SELECT b.*, m.mail_time FROM bill b JOIN mail m ON m.id = b.mail_id
              WHERE 1=1""")
    params: list = []
    if since:
        sql += " AND COALESCE(b.cycle_end, b.bill_month) >= ?"
        params.append(since)
    sql += " ORDER BY COALESCE(b.cycle_end, b.bill_month || '-01', '') DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        if d["cycle_start"] and d["cycle_end"]:
            agg = conn.execute(
                """SELECT COUNT(*) n, ROUND(COALESCE(SUM(signed_amount), 0), 2) v
                   FROM tx WHERE tx_date BETWEEN ? AND ? AND kind IN ('消费', '退款')""",
                (d["cycle_start"], d["cycle_end"])).fetchone()
            d["reminder_count"] = agg["n"]
            d["reminder_net"] = agg["v"]
            if d["debits"] is not None:
                bill_net = round((d["debits"] or 0) - (d["credits"] or 0), 2)
                d["bill_net"] = bill_net
                d["reconcile_diff"] = round(bill_net - agg["v"], 2)
        out.append(d)
    return out


def bill_summary(conn: sqlite3.Connection) -> dict:
    """账单口径的汇总：额度、应还、最低还款、利息、还款覆盖。"""
    r = conn.execute(
        """SELECT COUNT(*) n,
                  MAX(credit_limit) credit_limit_max,
                  ROUND(SUM(due_amount), 2) due_total,
                  ROUND(SUM(interest), 2) interest_total,
                  ROUND(AVG(due_amount), 2) due_avg,
                  MIN(bill_month) first, MAX(bill_month) last
           FROM bill""").fetchone()
    latest = conn.execute(
        """SELECT bill_month, cycle_start, cycle_end, due_date, credit_limit,
                  due_amount, min_amount, debits, credits, interest
           FROM bill ORDER BY COALESCE(cycle_end, bill_month) DESC LIMIT 1""").fetchone()
    pay = conn.execute(
        "SELECT COUNT(*) n, ROUND(COALESCE(SUM(bill_amount), 0), 2) v FROM mail "
        "WHERE category = '自动还款'").fetchone()
    utilization = None
    if latest and latest["credit_limit"]:
        utilization = round((latest["due_amount"] or 0) / latest["credit_limit"] * 100, 1)
    return {
        "bill_count": r["n"],
        "range": [r["first"], r["last"]],
        "credit_limit": (dict(latest)["credit_limit"] if latest else None),
        "credit_limit_max": r["credit_limit_max"],
        "interest_total": r["interest_total"] or 0,
        "due_avg": r["due_avg"],
        "latest": dict(latest) if latest else None,
        "latest_utilization_pct": utilization,
        "repayment_count": pay["n"],
        "repayment_total": pay["v"],
        "caveat": "due_amount 是「本期应还」，不是消费额；credit_limit 取最新一期的账单额度，"
                  "credit_limit_max 是历史最高（含临时额度）。latest_utilization_pct 只是"
                  "本期应还占额度比例，真实已用额度见 credit_usage（由可用额度反推）。",
    }


def credit_usage(conn: sqlite3.Connection, limit: int = 36) -> dict:
    """额度使用率：用每日信用管家的可用额度反推已用额度。

    额度上限按账单里的 credit_limit 分时段取值（银行会调额），
    因此 used = 当期额度 - 可用额度，pct = used / 当期额度。
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT dt, ROUND(avail_credit, 2) avail, limit_credit, points FROM credit
           WHERE avail_credit IS NOT NULL ORDER BY dt""")]
    if not rows:
        return {"series": [], "latest": None, "limit_timeline": []}
    limits = [dict(r) for r in conn.execute(
        """SELECT bill_month, credit_limit FROM bill
           WHERE credit_limit IS NOT NULL ORDER BY bill_month""")]
    timeline = sorted({(l["bill_month"], l["credit_limit"]) for l in limits})

    def limit_for(month: str) -> float | None:
        best = None
        for m, v in timeline:
            if m <= month:
                best = v
            else:
                break
        return best if best is not None else (timeline[0][1] if timeline else None)

    series = []
    for r in rows:
        # 邮件自带额度优先（老版式能反推出已用+可用），否则按账单额度分时段取
        lim = r["limit_credit"] or limit_for(r["dt"][:7])
        if not lim:
            continue
        used = round(lim - r["avail"], 2)
        # 可用额度高于账单里的固定额度 → 当期有临时额度，按 0 已用处理并打标，
        # 宁可少报也不要出现「已用 -5 万」这种结论
        temp = used < 0
        if temp:
            used = 0.0
        series.append({"dt": r["dt"], "avail": r["avail"], "limit": lim,
                       "used": used,
                       "pct": round(used / lim * 100, 1) if lim else None,
                       "temp_limit": temp, "points": r["points"]})
    latest = series[-1] if series else None
    changes = []
    prev = None
    for m, v in timeline:
        if v != prev:
            changes.append({"month": m, "credit_limit": v})
            prev = v
    return {"series": series[-limit:], "latest": latest, "limit_timeline": changes}


def interest_months(conn: sqlite3.Connection) -> list[dict]:
    """出现过利息的账单期（循环利息/取现/最低还款的成本）。"""
    return [dict(r) for r in conn.execute(
        """SELECT bill_month, ROUND(interest, 2) interest, ROUND(due_amount, 2) due_amount
           FROM bill WHERE COALESCE(interest, 0) > 0 ORDER BY bill_month""")]


def aggregate(conn: sqlite3.Connection, dimension: str = "month", since=None,
              until=None, kind=None, channel=None, card=None, keyword=None,
              limit: int | None = None) -> list[dict]:
    """按维度聚合，按净支出降序；dimension 见 DIMENSIONS。"""
    if dimension not in DIMENSIONS:
        raise ValueError(f"不支持的维度：{dimension}，可选 {list(DIMENSIONS)}")
    where, params = _filters(since, until, kind, channel, card, keyword)
    if dimension in ("month", "year", "hour"):
        order = "name ASC"
    elif dimension == "amount_bucket":
        order = "MIN(t.amount) ASC"  # 区间桶按金额下限排序
    else:
        order = "net_amount DESC"
    sql = f"""
        SELECT {DIMENSIONS[dimension]} AS name,
               COUNT(*) AS tx_count,
               ROUND(SUM(t.signed_amount), 2) AS net_amount,
               ROUND(SUM(t.amount), 2) AS gross_amount
        FROM tx t
        WHERE {where}
        GROUP BY name
        HAVING name IS NOT NULL AND name <> ''
        ORDER BY {order}
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def aggregate_totals(conn: sqlite3.Connection, since=None, until=None, kind=None,
                     channel=None, card=None, keyword=None) -> dict:
    """筛选后的全量合计，不受分组 LIMIT 截断影响。

    aggregate 的分组列表会被 limit 截断，拿返回行求和当总量会系统性偏小；
    表头口径（共 N 笔 / 净支出 X）必须单独按同一套过滤条件全量统计。
    """
    where, params = _filters(since, until, kind, channel, card, keyword)
    r = conn.execute(
        f"""SELECT COUNT(*) AS tx_count,
                   ROUND(COALESCE(SUM(t.signed_amount), 0), 2) AS net_amount,
                   ROUND(COALESCE(SUM(t.amount), 0), 2) AS gross_amount
            FROM tx t WHERE {where}""", params).fetchone()
    return {"count": r["tx_count"], "net": r["net_amount"], "gross": r["gross_amount"]}


# --------------------------------------------------------------------------- #
# 环比 / 同比
# --------------------------------------------------------------------------- #

PERIOD_COLUMN = {"month": "t.month", "year": "CAST(t.year AS TEXT)"}


def shift_period(label: str, granularity: str, step: int) -> str:
    """把 2026-10 / 2026 平移 step 个周期，用于取上一期或去年同期。"""
    if granularity == "year":
        return str(int(label) + step)
    year, month = (int(x) for x in label.split("-")[:2])
    index = year * 12 + month - 1 + step
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def period_bounds(label: str, granularity: str) -> tuple[str, str]:
    """周期对应的首末日期，便于向用户交代口径。"""
    if granularity == "year":
        return f"{label}-01-01", f"{label}-12-31"
    year, month = (int(x) for x in label.split("-")[:2])
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def period_series(conn: sqlite3.Connection, granularity: str = "month",
                  since=None, until=None) -> dict[str, dict]:
    """按周期汇总净支出，返回 {周期: {label, net, count}}，按周期升序。"""
    if granularity not in PERIOD_COLUMN:
        raise ValueError(f"granularity 只支持 {list(PERIOD_COLUMN)}")
    where, params = [], []
    if since:
        where.append("t.tx_date >= ?")
        params.append(since)
    if until:
        where.append("t.tx_date <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""SELECT {PERIOD_COLUMN[granularity]} AS p,
                   COUNT(*) AS n,
                   ROUND(SUM(t.signed_amount), 2) AS net
            FROM tx t{clause}
            GROUP BY p ORDER BY p""", params)
    return {r["p"]: {"label": r["p"], "net": r["net"] or 0.0, "count": r["n"]} for r in rows}


def _pct(delta: float, base: float) -> float | None:
    """变化率；基期为 0 时没有意义，返回 None 而不是 0 或无穷大。"""
    if not base:
        return None
    return round(delta / abs(base) * 100, 1)


def compare_periods(conn: sqlite3.Connection, granularity: str = "month",
                    current=None, compare: str = "mom", by=None,
                    since=None, until=None, top: int = 6,
                    series_len: int = 12) -> dict:
    """本期 vs 上一期（mom）或去年同期（yoy）。

    by 指定维度时，一并给出「哪些类别把差额推上去/拉下来」，
    这是回答「这个月为什么多花了」的关键——只看总差额没有用。
    """
    if compare not in ("mom", "yoy"):
        raise ValueError("compare 只支持 mom（环比）/ yoy（同比）")
    data = period_series(conn, granularity, since, until)
    if not data:
        return {"error": "该范围内没有交易数据"}

    labels = sorted(data)
    # 数据截止日：传了 until 就以截断范围为准，否则截出来的半个月不会被标记 partial
    if until:
        last_tx = conn.execute(
            "SELECT MAX(tx_date) FROM tx WHERE tx_date <= ?", (until,)).fetchone()[0] or ""
    else:
        last_tx = conn.execute("SELECT MAX(tx_date) FROM tx").fetchone()[0] or ""
    if current:
        cur_label = _normalize_period(current, granularity)
    else:
        cur_label = labels[-1]
    # 月维度同比退 12 个月，年维度同比退 1 年
    step = (-1 if compare == "mom" else (-12 if granularity == "month" else -1))

    # 当期还没走完时，默认退到上一个完整周期——拿半个月比整月只会系统性偏低
    skipped = ""
    if not current and len(labels) > 1:
        _, cur_last_day = period_bounds(cur_label, granularity)
        if cur_label == labels[-1] and last_tx and cur_last_day > last_tx:
            skipped = cur_label
            cur_label = labels[-2]

    base_label = shift_period(cur_label, granularity, step)

    cur = data.get(cur_label, {"label": cur_label, "net": 0.0, "count": 0})
    base = data.get(base_label, {"label": base_label, "net": 0.0, "count": 0})
    cur_missing = cur_label not in data
    base_missing = base_label not in data
    delta = round(cur["net"] - base["net"], 2)

    # 当期是否还没走完：最后一天晚于数据截止日，就不能当完整周期比
    _, cur_last_day = period_bounds(cur_label, granularity)
    partial = bool(last_tx and cur_label in labels and cur_label == labels[-1]
                   and cur_last_day > last_tx)

    series = []
    yoy_step = -12 if granularity == "month" else -1
    for label in labels[-max(1, series_len):]:
        prev = data.get(shift_period(label, granularity, -1))
        yoy = data.get(shift_period(label, granularity, yoy_step))
        series.append({
            "label": label,
            "net": data[label]["net"],
            "count": data[label]["count"],
            "mom_delta": round(data[label]["net"] - prev["net"], 2) if prev else None,
            "mom_pct": _pct(data[label]["net"] - prev["net"], prev["net"]) if prev else None,
            "yoy_delta": round(data[label]["net"] - yoy["net"], 2) if yoy else None,
            "yoy_pct": _pct(data[label]["net"] - yoy["net"], yoy["net"]) if yoy else None,
        })

    movers = []
    if by:
        if by not in DIMENSIONS:
            raise ValueError(f"不支持的维度：{by}")
        rows = conn.execute(f"""
            SELECT {DIMENSIONS[by]} AS label,
                   {PERIOD_COLUMN[granularity]} AS p,
                   ROUND(SUM(t.signed_amount), 2) AS net,
                   COUNT(*) AS n
            FROM tx t
            WHERE {PERIOD_COLUMN[granularity]} IN (?, ?)
            GROUP BY label, p""", (cur_label, base_label)).fetchall()
        bucket: dict[str, dict] = {}
        for r in rows:
            name = r["label"]
            if not name:
                continue
            item = bucket.setdefault(name, {"label": name, "net": 0.0,
                                            "baseline": 0.0, "count": 0})
            if r["p"] == cur_label:
                item["net"] += r["net"] or 0.0
                item["count"] += r["n"]
            else:
                item["baseline"] += r["net"] or 0.0
        for item in bucket.values():
            item["delta"] = round(item["net"] - item["baseline"], 2)
            item["pct"] = _pct(item["delta"], item["baseline"])
        movers = sorted(bucket.values(), key=lambda x: -abs(x["delta"]))[:max(1, top)]

    return {
        "granularity": granularity,
        "compare": compare,
        "compare_label": "环比" if compare == "mom" else "同比",
        "period": {**cur, "from": period_bounds(cur_label, granularity)[0],
                   "to": period_bounds(cur_label, granularity)[1],
                   "partial": partial, "missing": cur_missing},
        "baseline": {**base, "missing": base_missing},
        "delta": delta,
        "pct": _pct(delta, base["net"]),
        "movers": movers,
        "series": series,
        "skipped_partial": skipped,
        "caveat": ("当期尚未结束，与完整周期直接相减会低估。" if partial else "")
                  + (f"已跳过未走完的 {skipped}，改用上一个完整周期。" if skipped else "")
                  + ("本期没有任何交易，变化率会显示成 -100%，注意别当真实结论。" if cur_missing else "")
                  + ("基期没有任何交易，变化率无意义。" if base_missing else ""),
    }


def _normalize_period(value: str, granularity: str) -> str:
    """接受 2026-10 / 2026-10-15 / 2026，统一成周期标签。"""
    text = str(value).strip()
    if granularity == "year":
        return text[:4]
    match = re.match(r"^(20\d{2})[-/]?(\d{1,2})", text)
    if not match:
        raise ValueError(f"无法识别的周期：{value}")
    return f"{match.group(1)}-{int(match.group(2)):02d}"


# --------------------------------------------------------------------------- #
# 周期扣款 / 订阅识别
# --------------------------------------------------------------------------- #

# 间隔中位数落在区间内 → 判为该周期；min_cycles 是「至少要连续扣几期」，
# min_span 是「整体至少跨几个周期」，两个都卡住 3 个月 4 笔的巧合。
SUBSCRIPTION_CADENCE = {
    "monthly": {"label": "月度", "min_gap": 24, "max_gap": 40,
                "min_cycles": 5, "min_span": 6, "per_year": 12, "step_months": 1},
    "quarterly": {"label": "季度", "min_gap": 70, "max_gap": 110,
                  "min_cycles": 3, "min_span": 6, "per_year": 4, "step_months": 3},
    "yearly": {"label": "年度", "min_gap": 330, "max_gap": 400,
               "min_cycles": 2, "min_span": 2, "per_year": 1, "step_months": 12},
}


def _cycle_key(day: date, cadence: str) -> str:
    if cadence == "yearly":
        return str(day.year)
    if cadence == "quarterly":
        return f"{day.year}Q{(day.month - 1) // 3 + 1}"
    return day.strftime("%Y-%m")


def _cycle_span(first: date, last: date, cadence: str) -> int:
    if cadence == "yearly":
        return last.year - first.year + 1
    if cadence == "quarterly":
        return (last.year - first.year) * 4 + (last.month - 1) // 3 - (first.month - 1) // 3 + 1
    return (last.year - first.year) * 12 + last.month - first.month + 1


def detect_subscriptions(conn: sqlite3.Connection, since=None, until=None,
                         cadence: str | None = None, min_coverage: float = 0.7,
                         include_inactive: bool = True, limit: int = 50) -> dict:
    """从「同一商户、周期规律、金额接近」的消费里找出周期扣款。

    只做统计，不预设品牌名单——所以话费、物业费、会员费都能自己浮出来，
    而食堂、咖啡这种「每月都去但不规律」的会被规律性条件挡掉。
    """
    if cadence and cadence not in SUBSCRIPTION_CADENCE:
        raise ValueError(f"cadence 只支持 {list(SUBSCRIPTION_CADENCE)}")
    where = ["t.kind = '消费'", "t.sign = 1", "t.amount > 0", "t.merchant <> ''"]
    params: list = []
    if since:
        where.append("t.tx_date >= ?")
        params.append(since)
    if until:
        where.append("t.tx_date <= ?")
        params.append(until)
    rows = conn.execute(
        f"""SELECT t.merchant, t.tx_date, t.amount
            FROM tx t WHERE {' AND '.join(where)}
            ORDER BY t.merchant, t.tx_date""", params).fetchall()

    last_tx = conn.execute("SELECT MAX(tx_date) FROM tx").fetchone()[0] or ""
    ref_day = date.fromisoformat(until or last_tx or date.today().isoformat())

    groups: dict[str, dict] = {}
    for r in rows:
        key = r["merchant"].strip().lower()
        g = groups.setdefault(key, {"names": {}, "days": [], "amounts": []})
        g["names"][r["merchant"]] = g["names"].get(r["merchant"], 0) + 1
        g["days"].append(date.fromisoformat(r["tx_date"]))
        g["amounts"].append(float(r["amount"]))

    items = []
    for key, g in groups.items():
        days, amounts = g["days"], g["amounts"]
        if len(days) < 4:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        median_gap = statistics.median(gaps)
        hit = None
        for name, spec in SUBSCRIPTION_CADENCE.items():
            if spec["min_gap"] <= median_gap <= spec["max_gap"]:
                hit = (name, spec)
                break
        if not hit or (cadence and hit[0] != cadence):
            continue
        name, spec = hit
        by_cycle: dict[str, float] = {}
        for day, amount in zip(days, amounts):
            ck = _cycle_key(day, name)
            by_cycle[ck] = by_cycle.get(ck, 0.0) + amount
        cycles = len(by_cycle)
        span = _cycle_span(days[0], days[-1], name)
        coverage = cycles / span if span else 0
        per_cycle_tx = len(days) / cycles if cycles else 99
        if cycles < spec["min_cycles"] or span < spec["min_span"]:
            continue
        if coverage < min_coverage or per_cycle_tx > 2:
            continue

        values = list(by_cycle.values())
        median_amount = statistics.median(values)
        if median_amount <= 0:
            continue
        amount_mad = statistics.median(abs(v - median_amount) for v in values) / median_amount
        # 金额波动越小越像真订阅；话费这类正常波动归为「金额浮动」
        if amount_mad <= 0.05:
            stability, confidence = "固定金额", "high"
        elif amount_mad <= 0.3:
            stability, confidence = "小幅浮动", "medium"
        else:
            stability, confidence = "波动较大", "low"

        display = max(g["names"].items(), key=lambda kv: kv[1])[0]
        next_expected = days[-1] + timedelta(days=round(median_gap))
        days_idle = (ref_day - days[-1]).days
        active = days_idle <= median_gap * 1.6
        if not active and not include_inactive:
            continue
        # 每年扣 per_year 次 → 年成本 = 单期金额 × 次数，月成本 = 年成本 / 12
        annual_cost = median_amount * spec["per_year"]
        monthly_cost = annual_cost / 12
        items.append({
            "merchant": display,
            "cadence": name,
            "cadence_label": spec["label"],
            "occurrences": len(days),
            "cycles": cycles,
            "span_cycles": span,
            "coverage": round(coverage, 2),
            "per_cycle_tx": round(per_cycle_tx, 2),
            "amount": round(median_amount, 2),
            "amount_min": round(min(values), 2),
            "amount_max": round(max(values), 2),
            "amount_mad": round(amount_mad, 3),
            "stability": stability,
            "confidence": confidence,
            "monthly_cost": round(monthly_cost, 2),
            "annual_cost": round(monthly_cost * 12, 2),
            "total_paid": round(sum(amounts), 2),
            "first_date": days[0].isoformat(),
            "last_date": days[-1].isoformat(),
            "next_expected": next_expected.isoformat(),
            "active": active,
            "days_idle": days_idle,
        })

    items.sort(key=lambda x: (-x["monthly_cost"], x["merchant"]))
    items = items[:max(1, limit)]
    active_items = [i for i in items if i["active"]]
    return {
        "items": items,
        "summary": {
            "found": len(items),
            "active": len(active_items),
            "monthly_total": round(sum(i["monthly_cost"] for i in active_items), 2),
            "annual_total": round(sum(i["annual_cost"] for i in active_items), 2),
            "by_cadence": {spec["label"]: sum(1 for i in items if i["cadence"] == key)
                           for key, spec in SUBSCRIPTION_CADENCE.items()},
            "ref_date": ref_day.isoformat(),
        },
        "caveat": "识别的是「周期规律 + 金额接近」的扣款，不认品牌；金额波动大的会被标成"
                  "「波动较大」，通常对应话费这类按用量计费的账单，而不是订阅。"
                  "商户名由银行截断，同一家可能裂成多条。",
    }


def top_merchants(conn: sqlite3.Connection, limit: int = 10, since=None,
                  until=None, kind=None) -> list[dict]:
    where, params = _filters(since, until, kind)
    rows = conn.execute(f"""
        SELECT t.merchant AS name, COUNT(*) AS tx_count,
               ROUND(SUM(t.signed_amount), 2) AS net_amount,
               ROUND(AVG(t.amount), 2) AS avg_amount,
               ROUND(MAX(t.amount), 2) AS max_amount,
               MIN(t.tx_date) AS first_date, MAX(t.tx_date) AS last_date
        FROM tx t
        WHERE {where} AND t.merchant <> ''
        GROUP BY t.merchant
        ORDER BY net_amount DESC, tx_count DESC
        LIMIT ?""", [*params, limit])
    return [dict(r) for r in rows]


def _search_clauses(conn: sqlite3.Connection, keyword: str | None, since, until,
                    min_amount, max_amount, kind) -> tuple:
    """构造人民币/外币检索的 WHERE 与参数，明细与全量合计共用。

    FTS 与 LIKE 的取舍在这里一次性决定（FTS 命中为空才退 LIKE），
    保证 search_transactions 与 search_summary 两边口径完全一致。
    """
    base_where, base_params = _filters(since, until, kind)
    kw = (keyword or "").strip()
    extra_where, extra_params = "", []
    if min_amount is not None:
        extra_where += " AND t.amount >= ?"
        extra_params.append(min_amount)
    if max_amount is not None:
        extra_where += " AND t.amount <= ?"
        extra_params.append(max_amount)
    like_kw = " AND (t.merchant LIKE ? OR t.channel LIKE ?)"
    like_params = [f"%{kw}%"] * 2

    # 外币行少（几十笔），不进 FTS，直接 LIKE；金额过滤按原币数值
    fx_where = base_where + (like_kw if kw else "") + extra_where
    fx_params = base_params + (like_params if kw else []) + extra_params

    cny_where, cny_params = base_where + extra_where, base_params + extra_params
    if kw:
        # trigram 分词对 <3 字符的串不建索引，直接走 LIKE；
        # >=3 字符优先走 FTS，命中为空时再退一次 LIKE（索引漂移、全角差异都可能漏）
        use_fts = False
        if len(kw) >= 3:
            fts_where = (base_where
                         + " AND t.id IN (SELECT rowid FROM tx_fts WHERE tx_fts MATCH ?)"
                         + extra_where)
            fts_params = base_params + ['"' + kw.replace('"', '""') + '"'] + extra_params
            use_fts = conn.execute(
                f"SELECT 1 FROM tx t WHERE {fts_where} LIMIT 1",
                fts_params).fetchone() is not None
        if use_fts:
            cny_where, cny_params = fts_where, fts_params
        else:
            cny_where = base_where + like_kw + extra_where
            cny_params = base_params + like_params + extra_params
    return cny_where, cny_params, fx_where, fx_params


def search_transactions(conn: sqlite3.Connection, keyword: str | None = None,
                        since=None, until=None, min_amount=None, max_amount=None,
                        kind=None, limit: int = 20) -> list[dict]:
    cny_where, cny_params, fx_where, fx_params = _search_clauses(
        conn, keyword, since, until, min_amount, max_amount, kind)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT t.tx_date, t.tx_time, t.merchant, t.channel, t.kind, t.state,
               t.amount, t.sign, t.card, 'CNY' AS currency, m.category AS mail_category
        FROM tx t JOIN mail m ON m.id = t.mail_id
        WHERE {cny_where}
        ORDER BY t.tx_date DESC, t.tx_time DESC
        LIMIT ?""", [*cny_params, limit])]
    rows += [dict(r) for r in conn.execute(f"""
        SELECT t.tx_date, t.tx_time, t.merchant, t.channel, t.kind, t.state,
               t.amount, t.sign, t.card, t.currency, m.category AS mail_category
        FROM tx_fx t JOIN mail m ON m.id = t.mail_id
        WHERE {fx_where}
        ORDER BY t.tx_date DESC, t.tx_time DESC
        LIMIT ?""", [*fx_params, limit])]
    rows.sort(key=lambda r: (r["tx_date"], r["tx_time"]), reverse=True)
    return rows[:limit]


def search_summary(conn: sqlite3.Connection, keyword: str | None = None,
                   since=None, until=None, min_amount=None, max_amount=None,
                   kind=None) -> dict:
    """全量命中口径：笔数与净额不受明细 limit 截断影响。

    「命中 N 笔、合计 X 元」如果拿截断后的明细求和，会把前 30 条当成全部，
    这里按与 search_transactions 相同的谓词对全表统计。
    """
    cny_where, cny_params, fx_where, fx_params = _search_clauses(
        conn, keyword, since, until, min_amount, max_amount, kind)
    r = conn.execute(
        f"""SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(t.signed_amount), 0), 2) AS net
            FROM tx t WHERE {cny_where}""", cny_params).fetchone()
    fx = [{"currency": row["currency"], "count": row["n"], "net_amount": row["net"]}
          for row in conn.execute(
              f"""SELECT t.currency, COUNT(*) AS n, ROUND(SUM(t.signed_amount), 2) AS net
                  FROM tx_fx t WHERE {fx_where}
                  GROUP BY t.currency ORDER BY t.currency""", fx_params)]
    return {"matched": r["n"] + sum(f["count"] for f in fx),
            "cny_count": r["n"], "net_spend": r["net"], "foreign": fx}


def monthly(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT month, tx_count, net_amount, gross_amount FROM v_monthly ORDER BY month")]


def dashboard_payload(conn: sqlite3.Connection) -> dict:
    """给面板用的紧凑载荷：数组下标含义见 KNOWN 注释，减少 JSON 体积。"""
    txs = conn.execute("""
        SELECT t.tx_date, t.tx_time, t.merchant, t.channel, t.kind,
               t.amount, t.sign, t.card
        FROM tx t
        ORDER BY t.tx_date, t.tx_time""").fetchall()
    repays = conn.execute("""
        SELECT mail_date, category, bill_amount
        FROM mail WHERE bill_amount IS NOT NULL
        ORDER BY mail_date""").fetchall()
    meta = overview(conn)
    # 库文件可能被 CMB_DB 指到别处，面板页眉显示真实位置而不是写死
    row = conn.execute("PRAGMA database_list").fetchone()
    meta["db_file"] = db_label(row["file"]) if row else ""
    # 账单与额度：账本级数（bills 里带对账差额）
    rec = {b["bill_month"]: b.get("reconcile_diff") for b in bills(conn, limit=1000)}
    bill_rows = conn.execute("""
        SELECT bill_month, due_date, due_amount, min_amount, debits, interest
        FROM bill ORDER BY bill_month""").fetchall()
    usage = credit_usage(conn, 800)["series"]
    return {
        "meta": meta,
        "cols": ["date", "time", "merchant", "channel", "kind", "amount", "sign", "card"],
        "txs": [list(r) for r in txs],
        "repays": [list(r) for r in repays],
        # [月, 到期日, 应还, 最低, 本期消费, 利息, 对账差额]
        "bills": [[r["bill_month"], r["due_date"], r["due_amount"], r["min_amount"],
                   r["debits"], r["interest"], rec.get(r["bill_month"])] for r in bill_rows],
        # [日期, 可用额度, 已用额度, 额度上限, 临时额度月(0/1)]
        "credit": [[u["dt"], u["avail"], u["used"], u["limit"],
                    1 if u.get("temp_limit") else 0] for u in usage],
    }


def _split_sql(sql: str) -> list[str]:
    """按真实语句边界切分 SQL，返回去掉首尾空白的非空语句列表。

    引号（' " ` [）内的分号、--、/* 都是字面量，不参与切分；
    注释只在引号外识别。 doubled 引号（'' "" `` ]]）是转义，不结束引号。
    用文本正则去注释再找 ';' 会把 `SELECT 'a;b'` 这类合法查询误判成多语句。
    """
    stmts, buf = [], []
    i, n, quote = 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # 转义引号
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"`[":
            quote = "]" if ch == "[" else ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            buf.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            buf.append(" ")
            continue
        if ch == ";":
            stmts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stmts.append("".join(buf))
    return [s.strip() for s in stmts if s.strip()]


def run_sql(conn: sqlite3.Connection, sql: str, limit: int = 200) -> dict:
    """执行只读查询（仅 SELECT / WITH），返回列名与行。

    只读由 SQLite authorizer 在引擎层强制：只放行 SELECT / READ / FUNCTION /
    RECURSIVE 动作，INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA 一律拒绝。
    不做关键字黑名单——`INSERT\\nINTO` 这类换行变体能绕过文本匹配，
    而 `SELECT 'update t'` 这种合法字符串又会被误杀。
    语句边界用 _split_sql 识别，字符串里的 ';' 不会误判成多语句。
    """
    stmts = _split_sql(sql)
    if not stmts:
        raise ValueError("SQL 为空")
    if len(stmts) > 1:
        raise ValueError("一次只能执行一条语句")
    head = stmts[0].split(None, 1)[0].lower()
    if head not in ("select", "with", "explain"):
        raise ValueError("只允许只读查询（SELECT / WITH）")

    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               getattr(sqlite3, "SQLITE_RECURSIVE", 33)}  # WITH RECURSIVE

    def _guard(action, *_args):
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(_guard)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit)
    except sqlite3.DatabaseError as exc:
        if "not authorized" in str(exc).lower():
            raise ValueError("只允许只读查询，语句包含写操作或受限动作") from exc
        raise
    finally:
        # 部分 CPython 版本（如 3.10）set_authorizer(None) 不会真正清除回调，
        # 还原成「全部放行」等效于无 authorizer，且各版本行为一致
        conn.set_authorizer(lambda *_a: sqlite3.SQLITE_OK)
    return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows],
            "row_count": len(rows), "truncated": len(rows) == limit}


def _jsonable(v):
    if isinstance(v, bytes):
        return v.hex()
    return v


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_table(result: dict, max_rows: int = 30) -> None:
    cols, rows = result["columns"], result["rows"]
    if not cols:
        print("（无结果集）")
        return
    widths = [len(c) for c in cols]
    for r in rows[:max_rows]:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))
    print(" | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("-+-".join("-" * w for w in widths))
    for r in rows[:max_rows]:
        print(" | ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
    if len(rows) > max_rows:
        print(f"... 共 {len(rows)} 行，已截断显示")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}

    if "--stats" in flags:
        db = args[0] if args else DEFAULT_DB
        conn = connect(db)
        print("== overview ==")
        for k, v in overview(conn).items():
            print(f"  {k:16} {v}")
        print("\n== 按年 ==")
        _print_table({"columns": ["year", "tx", "net"],
                      "rows": [(r["name"], r["tx_count"], r["net_amount"])
                               for r in aggregate(conn, "year")]})
        print("\n== 邮件类型 ==")
        _print_table({"columns": ["category", "count"],
                      "rows": [tuple(r) for r in conn.execute(
                          "SELECT category, COUNT(*) FROM mail GROUP BY category "
                          "ORDER BY 2 DESC")]})
        print("\n== 数据质量 ==")
        cov = coverage(conn)
        _print_table({"columns": ["metric", "value"], "rows": [
            ("交易缺日期", conn.execute("SELECT COUNT(*) FROM tx WHERE tx_date=''").fetchone()[0]),
            ("交易缺商户", conn.execute("SELECT COUNT(*) FROM tx WHERE merchant=''").fetchone()[0]),
            ("交易缺渠道", conn.execute("SELECT COUNT(*) FROM tx WHERE channel=''").fetchone()[0]),
            ("FTS 行数", conn.execute("SELECT COUNT(*) FROM tx_fts").fetchone()[0]),
            ("月份数", cov["months"]),
            ("月均中位数", cov["median_tx_per_month"]),
            ("无交易月份", len(cov["missing_months"])),
            ("低覆盖月", len(cov["low_months"])),
            ("账单期数", cov["bill_months"]),
            ("无账单月份", len(cov["bill_missing_months"])),
            ("解析失败邮件", cov["parse_failed"]),
        ]})
        if cov["low_months"]:
            print("\n  低覆盖月：" + "、".join(
                f"{i['month']}({i['tx']})" for i in cov["low_months"][:12]))
        print("\n== 账单期（最近 6 期）==")
        _print_table({"columns": ["期", "额度", "应还", "最低", "到期", "消费", "利息", "对账差"],
                      "rows": [(b["bill_month"], b["credit_limit"], b["due_amount"],
                                b["min_amount"], b["due_date"], b["debits"],
                                b["interest"], b.get("reconcile_diff"))
                               for b in bills(conn, limit=6)]})
        conn.close()
        return 0

    if "--sql" in flags:
        if len(args) < 2:
            print('用法：python db.py --sql <数据库> "SELECT ..."', file=sys.stderr)
            return 2
        conn = connect(args[0])
        _print_table(run_sql(conn, args[1]))
        conn.close()
        return 0

    mail_dir = args[0] if args else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "招商银行")
    db = args[1] if len(args) > 1 else DEFAULT_DB
    info = import_dir(mail_dir, db, rebuild="--rebuild" in flags,
                      quiet="--json" in flags)
    if "--json" in flags:
        print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
