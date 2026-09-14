# -*- coding: utf-8 -*-
"""发送邮件（Web 版：附件以内存字节传入，不再从本地路径读文件）。

与 IMAP 一样，微软账号必须用 OAuth2（XOAUTH2）而不是密码，
否则会被返回 5.7.57 / 535 5.7.139 之类的「客户端未认证」错误。
"""
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import oauth
from accounts import get_password
from mail_conn import is_microsoft


class SendError(Exception):
    """带可读原因的发送失败。"""


def _friendly(e, from_addr):
    msg = str(e)
    low = msg.lower()
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
    """attachments: [{"filename": str, "data": bytes}]"""
    msg = MIMEMultipart()
    msg["From"] = (formataddr((str(Header(from_name, "utf-8")), from_addr))
                   if from_name else from_addr)
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = Header(subject or "(无主题)", "utf-8")
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    for item in attachments or []:
        filename = item.get("filename") or "attachment"
        part = MIMEBase("application", "octet-stream")
        part.set_payload(item.get("data") or b"")
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", filename))
        msg.attach(part)

    recipients = [a.strip() for a in
                  ",".join([to_addr or "", cc or "", bcc or ""]).split(",") if a.strip()]
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
            auth_str = oauth.xoauth2_string(from_addr, oauth.access_token(from_addr))
            server.auth("XOAUTH2", lambda challenge=None: auth_str.encode("utf-8"),
                        initial_response_ok=True)
        else:
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
    # 返回原始报文：调用方可以顺手 APPEND 到服务端「已发送」
    return raw
