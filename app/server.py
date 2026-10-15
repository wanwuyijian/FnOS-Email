# -*- coding: utf-8 -*-
"""fn-mail-assistant 主服务：HTTP API + 静态页面。

纯标准库，兼容 Python 3.9+。
路由鉴权：Bearer token，首次访问设置管理密码。
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import applog
import mailer
import scheduler as sched_mod
from storage import Store

APP_VERSION = "1.1.1"
SESSION_TTL = 7 * 86400
PBKDF2_ITERS = 100000

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_store = None
_sched = None
_sessions = {}          # token -> expire_ts
_sessions_lock = threading.Lock()
_upload_dir = None
_start_time = time.time()


def get_var_dir():
    v = os.environ.get("TRIM_PKGVAR")
    if v:
        return v
    # Windows 本地调试：./devvar
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "devvar")


def init():
    global _store, _sched, _upload_dir
    var_dir = get_var_dir()
    os.makedirs(var_dir, exist_ok=True)
    applog.init(var_dir)
    _upload_dir = os.path.join(var_dir, "tmp", "uploads")
    os.makedirs(_upload_dir, exist_ok=True)
    _store = Store(var_dir)
    _sched = sched_mod.Scheduler(_store)
    _sched.start()
    applog.get_logger("server").info(
        "服务初始化完成 version=%s var=%s", APP_VERSION, var_dir)


# ---------- 鉴权 ----------

def _hash_password(pwd, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                             salt.encode("utf-8"), PBKDF2_ITERS)
    return {"salt": salt, "hash": h.hex(), "iters": PBKDF2_ITERS}


def _verify_password(pwd, rec):
    try:
        h = hashlib.pbkdf2_hmac(
            "sha256", pwd.encode("utf-8"),
            rec["salt"].encode("utf-8"), int(rec["iters"]))
        return hmac.compare_digest(h.hex(), rec["hash"])
    except Exception:
        return False


def _new_session():
    tok = secrets.token_hex(32)
    with _sessions_lock:
        now = time.time()
        for k in [k for k, v in _sessions.items() if v < now]:
            _sessions.pop(k, None)
        _sessions[tok] = now + SESSION_TTL
    return tok


def _check_session(tok):
    if not tok:
        return False
    with _sessions_lock:
        exp = _sessions.get(tok)
        if not exp:
            return False
        if exp < time.time():
            _sessions.pop(tok, None)
            return False
        return True


# ---------- 工具 ----------

def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _mask_account(a):
    """脱敏：授权码不回传前端。"""
    a = dict(a)
    if "password" in a:
        a["password"] = "******" if a.get("password") else ""
        a["has_password"] = bool(a.get("password"))
    return a


def _save_account_password(old, new_data):
    """编辑账号时保留原授权码（前端传 ****** 表示不变）。"""
    if new_data.get("password") in (None, "", "******"):
        new_data["password"] = old.get("password", "") if old else ""
    return new_data


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------- 处理器 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "fnmail/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---- 基础 ----
    def log_message(self, fmt, *args):
        applog.get_logger("http").debug("%s " + fmt,
                                        self.address_string(), *args)

    def _send(self, code, data=b"", ctype="application/json; charset=utf-8",
              headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, _json_bytes(obj))

    def _ok(self, obj=None):
        self._json(200, {"ok": True, "data": obj if obj is not None else {}})

    def _err(self, code, msg):
        self._json(code, {"ok": False, "error": str(msg)[:500]})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if len(raw) > 64 * 1024 * 1024:
            raise ApiError(413, "请求体过大")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "JSON 解析失败")

    def _auth(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else ""
        if not _check_session(tok):
            raise ApiError(401, "未登录或会话过期")

    # ---- 路由 ----
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    ROUTES = [
        ("POST", r"^/api/auth/setup$", "h_auth_setup"),
        ("POST", r"^/api/auth/login$", "h_auth_login"),
        ("POST", r"^/api/auth/logout$", "h_auth_logout"),
        ("POST", r"^/api/auth/password$", "h_auth_password"),
        ("GET", r"^/api/auth/state$", "h_auth_state"),
        ("GET", r"^/api/status$", "h_status"),
        ("GET", r"^/api/version$", "h_version"),
        ("GET", r"^/api/presets$", "h_presets"),
        ("GET", r"^/api/accounts$", "h_accounts_list"),
        ("POST", r"^/api/accounts$", "h_accounts_create"),
        ("POST", r"^/api/accounts/test$", "h_accounts_test"),
        ("DELETE", r"^/api/accounts/(?P<id>[\w-]+)$", "h_accounts_delete"),
        ("GET", r"^/api/inbox$", "h_inbox"),
        ("GET", r"^/api/folders$", "h_folders"),
        ("POST", r"^/api/send$", "h_send"),
        ("POST", r"^/api/upload$", "h_upload"),
        ("GET", r"^/api/tasks$", "h_tasks_list"),
        ("POST", r"^/api/tasks$", "h_tasks_create"),
        ("POST", r"^/api/tasks/(?P<id>[\w-]+)/run$", "h_task_run"),
        ("GET", r"^/api/tasks/(?P<id>[\w-]+)/history$", "h_task_history"),
        ("DELETE", r"^/api/tasks/(?P<id>[\w-]+)$", "h_task_delete"),
        ("GET", r"^/api/history$", "h_history_all"),
        ("GET", r"^/api/fs/list$", "h_fs_list"),
        ("GET", r"^/api/logs$", "h_logs"),
        ("GET", r"^/api/logs/crash/(?P<name>[\w.-]+)$", "h_crash_get"),
        ("GET", r"^/api/diagnostics$", "h_diagnostics"),
    ]

    def _route(self, method):
        try:
            path = self.path.split("?", 1)[0]
            for m, pattern, fn_name in self.ROUTES:
                if m != method:
                    continue
                mo = re.match(pattern, path)
                if mo:
                    # 静态与鉴权例外
                    if fn_name not in ("h_auth_setup", "h_auth_login",
                                       "h_auth_state", "h_version"):
                        self._auth()
                    fn = getattr(self, fn_name)
                    fn(**mo.groupdict())
                    return
            if path == "/" or path.startswith("/ui/") or \
               path in ("/index.html", "/app.js", "/style.css"):
                return self._static(path)
            self._err(404, "接口不存在: %s" % path)
        except ApiError as e:
            self._err(e.code, e.msg)
        except mailer.MailError as e:
            # 邮件操作失败(网络/认证) -> 友好 JSON 错误
            self._err(400, str(e))
        except BrokenPipeError:
            pass
        except (ConnectionError, socket.timeout):
            pass
        except Exception as e:
            applog.get_logger("server").exception("处理请求异常 %s", path
                                                  if 'path' in dir() else '?')
            try:
                self._err(500, "服务器内部错误: %s" % str(e)[:200])
            except Exception:
                pass

    # ---- 静态 ----
    _MIME = {".html": "text/html; charset=utf-8",
             ".js": "application/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8",
             ".png": "image/png", ".svg": "image/svg+xml",
             ".ico": "image/x-icon"}

    def _static(self, path):
        if path == "/":
            f = "index.html"
        else:
            f = path[len("/ui/"):] if path.startswith("/ui/") else path.lstrip("/")
        fp = os.path.normpath(os.path.join(WEB_DIR, f))
        if not fp.startswith(os.path.normpath(WEB_DIR)) or \
                not os.path.isfile(fp):
            return self._err(404, "文件不存在")
        ext = os.path.splitext(fp)[1]
        with open(fp, "rb") as fh:
            data = fh.read()
        self._send(200, data, self._MIME.get(ext, "application/octet-stream"))

    # ---- auth ----
    def h_auth_state(self):
        has = bool(_store.get("admin_password"))
        self._ok({"initialized": has})

    def h_auth_setup(self):
        body = self._body()
        if _store.get("admin_password"):
            raise ApiError(400, "管理密码已初始化, 请直接登录")
        pwd = (body.get("password") or "").strip()
        if len(pwd) < 6:
            raise ApiError(400, "密码至少6位")
        _store.data["admin_password"] = _hash_password(pwd)
        _store.save()
        tok = _new_session()
        self._ok({"token": tok})

    def h_auth_login(self):
        body = self._body()
        rec = _store.get("admin_password")
        if not rec:
            raise ApiError(400, "尚未设置管理密码")
        if not _verify_password((body.get("password") or ""), rec):
            raise ApiError(401, "密码错误")
        tok = _new_session()
        self._ok({"token": tok})

    def h_auth_logout(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else ""
        with _sessions_lock:
            _sessions.pop(tok, None)
        self._ok()

    def h_auth_password(self):
        """修改管理密码: 需已登录 + 原密码验证; 成功后吊销其他会话。"""
        body = self._body()
        rec = _store.get("admin_password")
        if not rec:
            raise ApiError(400, "尚未设置管理密码")
        old = body.get("old_password") or ""
        if not _verify_password(old, rec):
            raise ApiError(401, "原密码错误")
        new = (body.get("new_password") or "").strip()
        if len(new) < 6:
            raise ApiError(400, "新密码至少6位")
        if new == old:
            raise ApiError(400, "新密码不能与原密码相同")
        _store.data["admin_password"] = _hash_password(new)
        _store.save()
        # 吊销除当前会话外的所有登录
        h = self.headers.get("Authorization", "")
        cur = h[7:] if h.startswith("Bearer ") else ""
        with _sessions_lock:
            for k in [k for k in _sessions if k != cur]:
                _sessions.pop(k, None)
        applog.get_logger("server").info("管理密码已修改")
        self._ok({"message": "密码修改成功"})

    # ---- status / version ----
    def h_status(self):
        accounts = _store.list_accounts()
        tasks = _store.list_tasks()
        enabled = [t for t in tasks if t.get("enabled")]
        self._ok({
            "version": APP_VERSION,
            "uptime": int(time.time() - _start_time),
            "accounts": len(accounts),
            "tasks": len(tasks),
            "tasks_enabled": len(enabled),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def h_version(self):
        self._ok({"version": APP_VERSION, "python": sys.version.split()[0],
                  "platform": sys.platform})

    def h_presets(self):
        self._ok(mailer.PRESETS)

    # ---- accounts ----
    def h_accounts_list(self):
        self._ok([_mask_account(a) for a in _store.list_accounts()])

    def h_accounts_create(self):
        body = self._body()
        old = None
        if body.get("id"):
            old = _store.get_account(body["id"])
            if not old:
                raise ApiError(404, "账号不存在")
        body = _save_account_password(old or {}, body)
        acc = {
            "id": body.get("id"),
            "name": (body.get("name") or body.get("username") or "").strip(),
            "email": (body.get("email") or body.get("username") or "").strip(),
            "username": (body.get("username") or "").strip(),
            "password": body.get("password") or "",
            "display_name": body.get("display_name") or "",
            "preset": body.get("preset") or "custom",
            "smtp_host": (body.get("smtp_host") or "").strip(),
            "smtp_port": int(body.get("smtp_port") or 465),
            "smtp_ssl": bool(body.get("smtp_ssl", True)),
            "imap_host": (body.get("imap_host") or "").strip(),
            "imap_port": int(body.get("imap_port") or 993),
            "imap_ssl": bool(body.get("imap_ssl", True)),
            "pop3_host": (body.get("pop3_host") or "").strip(),
            "pop3_port": int(body.get("pop3_port") or 995),
            "pop3_ssl": bool(body.get("pop3_ssl", True)),
            "protocol": body.get("protocol") or "imap",
            "create_time": (old or {}).get("create_time") or
                           time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not acc["username"] or not acc["password"]:
            raise ApiError(400, "账号和授权码不能为空")
        # 按预设自动填充未填写的服务器参数
        preset = mailer.PRESETS.get(body.get("preset") or "custom") \
            or mailer.PRESETS["custom"]
        if preset is not mailer.PRESETS["custom"]:
            if not acc["smtp_host"]:
                acc["smtp_host"] = preset["smtp"]["host"]
            if not acc["imap_host"]:
                acc["imap_host"] = preset["imap"]["host"]
            if not acc["pop3_host"]:
                acc["pop3_host"] = preset["pop3"]["host"]
        if not acc["smtp_host"]:
            raise ApiError(400, "SMTP 服务器不能为空")
        acc = _store.upsert_account(acc)
        self._ok(_mask_account(acc))

    def h_accounts_test(self):
        body = self._body()
        # 支持: 传完整配置直接测 / 传已存账号 id
        if body.get("id") and not body.get("smtp_host"):
            acc = _store.get_account(body["id"])
            if not acc:
                raise ApiError(404, "账号不存在")
        else:
            acc = dict(body)
            if body.get("id"):
                old = _store.get_account(body["id"])
                acc = _save_account_password(old or {}, acc)
        what = body.get("what") or "smtp"
        if what == "smtp":
            res = mailer.test_smtp(acc)
        elif what == "imap":
            res = mailer.test_imap(acc)
        elif what == "pop3":
            res = mailer.test_pop3(acc)
        else:
            raise ApiError(400, "未知测试类型: %s" % what)
        self._ok(res)

    def h_accounts_delete(self, id):
        _store.delete_account(id)
        self._ok()

    # ---- 收件 ----
    def _account_for(self, body_or_id):
        acc = _store.get_account(body_or_id) if isinstance(
            body_or_id, str) else None
        if acc:
            return acc
        raise ApiError(404, "账号不存在")

    def h_inbox(self):
        qs = self._query()
        acc_id = qs.get("account")
        acc = self._account_for(acc_id)
        proto = (qs.get("proto") or acc.get("protocol") or "imap").lower()
        limit = min(int(qs.get("limit", "20")), 100)
        folder = qs.get("folder", "INBOX")
        if proto == "pop3":
            mails = mailer.fetch_pop3(acc, limit=limit)
        else:
            mails = mailer.fetch_imap(acc, folder=folder, limit=limit)
        self._ok({"proto": proto, "count": len(mails), "mails": mails})

    def h_folders(self):
        qs = self._query()
        acc = self._account_for(qs.get("account"))
        if (acc.get("protocol") or "imap").lower() == "pop3":
            self._ok({"folders": ["INBOX"]})
        else:
            self._ok({"folders": mailer.list_imap_folders(acc)})

    # ---- 发送 ----
    def h_send(self):
        body = self._body()
        acc = _store.get_account(body.get("account_id") or "")
        if not acc:
            raise ApiError(400, "请选择发件账号")
        to = [a.strip() for a in
              re.split(r"[,;，；\s]+", body.get("to") or "")
              if a.strip()]
        if not to:
            raise ApiError(400, "收件人不能为空")
        attachments = []
        for p in (body.get("attachments") or []):
            if isinstance(p, dict):
                p = p.get("path") or ""
            p = (p or "").strip()
            if p:
                attachments.append(p)
        res = mailer.send_mail(
            acc, to, body.get("subject") or "(无主题)",
            body.get("body") or "", attachments=attachments,
            cc=[a.strip() for a in
                re.split(r"[,;，；\s]+", body.get("cc") or "") if a.strip()],
            html=bool(body.get("html", False)))
        self._ok({"to": res["to"], "cc": res["cc"], "files": res["files"]})

    def h_upload(self):
        """原始字节流上传: POST /api/upload?name=xxx
        保存到 var/tmp/uploads, 返回路径供发送附件引用。"""
        qs = self._query()
        name = re.sub(r'[\\/:*?"<>|]+', "_", qs.get("name") or "file.bin")
        if len(name) > 200:
            name = name[-200:]
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 512 * 1024 * 1024:
            raise ApiError(413, "文件过大(>512MB)")
        path = os.path.join(_upload_dir, "%d_%s" % (int(time.time()), name))
        with open(path, "wb") as f:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        self._ok({"path": path, "name": name, "size": n})

    # ---- tasks ----
    def _validate_task(self, body):
        sched = body.get("schedule") or {}
        if sched.get("type") not in (
                "once", "daily", "weekly", "monthly", "interval"):
            raise ApiError(400, "调度类型无效")
        if sched.get("type") == "once" and not sched.get("datetime"):
            raise ApiError(400, "请选择执行时间")
        if sched.get("type") == "interval" and \
                not sched.get("minutes"):
            raise ApiError(400, "请填写间隔分钟")
        to = [a.strip() for a in
              re.split(r"[,;，；\s]+", body.get("to") or "") if a.strip()]
        if not to:
            raise ApiError(400, "收件人不能为空")
        return to

    def h_tasks_list(self):
        self._ok(_store.list_tasks())

    def h_tasks_create(self):
        body = self._body()
        to = self._validate_task(body)
        task = {
            "id": body.get("id"),
            "name": (body.get("name") or "未命名任务").strip(),
            "enabled": bool(body.get("enabled", True)),
            "account_id": body.get("account_id"),
            "to": to,
            "cc": [a.strip() for a in
                   re.split(r"[,;，；\s]+", body.get("cc") or "")
                   if a.strip()],
            "subject": body.get("subject") or "",
            "body": body.get("body") or "",
            "html": bool(body.get("html", False)),
            "attachments": [p for p in (body.get("attachments") or [])
                            if (p or "").strip()],
            "zip_attachments": bool(body.get("zip_attachments", False)),
            "extensions": sched_mod.normalize_extensions(
                body.get("extensions")),
            "schedule": body.get("schedule") or {},
            "catch_up": bool(body.get("catch_up", True)),
            "retry": int(body.get("retry", 1)),
        }
        acc = _store.get_account(task["account_id"]) if task["account_id"] \
            else None
        if not acc:
            raise ApiError(400, "请选择发件账号")
        task = _store.upsert_task(task)
        # 重算下次运行
        nxt = sched_mod.compute_next_run(task["schedule"])
        _store.update_task_runtime(task["id"], {
            "next_run": sched_mod._fmt(nxt)})
        _sched.nudge()
        self._ok(task)

    def h_task_run(self, id):
        ok, msg = _sched.run_now(id)
        if not ok:
            raise ApiError(409, msg)
        self._ok({"message": msg})

    def h_task_history(self, id):
        self._ok(_store.list_history(id))

    def h_history_all(self):
        self._ok(_store.list_history())

    def h_task_delete(self, id):
        _store.delete_task(id)
        self._ok()

    # ---- 文件浏览 ----
    def h_fs_list(self):
        qs = self._query()
        path = qs.get("path") or "/"
        path = os.path.normpath(path)
        if not os.path.exists(path):
            raise ApiError(404, "路径不存在: %s" % path)
        if not os.path.isdir(path):
            raise ApiError(400, "不是目录")
        items = []
        try:
            for name in os.listdir(path):
                fp = os.path.join(path, name)
                try:
                    st = os.stat(fp)
                    items.append({
                        "name": name, "path": fp,
                        "dir": os.path.isdir(fp),
                        "size": 0 if os.path.isdir(fp) else st.st_size,
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
        except OSError as e:
            raise ApiError(403, "无法读取目录: %s" % e)
        items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        # 常用根路径提示
        roots = _suggest_roots()
        self._ok({"path": path, "parent": os.path.dirname(path)
                  if path != os.path.dirname(path) else None,
                  "items": items, "roots": roots})

    # ---- 日志与诊断 ----
    def h_logs(self):
        qs = self._query()
        lines = min(int(qs.get("lines", "200")), 1000)
        log_file = os.path.join(applog.LOG_DIR, applog.LOG_FILE)
        content = ""
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                content = "".join(f.readlines()[-lines:])
        crashes = []
        if applog.CRASH_DIR and os.path.isdir(applog.CRASH_DIR):
            for fn in sorted(os.listdir(applog.CRASH_DIR), reverse=True):
                p = os.path.join(applog.CRASH_DIR, fn)
                if os.path.isfile(p):
                    crashes.append({"name": fn,
                                    "size": os.path.getsize(p),
                                    "mtime": int(os.path.getmtime(p))})
        self._ok({"log": content, "crashes": crashes[:20],
                  "log_file": log_file})

    def h_crash_get(self, name):
        if not applog.CRASH_DIR:
            raise ApiError(404, "无崩溃目录")
        p = os.path.normpath(os.path.join(applog.CRASH_DIR, name))
        if not p.startswith(os.path.normpath(applog.CRASH_DIR)) or \
                not os.path.isfile(p):
            raise ApiError(404, "崩溃日志不存在")
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(512 * 1024)
        self._send(200, data.encode("utf-8"), "text/plain; charset=utf-8")

    def h_diagnostics(self):
        """导出诊断包 zip: 日志+崩溃+脱敏配置+版本信息。"""
        var_dir = get_var_dir()
        os.makedirs(os.path.join(var_dir, "tmp"), exist_ok=True)
        zpath = os.path.join(
            var_dir, "tmp", "diag_%s.zip" % time.strftime("%Y%m%d_%H%M%S"))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            info = {
                "app": "fn-mail-assistant", "version": APP_VERSION,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "python": sys.version, "platform": sys.platform,
                "os_release": _os_release(),
                "accounts": [{"name": a.get("name"),
                              "username": a.get("username")} for
                             a in _store.list_accounts()],
                "tasks": _store.list_tasks(),
            }
            zf.writestr("info.json", _json_bytes(info))
            cfg = json.loads(json.dumps(_store.data))  # deep copy
            if cfg.get("admin_password"):
                cfg["admin_password"] = "(已隐藏)"
            for a in cfg.get("accounts", []):
                a["password"] = "(已隐藏)"
            zf.writestr("config.masked.json", _json_bytes(cfg))
            log_dir = applog.LOG_DIR
            if log_dir and os.path.isdir(log_dir):
                for fn in os.listdir(log_dir):
                    fp = os.path.join(log_dir, fn)
                    if os.path.isfile(fp):
                        zf.write(fp, "logs/" + fn)
            cr_dir = applog.CRASH_DIR
            if cr_dir and os.path.isdir(cr_dir):
                for fn in os.listdir(cr_dir):
                    fp = os.path.join(cr_dir, fn)
                    if os.path.isfile(fp):
                        zf.write(fp, "crash/" + fn)
        with open(zpath, "rb") as f:
            data = f.read()
        self._send(200, data, "application/zip", headers={
            "Content-Disposition":
                "attachment; filename=\"diagnostics.zip\""})
        try:
            os.remove(zpath)
        except Exception:
            pass

    # ---- query 解析 ----
    def _query(self):
        from urllib.parse import parse_qs, urlsplit
        q = urlsplit(self.path).query
        return {k: v[0] for k, v in parse_qs(q).items()}


def _os_release():
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return sys.platform


def _suggest_roots():
    roots = ["/"]
    for p in ("/vol1", "/vol2", "/vol3", "/vol4", "/vol5",
              "/mnt", "/media", "/home", "/var/apps"):
        if os.path.isdir(p):
            roots.append(p)
    # 用户主目录
    v = os.environ.get("TRIM_RUN_USERNAME") or os.environ.get("TRIM_USERNAME")
    if v:
        for base in ("/home", "/var/apps/%s/home" % os.environ.get(
                "TRIM_APPNAME", "")):
            hp = os.path.join(base, v)
            if os.path.isdir(hp):
                roots.append(hp)
    return roots


def main():
    init()
    port = int(os.environ.get("TRIM_SERVICE_PORT", "8322") or 8322)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    applog.get_logger("server").info("HTTP 监听 0.0.0.0:%d", port)
    print("fn-mail-assistant listening on 0.0.0.0:%d" % port, flush=True)
    try:
        httpd.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        pass
    finally:
        _sched.stop()
        applog.get_logger("server").info("服务退出")


if __name__ == "__main__":
    main()
