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
from email.header import decode_header

from db import clear_attachments, save_attachment, save_email
from mail_conn import quote_mailbox


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


def fetch_folder(account_id, conn, canonical, imap_name, limit=120):
    """在已登录的连接上拉取一个文件夹，返回成功入库的邮件数。

    canonical 是入库用的统一 key（INBOX/Sent/Drafts/Trash/Spam），
    imap_name 是服务器上的真实文件夹名。
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
        return 0

    uids = data[0].split()[-int(limit):]
    count = 0
    for uid in uids:
        try:
            # 必须用 BODY.PEEK[]：`RFC822` 等价于 `BODY[]`，按 IMAP 规范会
            # **顺带把服务端这封邮件标成已读**（用户会莫名发现邮件全变已读了）。
            typ, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[] FLAGS)")
            if typ != "OK" or not msg_data:
                continue
            raw, flags = None, ""
            for part in msg_data:
                if isinstance(part, tuple):
                    head = part[0].decode("utf-8", "ignore") if isinstance(part[0], bytes) else str(part[0])
                    m = _FLAGS_RE.search(head)
                    if m:
                        flags = m.group(1)
                    raw = part[1]
            if raw is None:
                continue

            msg = email.message_from_bytes(raw)
            info = parse_message(msg)
            seen = 1 if "\\seen" in flags.lower() else 0

            email_id = save_email(
                account_id, canonical, int(uid),
                info["from_addr"], info["to_addr"], info["subject"], info["date"],
                info["body_text"], info["body_html"], seen, info["has_attach"])

            if info["attachments"]:
                clear_attachments(email_id)
                for fname, ctype, payload in info["attachments"]:
                    save_attachment(email_id, fname, ctype, payload)
            count += 1
        except Exception:
            # 单封邮件解析失败不该拖垮整个文件夹的同步
            continue

    try:
        conn.select("INBOX", readonly=True)
    except Exception:
        pass
    return count
