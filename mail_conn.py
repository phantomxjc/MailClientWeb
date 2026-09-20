# -*- coding: utf-8 -*-
r"""IMAP 连接、鉴权与文件夹发现。

三件事：
1. 统一建连：Outlook 走 XOAUTH2，其余服务商走密码/授权码；
2. 文件夹名带空格时必须加引号（QQ 邮箱实测：`SELECT "Sent Messages"` 才行，
   不加引号直接 `BAD EXAMINE parameters!`）；
3. 文件夹自动发现：不同服务商名字完全不同（QQ 用 Sent Messages、
   Outlook 用 Sent Items、163 用「已发送」），靠 \Sent/\Drafts/\Trash/\Junk
   特殊标记 + 名称启发式映射到统一的五个分类。
"""
import base64
import imaplib
import re

import oauth
from accounts import get_password
from config import IMAP_TIMEOUT
import db

# 统一的五个分类（界面与数据库都用这套 key）
CANON_KEYS = ["INBOX", "Sent", "Drafts", "Trash", "Spam"]
CANON_LABEL = {"INBOX": "收件箱", "Sent": "已发送", "Drafts": "草稿箱",
               "Trash": "已删除", "Spam": "垃圾邮件"}

_FLAG_MAP = {
    "\\sent": "Sent",
    "\\drafts": "Drafts",
    "\\trash": "Trash",
    "\\junk": "Spam",
}

_NAME_HINTS = {
    "Sent": ("sent", "sent items", "sent messages", "sent mail",
             "已发送", "已发送邮件", "发件箱", "寄件备份"),
    "Drafts": ("draft", "drafts", "草稿", "草稿箱"),
    "Trash": ("trash", "deleted", "deleted items", "deleted messages",
              "bin", "已删除", "已删除邮件", "废件箱"),
    "Spam": ("junk", "spam", "junk email", "junk e-mail",
             "垃圾", "垃圾邮件", "广告邮件"),
}

_MS_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com",
               "passport.com", "outlook.jp", "hotmail.co.uk")


# 微软官方相关页面（用于给用户一键跳转到正确位置）
OUTLOOK_IMAP_SETTINGS_URL = "https://outlook.live.com/mail/0/options/mail/forwardingImap"
SMTP_AUTH_DOC_URL = "https://aka.ms/smtp_auth_disabled"


class MailAuthError(Exception):
    """带可读原因的连接/鉴权错误（直接展示给用户）。

    kind 决定界面该给什么动作（而不是一律弹"重新授权"）：
      need_auth     —— 还没授权 / 令牌失效，真的需要重新做微软授权；
      imap_blocked  —— 登录被接受但微软拒绝打开邮箱（账号侧 IMAP 未开或被限流）；
      smtp_disabled —— 微软在服务端关掉了该邮箱的 SMTP 发信；
      password      —— 凭据问题（例如该用授权码）；
      network       —— 连不上服务器；
      generic       —— 其它。

    action 为 (按钮文字, 目标)：目标是 http(s) 链接就打开浏览器，
    是 "reauth" / "retry" 则由界面自行处理。
    """

    def __init__(self, message, kind="generic", action=None):
        super().__init__(message)
        self.kind = kind
        self.action = action


# ---------------------------------------------------------------- 服务商判断
def is_microsoft(acc):
    email = (acc.get("email") or "").lower()
    provider = (acc.get("provider") or "").lower()
    server = (acc.get("imap_server") or "").lower()
    if provider == "outlook":
        return True
    if "office365" in server or "outlook" in server:
        return True
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return domain in _MS_DOMAINS


# ---------------------------------------------------------------- 文件夹名处理
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-&]+$")


def quote_mailbox(name):
    """按 IMAP 规则给邮箱名加引号（含空格、中文、括号等必须加）。"""
    name = name or "INBOX"
    if _SAFE_NAME.match(name):
        return name
    return '"' + name.replace('"', '') + '"'


def decode_mutf7(name):
    """解 IMAP modified UTF-7，让中文文件夹名能显示与匹配（如 &XfJT0ZAB- → 已发送）。"""
    if "&" not in name:
        return name
    out, i = [], 0
    while i < len(name):
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        j = name.find("-", i)
        if j == -1:
            out.append(ch)
            break
        chunk = name[i + 1:j]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(chunk)
        i = j + 1
    return "".join(out)


_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


def raw_list(conn):
    r"""返回 [(原始名, flags字符串, 显示名)]，跳过 \Noselect 的纯容器目录。"""
    try:
        typ, data = conn.list()
    except Exception:
        return []
    if typ != "OK":
        return []
    out = []
    for raw in data or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line.strip())
        if not m:
            continue
        flags = m.group("flags")
        if "noselect" in flags.lower():
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1]
        out.append((name, flags, decode_mutf7(name)))
    return out


def resolve_folders(conn):
    """把服务器上的真实文件夹映射成 {INBOX/Sent/Drafts/Trash/Spam: 真实名}。"""
    found = {}
    for name, flags, display in raw_list(conn):
        flags_low = flags.lower()
        canon = None
        for flag, key in _FLAG_MAP.items():
            if flag in flags_low:
                canon = key
                break
        if canon is None and name.upper() == "INBOX":
            canon = "INBOX"
        if canon is None:
            low = display.lower()
            best = None
            for key, hints in _NAME_HINTS.items():
                for idx, hint in enumerate(hints):
                    if hint in low:
                        score = idx
                        if best is None or score < best[0]:
                            best = (score, key)
                        break
            if best:
                canon = best[1]
        if canon and canon not in found:
            found[canon] = name
    return found


# ---------------------------------------------------------------- 连接与鉴权
def _friendly_error(exc, acc):
    """把底层异常翻成「用户能照做」的中文，并给出 (原因, kind, 建议动作)。"""
    msg = str(exc)
    low = msg.lower()
    provider = (acc.get("provider") or "").lower()
    host = acc.get("imap_server") or ""
    email = acc.get("email") or ""

    # 微软 IMAP：登录被接受、但拒绝打开邮箱。
    # 实测（2026-09，用真实令牌跑过 common/consumers 两种端点、三个官方主机）：
    #   三个主机 + 两种端点全部返回同一句，说明不是端点/权限范围写错。
    if "authenticated but not connected" in low:
        return (
            f"{email} 登录已被微软接受，但微软拒绝打开邮箱"
            f"（User is authenticated but not connected.）。"
            f"这不是密码/授权问题，常见就两个原因："
            f"① 邮箱设置里没开 IMAP（微软新注册账号默认关闭）；"
            f"② 微软对同一邮箱的并发连接做了临时限流。"
            f"请先到网页版邮箱「设置 → 邮件 → 转发和 IMAP」打开"
            f"「允许设备和应用使用 IMAP」并保存，再点『立即重试』；"
            f"若是限流，一般 2–6 小时自动解除，期间请少开其它邮件客户端。",
            "imap_blocked", ("打开邮箱设置", OUTLOOK_IMAP_SETTINGS_URL))

    # SMTP AUTH 被服务端关闭（常见于 2026 年新注册的个人微软账号）
    if "smtpclientauthentication is disabled" in low or "5.7.139" in low:
        return (
            "微软已在服务端关闭该邮箱的 SMTP 发信"
            "（535 5.7.139 SmtpClientAuthentication is disabled for the Mailbox）。"
            "按微软官方说明：个人 outlook.com / hotmail 账号一旦被关闭 SMTP AUTH，"
            "用户端没有任何开关能重新打开（个人账号没有管理员控制台）。"
            "建议改用其它已配置的邮箱账号发信，或直接用网页版 Outlook 发送。",
            "smtp_disabled", ("查看微软说明", SMTP_AUTH_DOC_URL))

    if "basic authentication is disabled" in low:
        return ("微软已停用密码登录（基础认证），这个账号必须走 OAuth2 授权："
                "请点『重新授权』完成一次微软账号登录。",
                "need_auth", ("重新授权", "reauth"))
    if "authorization code" in low or "授权码" in msg:
        return ("该邮箱要求使用「授权码」而不是登录密码，"
                "请到邮箱网页版设置里生成授权码。", "password", None)
    if "imap access is disabled" in low or "imap is disabled" in low:
        return ("该邮箱没有开启 IMAP 服务，请登录网页版邮箱设置里开启 IMAP 后再试。",
                "password", None)
    if ("authenticationfailed" in low or "login failed" in low or "login fail" in low
            or "invalid credentials" in low
            or ("auth" in low and "fail" in low)):
        if "qq.com" in email or "qq.com" in host:
            return ("登录被拒绝：QQ 邮箱要填「授权码」"
                    "（设置→账户→开启 IMAP/SMTP 后生成），不是 QQ 密码。",
                    "password", None)
        if "163.com" in email or "126.com" in email:
            return "登录被拒绝：网易邮箱要填「授权码」，不是登录密码。", "password", None
        return ("登录被拒绝：账号或密码/授权码不正确，"
                f"也可能是服务商要求使用应用专用密码。（{msg[:120]}）",
                "password", None)
    if "unknown user" in low or "user unknown" in low or "no such user" in low:
        return f"账号不存在或被禁用：{email}", "password", None
    if "too many" in low or "rate" in low or "temporarily" in low:
        return "登录尝试过于频繁，请稍等几分钟再同步。", "imap_blocked", None
    if provider == "outlook" or is_microsoft(acc):
        return f"连接 Outlook 失败：{msg[:200]}", "generic", None
    return f"连接失败：{msg[:200]}", "generic", None


# ---------------------------------------------------------------- 客户端身份上报
# 网易/Coremail 系邮箱（163 / 126 / yeah / 139 / 189 / 阿里云 …）有一条硬性限制：
# 登录成功只是第一步，客户端还必须先发一条 RFC 2971 的 ID 命令自报身份，
# 否则后续任何 SELECT / EXAMINE 都会被拒，报：
#     EXAMINE Unsafe Login. Please contact kefu@188.com for help
# 现象就是「账号能加、头像能出、就是永远收不到一封邮件」。
# Python 标准库 imaplib 的命令表里没有 ID，直接调用会 KeyError，所以这里手工发一条。
_ID_SUFFIXES = ("163.com", "126.com", "yeah.net", "188.com", "139.com", "189.cn",
                "sina.com", "sina.cn", "sohu.com", "tom.com", "21cn.com",
                "aliyun.com", "coremail.cn")


def needs_client_id(email):
    dom = (email or "").rsplit("@", 1)[-1].lower()
    return dom in _ID_SUFFIXES


def send_client_id(conn, email):
    """给服务器报一次客户端身份。失败不抛异常——服务商不支持 ID 时回 BAD，忽略即可。"""
    from config import APP_VERSION
    tag = conn._new_tag()
    payload = ('("name" "Stardust Mail" "version" "%s" "vendor" "星尘邮箱" '
               '"support-email" "%s")' % (APP_VERSION, email))
    try:
        conn.send(tag + b" ID " + payload.encode("utf-8") + b"\r\n")
        # 自己把响应读到 tag 那一行为止（可能先来一行未标记的 * ID (...)）
        while True:
            line = conn.readline()
            if not line or line.startswith(tag):
                break
        return True
    except Exception:
        return False


def connect(acc, timeout=None):
    """建立并返回一个已登录的 IMAP4_SSL 连接；失败抛 MailAuthError。

    timeout 会设置到 socket 上。**必须设**：微软限流时（User is authenticated
    but not connected.）服务器可能既不回应也不断开，没有超时的话 socket 会一直
    挂着，同步线程吊死在这一步 —— 表现就是「一个邮箱有问题，排在它后面的邮箱
    全都不同步了」。有了超时，最坏只是这个账号自己失败。
    """
    host = acc.get("imap_server")
    port = int(acc.get("imap_port") or 993)
    if not host:
        raise MailAuthError("该账号没有配置 IMAP 服务器地址", "generic", None)

    if timeout is None:
        timeout = IMAP_TIMEOUT
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        try:
            conn.sock.settimeout(timeout)      # 双保险：有的版本构造参数不落到 socket 上
        except Exception:
            pass
    except Exception as e:
        raise MailAuthError(f"无法连接 {host}:{port} —— {e}", "network", None)

    email = acc.get("email") or ""
    try:
        if is_microsoft(acc):
            try:
                token = oauth.access_token(email)
            except oauth.OAuthError as e:
                raise MailAuthError(str(e), "need_auth", ("重新授权", "reauth"))
            auth_str = oauth.xoauth2_string(email, token)
            conn.authenticate("XOAUTH2", lambda _challenge=None: auth_str.encode("utf-8"))
        else:
            password = get_password(email)
            if not password:
                raise MailAuthError("本地没有找到该账号的密码/授权码，请重新添加账号",
                                    "password", None)
            conn.login(email, password)

        # 网易/Coremail 系：登录后必须立刻自报身份，否则读信一律被拒
        if not is_microsoft(acc) and needs_client_id(email):
            send_client_id(conn, email)
    except MailAuthError:
        _safe_logout(conn)
        raise
    except Exception as e:
        _safe_logout(conn)
        msg, kind, action = _friendly_error(e, acc)
        raise MailAuthError(msg, kind, action)
    return conn


def _safe_logout(conn):
    try:
        conn.logout()
    except Exception:
        pass


# ---------------------------------------------------------------- 删除邮件
def delete_messages(acc, targets):
    """把一批邮件从服务器删掉。targets = [(folder_key, uid), ...]，都属同一账号。

    语义贴近常见邮箱客户端：
      · 邮件当前不在「已删除 / 垃圾邮件」里 → **移进「已删除」**（IMAP MOVE），
        服务器里它还留着，下次同步会作为「已删除」文件夹的邮件重新出现；
      · 已经在「已删除 / 垃圾邮件」里 → **彻底删除**（标 \\Deleted 再 EXPUNGE）。

    返回 `(failed, msg)`：failed 是没删成的 (folder_key, uid) 列表（连接层面失败
    直接抛 MailAuthError，不进这里）。单封失败只记进 failed，不连累其它封。
    """
    if not targets:
        return [], ""
    folders = db.get_folders(acc["id"])
    trash = folders.get("Trash")
    conn = connect(acc)
    failed = []
    try:
        for folder_key, uid in targets:
            src = folders.get(folder_key) or folder_key
            try:
                conn.select(quote_mailbox(src), readonly=False)
                if trash and folder_key not in ("Trash", "Spam"):
                    typ, _ = conn.uid("move", str(uid), quote_mailbox(trash))
                    if typ != "OK":
                        # MOVE 不被支持时的兜底：复制过去 + 源里标删 + 压缩
                        conn.uid("copy", str(uid), quote_mailbox(trash))
                        conn.uid("store", str(uid), "+FLAGS.SILENT", "\\Deleted")
                        conn.expunge()
                else:
                    conn.uid("store", str(uid), "+FLAGS.SILENT", "\\Deleted")
                    conn.expunge()
            except Exception:
                failed.append((folder_key, uid))
    finally:
        _safe_logout(conn)
    return failed, ""


def test_connection(acc):
    """只做一次连接鉴权与文件夹发现，用于添加账号后立即校验。"""
    conn = connect(acc)
    try:
        folders = resolve_folders(conn)
    finally:
        _safe_logout(conn)
    return folders
