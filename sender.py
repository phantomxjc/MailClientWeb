# -*- coding: utf-8 -*-
"""发送邮件（Web 版：附件以内存字节传入，不再从本地路径读文件）。

与 IMAP 一样，微软账号必须用 OAuth2（XOAUTH2）而不是密码，
否则会被返回 5.7.57 / 535 5.7.139 之类的「客户端未认证」错误。

关于中文地址（2.0.2 修）：
    SMTP 命令行只认 ASCII —— `RCPT TO:<...>` / `MAIL FROM:<...>` 都是
    `str.encode('ascii')`。所以「张三@qq.com」这种地址会在发信时报
    `'ascii' codec can't encode characters in position 9-10`
    （position 9-10 正是 "RCPT TO:<" 之后的头两个中文字）。
    中文**显示名**是另一回事：它只进邮件头（RFC2047 编码），不进命令行，
    所以「张三 <zhangsan@qq.com>」是可以正常发的。
    这里统一在发信前把收件人解析、校验成 ASCII 地址，把问题挡在发信之前。
"""
import re
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses

import oauth
from accounts import get_password
from mail_conn import is_microsoft

# 本地部分允许的字符（RFC 5322 atom + 点）
_LOCAL_OK = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+$")
# 「张三 a@b.com」——显示名和地址之间只有空格、没有尖括号
_NAME_ADDR = re.compile(r"^(.*\S)\s+([^\s<>()（）]+@[^\s<>()（）]+)$")


class SendError(Exception):
    """带可读原因的发送失败。"""


def _ascii_addr(addr, field="收件人"):
    """把邮箱地址规范成纯 ASCII。

    域名可以是中文（走 IDNA 转 punycode），但 @ 前面必须是英文字符 ——
    那不是编码问题，是对方真实地址就不该有中文。
    """
    addr = (addr or "").strip().strip("<>").strip()
    if not addr:
        return ""
    if "@" not in addr:
        if re.search(r"[＠﹫]", addr):
            raise SendError(f"{field}「{addr}」里用的是全角「＠」，请改成英文的 @。")
        raise SendError(f"{field}地址「{addr}」不完整，缺少 @ 符号。")
    if re.search(r"\s", addr):
        raise SendError(
            f"{field}「{addr}」里有空格，无法识别。写法请用：张三 <zhangsan@qq.com>")
    local, _, domain = addr.rpartition("@")
    if not local or not domain:
        raise SendError(f"{field}地址「{addr}」格式不对。")
    try:
        local.encode("ascii")
    except UnicodeEncodeError:
        raise SendError(
            f"{field}地址「{addr}」的 @ 前含中文字符，SMTP 协议不支持这种地址。"
            "请填对方真实的邮箱地址（英文/数字），中文名请写到显示名位置："
            "张三 <zhangsan@qq.com>。")
    if not _LOCAL_OK.match(local):
        raise SendError(f"{field}地址「{addr}」含不可用的字符（如全角符号），请检查。")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise SendError(f"{field}地址「{addr}」的域名无法识别，请检查。")
    return f"{local}@{domain}"


def _pairs(fields):
    """把原始输入拆成 (显示名, 地址) 流。

    getaddresses 只认「张三 <a@b.com>」这种带尖括号的写法，遇到
    「张三 a@b.com」（没有尖括号）会把整串当成地址。老版本能发这种写法
    （smtplib 内部又 parseaddr 了一次），所以这里补一刀，别让它退化成报错。
    """
    for name, addr in getaddresses(fields):
        name, addr = (name or "").strip().strip('"'), (addr or "").strip()
        if not addr:
            continue
        if not name:
            m = _NAME_ADDR.match(addr)
            if m:
                left, right = m.group(1).strip().strip('"\''), m.group(2)
                if "@" in left:
                    # 「a@b.com c@d.com」：空格分隔的多个地址，没有显示名
                    for p in re.split(r"\s+", left):
                        if "@" in p:
                            yield "", p
                    yield "", right
                    continue
                yield left, right
                continue
        yield name, addr


def parse_recipients(*fields):
    """把收件人/抄送/密送输入框解析成 [{'name': 显示名, 'addr': ASCII 地址}]。

    容忍这些写法：「a@b.com」「张三 <a@b.com>」「张三 a@b.com」「a@b.com, c@d.com」。
    显示名可以是中文（不影响发信），地址含非 ASCII 会抛出可读的 SendError。
    """
    text = [str(f) for f in fields if f and str(f).strip()]
    out, seen = [], set()
    for name, addr in _pairs(text):
        fixed = _ascii_addr(addr, "收件人")
        key = fixed.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "addr": fixed})
    return out


def _head(name, addr):
    """邮件头的显示名 + 地址。中文显示名走 RFC2047，不会污染 SMTP 命令行。"""
    if not name:
        return addr
    try:
        name.encode("ascii")
        return formataddr((name, addr))
    except UnicodeEncodeError:
        return formataddr((str(Header(name, "utf-8")), addr))


def _friendly(e, from_addr):
    msg = str(e)
    low = msg.lower()
    if isinstance(e, UnicodeEncodeError):
        # 兜底：万一还有漏网的非 ASCII，别把 Python 的原始报错丢给用户
        return ("地址里有 SMTP 不支持的非英文字符。请检查收件人是否写成了"
                "「张三@qq.com」这类中文地址；中文名请写成：张三 <zhangsan@qq.com>。")
    # 实测（2026-09，个人 outlook.com 账号）：服务端直接回
    # 535 5.7.139 SmtpClientAuthentication is disabled for the Mailbox
    if "smtpclientauthentication is disabled" in low or "5.7.139" in msg:
        return ("微软已在服务端关闭该邮箱的 SMTP 发信"
                "（535 5.7.139 SmtpClientAuthentication is disabled for the Mailbox）。"
                "按微软官方说明：个人 outlook.com / hotmail 账号一旦被关闭 SMTP AUTH，"
                "用户端没有任何开关能重新打开它（个人账号没有管理员控制台），"
                "工作/学校账号则需管理员在 Exchange 里开启。"
                "建议改用其它已配置的邮箱账号发信，或直接用网页版 Outlook 发送。")
    if "basic authentication is disabled" in low:
        return "微软已停用密码发信，该账号需要点「使用微软账号登录」完成 OAuth 授权。"
    if "535" in msg or ("authentication" in low and "fail" in low):
        return ("发信认证失败：请检查密码/授权码是否正确"
                "（QQ、163 等要用授权码，不是登录密码）。")
    if "recipient" in low and "refused" in low:
        return "收件人被服务器拒绝，请检查收件人地址是否正确。"
    if "sender" in low and "rejected" in low:
        return f"发件人被拒绝，请确认 {from_addr} 的邮箱设置允许通过客户端发信。"
    return f"发送失败：{msg[:200]}"


def send_email(smtp_server, smtp_port, from_addr, to_addr, subject, body,
               attachments=None, html=True, provider=None, from_name=None,
               cc=None, bcc=None):
    """attachments: [{"filename": str, "data": bytes}]

    返回 (raw 报文 bytes, recipients 列表) —— 调用方需要这两个：
    原始报文用于 APPEND 到服务端「已发送」，收件人列表用于记常用联系人。
    """
    # 先解析地址：有中文地址就在这里给出人话报错，别等 smtplib 抛 ascii 错
    to_list = parse_recipients(to_addr)
    cc_list = parse_recipients(cc)
    bcc_list = parse_recipients(bcc)
    if not to_list:
        raise SendError("请至少填写一个有效的收件人地址。")
    from_addr = _ascii_addr(from_addr, "发件账号")

    msg = MIMEMultipart()
    msg["From"] = (_head(from_name, from_addr) if from_name else from_addr)
    msg["To"] = ", ".join(_head(c["name"], c["addr"]) for c in to_list)
    if cc_list:
        msg["Cc"] = ", ".join(_head(c["name"], c["addr"]) for c in cc_list)
    # 密送不进邮件头（进了就等于告诉收件人你密送给了谁），只进收件人列表
    msg["Subject"] = Header(subject or "(无主题)", "utf-8")
    # 收件方 IMAP 拉取后靠 Date 头判断时间；不设的话收件箱里这封邮件没有时间，
    # 列表会按 ts=0 排到最后（见 db.py 的 ORDER BY COALESCE(e.ts,0) DESC）。
    # 本地用本地时区写入，与邮件正文时间一致。
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    for item in attachments or []:
        filename = item.get("filename") or "attachment"
        part = MIMEBase("application", "octet-stream")
        part.set_payload(item.get("data") or b"")
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", filename))
        msg.attach(part)

    all_rcpt = to_list + cc_list + bcc_list
    recipients = [c["addr"] for c in all_rcpt]
    raw = msg.as_bytes()

    try:
        if int(smtp_port) == 465:
            server = smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=30)
        else:
            server = smtplib.SMTP(smtp_server, int(smtp_port), timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
    except Exception as e:
        raise SendError(f"无法连接发信服务器 {smtp_server}:{smtp_port} —— {e}")

    try:
        ms_account = is_microsoft({"email": from_addr, "imap_server": smtp_server,
                                   "provider": provider or ""})
        if ms_account and oauth.has_tokens(from_addr):
            try:
                tok = oauth.access_token(from_addr)
            except Exception:
                raise SendError(
                    "该 Outlook 账号的微软授权已失效（刷新令牌失败），"
                    "请到「设置 → 账号」重新点『使用微软账号登录』完成授权后再发。")
            auth_str = oauth.xoauth2_string(from_addr, tok)

            # smtplib 要求 authobject 返回「ASCII 字符串」，它内部还要再
            # encode('ascii')/base64 一次。老代码这里返回了 bytes，
            # 结果微软账号发信必定抛 AttributeError: 'bytes' object has no attribute 'encode'。
            # 另外：服务器回 334 挑战说明认证失败，按协议应回空串，
            # 让服务器把真实错误码（如 invalid_grant）报出来。
            def _xoauth2(challenge=None):
                return auth_str if challenge is None else ""

            server.auth("XOAUTH2", _xoauth2, initial_response_ok=True)
        else:
            if ms_account and not oauth.has_tokens(from_addr):
                raise SendError(
                    "该 Outlook 账号还没完成微软授权，微软已停用密码发信。"
                    "请到「设置 → 账号」点『使用微软账号登录』完成 OAuth2 授权后再发。")
            password = get_password(from_addr)
            if not password:
                raise SendError("本地没有找到该账号的密码/授权码，请重新添加账号")
            server.login(from_addr, password)

        server.sendmail(from_addr, recipients, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(_friendly(e, from_addr))
    except SendError:
        raise
    except Exception as e:
        raise SendError(_friendly(e, from_addr))
    finally:
        try:
            server.quit()
        except Exception:
            pass
    # 返回原始报文 + 收件人：调用方可以把报文 APPEND 到服务端「已发送」，
    # 并把收件人记进常用联系人
    return raw, all_rcpt
