# -*- coding: utf-8 -*-
"""验证「邮件没有时间」这条线的修复（2.0.5 起，2.0.6 收尾）。

覆盖 6 组：
  1) 发信报文带 Date 头（根因：以前发信漏写，收回来就没时间）
  2) IMAP INTERNALDATE → RFC2822 日期串的转换
  3) save_email：有日期按日期算 ts；没日期就留空，绝不伪造成「现在」
  4) _drop_fake_ts：清掉 2.0.5 刷出来的假时间（否则邮件冒充最新被顶到最上面）
  5) repair_missing_dates：向服务器要 INTERNALDATE，把老邮件的时间补回来
  6) 列表排序：未读一律在最上，组内按时间倒序

直接 `python tests/test_date_ts.py` 运行（桩掉 smtplib 与 IMAP 连接，DB 用临时文件）。
"""
import os, sys, pathlib, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import smtplib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email import message_from_bytes

import db
import parser as P
import sender as S

_fail = 0


def check(name, ok):
    global _fail
    print(("  OK  " if ok else "  MISS") + " " + name)
    if not ok:
        _fail += 1


# ───────────────────────── 1) 发信报文带 Date 头 ─────────────────────────
_orig_init = smtplib.SMTP.__init__


def _patched_init(self, *a, **k):
    _orig_init(self, *a, **k)


smtplib.SMTP.__init__ = _patched_init
smtplib.SMTP.connect = lambda self, *a, **k: (220, b"ok")
smtplib.SMTP.starttls = lambda self, *a, **k: (220, b"ok")
smtplib.SMTP.ehlo = lambda self, *a, **k: (250, b"ok")
smtplib.SMTP.quit = lambda self: (221, b"bye")
smtplib.SMTP.close = lambda self: None
smtplib.SMTP.login = lambda self, *a, **k: (235, b"ok")
smtplib.SMTP.sendmail = lambda self, *a, **k: {}  # 不连网，raw 由 send_email 返回

S.is_microsoft = lambda *a, **k: False
S.get_password = lambda addr: "authcode123"

print("=== 1) 发信报文 Date 头 ===")
raw, rcpt = S.send_email(
    smtp_server="smtp.qq.com", smtp_port=587, from_addr="me@qq.com",
    to_addr="you@qq.com", subject="标题", body="正文", html=True,
)
check("  发信返回 raw 报文", raw is not None and len(raw) > 0)
em = message_from_bytes(raw)
check("  报文含 Date 头", em.get("Date") is not None)
try:
    check("  Date 头可被解析为时间", parsedate_to_datetime(em.get("Date")) is not None)
except Exception:
    check("  Date 头可被解析为时间", False)


# ───────────────────── 2) INTERNALDATE → RFC2822 ─────────────────────
print("=== 2) INTERNALDATE 解析 ===")
hdr = P.internaldate_to_header("15-Sep-2026 09:28:00 +0800")
check("  标准 IMAP 格式可转换", bool(hdr))
if hdr:
    dt = parsedate_to_datetime(hdr).astimezone(timezone.utc)
    check("  转换结果保留时区(+0800 → 01:28 UTC)", dt.hour == 1 and dt.minute == 28)
check("  带引号也能转", bool(P.internaldate_to_header('"15-Sep-2026 09:28:00 +0800"')))
check("  无时区按 UTC 兜底", bool(P.internaldate_to_header("15-Sep-2026 09:28:00")))
check("  空值返回空串", P.internaldate_to_header("") == "")
check("  垃圾值返回空串", P.internaldate_to_header("not-a-date") == "")


# ───────────────── 建库 ─────────────────
tmp = tempfile.mkdtemp()
db.DB_PATH = os.path.join(tmp, "t.db")
db.init_db()
db.get_conn().execute("INSERT INTO accounts (name,email,provider) VALUES (?,?,?)",
                      ("测试号", "me@qq.com", "QQ"))


# ───────────── 3) save_email：有日期算 ts、没日期留空 ─────────────
print("=== 3) save_email 的 ts 取值 ===")
db.save_email(1, "INBOX", 1, "a@x.com", "b@x.com", "无日期邮件", None,
              "body", None, 0, 0)
db.save_email(1, "INBOX", 2, "a@x.com", "b@x.com", "有日期邮件",
              "Mon, 01 Jan 2024 10:00:00 +0000", "body", None, 0, 0)
rows = {r["uid"]: r["ts"] for r in
        db.get_conn().execute("SELECT uid, ts FROM emails ORDER BY uid").fetchall()}
check("  date 为空 → ts 留空（不伪造「现在」）", rows.get(1) in (None, 0))
check("  date 正常 → ts 来自 Date 头(2024)",
      rows.get(2) not in (None, 0) and abs(rows.get(2) - 1704103200) < 86400)


# ───────────── 4) _drop_fake_ts 清掉假时间 ─────────────
print("=== 4) 假时间清理 ===")
db.get_conn().execute(
    "UPDATE emails SET ts=?, date=NULL WHERE uid=1", (9999999999,))
db.get_conn().commit()
db.init_db()          # 重新初始化 → 触发 _drop_fake_ts
row = db.get_conn().execute("SELECT ts FROM emails WHERE uid=1").fetchone()
check("  date 为空却有 ts（假时间）被清掉", row["ts"] in (None, 0))

# 有 date 的邮件不该被误伤
db.save_email(1, "INBOX", 3, "a@x.com", "b@x.com", "有日期有ts",
              "Mon, 01 Jan 2024 10:00:00 +0000", "body", None, 0, 0)
db.init_db()
row = db.get_conn().execute("SELECT ts FROM emails WHERE uid=3").fetchone()
check("  有 date 的邮件 ts 不受影响", row["ts"] not in (None, 0))


# ───────────── 5) repair_missing_dates 补真实时间 ─────────────
print("=== 5) 历史邮件补时间 ===")


class FakeIMAP:
    """只实现用到的两个方法：select / uid fetch。"""

    def __init__(self, mapping):
        self.mapping = mapping          # uid(str) -> INTERNALDATE 原文
        self.asked = []

    def select(self, name, readonly=False):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd != "fetch":
            return ("OK", [b""])
        self.asked.append(str(args[0]))
        out = []
        for u in str(args[0]).split(","):
            if u in self.mapping:
                out.append(f'{u} (INTERNALDATE "{self.mapping[u]}")'.encode())
        return ("OK", out)


# uid=1 是「没 date」那封，另外补一封也无 date 的
db.save_email(1, "INBOX", 4, "c@x.com", "d@x.com", "老邮件没时间", None,
              "body", None, 0, 0)
db.get_conn().execute("UPDATE emails SET ts=NULL WHERE uid=4")
db.get_conn().commit()

missing = db.emails_missing_date(1, "INBOX")
check("  能查出「时间不可信」的邮件", any(r["uid"] == 1 for r in missing)
      and any(r["uid"] == 4 for r in missing))

conn = FakeIMAP({"1": "14-Sep-2026 20:15:00 +0800", "4": "15-Sep-2026 08:00:00 +0800"})
fixed = P.repair_missing_dates(1, conn, "INBOX", "INBOX")
check("  修复返回补好的封数", fixed == 2)

row = db.get_conn().execute("SELECT date, ts FROM emails WHERE uid=4").fetchone()
check("  补出的 date 可解析", bool(row["date"]) and parsedate_to_datetime(row["date"]) is not None)
check("  补出的 ts 与 INTERNALDATE 一致(08:00 +0800 → 00:00 UTC)",
      row["ts"] is not None and
      parsedate_to_datetime(row["date"]).astimezone(timezone.utc).hour == 0)
# 时间戳不手算，交给 datetime 推：15-Sep-2026 08:00 +0800 == 00:00 UTC
_exp_ts = int(datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc).timestamp())
check("  ts 数值正确", row["ts"] == _exp_ts)

again = db.emails_missing_date(1, "INBOX")
check("  补好后不再被查出来（下次同步不再重复问服务器）",
      not any(r["uid"] == 4 for r in again))

check("  批量只发一条 IMAP 命令", len(conn.asked) == 1 and "," in conn.asked[0])


# ───────────── 6) 列表排序：未读优先 ─────────────
print("=== 6) 列表排序 ===")
db.save_email(1, "INBOX", 10, "old@x.com", "b@x.com", "已读的老邮件",
              "Mon, 01 Jan 2020 10:00:00 +0000", "body", None, 1, 0)   # seen=1
db.save_email(1, "INBOX", 11, "new@x.com", "b@x.com", "未读的新邮件",
              "Mon, 01 Jan 2025 10:00:00 +0000", "body", None, 0, 0)   # seen=0
db.save_email(1, "INBOX", 15, "none@x.com", "b@x.com", "时间仍空",
              None, "body", None, 0, 0)                               # seen=0 且无时间
order = db.get_emails(limit=50)
idx_unread = [i for i, r in enumerate(order) if r["subject"] == "未读的新邮件"]
idx_read = [i for i, r in enumerate(order) if r["subject"] == "已读的老邮件"]
check("  未读排在已读前面", idx_unread and idx_read and idx_unread[0] < idx_read[0])
same_group = [r["subject"] for r in order if r["seen"] == 0]
check("  未读组内部按时间倒序（新的在前）",
      same_group.index("老邮件没时间") < same_group.index("无日期邮件")
      < same_group.index("未读的新邮件"))
check("  未读组里时间仍为空的排最后", same_group[-1] == "时间仍空")

print()
if _fail:
    print(f"失败 {_fail} 项")
    sys.exit(1)
print("全部通过")
