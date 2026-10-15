# -*- coding: utf-8 -*-
"""日志与崩溃防护模块：轮转日志 + faulthandler + 全局异常钩子。"""
import faulthandler
import logging
import logging.handlers
import os
import sys
import time
import traceback

LOG_DIR = None
CRASH_DIR = None
LOG_FILE = "app.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5


def init(var_dir):
    """初始化日志系统。var_dir: TRIM_PKGVAR 数据目录。"""
    global LOG_DIR, CRASH_DIR
    LOG_DIR = os.path.join(var_dir, "logs")
    CRASH_DIR = os.path.join(var_dir, "crash")
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CRASH_DIR, exist_ok=True)

    # 轮转文件日志
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE),
        maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # 控制台输出（launcher 重定向到 stdout 日志）
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # 段错误等原生崩溃 -> crash/crash-*.log
    fh_path = os.path.join(CRASH_DIR, "faulthandler.log")
    try:
        _fh_file = open(fh_path, "a", encoding="utf-8")
        faulthandler.enable(file=_fh_file, all_threads=True)
    except Exception:
        faulthandler.enable()

    # 未捕获 Python 异常 -> crash/crash-*.log
    def _excepthook(etype, evalue, etb):
        try:
            write_crash("uncaught-exception",
                        "".join(traceback.format_exception(etype, evalue, etb)))
        except Exception:
            pass
        sys.__excepthook__(etype, evalue, etb)

    sys.excepthook = _excepthook
    logging.getLogger("applog").info("日志系统初始化完成, log_dir=%s", LOG_DIR)


def write_crash(kind, detail):
    """写崩溃日志，返回文件路径。"""
    if CRASH_DIR is None:
        print(detail, file=sys.stderr)
        return None
    os.makedirs(CRASH_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(CRASH_DIR, "crash-%s-%s.log" % (ts, kind))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("time: %s\nkind: %s\npython: %s\nplatform: %s\n\n%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), kind,
                sys.version.replace("\n", " "), sys.platform, detail))
    except Exception:
        path = None
    return path


def get_logger(name):
    return logging.getLogger(name)
