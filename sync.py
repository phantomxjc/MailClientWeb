# -*- coding: utf-8 -*-
"""后台同步（把桌面版的 QThread 换成普通线程 + 可轮询的状态对象）。

Web 端没有信号槽，前端靠轮询 /api/sync/status 拿进度，所以这里维护一份
加锁的状态字典，前端每次拉取都是一个快照。

**铁律：一个账号出问题，绝不能影响其它账号。** 为此做了三层保护：
  1. 每个账号一个独立子线程，父线程 join(SYNC_ACCOUNT_TIMEOUT) ——
     服务器卡死（微软限流时就是这样：不回包也不断开）也只是这一个账号超时跳过，
     后面排队的账号照常同步。这是「一个邮箱有问题，它后面的邮箱全都不更新」
     的根治办法；
  2. IMAP socket 设超时（见 config.IMAP_TIMEOUT），不让单条命令无限等；
  3. 每个账号、每个文件夹各自 try/except，失败原因记进 errors 交给界面显示。

每个账号的最终结果都会写进 state["accounts"]，界面据此在左侧账号行上标出
「这次没同步成功」的那个 —— 出问题时一眼就能看到是哪个邮箱。
"""
import threading
import time

import db
import notifier
from config import SYNC_ACCOUNT_TIMEOUT
from mail_conn import (CANON_KEYS, CANON_LABEL, MailAuthError, connect,
                       resolve_folders)
from parser import fetch_folder, repair_missing_dates

# 各文件夹的拉取上限：收件箱最多，其余适度
LIMITS = {"INBOX": 120, "Sent": 80, "Drafts": 50, "Trash": 50, "Spam": 50}

# 这些原因可能是临时的（微软限流 / 网络抖动），值得等一下再试一次
TRANSIENT_KINDS = {"imap_blocked", "network"}
RETRY_DELAY = 8
MAX_ATTEMPTS = 3

_MAX_MESSAGES = 40
_ERROR_MAX = 400            # 单个错误原因入库/下发的最大长度，别把界面撑爆


def _clip(text, limit=_ERROR_MAX):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


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
            "errors": [],                 # [{email, message, kind, action_url, partial}]
            "accounts": {},               # 账号 id(str) -> {email, ok, count, error…}
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
            snap["errors"] = [dict(e) for e in self._state["errors"]]
            snap["accounts"] = {k: dict(v) for k, v in self._state["accounts"].items()}
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

    # ------------------------------------------------------------ 连接
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

    # ------------------------------------------------------------ 单个账号
    def _sync_account(self, acc, bag, result):
        """在一个账号上把五个文件夹同步一遍。

        result 是外部传进来的字典，结果原地写入（超时被外层快照时不会互相干扰）；
        bag 收集本轮新到的邮件，由外层决定要不要交给提醒模块。
        本函数不向外抛异常 —— 外层是子线程，抛出去就没人接了。
        """
        result.update({"id": acc["id"], "email": acc["email"], "ok": False,
                       "partial": False, "count": 0, "folders": 0, "failed": 0,
                       "error": "", "kind": "generic", "action_url": ""})
        conn, err = self._connect_with_retry(acc)
        if err is not None:
            action_url = (err.action or ("", ""))[1]
            result.update(error=_clip(err), kind=err.kind,
                          action_url=action_url if str(action_url).startswith("http") else "")
            return

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
                    result["folders"] += 1
                    for it in fresh:
                        it["account"] = acc["email"]
                    bag.extend(fresh)
                except Exception as e:
                    result["failed"] += 1
                    if not result["error"]:
                        result["error"] = _clip(f"{CANON_LABEL[key]} 同步失败：{e}")
                    self._push(f"{acc['email']} · {CANON_LABEL[key]} 同步失败：{e}")
                # 顺手给「没有时间」的历史邮件补上服务器接收时间。
                # 单独 try：补时间失败绝不能影响正常收信。
                try:
                    fixed = repair_missing_dates(acc["id"], conn, key, name)
                    if fixed:
                        self._push(
                            f"{acc['email']} · {CANON_LABEL[key]} 补全 {fixed} 封邮件的时间")
                except Exception:
                    pass

            result["count"] = total
            result["ok"] = result["failed"] == 0
            # 部分文件夹失败：不算成功，但已经拉到的邮件照常入库（提醒也照发）
            result["partial"] = bool(result["failed"]) and bool(result["folders"])
        except Exception as e:
            result["ok"] = False
            result["partial"] = False
            if not result["error"]:
                result["error"] = _clip(f"同步出错：{e}")
            self._push(f"{acc['email']} 同步出错：{e}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _run_account(self, acc):
        """把账号同步放进受限子线程，返回 (结果, 新邮件列表)。

        超时就放弃这个线程（daemon，不会阻塞进程退出）、继续下一个账号。
        """
        result, bag = {}, []
        worker = threading.Thread(target=self._sync_account,
                                  args=(acc, bag, result),
                                  name=f"sync-acc-{acc['id']}", daemon=True)
        worker.start()
        worker.join(SYNC_ACCOUNT_TIMEOUT)

        if worker.is_alive():
            minutes = max(1, int(SYNC_ACCOUNT_TIMEOUT // 60))
            snapshot = dict(result)               # snapshot：僵尸线程后续写入不影响这份结果
            snapshot.update({
                "id": acc["id"], "email": acc["email"], "ok": False, "partial": False,
                "count": int(snapshot.get("count") or 0), "kind": "timeout",
                "action_url": "",
                "error": f"同步超过 {minutes} 分钟仍未完成，已跳过这个账号（服务器无响应"
                         f"或网络卡住），其它账号不受影响。可以稍后单独重试它。",
            })
            self._push(f"⚠ {acc['email']}：{snapshot['error']}")
            return snapshot, []                   # 超时不交提醒，避免半截数据误导

        result.setdefault("partial", False)
        return result, bag

    # ------------------------------------------------------------ 全量执行
    def _run(self, account_ids):
        try:
            accounts = db.get_accounts()
            if account_ids:
                accounts = [a for a in accounts if a["id"] in set(account_ids)]
            if not accounts:
                self._push("暂无账号，请先添加邮箱账号")
                return

            self._push(f"开始同步 {len(accounts)} 个账号（互不影响，单个失败会跳过）")
            # 本轮新到的邮件，攒到最后统一提醒（逐封还是合并由设置决定）
            pending = []
            failed = []
            for acc in accounts:
                self._push(f"正在同步 {acc['email']} …")
                res, bag = self._run_account(acc)
                res["email"] = res.get("email") or acc["email"]

                with self._lock:
                    self._state["accounts"][str(acc["id"])] = res

                if res.get("ok"):
                    pending.extend(bag)
                    self._push(f"✓ {res['email']} 同步完成，共 {res['count']} 封")
                    continue

                # 部分文件夹成功：邮件已经入库，提醒照发，只在界面上标个警告
                if res.get("partial"):
                    pending.extend(bag)
                failed.append(res)
                reason = _clip(res.get("error") or "未知原因", 200)
                self._push(f"⚠ {res['email']} {'部分文件夹未同步' if res.get('partial') else '未同步成功'}："
                           f"{reason}（已继续同步后面的账号）")
                with self._lock:
                    self._state["errors"].append({
                        "email": res["email"], "message": reason,
                        "kind": res.get("kind") or "generic",
                        "action_url": res.get("action_url") or "",
                        "partial": bool(res.get("partial")),
                    })

            ok_count = len(accounts) - len(failed)
            if failed:
                names = "、".join(f["email"] for f in failed[:4])
                if len(failed) > 4:
                    names += f" 等 {len(failed)} 个"
                self._push(f"全部同步完成：成功 {ok_count} 个，失败 {len(failed)} 个（{names}）")
            else:
                self._push(f"全部同步完成：{ok_count} 个账号均已更新")
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
    """定时自动同步。

    间隔可以在「设置 → 通用」里改（存 meta 表），所以这里不再睡一整个周期，
    而是每 30 秒醒一次看时间到没到 —— 改完设置立刻生效，不用重启。
    interval_minutes 传 0 表示默认关闭（启动时的兜底值）。
    """
    time.sleep(20)                                   # 先让 Web 起来，别抢启动时间
    last_started = 0.0
    while True:
        try:
            minutes = interval_minutes
            try:
                minutes = int(db.get_ui_settings().get("sync_interval", interval_minutes))
            except Exception:
                pass
            if (minutes and minutes > 0
                    and time.time() - last_started >= minutes * 60
                    and not sync_manager.is_running()
                    and db.get_accounts()):
                if sync_manager.start():
                    last_started = time.time()
        except Exception:
            pass
        time.sleep(30)
