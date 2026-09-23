# -*- coding: utf-8 -*-
"""StardustMail 2.2.2 验证：
   ① 发信补 Message-ID（防企业邮箱「已发送」双份 / 被判垃圾邮件）
   ② 收件人跨字段全局去重（to+cc 同一个人不再发两封）
   ③ 增量同步（邮件不全的根治：本地已有的不再重拉，新邮件不限量）
   ④ 定时/延时发送队列（入库 → 到期扫描 → 取消 → 立即发送）
"""
import os, sys, shutil, tempfile, pathlib, time, json, base64

DATA = r"C:\Users\Administrator\AppData\Local\Temp\mc_test_v222"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["DATA_DIR"] = DATA
os.environ["AUTH_DISABLED"] = "1"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import smtplib
import db
db.init_db()
import sender as S
import parser as P

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK ] " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))


# ─────────────────────────────── SMTP 桩（同 test_send_and_contacts：真 smtplib 编码）
def _patch_smtp():
    _orig_init = smtplib.SMTP.__init__
    inst = []

    def patched_init(self, host="", port=0, *a, **kw):
        _orig_init(self, "", 0)
        self.esmtp_features = {"auth": "PLAIN LOGIN"}
        self.does_esmtp = True
        self.ehlo_resp = 250
        self.log = []
        inst.append(self)

    def _send(self, s):
        if isinstance(s, str):
            s = s.encode(self.command_encoding)
        self.log.append(s)
        return self.getreply()

    def _getreply(self):
        last = (self.log[-1].upper() if self.log else b"")
        if last.startswith(b"DATA"):
            return (354, b"go ahead")
        if last.startswith(b"AUTH"):
            return (235, b"ok")
        return (250, b"ok")

    def _data(self, msg):
        self.putcmd("data")
        code, repl = self.getreply()
        if code != 354:
            raise smtplib.SMTPDataError(code, repl)
        if isinstance(msg, str):
            msg = smtplib._fix_eols(msg).encode(self.command_encoding)
        q = smtplib._quote_periods(msg)
        if q[-2:] != smtplib.bCRLF:
            q += smtplib.bCRLF
        q += b"." + smtplib.bCRLF
        self.send(q)
        return (250, b"queued")

    smtplib.SMTP.__init__ = patched_init
    smtplib.SMTP.connect = lambda self, *a, **k: (220, b"ok")
    smtplib.SMTP.starttls = lambda self, *a, **k: (220, b"ok")
    smtplib.SMTP.quit = lambda self, *a, **k: (221, b"bye")
    smtplib.SMTP.close = lambda self: None
    smtplib.SMTP.ehlo_or_helo_if_needed = lambda self: None
    smtplib.SMTP.ehlo = lambda self, name="": (250, b"ok")
    smtplib.SMTP.login = lambda self, a, p: (235, b"ok")
    smtplib.SMTP.send = _send
    smtplib.SMTP.getreply = _getreply
    smtplib.SMTP.data = _data
    return inst


print("=== 1) Message-ID 与收件人全局去重 ===")
inst = _patch_smtp()
S.get_password = lambda addr: "authcode123"

BASE = dict(smtp_server="smtp.qiye.aliyun.com", smtp_port=587, from_addr="me@corp.com",
            subject="t", body="b")
kw = dict(BASE); kw.update({"to_addr": "x@y.com", "cc": "x@y.com"})
raw, rcpt, msgid = S.send_email(**kw)
check("to 和 cc 同一个人只投递一次（老版本 RCPT 两次=收到两封）",
      [r["addr"] for r in rcpt] == ["x@y.com"], rcpt)

check("生成的报文带 Message-ID", b"Message-ID:" in raw and msgid and msgid.startswith("<"))
check("  Message-ID 域名取自发件人", f"@{S.send_email and 'corp.com'}>" in str(msgid) or "corp.com" in str(msgid), msgid)

# 重复调用 Message-ID 各不相同
kw2 = dict(BASE); kw2.update({"to_addr": "z@y.com"})
_, _, msgid2 = S.send_email(**kw2)
check("两封信的 Message-ID 互不相同", msgid != msgid2)

# ─────────────────────────────── 定时发送队列（db 层 + API 层）
print("\n=== 2) 定时发送队列 ===")
tid = db.add_scheduled(1, "a@b.com", "c@d.com", "", "hi", "body", "[]", True,
                       int(time.time()) + 600)
check("任务能入库", bool(tid))
row = db.get_scheduled(tid)
check("  读取完整（含正文与账号字段）", row and row["subject"] == "hi" and "smtp_server" in row)

check("未到点不进 due", db.due_scheduled() == [], db.due_scheduled())
db.update_scheduled(tid, send_at=int(time.time()) - 10)
check("到点后能扫到", db.due_scheduled() == [tid])

lst = db.list_scheduled()
check("队列列表不带正文/附件（防撑爆接口）",
      lst and "body" not in lst[0] and "attachments" not in lst[0])

check("取消（删除）", db.delete_scheduled(tid))
check("  删后查不到", db.get_scheduled(tid) is None)

# ─────────────────────────────── 增量同步（邮件不全的根治）
print("\n=== 3) 增量同步 ===")


class FakeIMAP:
    """极简 IMAP 桩：select / uid(search|fetch)，能数出 BODY.PEEK 的次数。"""

    def __init__(self, server_uids):
        self.server_uids = list(server_uids)      # 服务器上现存的 uid
        self.mails = {}                           # uid -> (head, raw)
        self.peeks = []                           # 拉过正文的 uid
        self.flag_pulls = []                      # FLAGS 轻量查询的批

    def _mk(self, uid):
        from datetime import datetime, timezone
        import email.utils as eu
        raw = (f"From: sender{uid}@x.com\r\nTo: me@qq.com\r\n"
               f"Subject: mail-{uid}\r\nDate: {eu.format_datetime(datetime.now(timezone.utc))}\r\n"
               f"\r\nbody {uid}").encode()
        head = f'1 (UID {uid} FLAGS () INTERNALDATE "15-Sep-2026 09:28:00 +0800")'.encode()
        return head, raw

    def select(self, box, readonly=False):
        for u in self.server_uids:
            if u not in self.mails:
                self.mails[u] = self._mk(u)
        return ("OK", [b"3"])

    def uid(self, cmd, *args):
        a = [x.decode() if isinstance(x, bytes) else str(x) for x in args]
        if cmd == "search":
            if a[-1] == "ALL":
                return ("OK", [b" ".join(str(u).encode() for u in self.server_uids)])
            if "UID" in a:                          # 增量查询 UID n:*
                lo = int(a[a.index("UID") + 1].split(":")[0])
                hits = [u for u in self.server_uids if u >= lo]
                return ("OK", [b" ".join(str(u).encode() for u in hits)])
        if cmd == "fetch":
            spec = a[1] if len(a) > 1 else ""
            if "BODY.PEEK" in spec:                # 拉正文（uid 在第一个参数）
                uid = int(a[0])
                self.peeks.append(uid)
                return ("OK", [self.mails[uid]])
            if "FLAGS" in spec:                    # 轻量刷 FLAGS
                self.flag_pulls.append(spec)
                outs = []
                for u in [int(x) for x in a[0].split(",")]:
                    seen = "\\Seen" if u % 2 == 0 else ""
                    outs.append(f"{u} (UID {u} FLAGS ({seen}))".encode())
                return ("OK", outs)
        return ("NO", [b"unsupported"])

    def logout(self):
        return ("BYE", [b""])


acc_id = db.add_account("测试", "me@qq.com", "QQ", "imap.qq.com", 993,
                        "smtp.qq.com", 587)
import accounts as _acc
_acc.save_password("me@qq.com", "pw123456")   # 立即发送走真链路时取密码用（加密存）
conn = FakeIMAP([1, 2, 3])
got, fresh = P.fetch_folder(acc_id, conn, "INBOX", "INBOX", limit=120)
check("首次全量：3 封入库", got == 3 and len(conn.peeks) == 3, (got, conn.peeks))
check("  首次同步不触发提醒", fresh == [])

# 服务器上再进 2 封新邮件（一次出差攒 300 封也一样全拉）
conn.server_uids += [4, 5]
got2, fresh2 = P.fetch_folder(acc_id, conn, "INBOX", "INBOX", limit=120)
check("增量轮：只拉新到的 2 封（不重拉已有正文）",
      got2 == 2 and conn.peeks == [1, 2, 3, 4, 5], (got2, conn.peeks))
check("  新邮件进入提醒列表", len(fresh2) == 2 and all(f["folder"] == "INBOX" for f in fresh2))
check("  已入库邮件走了 FLAGS 轻量刷新（最近 120 封）", len(conn.flag_pulls) >= 1)
ids = db.existing_uids(acc_id, "INBOX")
check("  本地最终 5 封齐全（老版本 limit 截断会丢）", ids == {1, 2, 3, 4, 5}, ids)

# FLAGS 刷新真的落库：uid=4 是已读
row4 = db.get_conn().execute(
    "SELECT seen FROM emails WHERE account_id=? AND uid=4 AND folder='INBOX'",
    (acc_id,)).fetchone()
check("  已读状态按服务器对齐（uid4 已读）", row4 and row4["seen"] == 1, row4)

# ─────────────────────────────── /api/send 定时排队（API 层）
print("\n=== 4) /api/send 定时排队 ===")
import app as A
A.app.config["TESTING"] = True          # 500 时直接抛栈，别只给 HTML 错误页
client = A.app.test_client()
future = int(time.time()) + 3600
r = client.post("/api/send", data={
    "account_id": str(acc_id), "to": "dst@ex.com", "subject": "定时信",
    "body": "hi", "html": "1", "send_at": str(future)})
d = r.get_json()
check("未来时间 → 返回 scheduled:true", r.status_code == 200 and d.get("scheduled"), d)
check("  任务在队列里且未到点不发送",
      db.get_scheduled(d["id"]) and db.get_scheduled(d["id"])["status"] == "pending")
check("  计划时间正确入库", db.get_scheduled(d["id"])["send_at"] == future)

r2 = client.post("/api/send", data={
    "account_id": str(acc_id), "to": "dst@ex.com", "subject": "立即", "body": "x"})
check("不带 send_at → 立即发送（走桩 SMTP）",
      r2.status_code == 200 and not (r2.get_json() or {}).get("scheduled"),
      r2.get_data(as_text=True)[:200])

r3 = client.get("/api/scheduled")
items = r3.get_json()["items"]
check("定时队列 API 能列出", len(items) == 1 and items[0]["id"] == d["id"], items)

r4 = client.delete(f"/api/scheduled/{d['id']}")
check("取消接口", r4.status_code == 200 and not db.list_scheduled())

print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
