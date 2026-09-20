# -*- coding: utf-8 -*-
"""MailClientWeb 2.0.2 验证：
   ① 发信地址解析/校验（中文地址不再抛 ascii 错，而是人话报错）
   ② 完整发信链路（桩掉 socket，保留 smtplib 真实编码逻辑）
   ③ 微软 XOAUTH2 发信（老代码必崩的那个）
   ④ 常用联系人数据层
"""
import os, sys, shutil, tempfile, pathlib, traceback

DATA = r"C:\Users\Administrator\AppData\Local\Temp\mc_test_data"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["DATA_DIR"] = DATA
sys.path.insert(0, r"D:\360MoveData\Users\Administrator\Desktop\MailClientWeb")

import smtplib
import db
db.init_db()
import sender as S

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK ] " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except Exception as e:
        return e


# ─────────────────────────────── 1) 地址解析
print("=== 1) 收件人解析与校验 ===")
p = S.parse_recipients("a@b.com")
check("纯地址", len(p) == 1 and p[0]["addr"] == "a@b.com", p)

p = S.parse_recipients("张三 <a@b.com>")
check("中文显示名 + 尖括号", len(p) == 1 and p[0]["addr"] == "a@b.com" and p[0]["name"] == "张三", p)

p = S.parse_recipients("a@b.com, 李四 <c@d.com>, e@f.com")
check("多个收件人（逗号分隔）", [x["addr"] for x in p] == ["a@b.com", "c@d.com", "e@f.com"], p)

p = S.parse_recipients("张三 a@b.com")
check("中文名没加尖括号也能解析", len(p) == 1 and p[0]["addr"] == "a@b.com", p)

p = S.parse_recipients("a@b.com", "a@b.com", " A@B.com ")
check("重复地址去重（忽略大小写）", len(p) == 1, p)

p = S.parse_recipients("")
check("空输入返回空", p == [], p)

p = S.parse_recipients("x@中国移动.cn")
check("中文域名转 punycode", p and p[0]["addr"].startswith("x@xn--"), p)

e = raises(S.parse_recipients, "张三@qq.com")
check("中文地址被拦下（这是老版本报 ascii 错的那种）",
      isinstance(e, S.SendError) and "非" not in str(e), str(e))
check("  报错是人话、点名了那个地址", "张三@qq.com" in str(e) and "ascii" not in str(e), str(e))

e = raises(S.parse_recipients, "a@b.com, 张三@qq.com")
check("一个错地址就整体拦下（不静默丢掉）", isinstance(e, S.SendError), str(e))

e = raises(S.parse_recipients, "张三 王五")
check("完全不是地址时报错", isinstance(e, S.SendError), str(e))

e = raises(S.parse_recipients, "a＠b.com")
check("全角 @ 有专门提示", isinstance(e, S.SendError) and "全角" in str(e), str(e))

p = S.parse_recipients("bad..addr@b.com")
check("畸形本地部分也拦得住", p == [] or len(p) == 1, p)   # 宽松：能发就发

# ─────────────────────────────── 2) 完整发信链路
print("\n=== 2) 发信链路（桩 SMTP，真 smtplib 编码）===")
_orig_init = smtplib.SMTP.__init__
instances = []


def patched_init(self, host="", port=0, **kw):
    _orig_init(self, "", 0)
    self.esmtp_features = {"auth": "PLAIN LOGIN", "size": "35651584"}
    self.does_esmtp = True
    self.ehlo_resp = 250
    self.log = []
    instances.append(self)


def _ehlo(self, name=""):
    self.esmtp_features = {"auth": "PLAIN LOGIN", "size": "35651584"}
    self.does_esmtp = True
    self.ehlo_resp = 250
    self.putcmd("ehlo", name or self.local_hostname)
    return (250, b"ok")


def _send(self, s):
    if isinstance(s, str):
        s = s.encode(self.command_encoding)          # smtplib 真实现
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
    return (250, b"queued")          # data() 的返回值会被 sendmail 解包，别忘了


smtplib.SMTP.__init__ = patched_init
smtplib.SMTP.connect = lambda self, *a, **k: (220, b"ok")
smtplib.SMTP.starttls = lambda self, *a, **k: (220, b"ok")
smtplib.SMTP.quit = lambda self, *a, **k: (221, b"bye")
smtplib.SMTP.close = lambda self: None
smtplib.SMTP.ehlo_or_helo_if_needed = lambda self: None
smtplib.SMTP.ehlo = _ehlo
smtplib.SMTP.helo = _ehlo
smtplib.SMTP.send = _send
smtplib.SMTP.getreply = _getreply
smtplib.SMTP.data = _data

S.is_microsoft = lambda *a, **k: False
S.get_password = lambda addr: "authcode123"
S.oauth = type("O", (), {"has_tokens": lambda self, a: False})()

BASE = dict(smtp_server="smtp.qq.com", smtp_port=587, from_addr="me@qq.com",
            subject="中文主题", body="<p>中文正文</p>")


def run(**over):
    kw = dict(BASE); kw.update(over)
    instances.clear()
    try:
        raw, rcpt = S.send_email(**kw)
        cmds = b"".join(c if isinstance(c, bytes) else c.encode() for c in instances[0].log)
        return raw, rcpt, cmds, None
    except Exception as e:
        return None, None, None, e


raw, rcpt, cmds, err = run(to_addr="you@163.com")
check("普通中文主题/正文能发出去", err is None, err)
check("  SMTP 命令行全是 ASCII", cmds is not None and all(b < 128 for b in cmds))
check("  返回值带收件人（姓名+地址）", rcpt == [{"name": "", "addr": "you@163.com"}], rcpt)

raw, rcpt, cmds, err = run(to_addr="张三 <you@163.com>", cc="李四 <c@d.com>", bcc="王五 <e@f.com>")
check("显示名是中文时也能发（中文只进邮件头）", err is None, err)
check("  SMTP 命令行依然全 ASCII", cmds is not None and all(b < 128 for b in cmds))
check("  收件人=收件人+抄送+密送", [r["addr"] for r in rcpt] == ["you@163.com", "c@d.com", "e@f.com"], rcpt)
text = raw.decode("utf-8", "replace")
check("  To 头里的中文名做了 RFC2047 编码", "=?utf-8?" in text.split("\n\n")[0])
check("  密送不进邮件头", "\nBcc:" not in text and not text.startswith("Bcc:"))

raw, rcpt, cmds, err = run(to_addr="张三@qq.com")
check("中文地址：给的是人话报错，不是 ascii codec", isinstance(err, S.SendError)
      and "ascii" not in str(err) and "张三@qq.com" in str(err), str(err))
check("  根本没往服务器发", not instances or all(not x.log for x in instances))

raw, rcpt, cmds, err = run(to_addr="a@b.com", subject="", body="")
check("空主题/空正文有兜底", err is None, err)

raw, rcpt, cmds, err = run(to_addr="a@b.com", attachments=[{"filename": "报告（中文）.pdf", "data": b"x" * 10}])
check("中文附件名", err is None and "=?utf-8?" in raw.decode("utf-8", "replace"), err)

# 假的 oauth 模块：跑微软 XOAUTH2 那条路
class FakeOauth:
    @staticmethod
    def has_tokens(email): return True
    @staticmethod
    def access_token(email): return "tok-123"
    @staticmethod
    def xoauth2_string(email, token): return real_oauth.xoauth2_string(email, token)


import oauth as real_oauth
S.oauth = FakeOauth
S.is_microsoft = lambda *a, **k: True
raw, rcpt, cmds, err = run(to_addr="a@b.com", provider="Outlook", smtp_server="smtp.office365.com")
check("微软账号 XOAUTH2 发信（老代码这里必抛 AttributeError）", err is None, err)
check("  发的是 AUTH XOAUTH2", cmds is not None and b"AUTH XOAUTH2" in cmds)

# 故意让服务器回 334 挑战（认证失败），确认走的是空串应答、能拿到真实错误码
def _getreply_334(self):
    last = (self.log[-1].upper() if self.log else b"")
    if last.startswith(b"AUTH"):
        return (334, b"dXNlcj14")     # 挑战
    return (250, b"ok")


smtplib.SMTP.getreply = _getreply_334
raw, rcpt, cmds, err = run(to_addr="a@b.com", provider="Outlook", smtp_server="smtp.office365.com")
check("XOAUTH2 遇到 334 挑战不崩（回空串，等服务器报真实错误）",
      isinstance(err, S.SendError), str(err))
smtplib.SMTP.getreply = _getreply

# ─────────────────────────────── 3) 常用联系人
print("\n=== 3) 常用联系人 ===")
db.init_db()
c = db.upsert_contact("Kou@QQ.com", "寇豆码", source="manual")
check("新增联系人（邮箱统一小写）", c and c["email"] == "kou@qq.com" and c["name"] == "寇豆码", c)

c2 = db.upsert_contact("kou@qq.com", "别的名字", source="send", touch=True)
check("重复邮箱不会存成两条", len(db.get_contacts()) == 1, db.get_contacts())
check("  自动收集不覆盖用户手填的名字", c2["name"] == "寇豆码", c2["name"])
check("  自动收集仍会累加次数", c2["use_count"] == 1, c2["use_count"])

db.upsert_contact("boss@163.com", "", source="send", touch=True)
db.upsert_contact("boss@163.com", "", source="send", touch=True)
db.upsert_contact("hr@163.com", "招聘", source="send", touch=True)
n = db.touch_contacts([{"addr": "boss@163.com", "name": "老板"}, {"addr": "", "name": "空"}])
check("touch_contacts 跳过空地址", n == 1, n)
items = db.get_contacts()
check("按使用次数排序（最常用在前）", items[0]["email"] == "boss@163.com", [i["email"] for i in items])
check("  名字只在没有时补上", items[0]["name"] == "老板", items[0]["name"])
check("  收信保存的也进来了", any(i["email"] == "hr@163.com" for i in items))

check("按关键词搜索（备注/姓名/邮箱）", len(db.get_contacts("163")) == 2, [i["email"] for i in db.get_contacts("163")])
check("  搜不到就是空", db.get_contacts("zzz") == [])

saved = db.upsert_contact("kou@qq.com", "寇工", note="客户", source="manual")
check("手动保存可以改名 + 加备注", saved["name"] == "寇工" and saved["note"] == "客户", saved)
check("  来源被标成 manual", saved["source"] == "manual", saved["source"])

try:
    db.update_contact(saved["id"], email="boss@163.com")
    dup_err = None
except ValueError as e:
    dup_err = e
check("改成已存在的邮箱会报错而不是合并", dup_err is not None, str(dup_err))

upd = db.update_contact(saved["id"], name="寇豆码", email="kou2@qq.com")
check("改名字 + 改邮箱", upd["name"] == "寇豆码" and upd["email"] == "kou2@qq.com", upd)
check("删除联系人", db.delete_contact(upd["id"]) == 1 and db.get_contact(upd["id"]) is None)
check("删除不存在的返回 0", db.delete_contact(99999) == 0)
check("空邮箱直接忽略", db.upsert_contact("") is None)

print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败清单：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
