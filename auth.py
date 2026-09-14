# -*- coding: utf-8 -*-
"""账号密码登录：建号 / 校验 / 失败限流 / 改密码。

原来的「单一访问口令」换成常规的 Web 登录：
  · 首次启动（users 表为空）自动建一个管理员账号，默认 admin / admin123；
  · 密码用 PBKDF2-SHA256 加盐哈希存库，库里没有明文；
  · 登录后可在界面上改密码；
  · 同一用户名连续登录失败会被短暂锁定，挡一下暴力破解。
"""
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

import db

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"
MIN_PASSWORD_LEN = 6

# 失败限流：连续失败 MAX_FAILS 次锁 LOCK_SECONDS 秒（进程内存即可，重启清零无所谓）
MAX_FAILS = 5
LOCK_SECONDS = 300

_fails = {}                                     # username -> {"count": 失败次数, "until": 锁定截止}
_lock = threading.Lock()

# 用户名不存在时也走一次哈希校验，避免「响应快=用户名不存在」的探测
_DUMMY_HASH = generate_password_hash("__no_such_user__")


# ---------------------------------------------------------------- 初始化
def seed_default_user():
    """首次启动建管理员账号。返回 (username, is_default_pwd) 或 None。"""
    if db.count_users():
        return None
    # AUTH_PASSWORD / AUTH_PASSWORD_FILE 只影响「第一次建号」的初始密码，
    # 之后怎么改密码都由界面上操作，不再受环境变量影响。
    from config import AUTH_PASSWORD
    password = AUTH_PASSWORD or DEFAULT_PASSWORD
    is_default = 0 if AUTH_PASSWORD else 1
    db.add_user(DEFAULT_USERNAME, generate_password_hash(password), is_default)
    return DEFAULT_USERNAME, bool(is_default)


def default_password_in_use():
    """admin 是否还挂着初始密码（用于启动告警与界面提醒条）。"""
    user = db.get_user(DEFAULT_USERNAME)
    return bool(user and user.get("is_default_pwd"))


# ---------------------------------------------------------------- 限流
def _locked_left(username):
    with _lock:
        rec = _fails.get(username)
        if not rec:
            return 0
        until = rec["until"]
        if until > time.time():
            return int(until - time.time())
        if until:                     # 锁已到期 → 计数清零，重新给机会
            _fails.pop(username, None)
        return 0                      # 注意：没锁定时不能清计数，否则永远攒不到上限


def _record_fail(username):
    with _lock:
        rec = _fails.setdefault(username, {"count": 0, "until": 0.0})
        rec["count"] += 1
        if rec["count"] >= MAX_FAILS:
            rec["until"] = time.time() + LOCK_SECONDS


def _clear_fail(username):
    with _lock:
        _fails.pop(username, None)


# ---------------------------------------------------------------- 校验
def verify(username, password):
    """校验账号密码。成功返回 (user_dict, "")，失败返回 (None, 错误文案)。"""
    username = (username or "").strip()
    if not username or not password:
        return None, "请输入用户名和密码"

    left = _locked_left(username)
    if left:
        return None, f"失败次数过多，请 {left} 秒后再试"

    user = db.get_user(username)
    if user:
        ok = check_password_hash(user["password_hash"], password)
    else:
        check_password_hash(_DUMMY_HASH, password)     # 走一遍，抹平时序差异
        ok = False

    if not ok:
        _record_fail(username)
        return None, "用户名或密码不正确"

    _clear_fail(username)
    db.touch_login(user["username"])
    return user, ""


# ---------------------------------------------------------------- 改密码
def change_password(username, old_password, new_password):
    """修改密码。返回 (ok, 错误文案)。"""
    user = db.get_user(username or "")
    if not user:
        return False, "登录状态已失效，请重新登录"
    if not check_password_hash(user["password_hash"], old_password or ""):
        return False, "当前密码不正确"
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        return False, f"新密码至少 {MIN_PASSWORD_LEN} 位"
    if new_password == old_password:
        return False, "新密码不能和当前密码相同"
    if new_password == DEFAULT_PASSWORD:
        return False, "新密码不能使用初始密码 admin123"
    if new_password.strip() != new_password:
        return False, "新密码首尾不能有空格"

    db.set_user_password(user["username"], generate_password_hash(new_password), 0)
    return True, ""


def public_user(username):
    """给前端的用户信息（不含哈希）。"""
    user = db.get_user(username or "")
    if not user:
        return None
    return {
        "username": user["username"],
        "is_default_pwd": bool(user.get("is_default_pwd")),
        "last_login": user.get("last_login") or "",
    }
