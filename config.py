# -*- coding: utf-8 -*-
"""配置：数据目录、服务商预设、运行参数。

与桌面版的最大区别是数据目录改为由环境变量控制（Docker 里挂到 /data），
不再依赖 exe 所在目录。
"""
import os

APP_NAME = "星尘邮箱"
APP_NAME_EN = "Stardust"
APP_VERSION = "2.2.1"

# 项目根目录（app.py / config.py 所在目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """返回可持久化的数据目录。

    Docker 部署时把 /data 挂成卷，所有需要长期保留的东西都放这里：
    数据库、微软 OAuth 令牌、凭据加密密钥、Flask 会话密钥。
    """
    env = os.environ.get("DATA_DIR", "").strip()
    path = os.path.abspath(env) if env else os.path.join(BASE_DIR, "data")
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = data_dir()
DB_PATH = os.path.join(DATA_DIR, "mail_client.db")
CRED_KEY_PATH = os.path.join(DATA_DIR, "cred.key")        # 密码/授权码加密密钥
SECRET_KEY_PATH = os.path.join(DATA_DIR, "flask_secret.key")  # 会话签名密钥

# 登录：用户名 + 密码（见 auth.py）。首次启动会用下面的值建管理员账号，
# 不设就是 admin / admin123 —— 登录后请到界面上改密码（改完环境变量不再影响）。
#   1) 环境变量  AUTH_PASSWORD=初始密码
#   2) 口令文件  AUTH_PASSWORD_FILE=/path/to/file  （Docker/K8s secret 模式，命令行不暴露明文）
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "").strip()
if not AUTH_PASSWORD:
    _pwfile = os.environ.get("AUTH_PASSWORD_FILE", "").strip()
    if _pwfile and os.path.exists(_pwfile):
        with open(_pwfile, encoding="utf-8") as _f:
            AUTH_PASSWORD = _f.read().strip()

# 仅本机调试用：显式设 AUTH_DISABLED=1 可关闭登录校验（启动日志会警告），切勿公网使用。
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")

# 自动同步间隔（分钟），0 表示关闭定时同步
try:
    SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "10"))
except ValueError:
    SYNC_INTERVAL_MINUTES = 10

# ---------------------------------------------------------------- 网络超时
# 这两个值是「一个邮箱有问题，后面邮箱也不更新」的根治办法：
# 服务器卡住时不设超时，socket 会一直等下去，整个同步线程就吊死在那里，
# 排在它后面的账号自然一封都拉不到。设了超时，最坏情况也只是这一个账号失败。
def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# 单次 IMAP 读写的最长等待（秒）
IMAP_TIMEOUT = _env_int("IMAP_TIMEOUT", 30)

# 单个账号一轮同步的总上限（秒）。超过就先跳过它、继续同步后面的账号，
# 并在界面上明确写出「已跳过」——不能让一个坏账号拖住其余所有邮箱。
SYNC_ACCOUNT_TIMEOUT = _env_int("SYNC_ACCOUNT_TIMEOUT", 300)

# oauth 字段：microsoft 表示该服务商必须（或推荐）用 OAuth2 现代认证登录
PROVIDERS = {
    "QQ": {
        "label": "QQ 邮箱",
        "imap": "imap.qq.com", "imap_port": 993,
        "smtp": "smtp.qq.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": ("QQ 邮箱必须用「授权码」登录，不是 QQ 密码。"
                      "获取：网页版 QQ 邮箱 → 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码。"),
    },
    "163": {
        "label": "网易 163 邮箱",
        "imap": "imap.163.com", "imap_port": 993,
        "smtp": "smtp.163.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "网易邮箱需使用客户端「授权码」，不是登录密码。",
    },
    "126": {
        "label": "网易 126 邮箱",
        "imap": "imap.126.com", "imap_port": 993,
        "smtp": "smtp.126.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "网易邮箱需使用客户端「授权码」，不是登录密码。",
    },
    "Outlook": {
        "label": "Outlook / Hotmail（个人）",
        "imap": "outlook.office365.com", "imap_port": 993,
        "smtp": "smtp-mail.outlook.com", "smtp_port": 587,
        "oauth": "microsoft",
        "auth_note": ("微软已停用密码登录（基础认证），此账号请点「使用微软账号登录」"
                      "完成一次授权；同时请确认网页版 Outlook 设置里已开启 IMAP。"),
    },
    "Outlook365": {
        "label": "Microsoft 365（工作/学校）",
        "imap": "outlook.office365.com", "imap_port": 993,
        "smtp": "smtp.office365.com", "smtp_port": 587,
        "oauth": "microsoft",
        "auth_note": ("工作/学校账号需管理员允许 IMAP，并点「使用微软账号登录」"
                      "授权（走 OAuth2，不用密码）。"),
    },
    "Gmail": {
        "label": "Gmail",
        "imap": "imap.gmail.com", "imap_port": 993,
        "smtp": "smtp.gmail.com", "smtp_port": 587,
        "oauth": "",
        "auth_note": "Gmail 需在账号安全设置里开启两步验证后生成「应用专用密码」。",
    },
    "Sina": {
        "label": "新浪邮箱",
        "imap": "imap.sina.com", "imap_port": 993,
        "smtp": "smtp.sina.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "新浪邮箱需在网页版设置里开启 IMAP/SMTP 后使用「客户端授权码」登录。",
    },
    "Aliyun": {
        "label": "阿里邮箱（含钉钉/淘宝邮箱）",
        "imap": "imap.aliyun.com", "imap_port": 993,
        "smtp": "smtp.aliyun.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "阿里邮箱需在设置里开启 IMAP/SMTP 服务，用「授权码」登录（非登录密码）。",
    },
    "Mobile139": {
        "label": "移动 139 邮箱",
        "imap": "imap.139.com", "imap_port": 993,
        "smtp": "smtp.139.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "139 邮箱需在手机端或网页版开启 IMAP/SMTP，用服务密码或授权码登录。",
    },
    "Exmail": {
        "label": "腾讯企业邮（企业微信邮箱）",
        "imap": "imap.exmail.qq.com", "imap_port": 993,
        "smtp": "smtp.exmail.qq.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "腾讯企业邮可用邮箱密码或管理员下发的客户端专用密码登录；IMAP/SMTP 需在管理后台开启。",
    },
    "E189": {
        "label": "电信 189 邮箱",
        "imap": "imap.189.cn", "imap_port": 993,
        "smtp": "smtp.189.cn", "smtp_port": 465,
        "oauth": "",
        "auth_note": "189 邮箱需在设置里开启 IMAP/SMTP，使用授权码登录。",
    },
    "Yeah": {
        "label": "网易 Yeah 邮箱",
        "imap": "imap.yeah.net", "imap_port": 993,
        "smtp": "smtp.yeah.net", "smtp_port": 465,
        "oauth": "",
        "auth_note": "Yeah 邮箱与网易同体系，需在设置里开启 IMAP/SMTP 后用「授权码」登录。",
    },
    "iCloud": {
        "label": "iCloud 邮箱（Apple）",
        "imap": "imap.mail.me.com", "imap_port": 993,
        "smtp": "smtp.mail.me.com", "smtp_port": 587,
        "oauth": "",
        "auth_note": "iCloud 需在 Apple ID 设置开启两步验证，并用「应用专用密码」登录。",
    },
    "Yahoo": {
        "label": "Yahoo 邮箱",
        "imap": "imap.mail.yahoo.com", "imap_port": 993,
        "smtp": "smtp.mail.yahoo.com", "smtp_port": 465,
        "oauth": "",
        "auth_note": "Yahoo 需在账号安全设置里生成「应用专用密码」后登录（基础密码已停用）。",
    },
    "Custom": {
        "label": "自定义 / 其他邮箱",
        "imap": "", "imap_port": 993,
        "smtp": "", "smtp_port": 465,
        "oauth": "",
        "auth_note": "填写服务商提供的 IMAP/SMTP 地址与端口。",
    },
}

FOLDERS = ["INBOX", "Sent", "Drafts", "Trash", "Spam"]

# 按邮箱域名猜服务商，方便用户少选一次
DOMAIN_PROVIDER = {
    "qq.com": "QQ", "vip.qq.com": "QQ", "foxmail.com": "QQ",
    "163.com": "163", "126.com": "126", "yeah.net": "Yeah",
    "outlook.com": "Outlook", "hotmail.com": "Outlook", "live.com": "Outlook",
    "msn.com": "Outlook", "outlook.cn": "Outlook365",
    "gmail.com": "Gmail", "googlemail.com": "Gmail",
    # 新增常用邮箱
    "sina.com": "Sina", "sina.cn": "Sina",
    "aliyun.com": "Aliyun",
    "139.com": "Mobile139",
    "exmail.qq.com": "Exmail",
    "189.cn": "E189", "21cn.com": "E189",
    "icloud.com": "iCloud", "me.com": "iCloud", "mac.com": "iCloud",
    "yahoo.com": "Yahoo", "yahoo.com.cn": "Yahoo", "ymail.com": "Yahoo",
}


def provider_of(provider_key):
    """取服务商预设，未知则回退到自定义。"""
    return PROVIDERS.get(provider_key) or PROVIDERS["Custom"]
