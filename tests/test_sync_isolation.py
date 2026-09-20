# -*- coding: utf-8 -*-
"""StardustMail 2.0.7 同步隔离验证：

核心回归：一个邮箱出问题（报错 / 卡死），绝不能拖垮后面的账号。

覆盖三种失败形态：
  ① 普通报错（MailAuthError，非瞬时）—— 记入 accounts[id].ok=False，后续照常
  ② 微软式「卡死」（connect 不回包也不断开）—— 看门狗 join(SYNC_ACCOUNT_TIMEOUT)
     超时后只跳过这个账号，后面的账号照常同步（这是 2.0.6 之前「一个坏、全不动」
     的根治点）
  ③ 部分文件夹失败（INBOX 成功、其它失败）—— ok=False 但 partial=True，
     已拉到的邮件入库、提醒照发

全程不打真实网络：connect / fetch_folder / resolve_folders / db.* 全部桩掉。
"""
import os, sys, shutil, time

DATA = r"C:\Users\Administrator\AppData\Local\Temp\mc_test_sync"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["DATA_DIR"] = DATA
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # 项目根（不写死盘符，改目录也不怕）

import db
db.init_db()
import sync
import mail_conn

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK ] " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))


def reset_state():
    sync.sync_manager._state["accounts"] = {}
    sync.sync_manager._state["errors"] = []


# ───────────────────── 准备：桩掉所有会碰网络 / 磁盘的东西
class FakeConn:
    """假装连上了的 IMAP 连接；logout 必须存在否则 finally 会报错。"""
    def logout(self):
        return ("BYE", b"logged out")


def patch_all(success_ids):
    """success_ids：哪些账号 id 的 connect 会成功返回 FakeConn，其余视为「坏账号」。"""
    # 真实 db 的 get_accounts 被替换成测试数据
    fake_accounts = [
        {"id": 1, "email": "phantomxujc@outlook.com"},   # 坏账号（演示里的那个）
        {"id": 2, "email": "phantomxjc@qq.com"},          # 好账号
        {"id": 3, "email": "a@b.com"},                    # 好账号
    ]

    def fake_get_accounts():
        return fake_accounts

    def fake_connect(acc):
        if acc["id"] in success_ids:
            return FakeConn()                 # 连上
        raise mail_conn.MailAuthError(
            f"账号 {acc['email']} 鉴权失败（演示）", "generic", None)

    sync.connect = fake_connect
    sync.resolve_folders = lambda conn: {"INBOX": "INBOX", "Sent": "Sent"}
    sync.fetch_folder = lambda *a, **k: (0, [])     # 没新邮件，纯验证控制流
    sync.repair_missing_dates = lambda *a, **k: 0
    sync.db.get_accounts = fake_get_accounts
    sync.db.save_folders = lambda *a, **k: None
    sync.db.get_stats = lambda: {}
    sync.notifier.get_settings = lambda: {"notify_spam": False}
    sync.notifier.notify_new_mails = lambda *a, **k: 0
    return fake_accounts


print("=== ① 一个账号报错，后续账号不受影响 ===")
reset_state()
patch_all(success_ids={2, 3})
sync.sync_manager._run([1, 2, 3])
st = sync.sync_manager._state
check("坏账号 #1 被记成失败", st["accounts"].get("1", {}).get("ok") is False,
      st["accounts"].get("1", {}).get("error", "")[:40])
check("好账号 #2 同步成功", st["accounts"].get("2", {}).get("ok") is True)
check("好账号 #3 同步成功", st["accounts"].get("3", {}).get("ok") is True)
check("失败时界面能拿到失败原因", bool(st["accounts"].get("1", {}).get("error")))
check("errors 列表只收失败账号（1 条）", len(st["errors"]) == 1,
      f"{len(st['errors'])} 条")
check("坏账号排在第一个，后面账号依然更新（根治点）",
      st["accounts"].get("2", {}).get("ok") and st["accounts"].get("3", {}).get("ok"))

print("=== ② 微软式卡死：connect 永远不返回，看门狗必须跳过它 ===")
reset_state()
# 把看门狗超时缩到 0.2s，让测试秒级跑完（真实环境是 300s）
sync.SYNC_ACCOUNT_TIMEOUT = 0.2
block_ids = {2, 3}


def fake_connect_block(acc):
    if acc["id"] in block_ids:
        return FakeConn()
    # 坏账号 #1：模拟服务器不回包也不断开 —— 直接睡死
    time.sleep(60)
    return FakeConn()


def fake_get_accounts2():
    return [
        {"id": 1, "email": "phantomxujc@outlook.com"},
        {"id": 2, "email": "phantomxjc@qq.com"},
        {"id": 3, "email": "a@b.com"},
    ]


sync.connect = fake_connect_block
sync.db.get_accounts = fake_get_accounts2
t0 = time.time()
sync.sync_manager._run([1, 2, 3])
elapsed = time.time() - t0
st = sync.sync_manager._state
check("坏账号 #1 被判为 timeout（kind=timeout）",
      st["accounts"].get("1", {}).get("kind") == "timeout",
      st["accounts"].get("1", {}).get("kind"))
check("坏账号 #1 标记失败", st["accounts"].get("1", {}).get("ok") is False)
check("好账号 #2 在卡死后仍同步成功", st["accounts"].get("2", {}).get("ok") is True)
check("好账号 #3 在卡死后仍同步成功", st["accounts"].get("3", {}).get("ok") is True)
# 关键：整轮不能等满 60s，看门狗要在超时值附近就放行
check("看门狗及时放行（远小于 60s）", elapsed < 30, f"{elapsed:.1f}s")
sync.SYNC_ACCOUNT_TIMEOUT = 300   # 还原，免得影响别的

print("=== ③ 部分文件夹失败：INBOX 成功、其它失败 → partial ===")
reset_state()


def fake_fetch_partial(acc_id, conn, key, name, limit):
    if key == "INBOX":
        return (5, [{"uid": "1", "subject": "新邮件"}])   # INBOX 成功拉到 5 封
    raise RuntimeError("模拟 Spam 同步失败")               # 其它文件夹失败


sync.SYNC_ACCOUNT_TIMEOUT = 300
sync.connect = lambda acc: FakeConn()
sync.fetch_folder = fake_fetch_partial
sync.resolve_folders = lambda conn: {"INBOX": "INBOX", "Sent": "Sent", "Spam": "Spam"}
sync.db.get_accounts = lambda: [{"id": 9, "email": "partial@test.com"}]
sync.sync_manager._run([9])
st = sync.sync_manager._state
acc9 = st["accounts"].get("9", {})
check("partial 账号 ok=False", acc9.get("ok") is False)
check("partial 账号 partial=True", acc9.get("partial") is True)
check("partial 账号已拉到邮件（count>0）", int(acc9.get("count") or 0) > 0,
      f"count={acc9.get('count')}")
check("partial 仍进入 errors 列表", any(e["email"] == "partial@test.com" for e in st["errors"]))

print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("✓ 同步隔离验证全部通过")
