# -*- coding: utf-8 -*-
"""
离线验证：用合成数据预置股票库 + 缓存，验证 UI 三大新能力：
  1. 启动时从缓存读取（不联网、秒开、渲染正常）
  2. 股票库下拉切换（两只见缓存股票互切）
  3. 删除股票（从库中移除 + 清理缓存文件）
运行：python test_offline.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

from core import data_fetcher, library

FAKE = [
    ("123456", "测试一号"),
    ("654321", "测试二号"),
]

# 测试前后备份/恢复真实股票库，避免污染用户数据
_LIB_BACKUP = None


def backup_real_library():
    global _LIB_BACKUP
    try:
        if os.path.exists(library.LIBRARY_PATH):
            with open(library.LIBRARY_PATH, "r", encoding="utf-8") as f:
                _LIB_BACKUP = json.load(f)
    except Exception:  # noqa: BLE001
        _LIB_BACKUP = None


def restore_real_library():
    if _LIB_BACKUP is not None:
        library.save_library(_LIB_BACKUP)


def make_hist(seed=1):
    rng = np.random.RandomState(seed)
    n = 150
    dates = pd.bdate_range("2026-01-05", periods=n)
    close = np.linspace(100, 130, n) + np.sin(np.arange(n) / 5) * 3
    opn = close + rng.uniform(-1, 1, n)
    high = np.maximum(opn, close) + rng.uniform(0, 1, n)
    low = np.minimum(opn, close) - rng.uniform(0, 1, n)
    vol = rng.uniform(1e5, 5e5, n)
    return pd.DataFrame({
        "日期": dates.strftime("%Y-%m-%d"), "开盘": opn.round(2),
        "收盘": close.round(2), "最高": high.round(2), "最低": low.round(2),
        "成交量": vol.round(0), "成交额": (vol * close).round(0),
    })


def seed_cache():
    """预置两只股票的历史K线缓存（时间戳=当前，视为新鲜，避免后台刷新触发联网）。"""
    for i, (code, name) in enumerate(FAKE):
        key = data_fetcher.hist_cache_key(code, "daily")
        data_fetcher._cache_save(key, make_hist(i + 1))
    # 同步写入 meta 时间戳（_cache_save 已写，此处确认新鲜）
    for code, _ in FAKE:
        ts = data_fetcher.cache_timestamp(data_fetcher.hist_cache_key(code, "daily"))
        assert ts is not None, f"{code} 缓存时间戳缺失"


def reset_state():
    lib = {"stocks": [], "last_viewed": ""}
    library.save_library(lib)
    for code, _ in FAKE:
        data_fetcher.delete_stock_cache(code)


def main():
    backup_real_library()
    reset_state()
    # 建立股票库
    for code, name in FAKE:
        library.add_stock(code, name)
    library.set_last_viewed(FAKE[0][0])
    seed_cache()

    at = AppTest.from_file("app.py", default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    print("✅ 1) 启动成功（缓存优先，无异常），标题:", at.title[0].value)
    print("   指标卡:", len(at.metric), "| 图表:", len(at.get("plotly_chart")),
          "| 数据框:", len(at.dataframe))

    # 策略状态卡应出现（4张，但 status_card 中只有含“策略”两字的被匹配到）
    cards = [m.value for m in at.markdown if "border-left" in m.value]
    print("   ✅ 策略信号看板卡片:", len(cards))

    # 切换股票：selectbox[0]=周期, [1]=股票库
    sbs = at.selectbox
    print(f"   selectbox 数量: {len(sbs)}（0=周期, 1=股票库）")
    assert len(sbs) >= 2
    sbs[1].select(1)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    print(f"   ✅ 2) 切换到股票库第2只后无异常，selectbox当前值: {sbs[1].value}")

    # 删除股票：先确保选中第2只，再删除
    at.button(key="btn_del").click()
    at.run()
    assert not at.exception
    assert at.button(key="btn_del_yes")
    print("   ✅ 3) 删除确认框出现")
    at.button(key="btn_del_yes").click()
    at.run()
    assert not at.exception
    lib = library.load_library()
    assert len(lib["stocks"]) == 1 and lib["stocks"][0]["code"] == FAKE[0][0], lib
    # 验证被删股票缓存已清理
    ts = data_fetcher.cache_timestamp(data_fetcher.hist_cache_key(FAKE[1][0], "daily"))
    assert ts is None, "被删股票缓存未清理"
    print("   ✅ 删除成功：库内剩 1 只，被删股票缓存文件已清理")

    print("\n🎉 离线验证全部通过")
    reset_state()
    restore_real_library()


if __name__ == "__main__":
    main()
