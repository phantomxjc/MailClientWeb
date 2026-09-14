# -*- coding: utf-8 -*-
"""新邮件提醒：推送到「用户自己申请、自己填 Key」的第三方服务。

为什么不走浏览器通知 —— Chrome / Edge 只在**安全上下文**（HTTPS 或 localhost）
下才给 Notification 权限。自托管用户绝大多数是 `http://192.168.x.x:8090` 访问，
`Notification.requestPermission()` 会被直接拒掉。走第三方推送则与访问方式无关：
邮件在 NAS 上，通知走服务商的公网通道到手机，两边不需要能互相访问。

所有通道本质都是「一个 HTTP 请求 + 一段 JSON/表单」，没有 SDK 依赖，
所以这里只用标准库 urllib —— 不为一条通知往镜像里塞第三方库。

安全边界：这些 Key 是凭据，跟邮箱密码共用同一把 Fernet 密钥（credentials 那套），
加密后存 meta 表。对外接口只回传「已配置 / 未配置」，不回传明文。
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

DEFAULTS = {
    "enabled": False,
    "channel": "serverchan",
    "base_url": "",          # 拼「查看邮件」链接用，留空则通知里不带链接
    "merge": False,          # True = 一批合并成一条；False = 一封一条
    "min_interval": 0,       # 分钟；>0 时距上次推送不足这么久就跳过本次
    "quiet_start": "",       # 免打扰起（HH:MM），两个都填才生效
    "quiet_end": "",
    "notify_spam": False,    # 垃圾邮件要不要也提醒
}


# ---------------------------------------------------------------- HTTP

def _post(url, payload=None, form=False, raw_text=None, timeout=TIMEOUT):
    headers = {"User-Agent": "MailClient-Web/2.0 (+https://github.com/phantomxjc/MailClientWeb)"}
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

def _send_serverchan(cconf, title, body, link):
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


def _send_pushplus(cconf, title, body, link):
    token = _need(cconf, "token", "Token")
    return _post("https://www.pushplus.plus/send", {
        "token": token, "title": title, "content": body, "template": "markdown"})


def _send_bark(cconf, title, body, link):
    key = _need(cconf, "key", "Key")
    url = key.rstrip("/") if key.startswith("http") else f"https://api.day.app/{key}"
    payload = {"title": title, "body": body, "group": "MailClient"}
    if link:
        payload["url"] = link
    return _post(url, payload)


def _send_wecom(cconf, title, body, link):
    url = _need(cconf, "url", "Webhook 地址")
    text = f"**{title}**\n{body}"
    return _post(url, {"msgtype": "markdown", "markdown": {"content": text}})


def _send_dingtalk(cconf, title, body, link):
    url = _need(cconf, "url", "Webhook 地址")
    return _post(url, {"msgtype": "markdown",
                       "markdown": {"title": title, "text": f"### {title}\n\n{body}"}})


def _send_feishu(cconf, title, body, link):
    url = _need(cconf, "url", "Webhook 地址")
    # 用纯文本：飞书的 markdown 卡片对链接语法支持挑剔，正文里已含完整链接
    plain = body.replace("**", "")
    m = re.search(r"\[点此查看这封邮件\]\((\S+?)\)", body)
    if m:
        plain = plain.replace(f"[点此查看这封邮件]({m.group(1)})", m.group(1))
    return _post(url, {"msg_type": "text", "content": {"text": f"{title}\n{plain}"}})


def _send_custom(cconf, title, body, link):
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
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "bark": _send_bark,
    "wecom": _send_wecom,
    "dingtalk": _send_dingtalk,
    "feishu": _send_feishu,
    "custom": _send_custom,
}


def _dispatch(channel, cconf, title, body, link):
    """同步发送一条，返回 (ok, 说明文字)。"""
    fn = _SENDERS.get(channel)
    if not fn:
        return False, f"未知通道：{channel}"
    try:
        code, text = fn(cconf or {}, title, body, link)
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
    return title, "\n".join(parts), link


def format_batch(items, base_url):
    title = f"{len(items)} 封新邮件"
    lines = [f"收件箱新增 **{len(items)}** 封未读：", ""]
    for it in items[:10]:
        subject, sender, account, _ = _clean_item(it)
        lines.append(f"- **{subject}** — {sender}")
    if len(items) > 10:
        lines.append(f"- …还有 {len(items) - 10} 封")
    if base_url:
        lines += ["", f"[打开收件箱]({base_url.rstrip('/')}/)"]
    return title, "\n".join(lines), base_url


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
    cfg["channel"] = cfg["channel"] if cfg["channel"] in CHANNELS else "serverchan"

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
        ch: bool(any(str(v).strip() for v in (secrets.get(ch) or {}).values()))
        for ch in CHANNELS
    }
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

    channel = str(payload.get("channel") or cur.get("channel") or "serverchan")
    stored = {
        "enabled": bool(payload.get("enabled")),
        "channel": channel if channel in CHANNELS else "serverchan",
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
            for title, body, link in jobs:
                try:
                    _dispatch(channel, cconf, title, body, link)
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
    if not any(str(v).strip() for v in cconf.values()):
        return 0
    if _in_quiet_hours(cfg):
        return 0
    gap = int(cfg.get("min_interval") or 0)
    if gap > 0:
        last = int(db.get_meta(LAST_SENT_KEY) or 0)
        if time.time() - last < gap * 60:
            return 0

    base_url = cfg.get("base_url") or ""
    if cfg.get("merge"):
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
    channel = str(payload.get("channel") or cfg.get("channel") or "serverchan")
    if channel not in CHANNELS:
        return False, f"未知通道：{channel}"
    cconf = secrets.get(channel) or {}
    base_url = str(payload.get("base_url") or cfg.get("base_url") or "").strip()
    sample = {
        "subject": "这是一封测试邮件",
        "from": "MailClient <no-reply@example.com>",
        "account": "demo@example.com",
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "email_id": None,
    }
    title, body, link = format_one(sample, base_url)
    title = "【测试】" + title
    return _dispatch(channel, cconf, title, body, link)
