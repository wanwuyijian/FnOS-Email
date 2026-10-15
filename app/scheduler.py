# -*- coding: utf-8 -*-
"""定时任务引擎：线程轮询 + catch-up 补发 + 重试 + 历史。

任务类型:
  once     指定时间执行一次
  daily    每天 HH:MM
  weekly   每周几 HH:MM  (weekday: 0=周一..6=周日)
  monthly  每月几号 HH:MM
  interval 每 N 分钟一次
任务字段:
  id, name, enabled, account_id, to(list), cc(list),
  subject, body, html(bool), attachments(list 路径/glob),
  zip_attachments(bool), schedule{type,...}, catch_up(bool),
  retry(int), last_run, last_result, running(bool)
"""
import datetime
import fnmatch
import glob
import logging
import os
import re
import threading
import time
import zipfile

import mailer

log = logging.getLogger("scheduler")

TICK_SECONDS = 20
MAX_RETRY = 3
RETRY_DELAY = 60  # 秒


def _now():
    return datetime.datetime.now()


def normalize_extensions(value):
    """归一化文件类型过滤: "pdf, .xlsx" 或列表 -> [".pdf", ".xlsx"]。"""
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;，；\s]+", value)
    else:
        parts = list(value)
    out = []
    for p in parts:
        p = str(p).strip().lower()
        if not p or p in ("*", ".*"):
            continue
        if not p.startswith("."):
            p = "." + p
        if p not in out:
            out.append(p)
    return out


def expand_attachments(paths, extensions=None):
    """展开路径列表：支持目录(递归)与 glob 通配符。返回真实文件列表。

    extensions: 文件类型过滤(如 [".pdf",".xlsx"])，仅作用于目录与通配符的
    展开结果；显式指定的单个文件不受过滤（用户明确选择的文件始终发送）。
    """
    exts = normalize_extensions(extensions)

    def match(fp):
        if not exts:
            return True
        return os.path.splitext(fp)[1].lower() in exts

    out = []
    for p in paths or []:
        p = p.strip()
        if not p:
            continue
        if os.path.isdir(p):
            for root, _d, fns in os.walk(p):
                for fn in sorted(fns):
                    fp = os.path.join(root, fn)
                    if match(fp):
                        out.append(fp)
        elif any(c in p for c in "*?["):
            for m in sorted(glob.glob(p, recursive=True)):
                if os.path.isfile(m) and match(m):
                    out.append(m)
        elif os.path.isfile(p):
            out.append(p)
    # 去重保序
    seen = set()
    res = []
    for f in out:
        k = os.path.normpath(f)
        if k not in seen:
            seen.add(k)
            res.append(f)
    return res


def zip_files(files, tmp_dir, task_name):
    """把附件打包为 zip，返回 zip 路径。"""
    os.makedirs(tmp_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r'[\\/:*?"<>|]+', "_", task_name or "attachments")
    zpath = os.path.join(tmp_dir, "%s_%s.zip" % (safe, ts))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for f in files:
            zf.write(f, os.path.basename(f))
    return zpath


def render_template(text, task, files):
    """渲染 {date}{time}{datetime}{files}{count} 占位符。"""
    if not text:
        return text
    rep = {
        "{date}": time.strftime("%Y-%m-%d"),
        "{time}": time.strftime("%H:%M:%S"),
        "{datetime}": time.strftime("%Y-%m-%d %H:%M:%S"),
        "{files}": "\n".join(files) if files else "(无附件)",
        "{count}": str(len(files)),
    }
    for k, v in rep.items():
        text = text.replace(k, v)
    return text


def compute_next_run(sched, after=None):
    """计算下次运行时间（datetime）。无法计算返回 None。"""
    after = after or _now()
    st = (sched or {}).get("type", "once")
    try:
        if st == "once":
            t = sched.get("datetime", "")
            dt = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M")
            return dt if dt > after else None
        if st == "daily":
            hh, mm = _parse_hm(sched.get("time", "08:00"))
            nxt = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= after:
                nxt += datetime.timedelta(days=1)
            return nxt
        if st == "weekly":
            hh, mm = _parse_hm(sched.get("time", "08:00"))
            wd = int(sched.get("weekday", 0))
            nxt = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta = (wd - after.weekday()) % 7
            nxt += datetime.timedelta(days=delta)
            if nxt <= after:
                nxt += datetime.timedelta(days=7)
            return nxt
        if st == "monthly":
            hh, mm = _parse_hm(sched.get("time", "08:00"))
            day = int(sched.get("day", 1))
            # 从当月起逐月向后找第一个 > after 的合法日期
            y, m = after.year, after.month
            for _i in range(24):  # 最多查2年
                try:
                    cand = datetime.datetime(y, m, day, hh, mm, 0)
                except ValueError:
                    cand = None  # 如 2月30日 不存在
                if cand and cand > after:
                    return cand
                m += 1
                if m > 12:
                    m, y = 1, y + 1
            return None
        if st == "interval":
            minutes = max(1, int(sched.get("minutes", 60)))
            return after + datetime.timedelta(minutes=minutes)
    except Exception:
        log.exception("compute_next_run 失败 sched=%r", sched)
    return None


def _parse_hm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})$", (s or "08:00").strip())
    if not m:
        return 8, 0
    return int(m.group(1)) % 24, int(m.group(2)) % 60


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


class Scheduler(threading.Thread):
    def __init__(self, store):
        super().__init__(daemon=True, name="scheduler")
        self.store = store
        self._stop = threading.Event()
        self._locks = {}            # task_id -> Lock
        self._master = threading.Lock()
        self._wakeup = threading.Event()

    # ---------- 生命周期 ----------
    def run(self):
        log.info("调度器启动, tick=%ds", TICK_SECONDS)
        # 启动时统一刷新 next_run
        self._refresh_next_runs(catchup=True)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("tick 异常(已忽略继续)")
            self._wakeup.wait(TICK_SECONDS)
            self._wakeup.clear()
        log.info("调度器已停止")

    def stop(self):
        self._stop.set()
        self._wakeup.set()

    def nudge(self):
        """任务变更后立即唤醒。"""
        self._wakeup.set()

    def _lock_for(self, task_id):
        with self._master:
            if task_id not in self._locks:
                self._locks[task_id] = threading.Lock()
            return self._locks[task_id]

    # ---------- 核心 ----------
    def _refresh_next_runs(self, catchup=False):
        """为任务计算 next_run；catchup=True 时补发错过的任务。"""
        now = _now()
        for t in self.store.list_tasks():
            if not t.get("enabled"):
                self.store.update_task_runtime(t["id"], {"next_run": None})
                continue
            sched = t.get("schedule") or {}
            nxt = t.get("next_run")
            if not nxt:
                nxt_dt = compute_next_run(sched, now)
                # 一次性任务: 时间已过且不补发 -> 直接禁用
                if (sched.get("type") == "once" and nxt_dt is None
                        and not t.get("catch_up", True)):
                    self.store.update_task_runtime(
                        t["id"], {"enabled": False, "next_run": None,
                                  "last_result": "已过期,任务禁用"})
                else:
                    self.store.update_task_runtime(
                        t["id"], {"next_run": _fmt(nxt_dt)})
                continue
            try:
                nxt_dt = datetime.datetime.strptime(nxt, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                nxt_dt = None
            if nxt_dt is None:
                nxt_dt = compute_next_run(sched, now)
                self.store.update_task_runtime(
                    t["id"], {"next_run": _fmt(nxt_dt)})
                continue
            if nxt_dt <= now:
                # 已到/已过：补发（catch_up 开启时）
                if catchup and t.get("catch_up", True):
                    self._run_in_thread(t, trigger="auto-catchup")
                # 一次性任务错过且不补发 -> 禁用
                if sched.get("type") == "once" and not (
                        catchup and t.get("catch_up", True)):
                    self.store.update_task_runtime(
                        t["id"], {"enabled": False, "next_run": None,
                                  "last_result": "已过期,任务禁用"})
                nxt_dt = compute_next_run(sched, now)
                self.store.update_task_runtime(
                    t["id"], {"next_run": _fmt(nxt_dt)})

    def _tick(self):
        now = _now()
        for t in self.store.list_tasks():
            if not t.get("enabled"):
                continue
            nxt = t.get("next_run")
            if not nxt:
                continue
            try:
                nxt_dt = datetime.datetime.strptime(nxt, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if nxt_dt <= now:
                self._run_in_thread(t, trigger="auto")
                sched = t.get("schedule") or {}
                nn = compute_next_run(sched, _now())
                update = {"next_run": _fmt(nn)}
                if sched.get("type") == "once":
                    update = {"enabled": False, "next_run": None}
                self.store.update_task_runtime(t["id"], update)

    # ---------- 执行 ----------
    def run_now(self, task_id):
        """手动立即执行。返回是否成功触发。"""
        t = self.store.get_task(task_id)
        if not t:
            return False, "任务不存在"
        lock = self._lock_for(task_id)
        if lock.locked():
            return False, "任务正在运行中"
        self._run_in_thread(t, trigger="manual")
        return True, "已触发"

    def _run_in_thread(self, task, trigger="auto"):
        tid = task["id"]
        lock = self._lock_for(tid)
        if not lock.acquire(blocking=False):
            log.info("任务 %s(%s) 已在运行, 跳过", tid, task.get("name"))
            return
        th = threading.Thread(
            target=self._run_safe, args=(task, lock, trigger),
            name="task-%s" % tid, daemon=True)
        th.start()

    def _run_safe(self, task, lock, trigger="auto"):
        tid = task["id"]
        try:
            self._execute(task, trigger)
        except Exception as e:
            log.exception("任务 %s 执行异常", tid)
            self.store.add_history(tid, {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False, "error": "内部错误: %s" % str(e)[:300],
                "trigger": trigger,
            })
            self.store.update_task_runtime(tid, {
                "last_run": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_result": "内部错误"})
        finally:
            lock.release()

    def _execute(self, task, trigger="auto"):
        tid = task["id"]
        name = task.get("name", tid)
        acc_id = task.get("account_id")
        account = self.store.get_account(acc_id) if acc_id else None
        log.info("开始执行任务 %s(%s)", tid, name)
        if not account:
            self._fail(task, "未配置发件账号", trigger)
            return
        exts = normalize_extensions(task.get("extensions"))
        files = expand_attachments(task.get("attachments"), exts)
        body = render_template(task.get("body", ""), task, files)
        subject = render_template(task.get("subject", ""), task, files)
        tmp_zip = None
        try:
            if task.get("attachments") and not files:
                if exts:
                    raise mailer.MailError(
                        "文件夹内没有符合类型的文件(类型: %s)"
                        % ", ".join(exts))
                raise mailer.MailError("附件为空, 请检查所配置的路径")
            attach_paths = files
            if task.get("zip_attachments") and files:
                tmp_dir = os.path.join(self.var_tmp(), "zip")
                tmp_zip = zip_files(files, tmp_dir, name)
                attach_paths = [tmp_zip]

            retry = max(0, min(int(task.get("retry", 1)), MAX_RETRY))
            attempt = 0
            last_err = None
            while True:
                try:
                    mailer.send_mail(
                        account, task.get("to", []), subject, body,
                        attachments=attach_paths, cc=task.get("cc", []),
                        html=bool(task.get("html", False)))
                    last_err = None
                    break
                except mailer.MailError as e:
                    last_err = e
                    attempt += 1
                    if attempt > retry:
                        raise
                    log.warning("任务 %s 第%d次发送失败将重试: %s",
                                tid, attempt, e)
                    time.sleep(RETRY_DELAY)

            now_s = time.strftime("%Y-%m-%d %H:%M:%S")
            self.store.add_history(tid, {
                "time": now_s, "ok": True, "error": "",
                "to": task.get("to", []), "files": files,
                "trigger": trigger,
            })
            self.store.update_task_runtime(tid, {
                "last_run": now_s, "last_result": "成功"})
            log.info("任务 %s 发送成功", tid)
        except mailer.MailError as e:
            self._fail(task, str(e), trigger)
        except Exception as e:
            log.exception("任务 %s 未知异常", tid)
            self._fail(task, "内部错误: %s" % str(e)[:300], trigger)
        finally:
            if tmp_zip:
                try:
                    os.remove(tmp_zip)
                except Exception:
                    pass

    def _fail(self, task, err, trigger):
        tid = task["id"]
        now_s = time.strftime("%Y-%m-%d %H:%M:%S")
        self.store.add_history(tid, {
            "time": now_s, "ok": False, "error": err[:300],
            "trigger": trigger})
        self.store.update_task_runtime(tid, {
            "last_run": now_s, "last_result": "失败: %s" % err[:100]})
        log.error("任务 %s 失败: %s", tid, err)

    def var_tmp(self):
        """临时目录（优先 TRIM_PKGVAR/tmp）。"""
        v = os.environ.get("TRIM_PKGVAR")
        if v:
            p = os.path.join(v, "tmp")
            os.makedirs(p, exist_ok=True)
            return p
        import tempfile
        return tempfile.gettempdir()
