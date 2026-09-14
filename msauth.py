# -*- coding: utf-8 -*-
"""微软 OAuth2 设备码流程的 Web 封装。

设备码流程本身是「给人看的」：向用户展示一串验证码，让他到
microsoft.com/devicelogin 输入，客户端在后台轮询拿令牌。桌面版用弹窗承载，
Web 版则是：前端发起 → 后端开一个后台线程轮询 → 前端轮询自家接口看结果。

流程状态放在内存里（进程内字典）。它天然是短生命周期的临时数据，
重启丢一次授权不心疼，所以不落库。
"""
import threading
import time
import uuid

import oauth
from config import PROVIDERS, provider_of
import db

# 个人微软账号域名（其余按 工作/学校 账号处理）
_PERSONAL_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com",
                     "passport.com", "outlook.jp", "hotmail.co.uk")

_FLOW_TTL = 1800
_flows = {}
_lock = threading.Lock()


def _purge():
    now = time.time()
    with _lock:
        for fid in [k for k, v in _flows.items() if now - v["created_at"] > _FLOW_TTL]:
            _flows.pop(fid, None)


def _provider_for(email, fallback="Outlook"):
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain.endswith("outlook.cn"):
        return "Outlook365"
    if domain in _PERSONAL_DOMAINS:
        return "Outlook"
    return fallback


def _poll(fid, resp, email_hint, client_id):
    flow = _flows[fid]

    def cancelled():
        return bool(_flows.get(fid, {}).get("cancelled"))

    try:
        tokens = oauth.poll_for_token(
            resp["device_code"], resp.get("interval"), resp.get("expires_in"),
            client_id=client_id, should_cancel=cancelled)
    except oauth.OAuthError as e:
        if not cancelled():
            with _lock:
                flow.update(state="error", message=str(e))
        return

    email = oauth.signed_in_email(tokens) or email_hint
    if not email:
        with _lock:
            flow.update(state="error",
                        message="授权成功但没能识别登录的邮箱，请手动填写账号")
        return

    oauth.save_tokens(email, tokens, client_id)

    provider = _provider_for(email, flow.get("fallback_provider") or "Outlook")
    preset = provider_of(provider)
    account_id = db.add_account(email.split("@")[0], email, provider,
                                preset["imap"], preset["imap_port"],
                                preset["smtp"], preset["smtp_port"])
    with _lock:
        flow.update(state="done", email=email, account_id=account_id,
                    provider=provider)


def start(email_hint="", client_id=None, fallback_provider="Outlook"):
    _purge()
    resp = oauth.start_device_flow(client_id)
    fid = uuid.uuid4().hex[:16]
    with _lock:
        _flows[fid] = {
            "id": fid, "state": "pending", "cancelled": False,
            "user_code": resp.get("user_code"),
            "verification_uri": resp.get("verification_uri"),
            "verification_uri_complete": resp.get("verification_uri_complete"),
            "expires_at": time.time() + int(resp.get("expires_in") or 900),
            "message": "", "email": "", "account_id": None,
            "created_at": time.time(), "fallback_provider": fallback_provider,
        }
    threading.Thread(target=_poll,
                     args=(fid, resp, email_hint, client_id or resp.get("client_id")),
                     name="msauth", daemon=True).start()
    return status(fid)


def status(fid):
    flow = _flows.get(fid)
    if not flow:
        return {"state": "error", "message": "授权会话不存在或已过期，请重新发起"}
    return {
        "id": flow["id"], "state": flow["state"], "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "verification_uri_complete": flow["verification_uri_complete"],
        "expires_in": max(0, int(flow["expires_at"] - time.time())),
        "message": flow["message"], "email": flow["email"],
        "account_id": flow["account_id"],
    }


def cancel(fid):
    with _lock:
        if fid in _flows:
            _flows[fid]["cancelled"] = True
            _flows[fid]["state"] = "cancelled"
            return True
    return False


def signed_in(email):
    return oauth.has_tokens(email)
