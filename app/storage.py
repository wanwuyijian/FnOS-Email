# -*- coding: utf-8 -*-
"""JSON 存储模块：原子写 + 备份 + schema 迁移。

数据文件（位于 TRIM_PKGVAR）：
  config.json        账号/任务/设置/管理密码
  tasks_history.json 每任务的历史发送记录
"""
import copy
import json
import os
import shutil
import threading
import time

SCHEMA_VERSION = 1


def _default_config():
    return {
        "schema_version": SCHEMA_VERSION,
        "accounts": [],          # 邮箱账号列表
        "tasks": [],             # 定时任务列表
        "settings": {},          # 全局设置
        "admin_password": None,  # PBKDF2 哈希 {"salt","hash","iters"}
    }


class Store:
    def __init__(self, var_dir):
        self.var_dir = var_dir
        self.config_path = os.path.join(var_dir, "config.json")
        self.history_path = os.path.join(var_dir, "tasks_history.json")
        self.lock = threading.RLock()
        self.data = None
        self.history = {}
        self.load()

    # ---------- 通用 ----------
    def _atomic_write(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 保留一份备份
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        os.replace(tmp, path)

    def _load_json(self, path, default):
        if not os.path.exists(path):
            return copy.deepcopy(default)
        for p in (path, path + ".bak"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
        return copy.deepcopy(default)

    # ---------- 配置 ----------
    def load(self):
        with self.lock:
            self.data = self._load_json(self.config_path, _default_config())
            self._migrate()
            self.history = self._load_json(self.history_path, {})
            try:
                os.chmod(self.config_path, 0o600)
            except Exception:
                pass

    def _migrate(self):
        """schema_version 迁移：升级 FPK 后自动适配旧配置。"""
        d = self.data
        sv = d.get("schema_version", 0)
        if sv > SCHEMA_VERSION:
            # 系统盘上跑着更新的版本？记录但不破坏
            pass
        # --- 未来迁移示例 ---
        # if sv < 2: d["new_field"]=default; d["schema_version"]=2
        d.setdefault("accounts", [])
        d.setdefault("tasks", [])
        d.setdefault("settings", {})
        d.setdefault("admin_password", None)
        d["schema_version"] = SCHEMA_VERSION

    def save(self):
        with self.lock:
            self._atomic_write(self.config_path, self.data)

    def get(self, *keys):
        with self.lock:
            cur = self.data
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return None
                cur = cur[k]
            return cur

    def set_setting(self, key, value):
        with self.lock:
            self.data["settings"][key] = value
            self.save()

    # ---------- 账号 ----------
    def list_accounts(self):
        with self.lock:
            return copy.deepcopy(self.data["accounts"])

    def get_account(self, acc_id):
        with self.lock:
            for a in self.data["accounts"]:
                if a["id"] == acc_id:
                    return copy.deepcopy(a)
        return None

    def upsert_account(self, acc):
        with self.lock:
            accs = self.data["accounts"]
            if acc.get("id"):
                for i, a in enumerate(accs):
                    if a["id"] == acc["id"]:
                        accs[i] = acc
                        break
                else:
                    accs.append(acc)
            else:
                acc["id"] = "acc_%d" % (int(time.time() * 1000) % 10**9,)
                acc["id"] = "acc_" + hex(int(time.time() * 1000000))[-10:]
                accs.append(acc)
            self.save()
            return acc

    def delete_account(self, acc_id):
        with self.lock:
            self.data["accounts"] = [a for a in self.data["accounts"]
                                     if a["id"] != acc_id]
            # 关联任务置为禁用并清空账号引用
            for t in self.data["tasks"]:
                if t.get("account_id") == acc_id:
                    t["account_id"] = None
                    t["enabled"] = False
            self.save()

    def test_account_password(self, acc, password):
        """保存授权码前验证：仅检查非空。实际连通测试由 mailer 完成。"""
        return bool(password)

    # ---------- 任务 ----------
    def list_tasks(self):
        with self.lock:
            return copy.deepcopy(self.data["tasks"])

    def get_task(self, task_id):
        with self.lock:
            for t in self.data["tasks"]:
                if t["id"] == task_id:
                    return copy.deepcopy(t)
        return None

    def upsert_task(self, task):
        with self.lock:
            tasks = self.data["tasks"]
            if task.get("id"):
                for i, t in enumerate(tasks):
                    if t["id"] == task["id"]:
                        # 保留运行时字段
                        for rk in ("last_run", "last_result", "next_run"):
                            if rk in tasks[i]:
                                task.setdefault(rk, tasks[i][rk])
                        tasks[i] = task
                        break
                else:
                    tasks.append(task)
            else:
                task["id"] = "task_" + hex(int(time.time() * 1000000))[-10:]
                tasks.append(task)
            self.save()
            return task

    def delete_task(self, task_id):
        with self.lock:
            self.data["tasks"] = [t for t in self.data["tasks"]
                                   if t["id"] != task_id]
            self.history.pop(task_id, None)
            self.save()
            self._atomic_write(self.history_path, self.history)

    def update_task_runtime(self, task_id, fields):
        with self.lock:
            for t in self.data["tasks"]:
                if t["id"] == task_id:
                    t.update(fields)
                    break
            self.save()

    # ---------- 历史 ----------
    def add_history(self, task_id, rec):
        with self.lock:
            lst = self.history.setdefault(task_id, [])
            lst.insert(0, rec)
            del lst[20:]  # 每任务保留20条
            self._atomic_write(self.history_path, self.history)

    def list_history(self, task_id=None):
        with self.lock:
            if task_id:
                return copy.deepcopy(self.history.get(task_id, []))
            out = {}
            for k, v in self.history.items():
                out[k] = copy.deepcopy(v)
            return out
