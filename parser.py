# -*- coding: utf-8 -*-
"""邮件拉取与解析。

相对早前版本的三个关键修正：
1. 用 `UID SEARCH` / `UID FETCH`，而不是消息序号 —— 序号会随邮箱变化漂移，
   会把同一封邮件存成不同记录、也会让"已删除"的邮件错位；
2. 一起取 FLAGS，把服务端已读（\\Seen）状态入库，不再全部显示成未读；
3. 每个文件夹单独拉取（收件箱/已发送/草稿/已删除/垃圾），
   而不是只同步 INBOX。
"""
import email
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import format_datetime, parsedate_to_datetime

from db import (clear_attachments, emails_missing_date, existing_uids,
                save_attachment, save_email, set_email_time)
from mail_conn import quote_mailbox

# 只有这两个文件夹的新邮件值得提醒：别人发进来的。
# Sent / Drafts 是自己写的，Trash 是不要的。
NOTIFY_FOLDERS = ("INBOX", "Spam")


def _fmt_time(date_header):
    """把 Date 头转成通知里好读的本地时间。"""
    if not date_header:
        return ""
    try:
        dt = parsedate_to_datetime(date_header)
        if dt is None:
            return ""
        if dt.tzinfo is None:
            return dt.strftime("%Y-%m-%d %H:%M")
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(date_header)[:40]


def decode_mime(s):
    if not s:
        return ""
    parts = decode_header(s)
    r = ""
    for b, enc in parts:
        if isinstance(b, bytes):
            try:
                r += b.decode(enc if enc else "utf-8", errors="ignore")
            except LookupError:
                r += b.decode("utf-8", errors="ignore")
        else:
            r += str(b)
    return r


def _decode_payload(part):
    """按邮件自带的 charset 解码（QQ/163 常见 gb2312/gbk，硬用 utf-8 会乱码）。"""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="ignore")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="ignore")


def parse_message(msg):
    """解析出可入库的字段。"""
    subject = decode_mime(msg.get("Subject"))
    from_addr = decode_mime(msg.get("From"))
    to_addr = decode_mime(msg.get("To"))
    date = msg.get("Date")

    body_text = ""
    body_html = ""
    has_attach = 0
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd or (part.get_filename() and "inline" not in cd):
                filename = part.get_filename()
                payload = part.get_payload(decode=True)
                if filename and payload:
                    has_attach = 1
                    attachments.append((decode_mime(filename), ct, payload))
                continue
            if ct == "text/plain" and not body_text:
                body_text = _decode_payload(part)
            elif ct == "text/html" and not body_html:
                body_html = _decode_payload(part)
    else:
        # 单段邮件：HTML 的必须存进 body_html，否则详情页会把源码当正文显示
        content = _decode_payload(msg)
        if msg.get_content_type() == "text/html":
            body_html = content
        else:
            body_text = content

    return {
        "subject": subject, "from_addr": from_addr, "to_addr": to_addr,
        "date": date, "body_text": body_text, "body_html": body_html,
        "has_attach": has_attach, "attachments": attachments,
    }


_FLAGS_RE = re.compile(r"FLAGS \(([^)]*)\)", re.IGNORECASE)

# IMAP 的 INTERNALDATE 固定长这样：15-Sep-2026 09:28:00 +0800
_INTERNALDATE_RE = re.compile(r'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
# `UID FETCH` 批量取时，服务器逐条回 `12 (INTERNALDATE "...")`
_UID_INTERNALDATE_RE = re.compile(rb'(\d+)\s+\(.*?INTERNALDATE\s+"([^"]+)"', re.IGNORECASE | re.DOTALL)


def internaldate_to_header(raw):
    """把 IMAP 的 INTERNALDATE（服务器收到信的时间）转成 RFC2822 日期串。

    用途是兜底：有些客户端发信不写 `Date` 头，那封邮件在收件方拉回来时
    `Date` 就是空的 —— 列表里不显示时间，排序也会被压到最底。
    服务器一定存了 INTERNALDATE，所以缺失时拿它顶上，总比空着强。
    """
    if not raw:
        return ""
    txt = raw.strip().strip('"')
    for fmt in ("%d-%b-%Y %H:%M:%S %z", "%d-%b-%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(txt, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            # 少数服务商不返回时区，按 UTC 处理（总比当成零时区乱算好）
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    return ""


def fetch_folder(account_id, conn, canonical, imap_name, limit=120):
    """在已登录的连接上拉取一个文件夹，返回 (入库邮件数, 新增未读列表)。

    canonical 是入库用的统一 key（INBOX/Sent/Drafts/Trash/Spam），
    imap_name 是服务器上的真实文件夹名。

    「新增」的判定方式：拉取前先取一次本地已有的 uid 集合，循环里不在集合内的
    就是这次新到的。两个坑要绕开：
      1. 不能拿 uid 大小当新旧 —— 有的服务商 uid 并不严格递增；
      2. 首次同步整批都是新的（本地空库），但那不是「刚收到」，所以整批不提醒，
         否则第一次添加账号就会甩出上百条通知。
    """
    typ, data = conn.select(quote_mailbox(imap_name), readonly=True)
    if typ != "OK":
        detail = data[0] if data and isinstance(data[0], bytes) else b""
        text = detail.decode(errors="ignore")[:120]
        if "unsafe login" in text.lower():
            # 网易/Coremail 系专属：登录后没上报客户端身份
            text += ("（该邮箱要求客户端登录后先上报 ID 身份；"
                     "若域名不常见，把它补进 mail_conn.py 的 _ID_SUFFIXES 即可）")
        raise RuntimeError(f"无法打开文件夹 {imap_name}：{text}")

    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        conn.select("INBOX", readonly=True)
        return 0, []

    uids = data[0].split()[-int(limit):]
    known = existing_uids(account_id, canonical)
    first_sync = not known
    new_items = []
    count = 0
    for uid in uids:
        try:
            # 必须用 BODY.PEEK[]：`RFC822` 等价于 `BODY[]`，按 IMAP 规范会
            # **顺带把服务端这封邮件标成已读**（用户会莫名发现邮件全变已读了）。
            typ, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[] FLAGS INTERNALDATE)")
            if typ != "OK" or not msg_data:
                continue
            raw, flags, internal = None, "", ""
            for part in msg_data:
                if isinstance(part, tuple):
                    head = part[0].decode("utf-8", "ignore") if isinstance(part[0], bytes) else str(part[0])
                    m = _FLAGS_RE.search(head)
                    if m:
                        flags = m.group(1)
                    m = _INTERNALDATE_RE.search(head)
                    if m:
                        internal = m.group(1)
                    raw = part[1]
            if raw is None:
                continue

            msg = email.message_from_bytes(raw)
            info = parse_message(msg)
            if not (info.get("date") or "").strip():
                # Date 头缺失（老版本自己发的信就是这样）→ 用服务器接收时间顶上
                info["date"] = internaldate_to_header(internal)
            seen = 1 if "\\seen" in flags.lower() else 0

            email_id = save_email(
                account_id, canonical, int(uid),
                info["from_addr"], info["to_addr"], info["subject"], info["date"],
                info["body_text"], info["body_html"], seen, info["has_attach"])

            if info["attachments"]:
                clear_attachments(email_id)
                for fname, ctype, payload in info["attachments"]:
                    save_attachment(email_id, fname, ctype, payload)

            if (canonical in NOTIFY_FOLDERS and not first_sync
                    and not seen and int(uid) not in known):
                new_items.append({
                    "subject": info["subject"],
                    "from": info["from_addr"],
                    "time": _fmt_time(info["date"]),
                    "folder": canonical,
                    "email_id": email_id,
                })
            count += 1
        except Exception:
            # 单封邮件解析失败不该拖垮整个文件夹的同步
            continue

    try:
        conn.select("INBOX", readonly=True)
    except Exception:
        pass
    return count, new_items


def repair_missing_dates(account_id, conn, canonical, imap_name, limit=300):
    """给「没有时间」的历史邮件补上服务器接收时间，返回补好的封数。

    为什么需要单独做一次：发信端在 2.0.5 之前漏写 `Date` 头，自己发给自己
    （或抄送自己）的邮件拉回本地后 `Date` 是空的，列表里既没有时间、排序也
    被挤到最底。这些邮件正文早就入库了，不可能靠重新拉一遍来修 —— 只能回头
    单独向服务器要一次 INTERNALDATE（IMAP 必定保存了这个），批量补。

    用 `UID FETCH n,m,k (INTERNALDATE)` 一次问多封，不取正文，代价很小；
    只跑一次（补好后 date 不再为空，下次就查不出来了）。
    """
    rows = emails_missing_date(account_id, canonical, limit)
    if not rows:
        return 0

    typ, _ = conn.select(quote_mailbox(imap_name), readonly=True)
    if typ != "OK":
        return 0

    uid_of = {str(r["uid"]): r["id"] for r in rows}
    found = {}
    # 分批问，单条命令别太长（部分服务商对命令行长度敏感）
    uids = list(uid_of.keys())
    for i in range(0, len(uids), 100):
        chunk = uids[i:i + 100]
        typ, data = conn.uid("fetch", ",".join(chunk), "(INTERNALDATE)")
        if typ != "OK" or not data:
            continue
        for part in data:
            blob = part if isinstance(part, bytes) else str(part).encode("utf-8", "ignore")
            m = _UID_INTERNALDATE_RE.search(blob)
            if not m:
                continue
            uid = m.group(1).decode("ascii", "ignore")
            header = internaldate_to_header(m.group(2).decode("utf-8", "ignore"))
            ts = None
            if header:
                try:
                    dt = parsedate_to_datetime(header)
                    ts = int(dt.timestamp()) if dt else None
                except Exception:
                    ts = None
            if uid in uid_of and (header or ts):
                found[uid_of[uid]] = (header, ts)

    for email_id, (header, ts) in found.items():
        set_email_time(email_id, header, ts)
    return len(found)
