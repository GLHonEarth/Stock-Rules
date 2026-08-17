# -*- coding: utf-8 -*-
"""
股票智能技术分析与策略决策系统 —— Streamlit 可视化看板（PRD 第 4 章）

运行方式：
    streamlit run app.py
浏览器访问 http://localhost:8501

交互设计：
  - 股票库持久化在 data/stock_library.json，跨会话保留
  - 启动/切换股票时优先读本地缓存（秒开），后台线程自动刷新过期数据
  - 可一键删除股票（同时清除其本地缓存数据）
"""
import concurrent.futures as cf
import copy
import os
import sys
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config, data_fetcher, library, position, strategies  # noqa: E402

PERIOD_MAP = {"日K": "daily", "周K": "weekly", "月K": "monthly"}
TAG_COLOR = {
    "bull": ("#16a34a", "看多"),
    "bear": ("#dc2626", "看空"),
    "hold": ("#f59e0b", "持有"),
    "wait": ("#6b7280", "等待"),
    "neutral": ("#2563eb", "中性"),
    "danger": ("#991b1b", "警示"),
}

# 策略状态 -> 中文短标签
TAG_CN = {
    "bull": "看多",
    "bear": "看空",
    "hold": "持有",
    "wait": "等待",
    "neutral": "中性",
    "danger": "警示",
}
# 综合分类颜色
CAT_COLOR = {
    "看多": "#16a34a",
    "观望": "#64748b",
    "看空": "#dc2626",
    "警示": "#f59e0b",
    "数据异常": "#991b1b",
}
# 综合信号计分：bull+1 / bear-1 / danger-2 / 其余 0
TAG_WEIGHT = {"bull": 1, "bear": -1, "hold": 0, "wait": 0, "neutral": 0, "danger": -2}

# 后台刷新状态（进程内共享）：code -> "running"/"done"；失败时间戳用于冷却
REFRESH_FLAGS = {}
REFRESH_SPAWNED = {}
REFRESH_FAILED = {}

st.set_page_config(page_title="股票智能技术分析与策略决策系统", layout="wide")


# --------------------------------------------------------------------------
# 数据加载：全量抓取 / 缓存优先 / 后台刷新
# --------------------------------------------------------------------------
def _fetch_all(code, period_label):
    """同步全量抓取（首次使用 / 手动刷新按钮触发）。"""
    return load_all(code, PERIOD_MAP.get(period_label, "daily"))


def load_all(code, period):
    """并行抓取一只股票的全部数据，返回 dict；单项失败返回 None 不阻塞整体。"""
    futures = {}

    def _wrap(fn):
        def _inner():
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                return ("ERR", str(e))
        return _inner

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        sina_code, em_code, name = data_fetcher.resolve_symbol(code)
        common = dict(symbol=em_code)

        futures["hist"] = ex.submit(_wrap(lambda: data_fetcher.get_hist(**common, period=period)))
        futures["realtime"] = ex.submit(_wrap(lambda: data_fetcher.get_realtime(**common)))
        futures["pe"] = ex.submit(_wrap(lambda: data_fetcher.get_valuation(**common)))
        futures["pb"] = ex.submit(_wrap(
            lambda: data_fetcher.get_valuation(**common, indicator="市净率")))
        futures["profile"] = ex.submit(_wrap(lambda: data_fetcher.get_profile(**common)))
        futures["growth"] = ex.submit(_wrap(lambda: data_fetcher.get_growth(**common)))

        out = {"code": em_code, "sina_code": sina_code, "name": name}
        for k, f in futures.items():
            out[k] = f.result()

    for k in ("hist", "realtime", "pe", "pb", "profile", "growth"):
        v = out[k]
        if isinstance(v, tuple) and v and v[0] == "ERR":
            out[k] = None
            out.setdefault("errors", {})[k] = v[1]
    out["hist_ts"] = time.time()
    return out


def _period_key(period_label):
    return PERIOD_MAP.get(period_label, "daily")


def load_cache_first(code, period_label):
    """
    缓存优先：只读本地缓存（任意时效，不触发网络），组装渲染所需 dict。
    若该周期历史K线从未缓存过，返回 None（调用方走全量抓取）。
    """
    period = _period_key(period_label)
    stock = library.get_stock(code) or {}
    data = {
        "code": code,
        "sina_code": data_fetcher.normalize_symbol(code)[0],
        "name": stock.get("name", ""),
        "hist": data_fetcher.get_hist_from_cache(code, period),
        "realtime": data_fetcher.get_realtime_from_cache(code),
        "pe": data_fetcher.get_valuation_from_cache(code),
        "pb": data_fetcher.get_valuation_from_cache(code, indicator="市净率"),
        "profile": data_fetcher.get_profile_from_cache(code),
        "growth": data_fetcher.get_growth_from_cache(code),
        "hist_ts": data_fetcher.cache_timestamp(
            data_fetcher.hist_cache_key(code, period)),
        "errors": {},
    }
    if period_label == "分时":
        data["intraday"] = data_fetcher.get_intraday_from_cache(code)
    data["from_cache"] = data["hist"] is not None
    return data


def _is_stale(code, period_label):
    """判断当前股票对应周期数据是否过期（无缓存或超 TTL）。"""
    if period_label == "分时":
        sina = data_fetcher.normalize_symbol(code)[0]
        key, ttl = f"intraday_{sina}_1", config.CACHE_TTL["intraday"]
    else:
        period = _period_key(period_label)
        key = data_fetcher.hist_cache_key(code, period)
        ttl_key = {"daily": "hist_daily", "weekly": "hist_week",
                   "monthly": "hist_month"}[period]
        ttl = config.CACHE_TTL[ttl_key]
    ts = data_fetcher.cache_timestamp(key)
    return ts is None or (time.time() - ts) > ttl


def _bg_refresh(code, period_label):
    """后台线程：抓取最新数据写入磁盘缓存（失败静默，不打断界面）。"""
    ok = False
    try:
        if period_label == "分时":
            data_fetcher.get_intraday(code)
        else:
            data_fetcher.get_hist(code, period=_period_key(period_label))
            data_fetcher.get_realtime(code)
        ok = True
    except Exception:  # noqa: BLE001
        pass
    finally:
        REFRESH_FLAGS[code] = "done"
        if not ok:
            REFRESH_FAILED[code] = time.time()


def _maybe_spawn_refresh(code, period_label, has_cache):
    """缓存过期时启动后台刷新（60s 冷却，失败后 5 分钟内不再重试）。"""
    if not has_cache or REFRESH_FLAGS.get(code):
        return
    if not _is_stale(code, period_label):
        return
    now = time.time()
    if now - REFRESH_SPAWNED.get(code, 0) < 60:
        return
    if now - REFRESH_FAILED.get(code, 0) < 300:
        return
    REFRESH_SPAWNED[code] = now
    REFRESH_FLAGS[code] = "running"
    threading.Thread(target=_bg_refresh, args=(code, period_label), daemon=True).start()


@st.fragment(run_every=3)
def _refresh_watcher(code):
    """后台刷新完成 → 整体重跑，界面自动更新为新数据。"""
    if REFRESH_FLAGS.get(code) == "done":
        REFRESH_FLAGS.pop(code, None)
        st.rerun()


def _fmt_ts(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# 图表构建
# --------------------------------------------------------------------------
def build_kline_chart(df, results, show_n=250):
    """核心图表区（PRD 4.2/4.3）：K线+均线+布林带+买卖点，成交量，MACD，KDJ，RSI。"""
    # 指标需在完整历史上计算（保证边界值连续），再切片展示最近 show_n 根
    full = strategies_ready_df(df)
    chart = full.tail(show_n).reset_index(drop=True)

    signals = strategies.merge_signals(results)
    if len(signals):
        signals = signals[signals["日期"].isin(chart["日期"])]
        # 高频策略（如马丁）单根K线可能同买同卖，为可读性每策略每方向最多画最近 40 个标记
        keep = []
        for (strat, act), g in signals.groupby(["策略", "方向"]):
            keep.append(g.tail(40))
        signals = pd.concat(keep)

    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.02,
        row_heights=[0.44, 0.11, 0.15, 0.15, 0.15],
        subplot_titles=("K线（均线/布林带/买卖点）", "成交量（手）", "MACD", "KDJ", "RSI"))

    # ---- 主图：蜡烛 + 均线 + 布林带 ----
    up = chart["收盘"] >= chart["开盘"]
    fig.add_trace(go.Candlestick(
        x=chart["日期"], open=chart["开盘"], high=chart["最高"],
        low=chart["最低"], close=chart["收盘"], name="K线",
        increasing_line_color="#ef232a", decreasing_line_color="#14b143",
        increasing_fillcolor="#ef232a", decreasing_fillcolor="#14b143"), row=1, col=1)

    ma_style = [("MA5", "#facc15", 1.2), ("MA10", "#c084fc", 1.0),
                ("MA20", "#60a5fa", 1.2), ("MA60", "#9ca3af", 1.0)]
    for col, color, w in ma_style:
        if col in chart:
            fig.add_trace(go.Scatter(
                x=chart["日期"], y=chart[col], name=col, line=dict(color=color, width=w)),
                row=1, col=1)
    for col, dash in (("BOLL_UP", "dot"), ("BOLL_LOW", "dot")):
        if col in chart:
            fig.add_trace(go.Scatter(
                x=chart["日期"], y=chart[col], name=col, line=dict(color="#64748b", width=1, dash=dash),
                showlegend=False), row=1, col=1)

    # ---- 买卖点标记（PRD 4.2：绿色上箭头=买，红色下箭头=卖）----
    for _, s in signals.iterrows():
        if s["方向"] == "buy":
            y = chart[chart["日期"] == s["日期"]]["最低"].iloc[0] * 0.985
            fig.add_trace(go.Scatter(
                x=[s["日期"]], y=[y], mode="markers+text", text=["▲"],
                textposition="top center", textfont=dict(size=13, color="#16a34a"),
                marker=dict(size=0),
                name=f"买点·{s['策略']}", legendgroup=f"buy_{s['策略']}",
                showlegend=False, hovertemplate=f"<b>买点</b> {s['策略']}<br>"
                f"日期 {s['日期']}<br>原因 {s['原因']}<extra></extra>"),
                row=1, col=1)
        else:
            y = chart[chart["日期"] == s["日期"]]["最高"].iloc[0] * 1.015
            fig.add_trace(go.Scatter(
                x=[s["日期"]], y=[y], mode="markers+text", text=["▼"],
                textposition="bottom center", textfont=dict(size=13, color="#dc2626"),
                marker=dict(size=0),
                name=f"卖点·{s['策略']}", legendgroup=f"sell_{s['策略']}",
                showlegend=False, hovertemplate=f"<b>卖点</b> {s['策略']}<br>"
                f"日期 {s['日期']}<br>原因 {s['原因']}<extra></extra>"),
                row=1, col=1)

    # ---- 副图1：成交量（红涨绿跌）----
    vol_colors = np.where(up, "rgba(239,35,42,0.7)", "rgba(20,177,67,0.7)")
    fig.add_trace(go.Bar(
        x=chart["日期"], y=chart["成交量"], name="成交量", marker_color=vol_colors,
        showlegend=False), row=2, col=1)

    # ---- 副图2：MACD ----
    if "MACD" in chart:
        macd_colors = np.where(chart["MACD"] >= 0, "rgba(239,35,42,0.7)", "rgba(20,177,67,0.7)")
        fig.add_trace(go.Bar(x=chart["日期"], y=chart["MACD"], name="MACD柱",
                             marker_color=macd_colors, showlegend=False), row=3, col=1)
        fig.add_trace(go.Scatter(x=chart["日期"], y=chart["DIF"], name="DIF",
                                 line=dict(color="#facc15", width=1.1)), row=3, col=1)
        fig.add_trace(go.Scatter(x=chart["日期"], y=chart["DEA"], name="DEA",
                                 line=dict(color="#60a5fa", width=1.1)), row=3, col=1)

    # ---- 副图3：KDJ ----
    for col, color in (("K", "#3b82f6"), ("D", "#f97316"), ("J", "#a855f7")):
        if col in chart:
            fig.add_trace(go.Scatter(x=chart["日期"], y=chart[col], name=col,
                                     line=dict(color=color, width=1.1)), row=4, col=1)

    # ---- 副图4：RSI ----
    for col, color in (("RSI6", "#3b82f6"), ("RSI12", "#f97316"), ("RSI24", "#a855f7")):
        if col in chart:
            fig.add_trace(go.Scatter(x=chart["日期"], y=chart[col], name=col,
                                     line=dict(color=color, width=1.1)), row=5, col=1)
    for level in (30, 70):
        fig.add_hline(y=level, line_dash="dot", line_color="#94a3b8", row=5, col=1)

    fig.update_layout(
        height=1000, xaxis_rangeslider_visible=False,
        dragmode="pan", margin=dict(l=40, r=20, t=60, b=30),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=11)),
        hovermode="x unified",
        xaxis=dict(type="category"))
    fig.update_xaxes(showgrid=True, gridcolor="rgba(148,163,184,0.15)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(148,163,184,0.15)")
    return fig


def strategies_ready_df(df):
    """对完整历史 df 叠加全部指标（供图表与策略共用，避免重复计算）。"""
    from core import indicators
    return indicators.add_all(df.copy())


def build_intraday_chart(intraday, prev_close):
    """分时模式（PRD 4.1）：价格线 + 均价线 + 成交量。"""
    d = intraday.copy()
    d["均价"] = (d["收盘"] * d["成交量"]).cumsum() / d["成交量"].cumsum()
    base = prev_close if prev_close and prev_close > 0 else d["收盘"].iloc[0]
    up = d["收盘"] >= base

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                        row_heights=[0.72, 0.28],
                        subplot_titles=("分时走势", "分时成交量（手）"))
    line_color = "#ef232a" if d["收盘"].iloc[-1] >= base else "#14b143"
    fig.add_trace(go.Scatter(
        x=d["日期"], y=d["收盘"], name="价格", line=dict(color=line_color, width=1.4),
        fill="tozeroy", fillcolor="rgba(99,102,241,0.08)"), row=1, col=1)
    fig.add_trace(go.Scatter(x=d["日期"], y=d["均价"], name="均价",
                             line=dict(color="#f59e0b", width=1.1, dash="dot")), row=1, col=1)
    fig.add_hline(y=base, line_dash="dash", line_color="#94a3b8", row=1, col=1,
                  annotation_text=f"昨收 {base}")
    vol_colors = np.where(up, "rgba(239,35,42,0.6)", "rgba(20,177,67,0.6)")
    fig.add_trace(go.Bar(x=d["日期"], y=d["成交量"], name="成交量",
                         marker_color=vol_colors, showlegend=False), row=2, col=1)
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, dragmode="pan",
                      margin=dict(l=40, r=20, t=50, b=30))
    return fig


# --------------------------------------------------------------------------
# UI 组件
# --------------------------------------------------------------------------
def status_card(result):
    color, tag = TAG_COLOR.get(result.status_tag, ("#6b7280", result.status_tag))
    return f"""
    <div style="border-left:5px solid {color}; background:#0f172a; border-radius:8px;
                padding:10px 14px; min-height:86px; box-shadow:0 1px 3px rgba(0,0,0,.3)">
      <div style="font-size:13px; color:#94a3b8">{result.name} · <b style="color:{color}">{tag}</b></div>
      <div style="font-size:13px; color:#e2e8f0; margin-top:6px; line-height:1.5">{result.status}</div>
    </div>
    """


def bidask_table(realtime):
    """五档盘口表：上为卖五→卖一，下为买一→买五。"""
    asks = realtime.get("卖盘") or []
    bids = realtime.get("买盘") or []
    rows = []
    for i in range(4, -1, -1):
        item = asks[i] if i < len(asks) else (0, 0)
        vol, price = (item[0], item[1]) if len(item) == 2 else (0, 0)
        rows.append({"档位": ["卖五", "卖四", "卖三", "卖二", "卖一"][i],
                     "卖价": price, "卖量(手)": int(vol),
                     "买价": np.nan, "买量(手)": np.nan})
    for i in range(5):
        item = bids[i] if i < len(bids) else (0, 0)
        vol, price = (item[0], item[1]) if len(item) == 2 else (0, 0)
        rows.append({"档位": ["买一", "买二", "买三", "买四", "买五"][i],
                     "卖价": np.nan, "卖量(手)": np.nan,
                     "买价": price, "买量(手)": int(vol)})
    return pd.DataFrame(rows)[["档位", "卖价", "卖量(手)", "买价", "买量(手)"]]


# --------------------------------------------------------------------------
# 实时行情头部：数量格式化 + 自定义行情栏（保证完整显示、时间精确到秒）
# --------------------------------------------------------------------------
def _fmt_vol(hand):
    """成交量（手）格式化：亿手 / 万手 / 手。"""
    hand = float(hand)
    if hand >= 1e8:
        return f"{hand / 1e8:.2f}亿手"
    if hand >= 1e4:
        return f"{hand / 1e4:.2f}万手"
    return f"{hand:.0f}手"


def _fmt_amount(yuan):
    """成交额（元）格式化：万亿 / 亿 / 万 / 元。"""
    yuan = float(yuan)
    if yuan >= 1e12:
        return f"{yuan / 1e12:.2f}万亿"
    if yuan >= 1e8:
        return f"{yuan / 1e8:.2f}亿"
    if yuan >= 1e4:
        return f"{yuan / 1e4:.2f}万"
    return f"{yuan:.0f}元"


def _fmt_update_time(ts_str):
    """
    规范化数据更新时间，确保精确到秒。
    兼容 "2026-08-07 15:34:59" / "2026-08-07 15:34" / "08-07 15:34:59" 等格式，
    统一输出 "MM-DD HH:MM:SS"；缺少秒的自动补 :00。
    """
    s = str(ts_str or "").strip()
    if not s:
        return "—"
    parts = s.replace("/", "-").split()
    if len(parts) >= 2:
        date_part, time_part = parts[0], parts[1]
        if len(time_part) == 5:            # HH:MM -> 补秒
            time_part += ":00"
        if len(date_part) == 10:           # "2026-08-07" -> "08-07"
            date_show = date_part[5:]
        else:
            date_show = date_part
        return f"{date_show} {time_part}"
    return s


def quote_header_html(rt, name, code):
    """
    顶部实时行情栏（HTML，flex 自动换行，任何窗口宽度下均完整显示）。
    价格红涨绿跌；更新时间精确到秒并以醒目颜色展示。
    """
    # 防御性读取：历史缓存字段可能缺失/异常，缺省不崩溃
    def _g(k, d=0.0):
        v = rt.get(k, d)
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    price = _g("最新价")
    chg = _g("涨跌幅")
    chg_amt = _g("涨跌额")
    up = chg >= 0
    color = "#ef4444" if up else "#22c55e"
    arrow = "▲" if up else "▼"
    opn, prev = _g("今开"), _g("昨收")
    hi, lo = _g("最高"), _g("最低")
    vol, amt = _g("成交量(手)"), _g("成交额(元)")
    ts = rt.get("时间戳", "") if isinstance(rt.get("时间戳", ""), str) else ""
    return f"""
    <div style="display:flex;flex-wrap:wrap;gap:12px 36px;align-items:center;
                background:#0f172a;border:1px solid #1e293b;border-radius:10px;
                padding:14px 22px;margin-bottom:8px;">
      <div style="margin-right:auto;">
        <div style="font-size:16px;color:#94a3b8;">{name}（{code}）· 最新价</div>
        <span style="font-size:40px;font-weight:800;color:{color};">{price:.2f}</span>
        <span style="margin-left:12px;font-size:22px;font-weight:800;color:{color};">{arrow} {chg:+.2f}%</span>
        <span style="margin-left:10px;font-size:16px;color:#94a3b8;">涨跌额 {chg_amt:+.2f}</span>
      </div>
      <div>
        <div style="font-size:15px;color:#94a3b8;">今开 / 昨收</div>
        <div style="font-size:21px;font-weight:700;">{opn:.2f} / {prev:.2f}</div>
      </div>
      <div>
        <div style="font-size:15px;color:#94a3b8;">最高 / 最低</div>
        <div style="font-size:21px;font-weight:700;">{hi:.2f} / {lo:.2f}</div>
      </div>
      <div>
        <div style="font-size:15px;color:#94a3b8;">成交量</div>
        <div style="font-size:21px;font-weight:700;">{_fmt_vol(vol)}</div>
      </div>
      <div>
        <div style="font-size:15px;color:#94a3b8;">成交额</div>
        <div style="font-size:21px;font-weight:700;">{_fmt_amount(amt)}</div>
      </div>
      <div>
        <div style="font-size:15px;color:#94a3b8;">数据更新时间</div>
        <div style="font-size:21px;font-weight:800;color:#facc15;">{_fmt_update_time(ts)}</div>
      </div>
    </div>
    """


def sidebar_library():
    """
    侧边栏股票库 UI：
      - 顶部添加股票（代码/名称）
      - 股票库下拉切换
      - 删除当前股票（内联二次确认）
    返回当前选中的 (code, display_name)。
    """
    with st.sidebar:
        st.header("📁 我的股票库")
        with st.expander("➕ 添加股票", expanded=True):
            q = st.text_input("输入股票代码或名称", key="add_q",
                              placeholder="如 600519 / 贵州茅台")
            if st.button("添加到股票库", width="stretch", key="btn_add"):
                if q.strip():
                    try:
                        _, em, name = data_fetcher.resolve_symbol(q.strip())
                        library.add_stock(em, name or q.strip())
                        st.session_state["added_stock"] = em
                    except Exception as e:  # noqa: BLE001
                        st.error(f"未找到该股票：{e}")
                else:
                    st.warning("请输入股票代码或名称")

        lib = library.load_library()  # 添加后需重载，确保下拉立即包含新股票
        code, disp = None, ""
        if lib["stocks"]:
            labels = [library.display_label(s) for s in lib["stocks"]]
            default_idx = next((i for i, s in enumerate(lib["stocks"])
                                if s["code"] == lib["last_viewed"]), 0)
            idx = st.selectbox("选择查看的股票", range(len(lib["stocks"])),
                               index=default_idx, format_func=lambda i: labels[i])
            code = lib["stocks"][idx]["code"]
            disp = lib["stocks"][idx].get("name") or code
            library.set_last_viewed(code)

            # 删除股票（内联二次确认）
            st.divider()
            if st.session_state.get("confirm_del") != code:
                if st.button("🗑 删除当前股票", width="stretch", key="btn_del"):
                    st.session_state["confirm_del"] = code
                    st.rerun()
            else:
                st.warning(f"确认从股票库删除「{disp}」及其本地缓存数据？")
                c1, c2 = st.columns(2)
                if c1.button("确认删除", key="btn_del_yes"):
                    st.session_state.pop("confirm_del", None)
                    removed = data_fetcher.delete_stock_cache(code)
                    library.remove_stock(code)
                    st.session_state["del_msg"] = f"已删除 {disp}，清理缓存 {removed} 个文件"
                    st.rerun()
                if c2.button("取消", key="btn_del_no"):
                    st.session_state.pop("confirm_del", None)
                    st.rerun()
        return code, disp


# --------------------------------------------------------------------------
# 股票库：一键更新全部数据 / 按策略信号分类总览
# --------------------------------------------------------------------------
def update_library_data(stocks):
    """
    一键更新股票库全部股票数据（日K/实时/估值/财务），写入本地缓存。
    返回失败列表 [(code, err)]。
    """
    n = len(stocks)
    if n == 0:
        return []
    progress = st.progress(0.0, text="准备更新...")
    errors = []
    for i, s in enumerate(stocks):
        progress.progress(i / n, text=f"正在更新 {i + 1}/{n}：{s['name']}（{s['code']}）...")
        try:
            load_all(s["code"], "daily")   # 全量抓取并写入缓存
        except Exception as e:  # noqa: BLE001
            errors.append((s["code"], str(e)[:80]))
    progress.progress(1.0, text="更新完成")
    progress.empty()
    return errors


def _stock_data_for_overview(s):
    """
    为总览页计算单只股票的四策略状态。
    性能策略：仅读本地缓存（秒开，绝不联网拖慢页面）；
    仅当历史K线从未缓存过时，才对这只股票抓取一次日K。
    估值/财务缺失时显示"—"，可用「一键更新股票库数据」补齐。
    返回 dict：code/name/price/tags/score/cat/err。
    """
    code, name = s["code"], s.get("name") or s["code"]
    try:
        hist = data_fetcher.get_hist_from_cache(code, "daily")
        if hist is None:
            hist = data_fetcher.get_hist(code, period="daily")
        pe = data_fetcher.get_valuation_from_cache(code)          # 纯缓存
        growth = data_fetcher.get_growth_from_cache(code)          # 纯缓存
        rt = data_fetcher.get_realtime_from_cache(code)            # 纯缓存
        price = rt["最新价"] if rt else (
            float(hist["收盘"].iloc[-1]) if hist is not None else None)
        pe_map = (data_fetcher.build_pe_pct_series(pe, hist)
                  if pe is not None and hist is not None else {})
        growth_pct = growth.get("净利润增长率(%)") if growth else None

        params = copy.deepcopy(config.STRATEGY_PARAMS)
        trad = strategies.run_traditional(hist, pe_map, growth_pct)
        mart = strategies.run_martingale(hist, params["martingale"])
        anti = strategies.run_anti_martingale(hist, params["anti_martingale"])
        dca = strategies.run_dca(hist, params["dca"], pe_map)

        tags = {"传统指标": trad.status_tag, "马丁策略": mart.status_tag,
                "反马丁": anti.status_tag, "定投策略": dca.status_tag}
        score = sum(TAG_WEIGHT.get(t, 0) for t in tags.values())
        if any(t == "danger" for t in tags.values()):
            cat = "警示"
        elif score > 0:
            cat = "看多"
        elif score < 0:
            cat = "看空"
        else:
            cat = "观望"
        return {"code": code, "name": name, "price": price,
                "tags": tags, "score": score, "cat": cat, "err": ""}
    except Exception as e:  # noqa: BLE001
        return {"code": code, "name": name, "price": None,
                "tags": {}, "score": 0, "cat": "数据异常", "err": str(e)[:80]}


@st.cache_data(ttl=300, show_spinner=False)
def _overview_data(stocks):
    """股票库总览数据（缓存 5 分钟；一键更新后自动失效）。"""
    return [_stock_data_for_overview(s) for s in stocks]


def _stock_card_html(r):
    """单只股票的总览卡片（HTML）。"""
    price = f"{r['price']:.2f}" if r.get("price") else "—"
    cat_color = CAT_COLOR.get(r["cat"], "#94a3b8")
    labels = [("传统", "传统指标"), ("马丁", "马丁策略"),
              ("反马丁", "反马丁"), ("定投", "定投策略")]
    tags_html = "　".join(
        f"<span style='color:{TAG_COLOR.get(r['tags'].get(k, ''), ('#94a3b8', ''))[0]}'>"
        f"{lbl}:{TAG_CN.get(r['tags'].get(k, ''), '?')}</span>"
        for lbl, k in labels)
    return f"""
    <div style="border:1px solid {cat_color};border-radius:8px;background:#0f172a;
                padding:10px 14px;min-width:210px;flex:1 1 210px;">
      <div style="font-size:15px;font-weight:700;">{r['name']}
        <span style="color:#94a3b8;font-weight:400;">{r['code']}</span>
        <span style="float:right;color:{cat_color};font-weight:800;">{r['cat']}</span></div>
      <div style="font-size:22px;font-weight:800;margin:4px 0;color:#e2e8f0;">{price}</div>
      <div style="font-size:13px;color:#94a3b8;">{tags_html}</div>
    </div>"""


def render_library_overview(stocks):
    """股票库总览页：按当前策略信号综合分类展示全部股票。"""
    st.subheader("📋 股票库总览 · 按策略信号分类")
    st.caption("综合信号 = 传统/马丁/反马丁/定投 四策略状态汇总（看多+1、看空-1、马丁警示-2）。"
               "基于本地缓存计算，点左侧「🔄 一键更新股票库数据」获取最新。")
    if not stocks:
        st.info("股票库为空，请在左侧添加股票。")
        return

    with st.spinner("正在计算各股票策略信号..."):
        rows = _overview_data(stocks)

    # ---- 统计条 ----
    cnt = {}
    for r in rows:
        cnt[r["cat"]] = cnt.get(r["cat"], 0) + 1
    st.markdown(
        f"📗 看多 **{cnt.get('看多', 0)}** 只　📙 观望 **{cnt.get('观望', 0)}** 只　"
        f"📕 看空 **{cnt.get('看空', 0)}** 只　⚠️ 警示 **{cnt.get('警示', 0)}** 只　"
        f"❌ 数据异常 **{cnt.get('数据异常', 0)}** 只")

    # ---- 策略状态矩阵表 ----
    df = pd.DataFrame([{
        "股票": f"{r['name']}（{r['code']}）",
        "最新价": round(r["price"], 2) if r.get("price") else None,
        "传统指标": TAG_CN.get(r["tags"].get("传统指标", ""), "?"),
        "马丁策略": TAG_CN.get(r["tags"].get("马丁策略", ""), "?"),
        "反马丁": TAG_CN.get(r["tags"].get("反马丁", ""), "?"),
        "定投策略": TAG_CN.get(r["tags"].get("定投策略", ""), "?"),
        "综合": r["cat"],
    } for r in rows])
    st.dataframe(df, width="stretch", hide_index=True)

    # ---- 按分类分组展示卡片 ----
    for cat in ("看多", "观望", "看空", "警示", "数据异常"):
        group = [r for r in rows if r["cat"] == cat]
        if not group:
            continue
        ccolor = CAT_COLOR.get(cat, "#94a3b8")
        st.markdown(f"### <span style='color:{ccolor};font-size:18px;'>"
                    f"{cat}（{len(group)} 只）</span>", unsafe_allow_html=True)
        cards = "".join(_stock_card_html(r) for r in group)
        st.markdown(
            f'<div style="display:flex;flex-wrap:wrap;gap:10px;">{cards}</div>',
            unsafe_allow_html=True)


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
def main():
    st.title("📈 股票智能技术分析与策略决策系统")
    st.caption("数据源：东方财富 / 新浪财经 / 百度股市通 / 巨潮资讯（全部免费接口，内置请求限速与缓存）")

    lib = library.load_library()

    # 处理"刚刚添加/删除/一键更新"的会话消息
    msg = st.session_state.pop("del_msg", None)
    if msg:
        st.toast(msg, icon="🗑")
    msg2 = st.session_state.pop("update_msg", None)
    if msg2:
        st.toast(msg2, icon="🔄")

    with st.sidebar:
        period_label = st.selectbox("时间周期", ["日K", "周K", "月K", "分时"])
        st.subheader("策略开关")
        use_trad = st.checkbox("传统技术面+基本面", value=True)
        use_mart = st.checkbox("马丁策略（风险高）", value=True)
        use_anti = st.checkbox("反马丁策略", value=True)
        use_dca = st.checkbox("定投策略", value=True)
        capital = st.number_input("模拟初始资金（元）", min_value=1000.0, value=100000.0,
                                  step=10000.0, help="用于持仓模拟计算器")
        show_n = st.slider("K线显示数量（根）", 60, 500, 250)
        st.divider()
        view = st.radio("视图", ["📈 个股分析", "📋 股票库总览"],
                        horizontal=True, help="「股票库总览」仅在打开时才计算，不拖慢个股页")
        st.divider()
        st.warning("⚠️ 本系统所有信号、仓位均为技术分析演示，不构成任何投资建议。"
                   "股市有风险，投资需谨慎。")

    code, disp = sidebar_library()

    # 一键更新股票库全部数据（进度显示，完成后自动刷新）
    with st.sidebar:
        st.divider()
        if st.button("🔄 一键更新股票库数据", width="stretch", key="btn_update_all",
                     help="重新抓取股票库中全部股票的行情/估值/财务数据并写入本地缓存，"
                          "让「股票库总览」反映最新信号。"):
            stocks_all = library.load_library()["stocks"]
            if stocks_all:
                errs = update_library_data(stocks_all)
                try:
                    _overview_data.clear()
                except Exception:  # noqa: BLE001
                    pass
                n_ok = len(stocks_all) - len(errs)
                st.session_state["update_msg"] = (
                    f"已更新 {n_ok}/{len(stocks_all)} 只股票数据"
                    + (f"，{len(errs)} 只失败：{errs[0][0]}" if errs else ""))
            else:
                st.session_state["update_msg"] = "股票库为空，无需更新"
            st.rerun()

    if not code:
        st.info("📭 股票库为空。请在左侧输入股票代码或名称，点击「添加到股票库」开始使用。"
                "添加后数据会自动获取并缓存，下次启动秒开。")
        return

    # ---------- 数据加载（缓存优先，秒开） ----------
    data = load_cache_first(code, period_label)
    if data["hist"] is None and period_label != "分时":
        with st.spinner(f"首次获取 {disp}（{code}）的行情数据..."):
            data = _fetch_all(code, period_label)
    elif data.get("intraday") is None and period_label == "分时":
        with st.spinner(f"首次获取 {disp}（{code}）的分时数据..."):
            try:
                data["intraday"] = data_fetcher.get_intraday(code)
                data["hist"] = data_fetcher.get_hist_from_cache(code, "daily") \
                    or data_fetcher.get_hist(code, period="daily")
            except Exception as e:  # noqa: BLE001
                st.error(f"分时数据获取失败：{e}")

    # 回填更准确的股票名称（实时行情/公司资料优先，代码输入时也能补全名称）
    real_name = ((data.get("realtime") or {}).get("名称")
                 or (data.get("profile") or {}).get("公司名称")
                 or data.get("name"))
    library.update_name(code, real_name)

    # 后台刷新过期数据（不阻塞界面）
    _maybe_spawn_refresh(code, period_label, data.get("from_cache", False))

    # 数据来源提示
    ts = _fmt_ts(data.get("hist_ts"))
    stale = _is_stale(code, period_label)
    if data.get("from_cache") and ts:
        if stale:
            st.caption(f"💾 数据来源：本地缓存（更新于 {ts}）｜后台正在自动刷新最新数据...")
        else:
            st.caption(f"💾 数据来源：本地缓存（更新于 {ts}）")
    elif data.get("from_cache"):
        st.caption("💾 数据来源：本地缓存")

    if data.get("hist") is None and period_label != "分时":
        st.error("未获取到历史行情数据。请检查网络后点击侧边栏「添加股票」重试，或稍后再试。")
        return

    # ---------- 渲染（惰性加载：仅渲染所选视图，避免总览拖慢个股页） ----------
    if view == "📋 股票库总览":
        render_library_overview(library.load_library()["stocks"])
    else:
        render_dashboard(data, code, disp, period_label, capital, show_n,
                         use_trad, use_mart, use_anti, use_dca)

    # 后台刷新完成自动更新
    _refresh_watcher(code)


def render_dashboard(data, code, disp, period_label, capital, show_n,
                     use_trad, use_mart, use_anti, use_dca):
    """主内容区渲染：实时行情 / 信号看板 / K线 / 持仓模拟 / 基本面 / 盘口。"""
    hist = data.get("hist")
    rt = data.get("realtime")
    title_name = data.get("name") or disp or code

    # ---------- 顶部：实时行情栏（自定义 HTML，完整显示、时间精确到秒） ----------
    if rt is not None:
        st.markdown(quote_header_html(rt, title_name, code), unsafe_allow_html=True)
    elif hist is not None:
        last = hist.iloc[-1]
        st.info(f"{title_name}（{code}）实时行情暂不可用，以下为最近交易日 "
                f"{last['日期']} 收盘价 {last['收盘']:.2f}。")

    if period_label == "分时":
        intraday = data.get("intraday")
        if intraday is None or len(intraday) == 0:
            st.error("今日暂无分时数据（非交易时段或休市）。")
            return
        st.subheader("📈 分时走势")
        prev_close = (rt or {}).get("昨收", hist["收盘"].iloc[-1] if hist is not None else None)
        st.plotly_chart(build_intraday_chart(intraday, prev_close), width="stretch")
        st.caption("💡 支持鼠标拖拽平移、滚轮缩放。")
        return

    # ---------- 信号看板（PRD 4.4） ----------
    st.subheader("📊 当前策略信号看板")
    pe_series = data.get("pe")
    pe_pct_map = {}
    if pe_series is not None and len(pe_series):
        pe_pct_map = data_fetcher.build_pe_pct_series(pe_series, hist)
    growth_pct = None
    if data.get("growth") is not None:
        growth_pct = data["growth"].get("净利润增长率(%)")

    params = dict(config.STRATEGY_PARAMS)
    for p in params.values():
        p["capital"] = capital

    enabled = []
    with st.spinner("策略计算中..."):
        if use_trad:
            enabled.append(strategies.run_traditional(hist, pe_pct_map, growth_pct))
        if use_mart:
            enabled.append(strategies.run_martingale(hist, params["martingale"]))
        if use_anti:
            enabled.append(strategies.run_anti_martingale(hist, params["anti_martingale"]))
        if use_dca:
            enabled.append(strategies.run_dca(hist, params["dca"], pe_pct_map))

    cols = st.columns(max(len(enabled), 1))
    for i, r in enumerate(enabled):
        cols[i % len(cols)].markdown(status_card(r), unsafe_allow_html=True)
    warn_msgs = [w for r in enabled for w in r.warnings]
    if warn_msgs:
        with st.expander("⚠️ 策略风险提示"):
            for w in warn_msgs:
                st.warning(w)

    # ---------- 核心图表区 ----------
    st.subheader("📈 核心图表区")
    fig = build_kline_chart(hist, enabled, show_n=show_n)
    st.plotly_chart(fig, width="stretch")
    st.caption("💡 支持鼠标拖拽平移、滚轮缩放；▲绿箭头=买点，▼红箭头=卖点，悬停查看具体原因。")

    # ---------- 最近信号表 ----------
    if enabled:
        with st.expander("📋 全部买卖信号明细（按日期排序）"):
            sig = strategies.merge_signals(enabled)
            if len(sig):
                st.dataframe(sig.sort_values("日期", ascending=False).head(100),
                             width="stretch", hide_index=True)
            else:
                st.info("所选周期与策略范围内暂无信号。")

    # ---------- 持仓模拟计算器（PRD 4.4） ----------
    last_close = float(hist["收盘"].iloc[-1])
    pos_df = position.position_table(enabled, last_close) if enabled else pd.DataFrame()
    if len(pos_df):
        st.subheader("💰 持仓模拟计算器")
        st.caption("累计口径：马丁/反马丁以「初始资金」为基准，定投以「累计投入」为基准；"
                   "总权益已包含历次平仓落袋的利润，与买卖信号一一对应。")
        st.dataframe(pos_df, width="stretch", hide_index=True)
        with st.expander("📉 各策略累计收益率曲线（回测）"):
            curves = position.latest_positions_series(enabled, last_close)
            if len(curves):
                fig2 = go.Figure()
                for col in curves.columns:
                    if col != "日期":
                        fig2.add_trace(go.Scatter(
                            x=curves["日期"], y=curves[col], name=col,
                            line=dict(width=1.3)))
                fig2.add_hline(y=0, line_color="#64748b", line_dash="dash")
                fig2.update_layout(height=360, margin=dict(l=40, r=20, t=30, b=30),
                                   legend=dict(orientation="h", y=1.05))
                st.plotly_chart(fig2, width="stretch")

    # ---------- 基本面数据卡片（PRD 4.4） ----------
    st.subheader("🏷️ 基本面数据卡片")
    bcols = st.columns(6)
    pe, pb = data.get("pe"), data.get("pb")
    pe_latest = pe_pct_map_latest = None
    if pe is not None and len(pe):
        pe_latest = float(pe["value"].iloc[-1])
        pe_pct_map_latest = data_fetcher.valuation_percentile(
            pe["value"].to_numpy(), pe_latest)
    pb_latest = None
    if pb is not None and len(pb):
        pb_latest = float(pb["value"].iloc[-1])
    bcols[0].metric("市盈率 PE(TTM)", f"{pe_latest:.2f}" if pe_latest else "—")
    bcols[1].metric("PE 一年百分位",
                    f"{pe_pct_map_latest*100:.0f}%" if pe_pct_map_latest is not None else "—",
                    help="当前 PE 在近一年历史中的位置，<50% 偏低，>80% 偏高")
    bcols[2].metric("市净率 PB", f"{pb_latest:.2f}" if pb_latest else "—")
    growth = data.get("growth")
    bcols[3].metric("净利润增长率",
                    f"{growth['净利润增长率(%)']:.1f}%" if growth else "—")
    profile = data.get("profile")
    bcols[4].metric("所属行业", (profile or {}).get("所属行业", "—"))
    bcols[5].metric("上市日期", (profile or {}).get("上市日期", "—") or "—")
    if profile and profile.get("主营业务"):
        st.caption(f"主营业务：{profile['主营业务']}")

    # ---------- 五档盘口（PRD 2.2） ----------
    if rt is not None:
        with st.expander("📚 五档买卖盘口（实时）"):
            st.dataframe(bidask_table(rt), width="stretch", hide_index=True)

    st.divider()
    st.caption("数据更新策略：实时行情 20 秒缓存 · 日K 20 分钟 · 估值/财务 12~24 小时。"
               "启动或切换股票时优先读本地缓存，后台自动刷新过期数据。")


if __name__ == "__main__":
    main()
