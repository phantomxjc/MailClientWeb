# -*- coding: utf-8 -*-
"""星尘邮箱（Stardust）—— Flask 主程序。

桌面版（PySide6）的 Web 化版本：同一套 IMAP/OAuth 协议层，界面换成浏览器。
路由分三类：
  1. 页面：/（三栏主界面）、/login；
  2. 数据接口：/api/bootstrap、/api/emails…；
  3. 动作接口：/api/sync、/api/send、/api/oauth/*。
"""
import html
import io
import os
import secrets
import threading
import time

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

import accounts
import auth
import db
import msauth
import notifier
import oauth
from config import (APP_VERSION, AUTH_DISABLED, DATA_DIR, DOMAIN_PROVIDER,
                    PROVIDERS, SYNC_INTERVAL_MINUTES, provider_of)
from mail_conn import (CANON_LABEL, CANON_KEYS, MailAuthError, connect,
                       quote_mailbox, resolve_folders)
from sender import SendError, send_email
from sync import background_loop, sync_manager

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024      # 附件上限 32MB
app.config["JSON_AS_ASCII"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0              # 静态文件强制回源校验


def _asset_version(rel):
    """静态资源指纹：取文件 mtime 拼在 ?v= 上。

    只靠 Cache-Control 不够——浏览器可能仍在用旧副本（尤其是标签页一直开着，
    或中间隔着反向代理）。带上 mtime 后，文件一改 URL 就变，缓存必然失效。
    """
    try:
        return str(int(os.path.getmtime(os.path.join(app.static_folder, rel))))
    except OSError:
        return APP_VERSION


def _build_id():
    """构建标识：取 static/ 与 templates/ 里最新的改动时间，展示在界面上。"""
    newest = 0.0
    base = os.path.dirname(os.path.abspath(__file__))
    for root in (app.static_folder, os.path.join(base, "templates")):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    return time.strftime("%m%d-%H%M", time.localtime(newest)) if newest else APP_VERSION


BUILD_ID = _build_id()
app.jinja_env.globals["static_v"] = _asset_version


@app.after_request
def _no_store_html(resp):
    """HTML 页面一律不缓存。

    只给静态资源加指纹是不够的：页面本身（/、/login）如果被浏览器缓存，
    它引用的还是旧的 ?v= 地址，标签页就会一直跑旧脚本、旧样式——表现为
    「代码明明改了，界面纹丝不动」。页面不缓存后，只要刷新一次，指纹必然是最新的。

    邮件正文（/api/emails/<id>/html）走 iframe，别加 no-store，免得每次重取闪一下。
    """
    if (resp.headers.get("Content-Type", "").startswith("text/html")
            and not request.path.startswith("/api/")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def _load_secret_key():
    """会话签名密钥落盘保存，重启后已登录的浏览器不掉线。"""
    path = os.path.join(DATA_DIR, "flask_secret.key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read().strip()
        if data:
            return data
    data = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(data)
    return data


app.secret_key = _load_secret_key()

_FOLDER_ORDER = [(k, CANON_LABEL[k]) for k in CANON_KEYS]


# ---------------------------------------------------------------- 访问控制
_OPEN_ENDPOINTS = {"login", "static", "healthz"}


@app.before_request
def _gate():
    """登录门禁：没登录只能看登录页。

    账号体系见 auth.py（初始 admin / admin123，登录后可改密码）。
    仅本机调试时设 AUTH_DISABLED=1 可整体关掉校验。
    """
    if AUTH_DISABLED:
        return None
    if request.endpoint in _OPEN_ENDPOINTS or request.path.startswith("/static/"):
        return None
    if session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录，请先访问 /login 登录"}), 401
    return redirect(url_for("login", next=request.path))


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": APP_VERSION})


@app.route("/login", methods=["GET", "POST"])
def login():
    if AUTH_DISABLED:
        return redirect(url_for("index"))
    error = ""
    username = (request.form.get("username") or "").strip()
    if request.method == "POST":
        user, error = auth.verify(username, request.form.get("password") or "")
        if user:
            session.permanent = True
            session["user"] = user["username"]
            session["uid"] = user["id"]
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")            # 只允许站内跳转，防开放重定向
            return redirect(nxt)
        time.sleep(0.8)                            # 轻微延时，挡一下暴力尝试
    return render_template("login.html", error=error, username=username,
                           version=APP_VERSION, build=BUILD_ID,
                           default_hint=auth.default_password_in_use())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/me")
def api_me():
    return jsonify({"user": auth.public_user(session.get("user"))})


@app.route("/api/password", methods=["POST"])
def api_change_password():
    if AUTH_DISABLED:
        return jsonify({"error": "当前为免登录调试模式，未启用账号体系"}), 400
    data = request.json or {}
    ok, message = auth.change_password(session.get("user"), data.get("old") or "",
                                       data.get("new") or "")
    if not ok:
        return jsonify({"error": message}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 新邮件提醒
@app.route("/api/notify")
def api_notify_get():
    """返回当前设置 + 通道清单。通道表单由后端字段定义驱动，加通道不用改前端。"""
    return jsonify({"settings": notifier.get_settings(),
                    "channels": notifier.CHANNELS})


@app.route("/api/notify", methods=["POST"])
def api_notify_save():
    return jsonify({"ok": True, "settings": notifier.save_settings(request.json or {})})


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """用界面上当前填的值试发一条（不要求先保存）。"""
    ok, message = notifier.send_test(request.json or {})
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


# ---------------------------------------------------------------- 界面设置
@app.route("/api/settings")
def api_settings_get():
    """设置弹窗的「通用」页：未读置顶、未读标红、自动同步间隔。"""
    return jsonify({"ui": db.get_ui_settings(),
                    "version": APP_VERSION, "build": BUILD_ID})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    ui = db.set_ui_settings(request.json or {})
    return jsonify({"ok": True, "ui": ui})


@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION, build=BUILD_ID,
                           username=session.get("user") or "",
                           auth_disabled=AUTH_DISABLED,
                           sync_interval=SYNC_INTERVAL_MINUTES)


# ---------------------------------------------------------------- 常用联系人
@app.route("/api/contacts")
def api_contacts():
    """列表接口。带 q 时按邮箱/姓名/备注模糊搜（写信弹窗的挑选框就用它）。"""
    q = request.args.get("q", "").strip()
    return jsonify({"items": db.get_contacts(q)})


@app.route("/api/contacts", methods=["POST"])
def api_contact_save():
    """新增或覆盖保存。同一个邮箱重复保存就是改名字，不会存成两条。"""
    data = request.json or {}
    email = (data.get("email") or "").strip()
    if "@" not in email:
        return jsonify({"error": "请填写正确的邮箱地址"}), 400
    c = db.upsert_contact(email, data.get("name") or "",
                          note=data.get("note"), source="manual")
    return jsonify({"ok": True, "contact": c})


@app.route("/api/contacts/<int:contact_id>", methods=["PUT", "DELETE"])
def api_contact_item(contact_id):
    if request.method == "DELETE":
        if not db.delete_contact(contact_id):
            return jsonify({"error": "联系人不存在"}), 404
        return jsonify({"ok": True})
    data = request.json or {}
    try:
        c = db.update_contact(contact_id,
                              name=data.get("name"),
                              note=data.get("note"),
                              email=data.get("email"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not c:
        return jsonify({"error": "联系人不存在"}), 404
    return jsonify({"ok": True, "contact": c})


# ---------------------------------------------------------------- 基础数据
def _account_payload():
    out = []
    for acc in db.get_accounts():
        item = dict(acc)
        item["is_microsoft"] = bool(provider_of(acc["provider"]).get("oauth") == "microsoft")
        item["has_secret"] = accounts.has_password(acc["email"]) or oauth.has_tokens(acc["email"])
        item["counts"] = db.folder_counts(acc["id"])
        item["unread"] = sum(v["unread"] for v in item["counts"].values())
        out.append(item)
    return out


@app.route("/api/bootstrap")
def api_bootstrap():
    return jsonify({
        "version": APP_VERSION,
        "build": BUILD_ID,
        "user": auth.public_user(session.get("user")),
        "auth_disabled": AUTH_DISABLED,
        "accounts": _account_payload(),
        "folders": [{"key": k, "label": v} for k, v in _FOLDER_ORDER],
        "providers": [{"key": k, "label": v["label"], "oauth": v["oauth"],
                       "auth_note": v["auth_note"],
                       "imap": v["imap"], "imap_port": v["imap_port"],
                       "smtp": v["smtp"], "smtp_port": v["smtp_port"]}
                      for k, v in PROVIDERS.items()],
        "domain_provider": DOMAIN_PROVIDER,
        "stats": db.get_stats(),
        "sync": sync_manager.status(),
        "sync_interval": SYNC_INTERVAL_MINUTES,
        "ui": db.get_ui_settings(),
    })


# ---------------------------------------------------------------- 邮件
@app.route("/api/emails")
def api_emails():
    account = request.args.get("account", "").strip()
    folder = request.args.get("folder", "INBOX").strip() or "INBOX"
    keyword = request.args.get("q", "").strip()
    unread_only = request.args.get("unread") == "1"
    account_id = int(account) if account.isdigit() else None
    items = db.get_emails(account_id, folder, keyword,
                          limit=200, unread_only=unread_only)
    # 列表里也把发件人拆成「姓名 / 地址」，前端渲染更干净（不用显示尖括号那串）
    for it in items:
        it["from_name"], it["from_addr"] = _split_from(it.get("msg_from") or "")
    return jsonify({
        "items": items,
        "folder": folder,
        "folder_label": CANON_LABEL.get(folder, folder),
    })


@app.route("/api/version")
def api_version():
    """只回构建号：前端定时对一下，发现服务端换新版了就提示刷新。"""
    return jsonify({"version": APP_VERSION, "build": BUILD_ID})


@app.route("/api/emails/<int:email_id>")
def api_email_detail(email_id):
    row = db.get_email(email_id)
    if not row:
        abort(404)
    if not row.get("seen"):
        # 打开即已读；服务端标记放在后台做，别让页面等网络
        db.mark_seen(email_id, 1)
        row["seen"] = 1
        threading.Thread(target=_push_seen_to_server, args=(row, True),
                         daemon=True).start()
    row["has_html"] = bool((row.get("body_html") or "").strip())
    row.pop("body_html", None)                        # 正文走 /html 接口，便于沙箱渲染
    row["attachments"] = db.get_attachments(email_id)
    row["from_name"], row["from_addr"] = _split_from(row.get("msg_from") or "")
    return jsonify(row)


@app.route("/api/emails/<int:email_id>/html")
def api_email_html(email_id):
    """正文 HTML 单独出接口：用 CSP sandbox 回应，邮件里的脚本一律不执行。"""
    row = db.get_email(email_id)
    if not row:
        abort(404)
    body = row.get("body_html") or ""
    if not body.strip():
        text = html.escape(row.get("body_text") or "（无正文）")
        body = (f'<pre style="white-space:pre-wrap;word-break:break-word;'
                f'font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                f'font-size:15px;line-height:1.85;color:#1e293b;margin:0;">{text}</pre>')
    resp = Response(body, mimetype="text/html; charset=utf-8")
    # 只允许弹窗（点链接用），脚本/表单/同源一律封掉
    resp.headers["Content-Security-Policy"] = "sandbox allow-popups allow-popups-to-escape-sandbox"
    return resp


@app.route("/api/emails/<int:email_id>/seen", methods=["POST"])
def api_email_seen(email_id):
    row = db.get_email(email_id)
    if not row:
        abort(404)
    seen = 0 if (request.json or {}).get("seen") in (0, False, "0") else 1
    db.mark_seen(email_id, seen)
    threading.Thread(target=_push_seen_to_server, args=(row, bool(seen)),
                     daemon=True).start()
    return jsonify({"ok": True, "seen": seen})


def _delete_emails(ids):
    """真删一批邮件：先回服务器（按账号分组，移入「已删除」或彻底清除），再删本地库。

    返回 {"deleted": 成功删本地的封数, "failed": 服务器没删成的封数}。
    一条重要原则：**只删服务器确实删成功的本地记录**。连接/鉴权失败或某封在服务器
    删不动时，这封邮件留在本地列表里（界面不丢、用户可重试），只记进 failed ——
    避免「界面上没了、服务器里还在」，下次同步又冒回来、让人以为没删掉。
    """
    targets = db.get_email_targets(ids)
    if not targets:
        return {"deleted": 0, "failed": 0}

    # 按账号分组，每条带 email id 以便回写
    by_account = {}
    for t in targets:
        by_account.setdefault(t["account_id"], []).append(t)

    ok_ids = []        # 服务器侧确认删成的 email id
    failed = 0
    for account_id, rows in by_account.items():
        acc = db.get_account(account_id)
        if not acc:
            failed += len(rows)
            continue
        try:
            from mail_conn import delete_messages
            miss, _ = delete_messages(acc, [(r["folder"], r["uid"]) for r in rows])
            # miss 是 (folder, uid) 列表，反查出对应的 email id
            miss_keys = set(miss)
            for r in rows:
                if (r["folder"], r["uid"]) in miss_keys:
                    failed += 1
                else:
                    ok_ids.append(r["id"])
        except Exception:
            # 连接/鉴权失败：整批服务器删不动，本地也暂不删，避免「界面没了但服务器还在」
            failed += len(rows)
            continue

    deleted = db.delete_emails(ok_ids) if ok_ids else 0
    return {"deleted": deleted, "failed": failed}


@app.route("/api/emails/<int:email_id>", methods=["DELETE"])
def api_delete_email(email_id):
    """删除单封邮件：移入服务器「已删除」文件夹；若它已在「已删除/垃圾」里则彻底删除。"""
    row = db.get_email(email_id)
    if not row:
        abort(404)
    res = _delete_emails([email_id])
    return jsonify({"ok": True, **res})


@app.route("/api/emails/batch-delete", methods=["POST"])
def api_batch_delete_emails():
    """批量删除：body = {"ids": [id, ...]}。"""
    ids = (request.json or {}).get("ids") or []
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return jsonify({"ok": False, "error": "没有要删除的邮件"}), 400
    res = _delete_emails(ids)
    return jsonify({"ok": True, **res})


@app.route("/api/emails/mark-all-read", methods=["POST"])
def api_mark_all_read():
    """批量标为已读。

    不传 `folder` = 该账号下**所有**文件夹（含垃圾邮件）；
    传 `folder`   = 只标那一个。

    「全部已读」按钮走的是前者。原因：左侧账号角标统计的是该账号**所有文件夹**
    的未读合计，按钮若只标当前文件夹，角标就不会归零，看着像没生效。
    """
    payload = request.json or {}
    account = payload.get("account")
    folder = (payload.get("folder") or "").strip() or None
    account_id = int(account) if str(account or "").isdigit() else None

    changed = db.mark_all_seen(account_id, folder)
    if changed:
        threading.Thread(target=_push_seen_bulk, args=(changed, True),
                         daemon=True).start()

    by_folder = {}
    for _aid, f, _uid in changed:
        by_folder[f] = by_folder.get(f, 0) + 1
    return jsonify({"ok": True, "cleared": len(changed),
                    "by_folder": by_folder, "stats": db.get_stats()})


@app.route("/api/attachments/<int:attachment_id>")
def api_attachment(attachment_id):
    row = db.get_attachment(attachment_id)
    if not row or not row.get("data"):
        abort(404)
    return send_file(io.BytesIO(row["data"]),
                     mimetype=row.get("content_type") or "application/octet-stream",
                     as_attachment=True,
                     download_name=row.get("filename") or "attachment")


def _split_from(value):
    """把 `"张三" <a@b.com>` 拆成 (显示名, 地址)。"""
    import email.utils as eu
    name, addr = eu.parseaddr(value)
    return (name or addr or value), (addr or "")


def _push_seen_to_server(row, seen):
    """把已读/未读状态推回 IMAP 服务器（尽力而为，失败只记录）。"""
    try:
        acc = db.get_account(row["account_id"])
        if not acc:
            return
        folders = db.get_folders(acc["id"])
        name = folders.get(row["folder"]) or row["folder"]
        conn = connect(acc)
        try:
            conn.select(quote_mailbox(name), readonly=False)
            conn.uid("store", str(row["uid"]),
                     "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception:
        pass


def _push_seen_bulk(rows, seen=True):
    """把一批已读状态回推服务器。

    按「账号 + 文件夹」分组，每组只开**一次**连接、用一条 UID STORE 发一批 UID ——
    逐封回推的话，几十封就是几十次 TCP 握手加登录，又慢又容易被服务商限流。
    """
    groups = {}
    for account_id, folder, uid in rows:
        groups.setdefault((account_id, folder), []).append(str(uid))

    for (account_id, folder), uids in groups.items():
        try:
            acc = db.get_account(account_id)
            if not acc:
                continue
            name = (db.get_folders(account_id) or {}).get(folder) or folder
            conn = connect(acc)
            try:
                conn.select(quote_mailbox(name), readonly=False)
                # UID STORE 接受逗号分隔的序列，一次别塞太多
                for i in range(0, len(uids), 500):
                    conn.uid("store", ",".join(uids[i:i + 500]),
                             "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception:
            pass


def _append_to_sent(acc, raw):
    """把已发送的邮件追加到服务端「已发送」（尽力而为）。"""
    try:
        folders = db.get_folders(acc["id"])
        name = folders.get("Sent")
        if not name:
            return
        conn = connect(acc)
        try:
            conn.append(quote_mailbox(name), "\\Seen", None, raw)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- 账号
@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    data = request.json or {}
    email = (data.get("email") or "").strip()
    if "@" not in email:
        return jsonify({"error": "请填写完整的邮箱地址"}), 400

    provider = (data.get("provider") or "").strip()
    if not provider:
        provider = DOMAIN_PROVIDER.get(email.rsplit("@", 1)[-1].lower(), "Custom")
    preset = provider_of(provider)

    imap_server = (data.get("imap_server") or preset["imap"]).strip()
    smtp_server = (data.get("smtp_server") or preset["smtp"]).strip()
    if not imap_server or not smtp_server:
        return jsonify({"error": "IMAP / SMTP 服务器地址不能为空"}), 400

    name = (data.get("name") or email.split("@")[0]).strip()
    account_id = db.add_account(
        name, email, provider, imap_server, int(data.get("imap_port") or preset["imap_port"]),
        smtp_server, int(data.get("smtp_port") or preset["smtp_port"]))

    password = data.get("password")
    if password:
        accounts.save_password(email, password)

    # 密码类账号：先连一次，把文件夹映射和 (主要) 错误立刻反馈给用户
    warning = ""
    if preset.get("oauth") != "microsoft" or not oauth.has_tokens(email):
        try:
            conn = connect(db.get_account(account_id))
            try:
                db.save_folders(account_id, resolve_folders(conn) or {"INBOX": "INBOX"})
            finally:
                conn.logout()
        except MailAuthError as e:
            warning = str(e)
        except Exception as e:
            warning = f"连接测试未通过：{e}"

    return jsonify({"ok": True, "account_id": account_id, "warning": warning,
                    "accounts": _account_payload()})


@app.route("/api/accounts/<int:account_id>", methods=["PUT"])
def api_update_account(account_id):
    acc = db.get_account(account_id)
    if not acc:
        abort(404)
    data = request.json or {}
    db.update_account(
        account_id,
        (data.get("name") or acc["name"]), (data.get("email") or acc["email"]),
        (data.get("provider") or acc["provider"]),
        (data.get("imap_server") or acc["imap_server"]),
        int(data.get("imap_port") or acc["imap_port"]),
        (data.get("smtp_server") or acc["smtp_server"]),
        int(data.get("smtp_port") or acc["smtp_port"]))
    if data.get("password"):
        accounts.save_password(data.get("email") or acc["email"], data["password"])
    return jsonify({"ok": True, "accounts": _account_payload()})


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    acc = db.get_account(account_id)
    if not acc:
        abort(404)
    db.delete_account(account_id)
    accounts.delete_password(acc["email"])
    oauth.clear_tokens(acc["email"])
    return jsonify({"ok": True, "accounts": _account_payload()})


@app.route("/api/accounts/<int:account_id>/test", methods=["POST"])
def api_test_account(account_id):
    acc = db.get_account(account_id)
    if not acc:
        abort(404)
    try:
        conn = connect(acc)
        try:
            folders = resolve_folders(conn) or {}
            db.save_folders(account_id, folders or {"INBOX": "INBOX"})
        finally:
            conn.logout()
        return jsonify({"ok": True,
                        "folders": {CANON_LABEL.get(k, k): v for k, v in folders.items()}})
    except MailAuthError as e:
        return jsonify({"ok": False, "error": str(e), "kind": e.kind,
                        "action_url": (e.action or ("", ""))[1] if e.action else ""})


# ---------------------------------------------------------------- 同步
@app.route("/api/sync", methods=["POST"])
def api_sync():
    payload = request.json or {}
    account = payload.get("account")
    ids = [int(account)] if str(account or "").isdigit() else None
    started = sync_manager.start(ids, throttle=5)
    return jsonify({"ok": True, "started": started, "sync": sync_manager.status()})


@app.route("/api/sync/status")
def api_sync_status():
    return jsonify(sync_manager.status())


# ---------------------------------------------------------------- 发信
@app.route("/api/send", methods=["POST"])
def api_send():
    form = request.form
    account_id = form.get("account_id") or ""
    if not account_id.isdigit():
        return jsonify({"error": "请选择发件账号"}), 400
    acc = db.get_account(int(account_id))
    if not acc:
        return jsonify({"error": "发件账号不存在"}), 404

    to_addr = (form.get("to") or "").strip()
    # 这里只挡住「完全没填」；地址本身合不合法交给 sender 判断 ——
    # 它会把「缺 @」「全角 @」「中文地址」这些分门别类讲清楚
    if not to_addr:
        return jsonify({"error": "请填写收件人地址"}), 400

    attachments = []
    for f in request.files.getlist("files"):
        if f and f.filename:
            attachments.append({"filename": f.filename, "data": f.read()})

    try:
        raw, rcpt = send_email(
            acc["smtp_server"], acc["smtp_port"], acc["email"], to_addr,
            (form.get("subject") or "").strip(),
            form.get("body") or "",
            attachments=attachments,
            html=(form.get("html", "1") == "1"),
            provider=acc["provider"],
            from_name=acc.get("name") or None,
            cc=(form.get("cc") or "").strip() or None,
            bcc=(form.get("bcc") or "").strip() or None)
    except SendError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # 兜底：别把 Python 原始异常糊到用户脸上
        return jsonify({"error": f"发送失败：{e}"}), 400

    # 发出去的收件人自动进常用联系人（次数 +1，用于「常用」排序）
    try:
        db.touch_contacts(rcpt)
    except Exception:
        pass

    threading.Thread(target=_append_to_sent, args=(acc, raw), daemon=True).start()
    return jsonify({"ok": True, "contacts": len(rcpt)})


# ---------------------------------------------------------------- 微软授权
@app.route("/api/oauth/start", methods=["POST"])
def api_oauth_start():
    data = request.json or {}
    try:
        flow = msauth.start(email_hint=(data.get("email") or "").strip(),
                            client_id=(data.get("client_id") or "").strip() or None,
                            fallback_provider=(data.get("provider") or "Outlook"))
    except oauth.OAuthError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(flow)


@app.route("/api/oauth/status")
def api_oauth_status():
    flow_id = request.args.get("id", "")
    flow = msauth.status(flow_id)
    if flow.get("state") == "done":
        flow["accounts"] = _account_payload()
    return jsonify(flow)


@app.route("/api/oauth/cancel", methods=["POST"])
def api_oauth_cancel():
    msauth.cancel((request.json or {}).get("id", ""))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 启动
def _start_background():
    db.init_db()
    created = auth.seed_default_user()
    if created:
        name, is_default = created
        if is_default:
            print(f"* 已创建管理员账号：{name} / {auth.DEFAULT_PASSWORD}（请登录后尽快修改密码）")
        else:
            print(f"* 已创建管理员账号：{name}（初始密码取自环境变量）")
    if auth.default_password_in_use():
        print("⚠ 安全提示：管理员仍在使用初始密码，登录后请在左下角「设置 → 账号安全」里更换。")
    # gunicorn 单 worker 多线程 + Werkzeug 调试重载都会进到这里，用环境变量挡一下重复启动
    if os.environ.get("MC_BG_STARTED") == "1":
        return
    os.environ["MC_BG_STARTED"] = "1"
    if SYNC_INTERVAL_MINUTES > 0:
        threading.Thread(target=background_loop, args=(SYNC_INTERVAL_MINUTES,),
                         name="auto-sync", daemon=True).start()


_start_background()


if __name__ == "__main__":
    if AUTH_DISABLED:
        print("⚠ 警告：AUTH_DISABLED=1，已关闭登录校验，仅限本机调试，切勿在公网使用！")
    port = int(os.environ.get("APP_PORT", "8090"))
    host = os.environ.get("APP_HOST", "127.0.0.1")
    print(f"* 星尘邮箱 (Stardust) {APP_VERSION}  →  http://{host}:{port}")
    print(f"* 数据目录：{DATA_DIR}")
    print("* 登录方式：用户名 + 密码（初始 admin / admin123，可登录后修改）")
    app.run(host=host, port=port, debug=False, threaded=True)
