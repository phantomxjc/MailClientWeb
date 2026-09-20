# -*- coding: utf-8 -*-
"""新邮件提醒：推送到「用户自己申请、自己填密钥」的推送服务。

首选是**微信公众号测试号**：扫码即得 appID/appsecret，免认证、不限条数、没有中间商，
收到的是微信原生「服务通知」。其余通道（Server 酱 / PushPlus / Bark / 群机器人 /
自定义 Webhook）都是「一个 Key 搞定」的简化路线，按需选。

为什么不走浏览器通知 —— Chrome / Edge 只在**安全上下文**（HTTPS 或 localhost）
下才给 Notification 权限。自托管用户绝大多数是 `http://192.168.x.x:8090` 访问，
`Notification.requestPermission()` 会被直接拒掉。走服务端推送则与访问方式无关：
邮件在 NAS 上，通知由 NAS 自己出网发到手机，两边不需要能互相访问。

所有通道本质都是「一个 HTTP 请求 + 一段 JSON/表单」，没有 SDK 依赖，
所以这里只用标准库 urllib —— 不为一条通知往镜像里塞第三方库。

安全边界：这些密钥是凭据，跟邮箱密码共用同一把 Fernet 密钥（credentials 那套），
加密后存 meta 表。对外接口只回传「已配置 / 未配置」，不回传明文；
标了 public 的字段（微信模板原文）例外，那不是秘密，回显反而省事。
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import accounts
import db

META_KEY = "notify_settings"
LAST_SENT_KEY = "notify_last_sent"
TIMEOUT = 12

# ---------------------------------------------------------------- 通道定义

# fields 既驱动后端取参，也驱动前端自动渲染表单 —— 加通道不用改 JS。
CHANNELS = {
    "wechat_mp": {
        "label": "微信公众号测试号（推荐）",
        "register": "https://mp.weixin.qq.com/debug/cgi-bin/sandbox",
        "note": "最省事的一条路：扫码登录就拿到 appID/appsecret，不用认证、不限条数，"
                "收到的是微信原生「服务通知」弹窗。顺序：①扫码登录 → ②用同一个微信扫"
                "页面上那张二维码关注（openID 就出现在下方用户列表里）→ ③「新增测试模板」，"
                "照下面「模板内容」的样子填，保存后拿到模板 ID。注意模板开头要先写一句"
                "固定文字（如「您有一封新邮件」），不能以 {{ 开头。",
        "fields": [
            {"key": "appid", "label": "appID", "placeholder": "wx1234567890abcdef"},
            {"key": "appsecret", "label": "appsecret", "placeholder": "测试号页面上那串长密钥"},
            {"key": "openid", "label": "接收者 openID", "placeholder": "oXXXXXXXXXXXXXXXXXXXX，多个用逗号隔开"},
            {"key": "template_id", "label": "模板 ID", "placeholder": "新增测试模板后拿到的那串 ID"},
            {"key": "tpl", "label": "模板内容（把测试号里那份原文粘进来）", "type": "textarea",
             "public": True,
             "placeholder": "您有一封新邮件　主题：{{subject.DATA}}　发件人：{{sender.DATA}}　时间：{{time.DATA}}",
             "hint": "在微信测试号后台「新增测试模板」里填，格式为 {{变量名.DATA}}，每行一个，"
                     "且**开头必须有一句固定文字**（如「您有一封新邮件」），不能以 {{ 开头。\n"
                     "变量名随便起，程序会自动识别：主题/标题→subject、发件人/来源→sender、"
                     "时间→time、账号/邮箱→account、数量→count、其余（内容/摘要/备注）→填进正文摘要。\n"
                     "直接复制这段就能用：\n"
                     "您有一封新邮件\n主题：{{subject.DATA}}\n发件人：{{sender.DATA}}\n时间：{{time.DATA}}"},
        ],
    },
    "serverchan": {
        "label": "Server 酱（方糖）",
        "register": "https://sct.ftqq.com",
        "note": "微信扫码登录即可拿 SendKey。免费版每天 5 条，逐封推送很快会用尽。",
        "fields": [
            {"key": "sendkey", "label": "SendKey", "placeholder": "SCT… 或 sctp…t…"},
        ],
    },
    "pushplus": {
        "label": "PushPlus（推送加）",
        "register": "https://www.pushplus.plus",
        "note": "需先实名认证才能调用接口。免费，微信服务号接收。",
        "fields": [
            {"key": "token", "label": "Token", "placeholder": "32 位 token"},
        ],
    },
    "bark": {
        "label": "Bark（iOS）",
        "register": "https://bark.day.app",
        "note": "仅 iOS。填 App 里那串 Key；自建服务器则直接填完整推送地址。",
        "fields": [
            {"key": "key", "label": "Key 或自建地址",
             "placeholder": "abcDEF123… 或 https://bark.你的域名.com"},
        ],
    },
    "wecom": {
        "label": "企业微信群机器人",
        "register": "https://developer.work.weixin.qq.com/document/path/91770",
        "note": "群设置 → 群机器人 → 添加，复制 Webhook 地址。",
        "fields": [
            {"key": "url", "label": "Webhook 地址",
             "placeholder": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…"},
        ],
    },
    "dingtalk": {
        "label": "钉钉群机器人",
        "register": "https://open.dingtalk.com/document/orgapp/custom-robot-access",
        "note": "安全设置请选「自定义关键词」，关键词填「新邮件」，否则会被拒。",
        "fields": [
            {"key": "url", "label": "Webhook 地址",
             "placeholder": "https://oapi.dingtalk.com/robot/send?access_token=…"},
        ],
    },
    "feishu": {
        "label": "飞书群机器人",
        "register": "https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot",
        "note": "群设置 → 群机器人 → 添加自定义机器人，复制 Webhook 地址。",
        "fields": [
            {"key": "url", "label": "Webhook 地址",
             "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/…"},
        ],
    },
    "custom": {
        "label": "自定义 Webhook",
        "register": "",
        "note": "任意 HTTP 接口。JSON / 表单模式会带 title、body、url 三个字段；"
                "纯文本模式把消息原文放进请求体。",
        "fields": [
            {"key": "url", "label": "接口地址", "placeholder": "https://example.com/hook"},
            {"key": "format", "label": "请求格式", "placeholder": "json",
             "options": ["json", "form", "text"], "default": "json"},
        ],
    },
}

# 成功码：各家不统一，取并集
_OK_CODES = {"0", "200"}

# 微信接口人话翻译：这些码看原文基本看不懂，直接告诉用户该去改哪儿
_WX_ERR = {
    "40001": "appsecret 不正确（或刚被重置过），请回测试号页面重新复制",
    "40013": "appID 不合法，请核对是不是复制多了空格",
    "40125": "appsecret 无效",
    "40164": "调用来源 IP 不在白名单里 —— 把这台机器的公网出口 IP 加进白名单，"
             "或者干脆换用测试号（测试号不需要配 IP）",
    "41001": "缺少 access_token，重新保存一次即可",
    "42001": "access_token 已过期，再点一次发送测试",
    "43004": "收件人还没关注这个测试号 —— 用同一个微信扫测试号页面上的二维码关注一下",
    "45009": "接口调用频率超限，等一会儿再试",
    "40037": "template_id 无效，请核对模板 ID",
    "47003": "模板参数对不上 —— 多半是「模板内容」没按实际模板填，"
             "把测试号里那份模板原文整段复制过来就好",
}
WX_TOKEN_KEY = "notify_wx_token"

DEFAULTS = {
    "enabled": False,
    "channel": "wechat_mp",
    "base_url": "",          # 拼「查看邮件」链接用，留空则通知里不带链接
    "merge": False,          # True = 一批合并成一条；False = 一封一条
    "min_interval": 0,       # 分钟；>0 时距上次推送不足这么久就跳过本次
    "quiet_start": "",       # 免打扰起（HH:MM），两个都填才生效
    "quiet_end": "",
    "notify_spam": False,    # 垃圾邮件要不要也提醒
}


# ---------------------------------------------------------------- HTTP

def _public_keys(ch):
    """通道里「不算密钥」的字段（如微信模板原文）——这些回显，密钥不回显。"""
    return {f["key"] for f in CHANNELS.get(ch, {}).get("fields", []) if f.get("public")}


def _post(url, payload=None, form=False, raw_text=None, timeout=TIMEOUT):
    headers = {"User-Agent": "StardustMail/2.2 (+https://github.com/phantomxjc/StardustMail)"}
    data = None
    if raw_text is not None:
        data = raw_text.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"
    elif form:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(800).decode("utf-8", "ignore")


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": "StardustMail/2.2 (+https://github.com/phantomxjc/StardustMail)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(2000).decode("utf-8", "ignore")


def _result_code(text):
    """抽出各家返回体里的状态码；抽不到返回 None（当成功处理）。"""
    try:
        d = json.loads(text)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    for k in ("errcode", "code", "StatusCode", "status"):
        if k in d:
            return str(d[k])
    return None


def _need(cconf, key, label):
    v = str(cconf.get(key) or "").strip()
    if not v:
        raise ValueError(f"未填写{label}")
    return v


# ---------------------------------------------------------------- 各通道发送

def _send_serverchan(cconf, title, body, link, fields=None):
    key = _need(cconf, "sendkey", "SendKey")
    if key.startswith("sctp"):
        m = re.match(r"sctp(\d+)t", key)
        if not m:
            raise ValueError("SendKey 以 sctp 开头但解析不出 uid，请核对")
        url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
    else:
        url = f"https://sctapi.ftqq.com/{key}.send"
    # title 长度上限 32，超了会被截
    return _post(url, {"title": title[:32], "desp": body}, form=True)


def _send_pushplus(cconf, title, body, link, fields=None):
    token = _need(cconf, "token", "Token")
    return _post("https://www.pushplus.plus/send", {
        "token": token, "title": title, "content": body, "template": "markdown"})


def _send_bark(cconf, title, body, link, fields=None):
    key = _need(cconf, "key", "Key")
    url = key.rstrip("/") if key.startswith("http") else f"https://api.day.app/{key}"
    payload = {"title": title, "body": body, "group": "星尘邮箱"}
    if link:
        payload["url"] = link
    return _post(url, payload)


def _send_wecom(cconf, title, body, link, fields=None):
    url = _need(cconf, "url", "Webhook 地址")
    text = f"**{title}**\n{body}"
    return _post(url, {"msgtype": "markdown", "markdown": {"content": text}})


def _send_dingtalk(cconf, title, body, link, fields=None):
    url = _need(cconf, "url", "Webhook 地址")
    return _post(url, {"msgtype": "markdown",
                       "markdown": {"title": title, "text": f"### {title}\n\n{body}"}})


def _send_feishu(cconf, title, body, link, fields=None):
    url = _need(cconf, "url", "Webhook 地址")
    # 用纯文本：飞书的 markdown 卡片对链接语法支持挑剔，正文里已含完整链接
    plain = body.replace("**", "")
    m = re.search(r"\[点此查看这封邮件\]\((\S+?)\)", body)
    if m:
        plain = plain.replace(f"[点此查看这封邮件]({m.group(1)})", m.group(1))
    return _post(url, {"msg_type": "text", "content": {"text": f"{title}\n{plain}"}})


# ---------------------------------------------------------------- 微信测试号
#
# 走的是老「模板消息」接口。正式服务号早就换成订阅通知了，但测试号一直保留着
# 模板消息，而且测试号不需要认证、不用配 IP 白名单，个人自托管场景刚好够用。
#
# 两个接口串起来：
#   取票   GET  /cgi-bin/token                     拿 access_token（7200 秒有效）
#   发信   POST /cgi-bin/message/template/send     带 touser + template_id + data
#
# access_token 有每日获取额度，所以缓存到 meta 表，快过期才去换。

# 模板变量模糊映射。用户自己建模板，变量名叫啥的都有，
# 所以按关键词猜 —— 顺序就是优先级：先认「发件人」，
# 否则 {{mailfrom.DATA}} 会被「mail」这条规则抢去当账号。
_WX_VAR_RULES = (
    (("sender", "from", "发件", "发送者", "来源"), "sender"),
    (("time", "date", "日期", "时间"), "time"),
    (("subject", "title", "主题", "标题"), "subject"),
    (("account", "email", "mail", "收件", "邮箱", "账号"), "account"),
    (("count", "num", "数量", "总"), "count"),
    (("remark", "note", "content", "body", "detail", "summary",
      "内容", "详情", "备注", "摘要"), "summary"),
)


def _wx_tpl_vars(tpl_text):
    """把模板原文里的 {{xxx.DATA}} 变量名抽出来（顺手认中文，微信虽然不要求）。"""
    return re.findall(r"\{\{\s*([A-Za-z0-9_\u4e00-\u9fa5]+)\s*\.DATA\s*\}\}", tpl_text or "")


def _wx_var_key(name):
    low = (name or "").lower()
    for words, target in _WX_VAR_RULES:
        if any(w in low for w in words):
            return target
    return "summary"        # 认不出来的一律填摘要，总比空着强


def _wx_trim(v, n):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v if len(v) <= n else v[: n - 1] + "…"


def _wx_access_token(appid, secret):
    cached = db.get_meta(WX_TOKEN_KEY)
    if cached:
        try:
            c = json.loads(cached)
            if c.get("appid") == appid and float(c.get("exp") or 0) > time.time() + 300:
                return c["token"]
        except Exception:
            pass
    q = urllib.parse.urlencode({"grant_type": "client_credential",
                                "appid": appid, "secret": secret})
    _, text = _get("https://api.weixin.qq.com/cgi-bin/token?" + q)
    try:
        d = json.loads(text)
    except Exception:
        raise ValueError(f"取 access_token 失败：{text[:180]}")
    if not d.get("access_token"):
        code = str(d.get("errcode", ""))
        raise ValueError(f"取 access_token 失败（{code}）："
                         f"{_WX_ERR.get(code) or d.get('errmsg')}")
    db.set_meta(WX_TOKEN_KEY, json.dumps(
        {"appid": appid, "token": d["access_token"],
         "exp": time.time() + int(d.get("expires_in") or 7200)}, ensure_ascii=False))
    return d["access_token"]


def _wx_post(token, payload):
    url = ("https://api.weixin.qq.com/cgi-bin/message/template/send"
           f"?access_token={token}")
    return _post(url, payload)


def _send_wechat_mp(cconf, title, body, link, fields=None):
    appid = _need(cconf, "appid", "appID")
    secret = _need(cconf, "appsecret", "appsecret")
    tid = _need(cconf, "template_id", "模板 ID")
    users = [u for u in re.split(r"[,;，；\s]+", _need(cconf, "openid", "openID")) if u]

    fields = fields or {}
    summary = fields.get("summary") or _wx_trim(body.replace("**", ""), 180)
    # 模板里有几个变量就构造几个：多传会被忽略，少传直接 47003
    names = _wx_tpl_vars(cconf.get("tpl")) or ["subject", "sender", "time"]
    pool = {
        "subject": _wx_trim(fields.get("subject") or title, 60),
        "sender": _wx_trim(fields.get("sender"), 40),
        "account": _wx_trim(fields.get("account"), 40),
        "time": _wx_trim(fields.get("time") or time.strftime("%Y-%m-%d %H:%M"), 30),
        "count": _wx_trim(fields.get("count"), 10),
        "summary": _wx_trim(summary, 180),
    }
    values = {k: pool[k] for k in {_wx_var_key(n) for n in names}}
    if not any(values.values()):
        values = {_wx_var_key(n): _wx_trim(summary, 180) for n in names}

    token = _wx_access_token(appid, secret)
    sent, bad = 0, ""
    for u in users:
        payload = {"touser": u, "template_id": tid,
                   "data": {n: {"value": values.get(_wx_var_key(n)) or "—"} for n in names}}
        if link:
            payload["url"] = link
        status, text = _wx_post(token, payload)
        d = {}
        try:
            d = json.loads(text) or {}
        except Exception:
            pass
        code = str(d.get("errcode", ""))
        if d.get("errcode") in (0, "0"):
            sent += 1
            continue
        if code in ("40001", "42001", "41001"):      # 票过期 / 被别处顶掉 → 重取一次
            db.set_meta(WX_TOKEN_KEY, "")
            token = _wx_access_token(appid, secret)
            status, text = _wx_post(token, payload)
            try:
                d = json.loads(text) or {}
            except Exception:
                d = {}
            if d.get("errcode") in (0, "0"):
                sent += 1
                continue
            code = str(d.get("errcode", ""))
        bad = f"{code} {_WX_ERR.get(code) or d.get('errmsg') or text[:120]}".strip()

    if sent == 0:
        raise ValueError(f"微信拒绝了这条推送：{bad}")
    if not bad:
        return status, f"errcode=0，已送达 {sent} 个接收者"
    return status, f"送达 {sent} 个，另有失败：{bad}"


def _send_custom(cconf, title, body, link, fields=None):
    url = _need(cconf, "url", "接口地址")
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")
    fmt = (cconf.get("format") or "json").strip().lower()
    if fmt == "form":
        return _post(url, {"title": title, "body": body, "url": link}, form=True)
    if fmt == "text":
        return _post(url, raw_text=f"{title}\n{body}")
    return _post(url, {"title": title, "body": body, "url": link})


_SENDERS = {
    "wechat_mp": _send_wechat_mp,
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "bark": _send_bark,
    "wecom": _send_wecom,
    "dingtalk": _send_dingtalk,
    "feishu": _send_feishu,
    "custom": _send_custom,
}


def _dispatch(channel, cconf, title, body, link, fields=None):
    """同步发送一条，返回 (ok, 说明文字)。"""
    fn = _SENDERS.get(channel)
    if not fn:
        return False, f"未知通道：{channel}"
    try:
        code, text = fn(cconf or {}, title, body, link, fields)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(400).decode("utf-8", "ignore")
        except Exception:
            pass
        return False, f"HTTP {e.code} · {detail or e.reason}"
    except urllib.error.URLError as e:
        return False, f"网络不可达：{e.reason}（NAS 需要能出网）"
    except Exception as e:
        return False, str(e)

    rc = _result_code(text)
    if rc is not None and rc not in _OK_CODES:
        return False, f"服务端返回 code={rc} · {text[:240]}"
    return True, f"HTTP {code} · {text[:240]}"


# ---------------------------------------------------------------- 消息组装

def _clean_item(it):
    subject = (it.get("subject") or "").strip() or "(无主题)"
    sender = (it.get("from") or "").strip() or "未知发件人"
    return subject, sender, (it.get("account") or "").strip(), (it.get("time") or "").strip()


def _link_of(base_url, email_id):
    if not base_url or not email_id:
        return ""
    return f"{base_url.rstrip('/')}/#mail={email_id}"


def format_one(it, base_url):
    subject, sender, account, when = _clean_item(it)
    title = f"新邮件 · {subject}"[:40]
    parts = [f"**主题**：{subject}", f"**发件人**：{sender}"]
    if account:
        parts.append(f"**收件账号**：{account}")
    if when:
        parts.append(f"**时间**：{when}")
    link = _link_of(base_url, it.get("email_id"))
    if link:
        parts += ["", f"[点此查看这封邮件]({link})"]
    fields = {
        "subject": subject, "sender": sender, "account": account,
        # 邮件自身没有时间字段时兜底成通知时间，免得微信模板里「时间：」后面空着
        "time": when or time.strftime("%Y-%m-%d %H:%M"), "count": "1",
        "summary": f"{subject} — {sender}",
    }
    return title, "\n".join(parts), link, fields


def _sender_names(cleaned, limit=3):
    """抽去重后的发件人显示名，给微信「发件人」字段用。

    2.0.4 修：原先汇总推送把 sender 写死成「N 封邮件」，在微信模板里等于没有信息；
    现在给「张三、李四 等 4 人」这种人话摘要。
    """
    names = []
    for _s, sender, _a, _w in cleaned:
        # 「张三 <a@b.com>」只留「张三」；本来就没有名字的保留完整地址
        name = re.split(r"[<（(]", sender, 1)[0].strip().strip("\"'“”")
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    if len(names) > limit:
        return "、".join(names[:limit]) + f" 等 {len(names)} 人"
    return "、".join(names)


def format_batch(items, base_url):
    title = f"{len(items)} 封新邮件"
    cleaned = [_clean_item(it) for it in items]
    lines = [f"收件箱新增 **{len(items)}** 封未读：", ""]
    for subject, sender, _account, _when in cleaned[:10]:
        lines.append(f"- **{subject}** — {sender}")
    if len(cleaned) > 10:
        lines.append(f"- …还有 {len(cleaned) - 10} 封")
    if base_url:
        lines += ["", f"[打开收件箱]({base_url.rstrip('/')}/)"]
    fields = {
        # title 仍是给其他通道看的通知标题；微信的「主题」变量给带「共」的整句，
        # 单看也读得通（模板首行固定文字是「您有一封新邮件」时不会露出「1 封新邮件」的怪相）
        "subject": f"共 {len(items)} 封新邮件",
        "sender": _sender_names(cleaned) or f"{len(items)} 封邮件",
        "account": "",
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "count": str(len(items)),
        "summary": "；".join(f"{s}（{sd}）" for s, sd, _, _ in cleaned[:5]),
    }
    return title, "\n".join(lines), base_url, fields


# ---------------------------------------------------------------- 设置读写

def get_settings(with_secrets=False):
    raw = db.get_meta(META_KEY)
    stored = {}
    if raw:
        try:
            stored = json.loads(raw) or {}
        except Exception:
            stored = {}
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in stored:
            cfg[k] = stored[k]
    cfg["channel"] = cfg["channel"] if cfg["channel"] in CHANNELS else "wechat_mp"

    secrets = {}
    enc = stored.get("config_enc")
    if enc:
        plain = accounts.decrypt_text(enc)
        if plain:
            try:
                secrets = json.loads(plain) or {}
            except Exception:
                secrets = {}
    cfg["config"] = secrets if with_secrets else {}
    # 只回传「这个通道配没配」，不回传明文
    cfg["configured"] = {
        ch: bool(any(str(v).strip() for k, v in (secrets.get(ch) or {}).items()
                     if k not in _public_keys(ch)))
        for ch in CHANNELS
    }
    # 非密钥字段（微信模板原文）回显，省得每台设备都要重粘一遍
    cfg["public"] = {
        ch: {k: str(v) for k, v in (secrets.get(ch) or {}).items()
             if k in _public_keys(ch) and str(v or "").strip()}
        for ch in CHANNELS
    }
    cfg["public"] = {k: v for k, v in cfg["public"].items() if v}
    return cfg


def _merge_secrets(current, incoming):
    """把前端提交的值并进已存的密钥；空值 = 不改动（界面不回显明文）。"""
    merged = {ch: dict(v or {}) for ch, v in (current or {}).items()}
    for ch, fields in (incoming or {}).items():
        if not isinstance(fields, dict):
            continue
        cur = merged.setdefault(ch, {})
        for k, v in fields.items():
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    continue
            if v is not None:
                cur[k] = v
    return merged


def save_settings(payload):
    payload = payload or {}
    cur = get_settings(with_secrets=True)
    secrets = _merge_secrets(cur.get("config"), payload.get("config"))

    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    if base_url and not re.match(r"^https?://", base_url, re.I):
        base_url = ""            # 只接受 http(s)，避免把奇怪的东西拼进通知里

    def _hhmm(v):
        v = str(v or "").strip()
        return v if re.match(r"^\d{1,2}:\d{2}$", v) else ""

    try:
        gap = max(0, min(1440, int(payload.get("min_interval") or 0)))
    except (TypeError, ValueError):
        gap = 0

    channel = str(payload.get("channel") or cur.get("channel") or "wechat_mp")
    stored = {
        "enabled": bool(payload.get("enabled")),
        "channel": channel if channel in CHANNELS else "wechat_mp",
        "base_url": base_url,
        "merge": bool(payload.get("merge")),
        "min_interval": gap,
        "quiet_start": _hhmm(payload.get("quiet_start")),
        "quiet_end": _hhmm(payload.get("quiet_end")),
        "notify_spam": bool(payload.get("notify_spam")),
        "config_enc": accounts.encrypt_text(json.dumps(secrets, ensure_ascii=False)) if secrets else "",
    }
    db.set_meta(META_KEY, json.dumps(stored, ensure_ascii=False))
    return get_settings()


def _in_quiet_hours(cfg):
    s, e = cfg.get("quiet_start") or "", cfg.get("quiet_end") or ""
    if not s or not e:
        return False
    now = time.strftime("%H:%M")
    if s <= e:
        return s <= now < e
    return now >= s or now < e        # 跨夜，比如 22:00–08:00


# ---------------------------------------------------------------- 对外入口

_send_lock = threading.Lock()


def _dispatch_async(channel, cconf, jobs):
    """顺序后台发送：逐封时不并发，免得被服务商当刷接口限流。"""
    def worker():
        with _send_lock:
            for job in jobs:
                try:
                    _dispatch(channel, cconf, *job)
                except Exception:
                    pass
    threading.Thread(target=worker, daemon=True, name="notify").start()


def notify_new_mails(items):
    """同步结束后调用。items 元素：{subject, from, account, time, email_id}"""
    if not items:
        return 0
    cfg = get_settings(with_secrets=True)
    if not cfg.get("enabled"):
        return 0
    channel = cfg["channel"]
    cconf = (cfg.get("config") or {}).get(channel) or {}
    pub = _public_keys(channel)
    if not any(str(v).strip() for k, v in cconf.items() if k not in pub):
        return 0
    if _in_quiet_hours(cfg):
        return 0
    gap = int(cfg.get("min_interval") or 0)
    if gap > 0:
        last = int(db.get_meta(LAST_SENT_KEY) or 0)
        if time.time() - last < gap * 60:
            return 0

    base_url = cfg.get("base_url") or ""
    # 2.0.4 修：只有 1 封时，即使开了「合并推送」也按单封详情发。
    # 否则微信模板的「主题/发件人」会渲染成汇总文案（如「1 封新邮件」「1 封邮件」），
    # 拿不到真实邮件信息 —— 开了合并只来一封是常态，这里必须让开。
    if cfg.get("merge") and len(items) > 1:
        jobs = [format_batch(items, base_url)]
    else:
        jobs = [format_one(it, base_url) for it in items]
    _dispatch_async(channel, cconf, jobs)
    db.set_meta(LAST_SENT_KEY, str(int(time.time())))
    return len(jobs)


def send_test(payload):
    """用界面上当前填的值发一条测试消息（不要求先保存）。"""
    payload = payload or {}
    cfg = get_settings(with_secrets=True)
    secrets = _merge_secrets(cfg.get("config"), payload.get("config"))
    channel = str(payload.get("channel") or cfg.get("channel") or "wechat_mp")
    if channel not in CHANNELS:
        return False, f"未知通道：{channel}"
    cconf = secrets.get(channel) or {}
    base_url = str(payload.get("base_url") or cfg.get("base_url") or "").strip()
    sample = {
        "subject": "这是一封测试邮件",
        "from": "星尘邮箱 <no-reply@example.com>",
        "account": "demo@example.com",
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "email_id": None,
    }
    title, body, link, fields = format_one(sample, base_url)
    title = "【测试】" + title
    return _dispatch(channel, cconf, title, body, link, fields)
