# -*- coding: utf-8 -*-
"""后台同步（把桌面版的 QThread 换成普通线程 + 可轮询的状态对象）。

Web 端没有信号槽，前端靠轮询 /api/sync/status 拿进度，所以这里维护一份
加锁的状态字典，前端每次拉取都是一个快照。

一个账号连不上不能拖累其它账号，所以每个账号各自 try/except。
微软那句 "User is authenticated but not connected." 有时是临时限流，
所以对这类「可能稍后自愈」的错误自动重试。
"""
import threading
import time

import db
import notifier
from mail_conn import (CANON_KEYS, CANON_LABEL, MailAuthError, connect,
                       resolve_folders)
from parser import fetch_folder

# 各文件夹的拉取上限：收件箱最多，其余适度
LIMITS = {"INBOX": 120, "Sent": 80, "Drafts": 50, "Trash": 50, "Spam": 50}

# 这些原因可能是临时的（微软限流 / 网络抖动），值得等一下再试一次
TRANSIENT_KINDS = {"imap_blocked", "network"}
RETRY_DELAY = 8
MAX_ATTEMPTS = 3

_MAX_MESSAGES = 40


class SyncManager:
    """全局单例：同一时刻只允许一个同步在跑。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._next_allowed = 0.0          # 手动点太勤时的节流
        self._state = {
            "running": False,
            "stage": "idle",              # idle / running / done / error
            "messages": [],
            "errors": [],                 # [{email, message, kind, action_url}]
            "accounts": {},               # email -> 同步到的邮件数
            "started_at": None,
            "finished_at": None,
        }

    # ------------------------------------------------------------ 状态
    def _push(self, text):
        with self._lock:
            self._state["messages"].append({"t": time.time(), "text": text})
            if len(self._state["messages"]) > _MAX_MESSAGES:
                del self._state["messages"][:-_MAX_MESSAGES]

    def status(self):
        with self._lock:
            snap = dict(self._state)
            snap["messages"] = list(self._state["messages"])
            snap["errors"] = list(self._state["errors"])
            snap["accounts"] = dict(self._state["accounts"])
            snap["stats"] = db.get_stats()
            return snap

    def is_running(self):
        with self._lock:
            return self._state["running"]

    # ------------------------------------------------------------ 触发
    def start(self, account_ids=None, throttle=0):
        """启动一次同步；已有同步在跑则返回 False。"""
        with self._lock:
            if self._state["running"]:
                return False
            now = time.time()
            if throttle and now < self._next_allowed:
                return False
            self._next_allowed = now + throttle
            self._state.update(running=True, stage="running", messages=[],
                               errors=[], accounts={},
                               started_at=now, finished_at=None)
        self._thread = threading.Thread(target=self._run, args=(account_ids,),
                                        name="sync", daemon=True)
        self._thread.start()
        return True

    # ------------------------------------------------------------ 执行
    def _connect_with_retry(self, acc):
        """返回 (conn, err)；err 为 None 表示成功。"""
        err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return connect(acc), None
            except MailAuthError as e:
                err = e
            except Exception as e:
                err = MailAuthError(f"连接失败：{e}", "generic", None)
            if err.kind in TRANSIENT_KINDS and attempt < MAX_ATTEMPTS:
                self._push(f"{acc['email']}：连接未成功（第 {attempt} 次，{err.kind}），"
                           f"{RETRY_DELAY} 秒后自动重试…")
                time.sleep(RETRY_DELAY)
                continue
            break
        return None, err

    def _run(self, account_ids):
        try:
            accounts = db.get_accounts()
            if account_ids:
                accounts = [a for a in accounts if a["id"] in set(account_ids)]
            if not accounts:
                self._push("暂无账号，请先添加邮箱账号")
                return

            # 本轮新到的邮件，攒到最后统一提醒（逐封还是合并由设置决定）
            pending = []
            for acc in accounts:
                self._push(f"正在同步 {acc['email']} …")
                conn, err = self._connect_with_retry(acc)
                if err is not None:
                    action_url = (err.action or ("", ""))[1]
                    if not str(action_url).startswith("http"):
                        action_url = ""
                    self._push(f"{acc['email']}：{err}")
                    with self._lock:
                        self._state["errors"].append({
                            "email": acc["email"], "message": str(err),
                            "kind": err.kind, "action_url": action_url})
                    continue
                if conn is None:
                    continue

                try:
                    try:
                        folders = resolve_folders(conn)
                    except Exception:
                        folders = {}
                    if "INBOX" not in folders:
                        folders["INBOX"] = "INBOX"
                    db.save_folders(acc["id"], folders)

                    total = 0
                    for key in CANON_KEYS:
                        name = folders.get(key)
                        if not name:
                            continue
                        self._push(f"{acc['email']} · 同步{CANON_LABEL[key]} …")
                        try:
                            got, fresh = fetch_folder(acc["id"], conn, key, name,
                                                      LIMITS.get(key, 50))
                            total += got
                            for it in fresh:
                                it["account"] = acc["email"]
                            pending.extend(fresh)
                        except Exception as e:
                            self._push(f"{acc['email']} · {CANON_LABEL[key]} 同步失败：{e}")
                    with self._lock:
                        self._state["accounts"][acc["email"]] = total
                    self._push(f"{acc['email']} 同步完成，共 {total} 封")
                except Exception as e:
                    self._push(f"{acc['email']} 同步出错：{e}")
                    with self._lock:
                        self._state["errors"].append({
                            "email": acc["email"], "message": f"同步出错：{e}",
                            "kind": "generic", "action_url": ""})
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass

            self._push("全部同步完成")
            self._notify(pending)
        except Exception as e:                                  # 兜底，别让线程静默死掉
            self._push(f"同步线程异常：{e}")
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["stage"] = "done"
                self._state["finished_at"] = time.time()

    def _notify(self, pending):
        """把本轮新邮件交给提醒模块。提醒出任何问题都不能影响同步本身。"""
        try:
            if not pending:
                return
            cfg = notifier.get_settings()
            if not cfg.get("notify_spam"):
                pending = [p for p in pending if p.get("folder") != "Spam"]
            if not pending:
                return
            sent = notifier.notify_new_mails(pending)
            if sent:
                self._push(f"已推送 {sent} 条新邮件提醒")
        except Exception as e:
            self._push(f"提醒推送失败：{e}")


sync_manager = SyncManager()


def background_loop(interval_minutes):
    """定时自动同步（0 表示关闭）。"""
    if interval_minutes <= 0:
        return
    time.sleep(20)                                   # 先让 Web 起来，别抢启动时间
    while True:
        try:
            if db.get_accounts() and not sync_manager.is_running():
                sync_manager.start()
        except Exception:
            pass
        time.sleep(max(interval_minutes, 1) * 60)
