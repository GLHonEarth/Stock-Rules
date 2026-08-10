# -*- coding: utf-8 -*-
"""
股票库模块：持久化用户的股票列表与最后查看记录。
数据保存在 data/stock_library.json，跨会话保留。
"""
import json
import os
import time

from core import config

DATA_DIR = os.path.join(config.BASE_DIR, "data")
LIBRARY_PATH = os.path.join(DATA_DIR, "stock_library.json")

_DEFAULT = {"stocks": [], "last_viewed": ""}


def load_library():
    """读取股票库，无文件或损坏时返回空库。"""
    if not os.path.exists(LIBRARY_PATH):
        return dict(_DEFAULT)
    try:
        with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
            lib = json.load(f)
        lib.setdefault("stocks", [])
        lib.setdefault("last_viewed", "")
        return lib
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT)


def save_library(lib):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


def get_stock(code):
    lib = load_library()
    for s in lib["stocks"]:
        if s["code"] == code:
            return s
    return None


def add_stock(code, name="", is_view=True):
    """添加股票到库（已存在则不重复添加），返回更新后的库。"""
    lib = load_library()
    if not any(s["code"] == code for s in lib["stocks"]):
        lib["stocks"].append({
            "code": code,
            "name": name or code,
            "added_at": time.strftime("%Y-%m-%d"),
        })
    if is_view:
        lib["last_viewed"] = code
    save_library(lib)
    return lib


def update_name(code, name):
    """数据抓取后回填更准确的股票名称。"""
    if not name:
        return
    lib = load_library()
    for s in lib["stocks"]:
        if s["code"] == code and (not s.get("name") or s["name"] == code):
            s["name"] = name
            save_library(lib)
            return


def remove_stock(code):
    """从库中删除股票；若被删的是最后查看项，切换到第一只（或清空）。"""
    lib = load_library()
    lib["stocks"] = [s for s in lib["stocks"] if s["code"] != code]
    if lib["last_viewed"] == code:
        lib["last_viewed"] = lib["stocks"][0]["code"] if lib["stocks"] else ""
    save_library(lib)
    return lib


def set_last_viewed(code):
    lib = load_library()
    if any(s["code"] == code for s in lib["stocks"]):
        lib["last_viewed"] = code
        save_library(lib)


def display_label(s):
    """侧边栏显示文案：名称（代码）。"""
    return f"{s.get('name') or s['code']}（{s['code']}）"
