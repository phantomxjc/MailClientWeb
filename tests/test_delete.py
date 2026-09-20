# -*- coding: utf-8 -*-
"""删除 / 批量删除 的后端逻辑自测（不连真实 IMAP）。

覆盖：
1. db.get_email_targets / db.delete_emails 直接删库；
2. app._delete_emails 的「先回服务器、再删本地」编排，
   以及服务器连接失败时不破坏本地数据（记 failed）。
"""
import os
import sys
import tempfile
import importlib

# 用一个干净的临时数据目录，避免动到真实库
_TMP = tempfile.mkdtemp(prefix="mc_del_")
os.environ["DATA_DIR"] = _TMP
os.environ["AUTH_DISABLED"] = "1"

# 必须在导入 app/db 之前把 DATA_DIR 设好（config 在 import 时读）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import app as appmod
import mail_conn

db.init_db()

# 保存真实实现：后面几节会把 mail_conn.delete_messages 换成桩，第 6 节要用回真身
_REAL_DELETE_MESSAGES = mail_conn.delete_messages


def check(name, cond, extra=""):
    print(("  ok  " if cond else " FAIL ") + name + (("  → " + str(extra)) if extra and not cond else ""))
    if not cond:
        global _FAILED
        _FAILED += 1


_FAILED = 0
n = 0


def _seed():
    """插一个账号 + 3 封邮件（含附件），返回 (account_id, [email_id...])。"""
    db.get_conn().execute("DELETE FROM emails")
    db.get_conn().execute("DELETE FROM accounts")
    db.get_conn().execute("DELETE FROM folders")
    db.get_conn().commit()
    cur = db.get_conn().execute(
        "INSERT INTO accounts (name, email, imap_server, imap_port) "
        "VALUES ('t','t@x.com','imap.x.com',993)")
    acc_id = cur.lastrowid
    db.get_conn().execute(
        "INSERT INTO folders (account_id, key, imap_name) VALUES (?,?,?)",
        (acc_id, "INBOX", "INBOX"))
    db.get_conn().execute(
        "INSERT INTO folders (account_id, key, imap_name) VALUES (?,?,?)",
        (acc_id, "Trash", "Trash"))
    ids = []
    for i in range(3):
        cur = db.get_conn().execute(
            "INSERT INTO emails (account_id, folder, uid, msg_from, subject, seen) "
            "VALUES (?,?,?,?,?,?)", (acc_id, "INBOX", 100 + i, "a@b.com", f"m{i}", 0))
        eid = cur.lastrowid
        db.get_conn().execute(
            "INSERT INTO attachments (email_id, filename, content_type, data) "
            "VALUES (?,?,?,?)", (eid, "f.txt", "text/plain", b"x"))
        ids.append(eid)
    db.get_conn().commit()
    return acc_id, ids


# ── 1. db 层直接删 ──
acc_id, ids = _seed()
t = db.get_email_targets(ids)
check("get_email_targets 返回 3 行且字段齐全",
      len(t) == 3 and all({"id", "account_id", "folder", "uid"} <= set(r) for r in t))
deleted = db.delete_emails(ids)
check("delete_emails 删掉 3 封", deleted == 3)
left = db.get_conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0]
atch = db.get_conn().execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
check("删邮件时附件外键一并清掉", left == 0 and atch == 0, f"emails={left} att={atch}")

# ── 2. app._delete_emails：服务器成功 ──
acc_id, ids = _seed()
captured = []
mail_conn.delete_messages = lambda acc, targets: (captured.extend(targets) or ([], ""))
res = appmod._delete_emails(ids)
check("服务器删除被调用且带 (folder,uid)",
      len(captured) == 3 and all(isinstance(x, tuple) and len(x) == 2 for x in captured))
check("_delete_emails 返回 deleted=3 failed=0",
      res.get("deleted") == 3 and res.get("failed") == 0, res)
left = db.get_conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0]
check("成功后本地也删光", left == 0, left)

# ── 3. 服务器连接失败：本地不动，记 failed ──
acc_id, ids = _seed()
def _boom(acc, targets):
    raise RuntimeError("连不上服务器")
mail_conn.delete_messages = _boom
res = appmod._delete_emails(ids)
check("连接失败时 failed=3、本地未删",
      res.get("failed") == 3 and db.get_conn().execute(
          "SELECT COUNT(*) FROM emails").fetchone()[0] == 3, res)

# ── 4. 空列表安全 ──
check("空 id 列表不报错",
      appmod._delete_emails([]) == {"deleted": 0, "failed": 0, "reason": ""})

# ── 5. 部分失败：单封 UID 在服务器删不动，只有成功的才删本地 ──
acc_id, ids = _seed()
def _partial(acc, targets):
    # 第 2 封（index 1）在服务器删不动，其余成功
    return [(targets[1][0], targets[1][1])], ""
mail_conn.delete_messages = _partial
res = appmod._delete_emails([ids[0], ids[1], ids[2]])
check("部分失败：failed=1、本地只删成功的 2 封",
      res.get("failed") == 1 and res.get("deleted") == 2, res)
left = db.get_conn().execute(
    "SELECT COUNT(*) FROM emails WHERE id=?", (ids[1],)).fetchone()[0]
check("删不动的那封仍留在本地列表", left == 1, left)

# ── 6. 163 场景：服务器不支持 MOVE（uid('move') 抛 BAD）→ 必须落兜底且算成功 ──
# 真实复现：163/Coremail 对 MOVE 回 BAD，imaplib 抛 IMAP4.error；
# 旧代码直接把整封记 failed，界面误报「账号连接失败」。
import imaplib
acc_id, ids = _seed()

class _FakeConn:
    """最小 IMAP 连接桩：move 抛异常，copy/store/expunge 正常应答。"""
    def __init__(self, move_raises=True, copy_raises=False):
        self.calls = []
        self.move_raises = move_raises
        self.copy_raises = copy_raises
    def select(self, mbx, readonly=False):
        self.calls.append(("select", mbx)); return "OK", [b""]
    def uid(self, cmd, *args):
        self.calls.append((cmd,) + args)
        if cmd == "move" and self.move_raises:
            raise imaplib.IMAP4.error("MOVE command error: BAD ['unknown command']")
        if cmd == "copy" and self.copy_raises:
            raise imaplib.IMAP4.error("COPY failed: over quota")
        return "OK", [b""]
    def expunge(self):
        self.calls.append(("expunge",)); return "OK", [b""]

acc_obj = {"id": acc_id, "email": "t@x.com", "imap_server": "imap.x.com", "imap_port": 993}
fc = _FakeConn()
mail_conn.connect = lambda a, timeout=None: fc
miss, errs = _REAL_DELETE_MESSAGES(acc_obj, [("INBOX", 100)])
check("MOVE 抛异常（163）时兜底成功、不记失败", miss == [] and errs == [], (miss, errs))
called = [c[0] for c in fc.calls]
check("兜底真的执行了 copy + store + expunge",
      "copy" in called and "store" in called and "expunge" in called, called)
check("源文件夹被重新选中（非只读）",
      any(c[0] == "select" and c[1] == "INBOX" for c in fc.calls), fc.calls)

# 6b. 连兜底都失败（如超配额）→ 记 failed 并带原因
fc2 = _FakeConn(copy_raises=True)
mail_conn.connect = lambda a, timeout=None: fc2
miss2, errs2 = _REAL_DELETE_MESSAGES(acc_obj, [("INBOX", 100)])
check("兜底也失败时记 failed 且带原因", len(miss2) == 1 and errs2 and "兜底" in errs2[0][2], errs2)

print("\n=== test_delete:", "全部通过" if _FAILED == 0 else f"{_FAILED} 项失败", "===")
sys.exit(1 if _FAILED else 0)
