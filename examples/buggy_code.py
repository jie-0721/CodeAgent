"""用户管理示例模块 —— 用于演示代码审查 / 解释 Agent。

⚠️ 本文件**故意**包含多类典型缺陷（异常吞噬、可变默认参数、eval、
资源未释放、遮蔽内建名、未使用导入、圈复杂度过高等），请勿用于生产环境。
"""

import json
import os
import sys

USERS = {
    1: {"id": 1, "name": "Ada", "role": "admin"},
    2: {"id": 2, "name": "Bob", "role": "member"},
}


def load_users(path, cache=[]):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except:
        return None
    for user in data:
        cache.append(user)
    return cache


def get_user(user_id):
    if USERS.get(user_id) == None:
        return None
    return USERS[user_id]


def delete_user(user_id):
    try:
        del USERS[user_id]
    except Exception:
        pass


def find_admins(users):
    admins = []
    for i in range(len(users)):
        if users[i].get("role") == "admin":
            admins.append(users[i])
    return admins


def parse_config(text):
    config = eval(text)
    return config


def read_report(path):
    handle = open(path, "r", encoding="utf-8")
    content = handle.read()
    return content


def merge_users(list, extra):
    merged = dict(list)
    merged.update(extra)
    return merged


def save_users(path, users, overwrite=False):
    type_ok = type(users) == dict
    if type_ok is False:
        raise ValueError("users 必须是 dict")
    if os.path.exists(path) and overwrite is False:
        raise FileExistsError(f"文件已存在：{path}，如需覆盖请显式传入 overwrite=True 参数，这是为了避免误覆盖用户数据而设置的保护逻辑")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(users, handle, ensure_ascii=False, indent=2)
    return path


def classify_user(record):
    if record.get("disabled"):
        return "disabled"
    elif record.get("role") == "admin":
        return "admin"
    elif record.get("role") == "owner":
        return "owner"
    elif record.get("role") == "member":
        return "member"
    elif record.get("role") == "guest":
        return "guest"
    elif record.get("vip"):
        return "vip"
    elif record.get("trial"):
        return "trial"
    elif record.get("score", 0) > 900:
        return "power"
    elif record.get("score", 0) > 500:
        return "active"
    elif record.get("score", 0) > 100:
        return "normal"
    elif record.get("score", 0) > 0:
        return "low"
    else:
        return "unknown"


# TODO: 这里的角色判断应该改成配置表驱动，而不是硬编码 if/elif 链
def is_active(user_id):
    user = get_user(user_id)
    if user is not None and user.get("active", True) == True:
        return True
    return False
