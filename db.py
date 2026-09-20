# -*- coding: utf-8 -*-
"""SQLite 存储层。

沿用桌面版已经踩过坑的结构，另加一张 credentials 表存放加密后的
密码/授权码（Docker 里没有 Windows 凭据管理器，keyring 不可用）。
"""
import sqlite3
import threading
from email.utils import parsedate_to_datetime

from config import DB_PATH, SYNC_INTERVAL_MINUTES

_local = threading.local()


def get_conn():
    """每个线程一个连接（Flask 多线程 + 后台同步线程并存）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            provider TEXT,
            imap_server TEXT,
            imap_port INTEGER,
            smtp_server TEXT,
            smtp_port INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            folder TEXT,
            uid INTEGER,
            msg_from TEXT,
            msg_to TEXT,
            subject TEXT,
            date TEXT,
            body_text TEXT,
            body_html TEXT,
            seen INTEGER DEFAULT 0,
            has_attachment INTEGER DEFAULT 0,
            ts INTEGER,
            UNIQUE(account_id, folder, uid)
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER,
            filename TEXT,
            content_type TEXT,
            data BLOB
        );

        CREATE TABLE IF NOT EXISTS folders (
            account_id INTEGER,
            key TEXT,
            imap_name TEXT,
            PRIMARY KEY (account_id, key)
        );

        CREATE TABLE IF NOT EXISTS credentials (
            email TEXT PRIMARY KEY,
            secret TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- 常用联系人：手动加的、从收到的邮件里存的、发信时自动记的，都放这张表。
        -- email 唯一（统一小写），同一个人的邮箱只会有一条记录。
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            note TEXT,
            source TEXT,
            use_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_default_pwd INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_emails_box ON emails(account_id, folder, ts);
        CREATE INDEX IF NOT EXISTS idx_attach_email ON attachments(email_id);
    """)
    _migrate(cur)
    conn.commit()
    _one_time_fixes(conn)
    _backfill_ts(conn)
    _drop_fake_ts(conn)


def _one_time_fixes(conn):
    """一次性数据修复（用 meta 表打标记，只跑一次）。

    1.2.0 之前版本把 IMAP「消息序号」当成 UID 入库，而序号会随邮箱变化漂移
    （实测 QQ：真实 UID 为 1-6、24、25，序号却是 1-8），结果同一封邮件被存成两条，
    已读状态也跟着乱。邮件本身就是服务器的本地缓存，所以直接清空重建，
    由下一次同步按真实 UID 重新拉取。
    """
    if conn.execute("SELECT value FROM meta WHERE key='uid_fix_1_2'").fetchone():
        return
    conn.execute("DELETE FROM attachments")
    conn.execute("DELETE FROM emails")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('uid_fix_1_2','done')")
    conn.commit()


def _migrate(cur):
    """老数据库升级：补 ts 列（用于按时间正确排序）。"""
    cols = {r[1] for r in cur.execute("PRAGMA table_info(emails)").fetchall()}
    if "ts" not in cols:
        cur.execute("ALTER TABLE emails ADD COLUMN ts INTEGER")


def _backfill_ts(conn):
    """把历史邮件的 RFC2822 日期解析成时间戳，否则列表排序会乱
    （字符串按星期几排，等于没排序）。"""
    rows = conn.execute(
        "SELECT id, date FROM emails WHERE (ts IS NULL OR ts = 0) AND date IS NOT NULL"
    ).fetchall()
    if not rows:
        return
    updates = []
    for row in rows:
        try:
            dt = parsedate_to_datetime(row["date"])
            if dt is not None:
                updates.append((int(dt.timestamp()), row["id"]))
        except Exception:
            continue
    if updates:
        conn.executemany("UPDATE emails SET ts=? WHERE id=?", updates)
        conn.commit()


def _drop_fake_ts(conn):
    """清掉「假时间」。

    2.0.5 为了不让没有时间的邮件沉底，把它们的 ts 统统刷成「进程启动那一刻」。
    副作用是这批邮件全排在同一时刻、还冒充最新被顶到列表最上面，比沉底更糟。
    现在改成清零：列表按 ts 空值排后面，同时交给 parser.repair_missing_dates
    去服务器取真实的 INTERNALDATE 填回来（那才是真正解决问题的路）。

    判据很干净：ts 只能从 date 推算，所以「date 为空却有 ts」必定是刷出来的。
    幂等，不需要一次性标记。
    """
    conn.execute(
        "UPDATE emails SET ts=NULL "
        "WHERE (date IS NULL OR TRIM(date)='') AND ts IS NOT NULL")
    conn.commit()


# ---------------------------------------------------------------- 凭据
def save_credential(email, secret):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO credentials (email, secret) VALUES (?,?)",
                 ((email or "").lower(), secret))
    conn.commit()


def get_credential(email):
    row = get_conn().execute("SELECT secret FROM credentials WHERE email=?",
                             ((email or "").lower(),)).fetchone()
    return row["secret"] if row else None


def delete_credential(email):
    conn = get_conn()
    conn.execute("DELETE FROM credentials WHERE email=?", ((email or "").lower(),))
    conn.commit()


# ---------------------------------------------------------------- 账号
def add_account(name, email, provider, imap_server, imap_port, smtp_server, smtp_port):
    """新增账号。

    注意不能用 `INSERT OR REPLACE`：email 上有 UNIQUE 约束，REPLACE 会先删掉
    旧行再插新行，account_id 变了 → 原有邮件全部变成孤儿（看起来像"邮件丢了"）。
    所以用 ON CONFLICT DO UPDATE，保住 id。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO accounts (name, email, provider, imap_server, imap_port,
                                 smtp_server, smtp_port)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET
               name=excluded.name,
               provider=excluded.provider,
               imap_server=excluded.imap_server,
               imap_port=excluded.imap_port,
               smtp_server=excluded.smtp_server,
               smtp_port=excluded.smtp_port""",
        (name, email, provider, imap_server, imap_port, smtp_server, smtp_port)
    )
    conn.commit()
    row = cur.execute("SELECT id FROM accounts WHERE email=?", (email,)).fetchone()
    return row[0] if row else cur.lastrowid


def update_account(account_id, name, email, provider, imap_server, imap_port,
                   smtp_server, smtp_port):
    conn = get_conn()
    conn.execute(
        """UPDATE accounts SET name=?, email=?, provider=?, imap_server=?,
           imap_port=?, smtp_server=?, smtp_port=? WHERE id=?""",
        (name, email, provider, imap_server, imap_port, smtp_server, smtp_port,
         account_id))
    conn.commit()


def get_accounts():
    rows = get_conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_account(account_id):
    row = get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def get_account_by_email(email):
    row = get_conn().execute("SELECT * FROM accounts WHERE lower(email)=lower(?)",
                             (email,)).fetchone()
    return dict(row) if row else None


def delete_account(account_id):
    conn = get_conn()
    conn.execute("DELETE FROM attachments WHERE email_id IN (SELECT id FROM emails WHERE account_id=?)", (account_id,))
    conn.execute("DELETE FROM emails WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()


# ---------------------------------------------------------------- 文件夹映射
def save_folders(account_id, mapping):
    """mapping: {"INBOX": "INBOX", "Sent": "Sent Messages", ...}"""
    if not mapping:
        return
    conn = get_conn()
    conn.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO folders (account_id, key, imap_name) VALUES (?,?,?)",
        [(account_id, k, v) for k, v in mapping.items()])
    conn.commit()


def get_folders(account_id):
    rows = get_conn().execute(
        "SELECT key, imap_name FROM folders WHERE account_id=?", (account_id,)).fetchall()
    return {r["key"]: r["imap_name"] for r in rows}


def folder_counts(account_id=None):
    """各分类的「总数 / 未读数」，供左侧文件夹角标使用。"""
    conn = get_conn()
    if account_id is None:
        rows = conn.execute(
            """SELECT folder, COUNT(*) AS total,
                      SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END) AS unread
               FROM emails GROUP BY folder""").fetchall()
    else:
        rows = conn.execute(
            """SELECT folder, COUNT(*) AS total,
                      SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END) AS unread
               FROM emails WHERE account_id=? GROUP BY folder""", (account_id,)).fetchall()
    out = {}
    for r in rows:
        out[r["folder"]] = {"total": r["total"] or 0, "unread": r["unread"] or 0}
    return out


# ---------------------------------------------------------------- 邮件
def save_email(account_id, folder, uid, msg_from, msg_to, subject, date,
               body_text, body_html, seen, has_attachment, ts=None):
    """插入或更新邮件。

    两个关键点：
    1. 重复同步不能把「已读」重置回未读 → 用 UPSERT 且 seen 取新旧最大值；
    2. 服务端已读的邮件首次入库就带 seen=1，不再全部显示成未读。
    """
    if ts is None and date:
        try:
            dt = parsedate_to_datetime(date)
            ts = int(dt.timestamp()) if dt else None
        except Exception:
            ts = None
    # 时间真取不到就留空（列表里按空值沉底），绝不伪造成「现在」——
    # 伪造会让这封邮件冒充最新被顶到列表最上面，比沉底更误导。
    # 正常路径走不到这里：parser 会用服务器的 INTERNALDATE 兜底。

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO emails
        (account_id, folder, uid, msg_from, msg_to, subject, date,
         body_text, body_html, seen, has_attachment, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, folder, uid) DO UPDATE SET
            msg_from=excluded.msg_from,
            msg_to=excluded.msg_to,
            subject=excluded.subject,
            date=excluded.date,
            body_text=excluded.body_text,
            body_html=excluded.body_html,
            has_attachment=excluded.has_attachment,
            ts=COALESCE(excluded.ts, emails.ts),
            seen=MAX(emails.seen, excluded.seen)""",
        (account_id, folder, uid, msg_from, msg_to, subject, date,
         body_text, body_html, seen, has_attachment, ts)
    )
    conn.commit()
    row = cur.execute(
        "SELECT id FROM emails WHERE account_id=? AND folder=? AND uid=?",
        (account_id, folder, uid)).fetchone()
    return row[0] if row else cur.lastrowid


def existing_uids(account_id, folder):
    """该账号该文件夹本地已有的 uid 集合。

    同步前取一次，就能判断哪些是这次真正「新到」的邮件 —— 比逐封 SELECT 省事，
    也比拿 uid 比大小可靠（uid 在有的服务商上并非严格递增）。
    """
    rows = get_conn().execute(
        "SELECT uid FROM emails WHERE account_id=? AND folder=?",
        (account_id, folder)).fetchall()
    return {int(r["uid"]) for r in rows if r["uid"] is not None}


def emails_missing_date(account_id, folder, limit=300):
    """找出「时间不可信」的邮件（date 为空，或因此 ts 也空/为 0）。

    这些就是老版本自己发出去的信：Date 头漏写 → 列表里没时间。拉出来交给
    parser.repair_missing_dates 回头找服务器要 INTERNALDATE。
    """
    rows = get_conn().execute(
        "SELECT id, uid FROM emails WHERE account_id=? AND folder=? "
        "AND (date IS NULL OR TRIM(date)='' OR ts IS NULL OR ts=0) "
        "ORDER BY id DESC LIMIT ?",
        (account_id, folder, int(limit))).fetchall()
    return [dict(r) for r in rows]


def set_email_time(email_id, date=None, ts=None):
    """补写某封邮件的时间（date 字符串与 ts 时间戳），供上面的修复流程调用。"""
    sets, params = [], []
    if date:
        sets.append("date=?")
        params.append(date)
    if ts:
        sets.append("ts=?")
        params.append(int(ts))
    if not sets:
        return False
    params.append(email_id)
    conn = get_conn()
    conn.execute(f"UPDATE emails SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return True


def get_meta(key, default=None):
    """meta 表的单值读取（存通知设置、一次性迁移标记这类零散数据）。"""
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
    conn.commit()


# ---------------------------------------------------------------- 界面设置
# 存 meta 表而不是配置文件：改完立刻生效，不用重启容器（列表排序要实时看得到变化）。
_UI_TRUE = ("1", "true", "on", "yes")
_UI_KEYS = ("unread_top", "unread_red", "sync_interval")


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in _UI_TRUE
    return bool(value)


def get_ui_settings():
    """界面设置：未读置顶 / 未读标红 / 自动同步间隔（分钟，0 = 关闭）。"""
    def flag(key, default):
        raw = get_meta("ui_" + key)
        return default if raw is None else str(raw).strip().lower() in _UI_TRUE

    raw = get_meta("ui_sync_interval")
    try:
        interval = int(str(raw)) if raw not in (None, "") else int(SYNC_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        interval = int(SYNC_INTERVAL_MINUTES)
    return {
        "unread_top": flag("unread_top", True),
        "unread_red": flag("unread_red", True),
        "sync_interval": max(0, min(interval, 1440)),
    }


def set_ui_settings(data):
    """写入界面设置（只认上面三个键），返回写完之后的值。"""
    if isinstance(data, dict):
        for key in _UI_KEYS:
            if key not in data:
                continue
            if key == "sync_interval":
                try:
                    minutes = int(data[key])
                except (TypeError, ValueError):
                    continue
                set_meta("ui_sync_interval", str(max(0, min(minutes, 1440))))
            else:
                set_meta("ui_" + key, "1" if _truthy(data[key]) else "0")
    return get_ui_settings()


def clear_attachments(email_id):
    """重新同步前清掉旧附件，避免同一封邮件反复同步后附件重复堆积。"""
    conn = get_conn()
    conn.execute("DELETE FROM attachments WHERE email_id=?", (email_id,))
    conn.commit()


def save_attachment(email_id, filename, content_type, data):
    conn = get_conn()
    conn.execute(
        "INSERT INTO attachments (email_id, filename, content_type, data) VALUES (?,?,?,?)",
        (email_id, filename, content_type, data))
    conn.commit()


def get_attachments(email_id):
    rows = get_conn().execute(
        "SELECT id, email_id, filename, content_type FROM attachments WHERE email_id=?",
        (email_id,)).fetchall()
    return [dict(r) for r in rows]


def get_attachment(attachment_id):
    row = get_conn().execute("SELECT * FROM attachments WHERE id=?",
                             (attachment_id,)).fetchone()
    return dict(row) if row else None


_ORDER = " ORDER BY e.seen ASC, COALESCE(e.ts, 0) DESC, e.id DESC"
# 排序三段：① 未读在前（`seen` 0 排 1 前面），主人要求未读一律顶到列表最上；
#            ② 同一组内按时间倒序，新的在上；
#            ③ 时间相同（或都为空）时按入库先后，后进的在上，保证顺序稳定。
# 可以在 设置 → 通用 里关掉「未读置顶」，关掉后走纯时间倒序（_ORDER_PLAIN）。
_ORDER_PLAIN = " ORDER BY COALESCE(e.ts, 0) DESC, e.id DESC"


def _order_clause():
    """按当前设置选排序方式。

    每次查一次 meta（单行主键 SELECT，代价可忽略）——这样改完设置刷新列表
    立刻见效，不需要重启进程或清缓存。
    """
    try:
        if str(get_meta("ui_unread_top", "1")).strip().lower() in ("0", "false", "off", "no"):
            return _ORDER_PLAIN
    except Exception:
        pass
    return _ORDER


def get_emails(account_id=None, folder=None, keyword="", limit=200, unread_only=False):
    """列表查询：支持账号、分类、关键字过滤（主题/发件人/收件人）。"""
    where, params = [], []
    if account_id:
        where.append("e.account_id=?")
        params.append(account_id)
    if folder:
        where.append("e.folder=?")
        params.append(folder)
    if unread_only:
        where.append("e.seen=0")
    if keyword:
        where.append("(e.subject LIKE ? OR e.msg_from LIKE ? OR e.msg_to LIKE ?)")
        like = f"%{keyword}%"
        params += [like, like, like]

    sql = ("SELECT e.id, e.account_id, e.folder, e.uid, e.msg_from, e.msg_to, "
           "e.subject, e.date, e.seen, e.has_attachment, e.ts, e.body_text, "
           "a.name AS account_name, a.email AS account_email "
           "FROM emails e JOIN accounts a ON e.account_id=a.id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += _order_clause() + " LIMIT ?"
    params.append(int(limit))
    rows = get_conn().execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        # 列表里只给一段纯文本摘要，避免把整封正文传到前端
        body = d.pop("body_text", "") or ""
        plain = " ".join(body.split())
        d["preview"] = plain[:140]
        out.append(d)
    return out


def get_email(email_id):
    row = get_conn().execute(
        "SELECT e.*, a.email AS account_email, a.name AS account_name "
        "FROM emails e JOIN accounts a ON e.account_id=a.id WHERE e.id=?",
        (email_id,)).fetchone()
    return dict(row) if row else None


def get_email_targets(ids):
    """批量取删除所需的 (id, account_id, folder, uid)，只认存在的行。

    用于「删除 / 批量删除」时既要删本地库、又要回服务器（按账号分组、移动/清除）。
    """
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = get_conn().execute(
        f"SELECT id, account_id, folder, uid FROM emails WHERE id IN ({placeholders})",
        tuple(int(x) for x in ids)).fetchall()
    return [dict(r) for r in rows]


def delete_emails(ids):
    """按 id 真删邮件及其附件（先删附件外键，再删邮件行）。"""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = get_conn()
    conn.execute(
        f"DELETE FROM attachments WHERE email_id IN ({placeholders})",
        tuple(int(x) for x in ids))
    conn.execute(
        f"DELETE FROM emails WHERE id IN ({placeholders})",
        tuple(int(x) for x in ids))
    conn.commit()
    return len(ids)


def get_unread_count(account_id=None, folder="INBOX"):
    conn = get_conn()
    if account_id is None:
        row = conn.execute("SELECT COUNT(*) FROM emails WHERE folder=? AND seen=0",
                           (folder,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE account_id=? AND folder=? AND seen=0",
            (account_id, folder)).fetchone()
    return row[0] if row else 0


def get_folder_total(account_id, folder):
    row = get_conn().execute(
        "SELECT COUNT(*) FROM emails WHERE account_id=? AND folder=?",
        (account_id, folder)).fetchone()
    return row[0] if row else 0


def mark_seen(email_id, seen=1):
    conn = get_conn()
    conn.execute("UPDATE emails SET seen=? WHERE id=?", (seen, email_id))
    conn.commit()


def mark_all_seen(account_id=None, folder=None):
    """把符合条件的**未读**邮件标为已读。

    返回被改动的 `[(account_id, folder, uid), ...]`，调用方拿它回推 IMAP 服务器 ——
    只改本地的话服务器上仍是未读，手机或网页版看到的还是未读，两边会一直对不上。

    `folder` 传 None 表示该账号下**所有**文件夹（含垃圾邮件）；
    传具体值则只动那一个文件夹。
    """
    conn = get_conn()
    where, params = ["seen=0"], []
    if account_id:
        where.append("account_id=?")
        params.append(account_id)
    if folder:
        where.append("folder=?")
        params.append(folder)
    clause = " WHERE " + " AND ".join(where)

    rows = conn.execute("SELECT account_id, folder, uid FROM emails" + clause,
                        params).fetchall()
    if rows:
        conn.execute("UPDATE emails SET seen=1" + clause, params)
        conn.commit()
    return [(r["account_id"], r["folder"], r["uid"]) for r in rows]


def get_stats():
    conn = get_conn()
    unread = conn.execute("SELECT COUNT(*) FROM emails WHERE seen=0").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    return {"unread": unread, "total": total}


# ---------------------------------------------------------------- 登录用户
def count_users():
    return get_conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def add_user(username, password_hash, is_default_pwd=0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, is_default_pwd) VALUES (?,?,?)",
        (username, password_hash, 1 if is_default_pwd else 0))
    conn.commit()
    return cur.lastrowid


def get_user(username):
    row = get_conn().execute("SELECT * FROM users WHERE lower(username)=lower(?)",
                             ((username or "").strip(),)).fetchone()
    return dict(row) if row else None


def set_user_password(username, password_hash, is_default_pwd=0):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_hash=?, is_default_pwd=?, updated_at=CURRENT_TIMESTAMP "
        "WHERE lower(username)=lower(?)",
        (password_hash, 1 if is_default_pwd else 0, (username or "").strip()))
    conn.commit()


def touch_login(username):
    conn = get_conn()
    conn.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE lower(username)=lower(?)",
                 ((username or "").strip(),))
    conn.commit()


# ---------------------------------------------------------------- 常用联系人
# 三类来源共用一个池子：
#   manual —— 用户手动添加/编辑的（名字以用户为准）
#   mail   —— 在收到的邮件里点「存为联系人」
#   send   —— 发信成功后自动记一笔（use_count+1，用于「常用」排序）
_CONTACT_COLS = ("id", "email", "name", "note", "source", "use_count", "last_used")


def _contact_row(row):
    return {k: row[k] for k in _CONTACT_COLS} if row else None


def norm_email(email):
    """统一小写去空格消毒 —— 大小写不同的同一个地址不该存成两条。"""
    return (email or "").strip().strip("<>").strip().lower()


def upsert_contact(email, name="", note=None, source="manual", touch=False):
    """存/更新一个联系人，返回该行。

    名字策略：手动存的一律以用户填的为准；自动收集（发信/收信）只在原来
    没有名字时补上，免得把用户手改过的名字覆盖掉。
    """
    email = norm_email(email)
    if not email:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    name = (name or "").strip()
    if row is None:
        conn.execute(
            "INSERT INTO contacts (email, name, note, source, use_count, last_used) "
            "VALUES (?,?,?,?,?, CASE WHEN ?=1 THEN CURRENT_TIMESTAMP END)",
            (email, name, note, source, 1 if touch else 0, 1 if touch else 0))
    else:
        sets, params = [], []
        if name and (source == "manual" or not (row["name"] or "").strip()):
            sets.append("name=?")
            params.append(name)
        if note is not None:
            sets.append("note=?")
            params.append(note)
        if source == "manual" and row["source"] != "manual":
            sets.append("source='manual'")
        if touch:
            sets.append("use_count=use_count+1")
            sets.append("last_used=CURRENT_TIMESTAMP")
        if sets:
            params.append(email)
            conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE email=?", params)
    conn.commit()
    return _contact_row(conn.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone())


def touch_contacts(items):
    """发信成功后记一笔：收件人出现次数 +1，用于「常用」排序。"""
    n = 0
    for it in items or []:
        if isinstance(it, dict):
            mail, nm = norm_email(it.get("addr") or it.get("email")), it.get("name") or ""
        else:
            mail, nm = norm_email(it), ""
        if not mail:
            continue
        upsert_contact(mail, nm, source="send", touch=True)
        n += 1
    return n


def get_contacts(keyword="", limit=500):
    """联系人列表：常用的排前面（使用次数 → 最近使用 → 有名字的优先）。"""
    kw = (keyword or "").strip()
    sql = "SELECT * FROM contacts"
    params = []
    if kw:
        sql += " WHERE email LIKE ? OR name LIKE ? OR note LIKE ?"
        like = f"%{kw}%"
        params += [like, like, like]
    sql += (" ORDER BY use_count DESC, last_used DESC,"
            " CASE WHEN COALESCE(name,'')='' THEN 1 ELSE 0 END, name COLLATE NOCASE")
    rows = get_conn().execute(sql + " LIMIT ?", params + [limit]).fetchall()
    return [_contact_row(r) for r in rows]


def get_contact(contact_id):
    return _contact_row(
        get_conn().execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone())


def update_contact(contact_id, name=None, note=None, email=None):
    """改名字/备注/邮箱。改了邮箱后如果和已有的撞了，直接报错不合并。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        return None
    sets, params = [], []
    if email is not None:
        mail = norm_email(email)
        if mail and mail != row["email"]:
            dup = conn.execute("SELECT id FROM contacts WHERE email=?", (mail,)).fetchone()
            if dup:
                raise ValueError("这个邮箱已经在常用联系人里了")
            sets.append("email=?")
            params.append(mail)
    if name is not None:
        sets.append("name=?")
        params.append((name or "").strip())
    if note is not None:
        sets.append("note=?")
        params.append(note)
    if sets:
        sets.append("source='manual'")
        params.append(contact_id)
        conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
    return get_contact(contact_id)


def delete_contact(contact_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    conn.commit()
    return cur.rowcount
