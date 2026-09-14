# -*- coding: utf-8 -*-
"""微软 OAuth2（现代认证）支持 —— 给 Outlook / Hotmail / M365 账号用。

为什么必须有这个模块
--------------------
微软早已停用 IMAP/SMTP 的「基础认证」（用账号密码直接登录）。
实测 outlook.office365.com 的 CAPABILITY 里只有 `AUTH=XOAUTH2` 且带
`LOGINDISABLED`，用密码 LOGIN 会被明确拒绝：

    imaplib.IMAP4.error: Basic authentication is disabled.

所以 Outlook 账号只能走 OAuth2。这里采用「设备码流程」（Device Code Flow）：
不需要内嵌浏览器、不需要本地回调端口，用户在自己浏览器里输一次验证码即可。

令牌存放
--------
存放于数据目录下的 `oauth_tokens.json`（与 mail_client.db 同目录）。
不放 keyring 是因为 Windows 凭据管理器的单条凭据有 2560 字节上限，
而一个刷新令牌 + 访问令牌的 JSON 通常会超过它。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from config import data_dir

# 公共客户端 ID：Thunderbird 注册的公开客户端，已获委托权限
# （IMAP.AccessAsUser.All / SMTP.Send），同时支持个人微软账号与工作学校账号。
# 若你有自己的 Azure 应用注册，可在「添加账号」里覆盖。
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

SCOPES = ("offline_access openid profile email "
          "https://outlook.office.com/IMAP.AccessAsUser.All "
          "https://outlook.office.com/SMTP.Send")

_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
DEVICE_CODE_URL = _AUTHORITY + "/devicecode"
TOKEN_URL = _AUTHORITY + "/token"

_TOKEN_FILE = "oauth_tokens.json"


class OAuthError(Exception):
    """授权/刷新过程中的可读错误。"""


# ---------------------------------------------------------------- 令牌存储
def _token_path():
    return os.path.join(data_dir(), _TOKEN_FILE)


def _load_all():
    path = _token_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_all(data):
    path = _token_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_tokens(email, tokens, client_id=None):
    """保存令牌（含过期时间戳），邮箱作为键。"""
    if not tokens.get("refresh_token") and not tokens.get("access_token"):
        raise OAuthError("授权返回里没有可用令牌")
    data = _load_all()
    expires_in = int(tokens.get("expires_in") or 3600)
    data[email.lower()] = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + max(expires_in - 120, 60),
        "client_id": client_id or tokens.get("client_id") or DEFAULT_CLIENT_ID,
        "scope": tokens.get("scope", ""),
    }
    _save_all(data)
    return data[email.lower()]


def load_tokens(email):
    return _load_all().get((email or "").lower())


def has_tokens(email):
    t = load_tokens(email)
    return bool(t and (t.get("refresh_token") or t.get("access_token")))


def clear_tokens(email):
    data = _load_all()
    if data.pop((email or "").lower(), None) is not None:
        _save_all(data)


# ---------------------------------------------------------------- 设备码流程
def _post_form(url, payload, timeout=30):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except ValueError:
            raise OAuthError(f"HTTP {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise OAuthError(f"网络不可达：{e.reason}")


def start_device_flow(client_id=None):
    """申请设备码。返回含 user_code / verification_uri / device_code 的字典。"""
    cid = client_id or DEFAULT_CLIENT_ID
    resp = _post_form(DEVICE_CODE_URL, {"client_id": cid, "scope": SCOPES})
    if "user_code" not in resp:
        raise OAuthError(resp.get("error_description")
                         or resp.get("error") or "申请设备码失败")
    resp["client_id"] = cid
    return resp


def poll_for_token(device_code, interval, expires_in, client_id=None,
                   should_cancel=None, on_wait=None):
    """轮询直到用户在浏览器完成登录，返回令牌字典。"""
    cid = client_id or DEFAULT_CLIENT_ID
    wait = max(int(interval or 5), 3)
    deadline = time.time() + int(expires_in or 900)
    while time.time() < deadline:
        if should_cancel and should_cancel():
            raise OAuthError("已取消授权")
        time.sleep(wait)
        if on_wait:
            on_wait()
        resp = _post_form(TOKEN_URL, {
            "client_id": cid,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        if resp.get("access_token"):
            resp["client_id"] = cid
            return resp
        err = resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            wait += 5
            continue
        if err == "expired_token":
            raise OAuthError("验证码已过期，请重新点击授权")
        if err == "authorization_declined":
            raise OAuthError("你在浏览器中拒绝了授权")
        raise OAuthError(resp.get("error_description") or err or "授权失败")
    raise OAuthError("等待超时，请重新点击授权")


def refresh_tokens(email):
    """用刷新令牌换新的访问令牌。"""
    t = load_tokens(email)
    if not t or not t.get("refresh_token"):
        raise OAuthError("该账号还没有完成微软授权，请点『使用微软账号登录』")
    resp = _post_form(TOKEN_URL, {
        "client_id": t.get("client_id") or DEFAULT_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": t["refresh_token"],
        "scope": SCOPES,
    })
    if not resp.get("access_token"):
        raise OAuthError(resp.get("error_description")
                         or resp.get("error") or "刷新令牌失败，请重新授权")
    # 微软可能不下发新的 refresh_token，沿用旧的
    resp.setdefault("refresh_token", t.get("refresh_token"))
    return save_tokens(email, resp, t.get("client_id"))


def access_token(email):
    """拿到可用的访问令牌（过期则自动刷新）。"""
    t = load_tokens(email)
    if not t:
        raise OAuthError("该账号还没有完成微软授权，请点『使用微软账号登录』")
    if t.get("access_token") and time.time() < float(t.get("expires_at") or 0):
        return t["access_token"]
    return refresh_tokens(email)["access_token"]


def xoauth2_string(email, token):
    """IMAP/SMTP 的 XOAUTH2 认证串。"""
    return f"user={email}\x01auth=Bearer {token}\x01\x01"


# ---------------------------------------------------------------- 身份解析
def decode_id_token(tokens):
    """解出 id_token 的 claims（仅用于取登录的邮箱，不做签名校验）。"""
    raw = tokens.get("id_token")
    if not raw or raw.count(".") < 2:
        return {}
    payload = raw.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def signed_in_email(tokens):
    """从令牌里取实际登录的邮箱地址。"""
    claims = decode_id_token(tokens)
    for key in ("preferred_username", "email", "upn", "unique_name"):
        val = claims.get(key)
        if val and "@" in str(val):
            return str(val)
    return ""
