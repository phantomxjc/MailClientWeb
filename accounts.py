# -*- coding: utf-8 -*-
"""密码 / 授权码存储。

桌面版用 keyring（Windows 凭据管理器），Docker 里没有那套东西，所以改成
「SQLite + Fernet 对称加密」：密文进库，密钥文件放在数据目录（Docker 里在
/data/cred.key，随卷持久化）。

安全边界要说清楚：密钥和数据在同一台机器上，能读到 /data 的人就能解出密码。
对单机 / NAS 私有部署是合理的权衡，但它不是「零知识」方案。
"""
import os

from cryptography.fernet import Fernet, InvalidToken

import db
from config import CRED_KEY_PATH

_fernet = None


def _cipher():
    global _fernet
    if _fernet is not None:
        return _fernet
    if not os.path.exists(CRED_KEY_PATH):
        # 首次运行生成密钥；0600 权限，仅属主可读
        key = Fernet.generate_key()
        fd = os.open(CRED_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    with open(CRED_KEY_PATH, "rb") as f:
        _fernet = Fernet(f.read().strip())
    return _fernet


def save_password(email, password):
    if password is None:
        return
    db.save_credential(email, _cipher().encrypt(password.encode("utf-8")).decode("ascii"))


def get_password(email):
    token = db.get_credential(email)
    if not token:
        return None
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # 密钥换过（比如 /data 被重建）→ 当作没存，让用户重新填
        return None


def delete_password(email):
    db.delete_credential(email)


def has_password(email):
    return bool(db.get_credential(email))
