# -*- coding: utf-8 -*-
"""邮件收发模块：SMTP 发送 / IMAP & POP3 收取。
纯标准库实现，兼容 Python 3.9+。
163 邮箱需在 IMAP 登录前发送 ID 命令（官方客户端伪装），否则报
“Unsafe Login / 请使用授权码登录” 或 SELECT 失败。
"""
import email
import email.header
import email.utils
import imaplib
import os
import poplib
import re
import smtplib
import socket
import ssl
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

# 预设模板：163 / 阿里个人 / 阿里企业 / 自定义
PRESETS = {
    "163": {
        "label": "网易163邮箱",
        "smtp": {"host": "smtp.163.com", "port": 465, "ssl": True},
        "pop3": {"host": "pop.163.com", "port": 995, "ssl": True},
        "imap": {"host": "imap.163.com", "port": 993, "ssl": True},
        "note": "需在网页版开启 SMTP/IMAP/POP3 服务并生成授权码",
    },
    "aliyun": {
        "label": "阿里云邮箱(个人)",
        "smtp": {"host": "smtp.aliyun.com", "port": 465, "ssl": True},
        "pop3": {"host": "pop.aliyun.com", "port": 995, "ssl": True},
        "imap": {"host": "imap.aliyun.com", "port": 993, "ssl": True},
        "note": "阿里个人邮箱，需开启 POP3/IMAP/SMTP 服务",
    },
    "aliyun-qiye": {
        "label": "阿里云邮箱(企业版)",
        "smtp": {"host": "smtp.qiye.aliyun.com", "port": 465, "ssl": True},
        "pop3": {"host": "pop.qiye.aliyun.com", "port": 995, "ssl": True},
        "imap": {"host": "imap.qiye.aliyun.com", "port": 993, "ssl": True},
        "note": "阿里企业邮箱，需管理员开启对应协议",
    },
    "custom": {
        "label": "自定义",
        "smtp": {"host": "", "port": 465, "ssl": True},
        "pop3": {"host": "", "port": 995, "ssl": True},
        "imap": {"host": "", "port": 993, "ssl": True},
        "note": "手动填写服务器地址",
    },
}

SOCKET_TIMEOUT = 30


def _decode_header_value(v):
    """解码邮件头 (=?utf-8?B?...?= 等)。"""
    if not v:
        return ""
    parts = email.header.decode_header(v)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(data)
    return "".join(out)


def guess_attachment_msgid(path, size):
    return make_msgid()


class MailError(Exception):
    pass


# ================= 发送 =================

def _ssl_context():
    ctx = ssl.create_default_context()
    return ctx


def _attach_file(msg, path):
    """把文件加入附件。"""
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.basename(path)
    # RFC2231 编码非 ASCII 文件名
    part = MIMEBase("application", "octet-stream")
    part.set_payload(data)
    encoders.encode_base64(part)
    try:
        part.add_header("Content-Disposition", "attachment",
                        filename=name)
    except Exception:
        part.add_header("Content-Disposition", "attachment",
                        filename=name.encode("utf-8").decode("latin-1", "replace"))
    msg.attach(part)


def send_mail(account, to_addrs, subject, body, attachments=None,
              cc=None, html=False):
    """发送邮件。attachments: [路径或目录]; 返回 dict。
    account: {smtp_host,smtp_port,smtp_ssl,username,password,from_addr}
    """
    to_addrs = [a.strip() for a in to_addrs if a and a.strip()]
    if not to_addrs:
        raise MailError("收件人为空")
    cc = [a.strip() for a in (cc or []) if a and a.strip()]
    user = account.get("username", "")
    pwd = account.get("password", "")
    host = account.get("smtp_host", "")
    port = int(account.get("smtp_port", 465) or 465)
    use_ssl = bool(account.get("smtp_ssl", True))
    from_addr = account.get("from_addr") or user

    msg = MIMEMultipart()
    msg["From"] = formataddr((account.get("display_name") or user, from_addr))
    msg["To"] = ", ".join(to_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    subtype = "html" if html else "plain"
    msg.attach(MIMEText(body, subtype, "utf-8"))

    files_sent = []
    for att in (attachments or []):
        if not att:
            continue
        if os.path.isdir(att):
            for root, _dirs, fnames in os.walk(att):
                for fn in sorted(fnames):
                    p = os.path.join(root, fn)
                    if os.path.isfile(p):
                        _attach_file(msg, p)
                        files_sent.append(p)
        elif os.path.isfile(att):
            _attach_file(msg, att)
            files_sent.append(att)
        else:
            raise MailError("附件路径不存在: %s" % att)

    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=SOCKET_TIMEOUT,
                                  context=_ssl_context())
    else:
        server = smtplib.SMTP(host, port, timeout=SOCKET_TIMEOUT)
    try:
        if not use_ssl:
            try:
                server.starttls(context=_ssl_context())
            except Exception:
                pass  # 服务器可能不支持 STARTTLS
        server.login(user, pwd)
        rcpt = to_addrs + cc
        server.sendmail(from_addr, rcpt, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("认证失败(检查账号/授权码): %s" % _clean_err(e))
    except smtplib.SMTPException as e:
        raise MailError("SMTP错误: %s" % _clean_err(e))
    except (socket.timeout, OSError) as e:
        raise MailError("网络错误(检查地址/端口/网络): %s" % _clean_err(e))
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"ok": True, "to": to_addrs, "cc": cc,
            "files": files_sent, "time": time.time()}


def _clean_err(e):
    s = str(e)
    # 163 等国内服务器错误信息常为 GBK 字节串, 尝试解码为可读中文
    try:
        m = re.search(r"b'([^']*)'", s)
        if m:
            raw = m.group(1)
            bs = raw.encode("latin-1", "backslashreplace") \
                     .decode("unicode_escape").encode("latin-1")
            txt = None
            for enc in ("gbk", "utf-8"):
                try:
                    txt = bs.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if txt:
                s = txt
    except Exception:
        pass
    return re.sub(r"[\r\n]+", " ", s)[:300]


def test_smtp(account):
    """测试 SMTP 连通性。"""
    host = account.get("smtp_host", "")
    port = int(account.get("smtp_port", 465) or 465)
    use_ssl = bool(account.get("smtp_ssl", True))
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    if use_ssl:
        srv = smtplib.SMTP_SSL(host, port, timeout=SOCKET_TIMEOUT,
                               context=_ssl_context())
    else:
        srv = smtplib.SMTP(host, port, timeout=SOCKET_TIMEOUT)
    try:
        if not use_ssl:
            try:
                srv.starttls(context=_ssl_context())
            except Exception:
                pass
        srv.login(account.get("username", ""), account.get("password", ""))
        return {"ok": True, "msg": "SMTP 登录成功"}
    except Exception as e:
        raise MailError("SMTP 测试失败: %s" % _clean_err(e))
    finally:
        try:
            srv.quit()
        except Exception:
            pass


# ================= 接收：IMAP =================

def _imap_connect(account):
    host = account.get("imap_host", "")
    port = int(account.get("imap_port", 993) or 993)
    use_ssl = bool(account.get("imap_ssl", True))
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port, timeout=SOCKET_TIMEOUT)
    else:
        conn = imaplib.IMAP4(host, port, timeout=SOCKET_TIMEOUT)
    # 163 必需：登录前发送 ID 命令，伪装已知客户端
    try:
        conn._simple_command(
            'ID', '("name" "fnmail" "contact" "noreply@fnos.local" "version" "1.0.0")')
        conn._untagged_responses.pop('ID', None)
    except Exception:
        pass  # 非 163 服务器一般容忍未知命令；个别抛错则忽略
    try:
        conn.login(account.get("username", ""), account.get("password", ""))
    except imaplib.IMAP4.error as e:
        try:
            conn.logout()
        except Exception:
            pass
        raise MailError("IMAP 登录失败(检查账号/授权码): %s" % _clean_err(e))
    return conn


def test_imap(account):
    conn = _imap_connect(account)
    try:
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise MailError("IMAP 无法打开 INBOX")
        return {"ok": True, "msg": "IMAP 登录成功"}
    except MailError:
        raise
    except Exception as e:
        raise MailError("IMAP 测试失败: %s" % _clean_err(e))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _parse_email(raw_bytes):
    """解析 RFC822 原始邮件为简化 dict。"""
    m = email.message_from_bytes(raw_bytes)
    out = {
        "subject": _decode_header_value(m.get("Subject", "")),
        "from": _decode_header_value(m.get("From", "")),
        "to": _decode_header_value(m.get("To", "")),
        "date": m.get("Date", ""),
        "message_id": m.get("Message-ID", ""),
        "text": "",
        "html": "",
        "attachments": [],
    }
    if m.is_multipart():
        for part in m.walk():
            if part.is_multipart():
                continue
            _consume_part(part, out)
    else:
        _consume_part(m, out)
    if not out["text"] and not out["html"]:
        out["text"] = "(无正文)"
    return out


def _consume_part(part, out):
    ctype = part.get_content_type()
    disp = (part.get("Content-Disposition") or "").lower()
    fname = part.get_filename()
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        return
    if fname or "attachment" in disp:
        out["attachments"].append({
            "filename": _decode_header_value(fname) if fname else "unnamed",
            "size": len(payload),
        })
    elif ctype == "text/plain":
        charset = part.get_content_charset() or "utf-8"
        out["text"] += payload.decode(charset, errors="replace")
    elif ctype == "text/html":
        charset = part.get_content_charset() or "utf-8"
        out["html"] += payload.decode(charset, errors="replace")


def fetch_imap(account, folder="INBOX", limit=20, since=None, mark_seen=False):
    """IMAP 收件。返回邮件列表（新->旧）。since: 'YYYY-MM-DD'。"""
    conn = _imap_connect(account)
    try:
        typ, _ = conn.select(folder, readonly=not mark_seen)
        if typ != "OK":
            raise MailError("IMAP 无法打开文件夹: %s" % folder)
        crit = None
        if since:
            crit = '(SINCE "%s")' % since
        typ, data = conn.search(None, crit) if crit else conn.search(None, "ALL")
        if typ != "OK":
            raise MailError("IMAP SEARCH 失败")
        ids = (data[0] or b"").split()
        ids = ids[-limit:] if limit else ids  # 最后 limit 封
        mails = []
        for num in reversed(ids):  # 新 -> 旧
            typ, d = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not d or d[0] is None:
                continue
            raw = d[0][1] if isinstance(d[0], tuple) else None
            if raw:
                mails.append(_parse_email(raw))
        return mails
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def list_imap_folders(account):
    conn = _imap_connect(account)
    try:
        typ, boxes = conn.list()
        folders = []
        for b in boxes or []:
            s = b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)
            m = re.search(r' "?([^" ]+)"?$', s)
            if m:
                folders.append(m.group(1).strip('"'))
        return sorted(set(folders))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ================= 接收：POP3 =================

def _pop_connect(account):
    host = account.get("pop3_host", "")
    port = int(account.get("pop3_port", 995) or 995)
    use_ssl = bool(account.get("pop3_ssl", True))
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    if use_ssl:
        conn = poplib.POP3_SSL(host, port, timeout=SOCKET_TIMEOUT)
    else:
        conn = poplib.POP3(host, port, timeout=SOCKET_TIMEOUT)
    try:
        conn.user(account.get("username", ""))
        conn.pass_(account.get("password", ""))
    except poplib.error_proto as e:
        try:
            conn.quit()
        except Exception:
            pass
        raise MailError("POP3 登录失败(检查账号/授权码): %s" % _clean_err(e))
    return conn


def test_pop3(account):
    conn = _pop_connect(account)
    try:
        n, _sz = conn.stat()
        return {"ok": True, "msg": "POP3 登录成功, 共 %d 封" % n}
    except Exception as e:
        raise MailError("POP3 测试失败: %s" % _clean_err(e))
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def fetch_pop3(account, limit=20, mark_seen=True):
    """POP3 收件（默认收后删除标记，POP3 服务器通常无已读概念，
    用 mark_seen=False 时仅查看不改变服务器状态）。"""
    conn = _pop_connect(account)
    try:
        n, _sz = conn.stat()
        start = max(1, n - limit + 1)
        mails = []
        for i in range(n, start - 1, -1):
            try:
                resp, lines, _oct = conn.retr(i)
            except Exception:
                continue
            raw = b"\r\n".join(lines)
            mails.append(_parse_email(raw))
        return mails
    finally:
        try:
            conn.quit()
        except Exception:
            pass
