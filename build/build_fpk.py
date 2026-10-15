# -*- coding: utf-8 -*-
"""build_fpk.py — 打包 fnOS .fpk（tar.gz），Windows 下可用。

流程:
1. 校验所有 JSON / manifest / 必需文件
2. app/ -> app.tgz (tar.gz, ./ 前缀条目)
3. 组装 fpk tar.gz: [appname]/manifest,ICON.PNG,ICON_256.PNG,
   config/{privilege,resource}, cmd/*, wizard/install, app.tgz

Windows 打包必须用 tarfile 手工设置 mode（cmd/* 与 launcher.sh 需 0755）。
用法: python build/build_fpk.py
"""
import io
import json
import os
import sys
import tarfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE, "app")
FPK_DIR = os.path.join(BASE, "fpk")
DIST = os.path.join(BASE, "dist")

APPNAME = "fn-mail-assistant"
VERSION = "1.0.0"
PLATFORM = "arm"

EXEC_FILES = {"main", "install_init", "install_callback", "uninstall_init",
              "uninstall_callback", "upgrade_init", "upgrade_callback",
              "config_init", "config_callback", "launcher.sh"}


def die(msg):
    print("[FAIL] " + msg)
    sys.exit(1)


def read_manifest():
    meta = {}
    with open(os.path.join(FPK_DIR, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def validate(meta):
    print("== 校验 ==")
    for k in ("appname", "version", "display_name", "platform", "source",
              "service_port", "desktop_uidir", "desktop_applaunchname"):
        if not meta.get(k):
            die("manifest 缺少字段: " + k)
    if meta["appname"] != APPNAME:
        die("manifest appname 不匹配: %s != %s" % (meta["appname"], APPNAME))
    # JSON 校验
    for rel in ("config/privilege", "config/resource", "wizard/install"):
        p = os.path.join(FPK_DIR, rel)
        if not os.path.isfile(p):
            die("缺少文件: fpk/" + rel)
        try:
            json.load(open(p, "r", encoding="utf-8"))
        except Exception as e:
            die("JSON 非法: %s (%s)" % (rel, e))
    # 安装位置: install_type 不得为 root, 否则强制装到系统分区
    itype = (meta.get("install_type") or "").strip()
    if itype == "root":
        die("install_type=root 会强制安装到系统分区, 请留空以便用户选择存储空间")
    # 桌面入口 ui/config: 外层键必须为 .url, 入口 key 必须为 appname 前缀
    uicfg = os.path.join(APP_DIR, "ui", "config")
    try:
        cfg = json.load(open(uicfg, "r", encoding="utf-8"))
    except Exception as e:
        die("ui/config JSON 非法: %s" % e)
    if ".url" not in cfg or not isinstance(cfg[".url"], dict):
        die('ui/config 外层键必须为 ".url" (当前: %s)' % list(cfg.keys()))
    key = meta["desktop_applaunchname"]
    if key not in cfg[".url"]:
        die('desktop_applaunchname(%s) 必须是 ui/config ".url" 下的键' % key)
    if not key.startswith(meta["appname"] + "."):
        die("入口 key 必须以 appname 为前缀: " + key)
    entry = cfg[".url"][key]
    if entry.get("icon") != "images/icon-{0}.png":
        die('入口 icon 必须为 "images/icon-{0}.png" (当前: %s)'
            % entry.get("icon"))
    if str(entry.get("port")) != str(meta.get("service_port")):
        die("入口 port(%s) 必须与 service_port(%s) 一致"
            % (entry.get("port"), meta.get("service_port")))
    # 必需文件
    for rel in ("ICON.PNG", "ICON_256.PNG"):
        if not os.path.isfile(os.path.join(FPK_DIR, rel)):
            die("缺少 fpk/" + rel)
    for rel in ("server.py", "mailer.py", "scheduler.py", "storage.py",
                "applog.py", "bin/launcher.sh",
                "web/index.html", "web/app.js", "web/style.css",
                "ui/images/icon-64.png", "ui/images/icon-256.png"):
        if not os.path.isfile(os.path.join(APP_DIR, rel)):
            die("缺少 app/" + rel)
    for rel in ("main", "install_init", "install_callback", "uninstall_init",
                "uninstall_callback", "upgrade_init", "upgrade_callback",
                "config_init", "config_callback"):
        if not os.path.isfile(os.path.join(FPK_DIR, "cmd", rel)):
            die("缺少 fpk/cmd/" + rel)
    print("校验通过: manifest/JSON/cmd/图标/后端 均齐全")


def add_file(tf, arcname, srcpath, mode=0o644):
    ti = tarfile.TarInfo(arcname)
    ti.size = os.path.getsize(srcpath)
    ti.mode = mode
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = int(os.path.getmtime(srcpath))
    with open(srcpath, "rb") as f:
        tf.addfile(ti, f)


def add_bytes(tf, arcname, data, mode=0o644):
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mode = mode
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = int(os.path.getmtime(__file__) or 0)
    tf.addfile(ti, io.BytesIO(data))


def add_dir(tf, arcname):
    ti = tarfile.TarInfo(arcname)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = int(os.path.getmtime(__file__) or 0)
    tf.addfile(ti)


def build_app_tgz(out_path):
    """app/ -> app.tgz, 条目带 ./ 前缀。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, dirs, files in os.walk(APP_DIR):
            dirs.sort()
            # 排除本地调试数据与缓存
            dirs[:] = [d for d in dirs
                       if d not in ("devvar", "__pycache__", ".git")]
            files = [f for f in sorted(files) if not f.endswith(".pyc")]
            rel = os.path.relpath(root, APP_DIR).replace("\\", "/")
            if rel != ".":
                add_dir(tf, "./" + rel)
            for fn in files:
                fp = os.path.join(root, fn)
                rel_f = os.path.relpath(fp, APP_DIR).replace("\\", "/")
                mode = 0o755 if fn in EXEC_FILES else 0o644
                add_file(tf, "./" + rel_f, fp, mode)
    data = buf.getvalue()
    with open(out_path, "wb") as f:
        f.write(data)
    print("app.tgz: %d bytes (%d 文件)" % (len(data),
          sum(len(f) for _, _, f in os.walk(APP_DIR))))
    return out_path


def build_fpk(app_tgz_path):
    meta = read_manifest()
    out = os.path.join(DIST, "%s-%s_%s.fpk" % (APPNAME, VERSION, PLATFORM))
    os.makedirs(DIST, exist_ok=True)
    root_dir = APPNAME  # fpk 根目录名
    with tarfile.open(out, "w:gz") as tf:
        add_dir(tf, root_dir)
        add_file(tf, "%s/manifest" % root_dir,
                 os.path.join(FPK_DIR, "manifest"))
        add_file(tf, "%s/ICON.PNG" % root_dir,
                 os.path.join(FPK_DIR, "ICON.PNG"))
        add_file(tf, "%s/ICON_256.PNG" % root_dir,
                 os.path.join(FPK_DIR, "ICON_256.PNG"))
        add_dir(tf, "%s/config" % root_dir)
        add_file(tf, "%s/config/privilege" % root_dir,
                 os.path.join(FPK_DIR, "config", "privilege"))
        add_file(tf, "%s/config/resource" % root_dir,
                 os.path.join(FPK_DIR, "config", "resource"))
        add_dir(tf, "%s/cmd" % root_dir)
        for fn in ("main", "install_init", "install_callback",
                   "uninstall_init", "uninstall_callback",
                   "upgrade_init", "upgrade_callback",
                   "config_init", "config_callback"):
            add_file(tf, "%s/cmd/%s" % (root_dir, fn),
                     os.path.join(FPK_DIR, "cmd", fn), mode=0o755)
        add_dir(tf, "%s/wizard" % root_dir)
        add_file(tf, "%s/wizard/install" % root_dir,
                 os.path.join(FPK_DIR, "wizard", "install"))
        add_file(tf, "%s/app.tgz" % root_dir, app_tgz_path)
    print("fpk 输出: %s (%d bytes)" % (out, os.path.getsize(out)))
    return out


def verify(out):
    print("== 验证 fpk 结构 ==")
    with tarfile.open(out, "r:gz") as tf:
        names = tf.getnames()
        need = ["%s/manifest" % APPNAME, "%s/ICON.PNG" % APPNAME,
               "%s/config/privilege" % APPNAME, "%s/config/resource" % APPNAME,
               "%s/app.tgz" % APPNAME, "%s/wizard/install" % APPNAME]
        for n in need:
            if n not in names:
                die("fpk 缺少: " + n)
        for n in names:
            if "/cmd/" in n:
                m = tf.getmember(n)
                if m.mode & 0o111 != 0o111:
                    die("cmd 无执行位: " + n)
        # app.tgz 内 launcher.sh 执行位
        m = tf.extractfile("%s/app.tgz" % APPNAME)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
            tmp.write(m.read())
            tmpath = tmp.name
        with tarfile.open(tmpath, "r:gz") as atf:
            am = atf.getmember("./bin/launcher.sh")
            if am.mode & 0o111 != 0o111:
                die("launcher.sh 无执行位")
            anames = atf.getnames()
            for req in ("./server.py", "./web/index.html", "./ui/config",
                        "./ui/images/icon-64.png", "./ui/images/icon-256.png"):
                if req not in anames:
                    die("app.tgz 缺少: " + req)
        os.remove(tmpath)
    print("fpk 结构验证通过 ✓")
    print("成员: %s" % ", ".join(names))


def main():
    meta = read_manifest()
    validate(meta)
    global VERSION, PLATFORM
    VERSION = meta.get("version", VERSION)
    PLATFORM = meta.get("platform", PLATFORM)
    tgz = os.path.join(BASE, "pack", "app.tgz")
    os.makedirs(os.path.dirname(tgz), exist_ok=True)
    build_app_tgz(tgz)
    out = build_fpk(tgz)
    verify(out)
    print("\n完成! 产物: %s" % out)


if __name__ == "__main__":
    main()
