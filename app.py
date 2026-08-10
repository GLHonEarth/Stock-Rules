# -*- coding: utf-8 -*-
"""
股票智能技术分析与策略决策系统 —— Streamlit 可视化看板（PRD 第 4 章）

运行方式：
    streamlit run app.py
浏览器访问 http://localhost:8501
"""
import concurrent.futures as cf
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config, data_fetcher, position, strategies  # noqa: E402

PERIOD_MAP = {"日K": "daily", "周K": "weekly", "月K": "monthly"}
TAG_COLOR = {
    "bull": ("#16a34a", "看多"),
    "bear": ("#dc2626", "看空"),
    "hold": ("#f59e0b", "持有"),
    "wait": ("#6b7280", "等待"),
    "neutral": ("#2563eb", "中性"),
    "danger": ("#991b1b", "警示"),
}

st.set_page_config(page_title="股票智能技术分析与策略决策系统", layout="wide")


# --------------------------------------------------------------------------
# 数据加载（并行抓取，全部带磁盘缓存与降级）
# --------------------------------------------------------------------------
def load_all(query, period):
    """并行加载一只股票的全部数据，返回 dict；单项失败返回 None 不阻塞整体。"""
    futures = {}

    def _wrap(fn):
        def _inner():
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                return ("ERR", str(e))
        return _inner

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        # 必须先解析代码（阻塞）
        sina_code, em_code, name = data_fetcher.resolve_symbol(query)
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
    return out


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
    colors = np.where(up, "#ef232a", "#14b143")   # 红涨绿跌
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
    rows = []
    for i in range(4, -1, -1):
        vol, price = realtime["卖盘"][i]
        rows.append({"档位": ["卖五", "卖四", "卖三", "卖二", "卖一"][i],
                     "卖价": price, "卖量(手)": int(vol),
                     "买价": np.nan, "买量(手)": np.nan})
    for i in range(5):
        vol, price = realtime["买盘"][i]
        rows.append({"档位": ["买一", "买二", "买三", "买四", "买五"][i],
                     "卖价": np.nan, "卖量(手)": np.nan,
                     "买价": price, "买量(手)": int(vol)})
    return pd.DataFrame(rows)[["档位", "卖价", "卖量(手)", "买价", "买量(手)"]]


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
def main():
    st.title("📈 股票智能技术分析与策略决策系统")
    st.caption("数据源：东方财富 / 新浪财经 / 百度股市通 / 巨潮资讯（全部免费接口，内置请求限速与缓存）")

    with st.sidebar:
        st.header("⚙️ 控制区")
        query = st.text_input("股票代码或名称", value="600519",
                              placeholder="如 600519 / sh600519 / 贵州茅台")
        period_label = st.selectbox("时间周期", ["日K", "周K", "月K", "分时"])
        st.subheader("策略开关")
        use_trad = st.checkbox("传统技术面+基本面", value=True)
        use_mart = st.checkbox("马丁策略（风险高）", value=True)
        use_anti = st.checkbox("反马丁策略", value=True)
        use_dca = st.checkbox("定投策略", value=True)
        capital = st.number_input("模拟初始资金（元）", min_value=1000.0, value=100000.0,
                                  step=10000.0, help="用于持仓模拟计算器")
        show_n = st.slider("K线显示数量（根）", 60, 500, 250)
        if st.button("🔄 强制刷新数据", width="stretch"):
            for f in os.listdir(config.CACHE_DIR):
                if f.endswith((".csv", ".meta.json")):
                    try:
                        os.remove(os.path.join(config.CACHE_DIR, f))
                    except OSError:
                        pass
            st.rerun()
        st.divider()
        st.warning("⚠️ 本系统所有信号、仓位均为技术分析演示，不构成任何投资建议。"
                   "股市有风险，投资需谨慎。")

    if not query.strip():
        st.info("请在左侧输入股票代码或名称")
        return

    # ---------- 数据加载 ----------
    with st.spinner("正在获取行情数据（首次较慢，之后自动走缓存）..."):
        try:
            data = load_all(query.strip(), PERIOD_MAP.get(period_label, "daily"))
        except Exception as e:  # noqa: BLE001
            st.error(f"数据加载失败：{e}\n\n请检查网络连接（数据源为公开财经接口，偶发限流，"
                     "可稍后重试或点击左侧「强制刷新数据」）。")
            return

    if data.get("hist") is None:
        st.error("未获取到历史行情数据" + (f"：{data['errors'].get('hist')}" if data.get("errors") else "")
                 + "\n\n请检查股票代码是否正确、网络是否可用。")
        return

    hist = data["hist"]
    st.session_state["data"] = data
    title_name = data.get("name") or f"{data['code']}"

    # ---------- 顶部：实时行情指标卡 ----------
    rt = data.get("realtime")
    if rt is not None:
        up_down = "🔴" if rt["涨跌幅"] >= 0 else "🟢"
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric(f"{title_name}（{data['code']}）最新价",
                  f"{rt['最新价']:.2f}", f"{up_down} {rt['涨跌幅']:+.2f}%",
                  delta_color="normal")
        c2.metric("今开 / 昨收", f"{rt['今开']:.2f} / {rt['昨收']:.2f}")
        c3.metric("最高 / 最低", f"{rt['最高']:.2f} / {rt['最低']:.2f}")
        c4.metric("成交量 / 成交额", f"{rt['成交量(手)']/10000:.1f}万手 / {rt['成交额(元)']/1e8:.2f}亿")
        c5.metric("更新时间", rt["时间戳"])
    else:
        last = hist.iloc[-1]
        st.info(f"{title_name}（{data['code']}）实时行情暂不可用，以下为最近交易日 "
                f"{last['日期']} 收盘价 {last['收盘']:.2f}。"
                + (f"（原因：{data['errors'].get('realtime')}）" if data.get("errors") else ""))

    # ---------- 信号看板（PRD 4.4） ----------
    st.subheader("📊 当前策略信号看板")
    enabled = []
    if period_label == "分时":
        st.info("分时模式仅展示当日走势；策略信号基于日/周/月 K 线计算，请切换周期查看。")
    else:
        pe_series = data.get("pe")
        pe_pct_map = {}
        if pe_series is not None:
            pe_pct_map = data_fetcher.build_pe_pct_series(pe_series, hist)
        growth_pct = None
        if data.get("growth") is not None:
            growth_pct = data["growth"].get("净利润增长率(%)")

        params = dict(config.STRATEGY_PARAMS)
        for p in params.values():
            p["capital"] = capital

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
    if period_label == "分时":
        try:
            intraday = data_fetcher.get_intraday(data["code"])
            prev_close = (rt or {}).get("昨收", hist["收盘"].iloc[-1])
            st.plotly_chart(build_intraday_chart(intraday, prev_close),
                            width="stretch")
        except Exception as e:  # noqa: BLE001
            st.error(f"分时数据获取失败：{e}")
    else:
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
    if enabled:
        st.subheader("💰 持仓模拟计算器")
        last_close = float(hist["收盘"].iloc[-1])
        st.dataframe(position.position_table(enabled, last_close),
                     width="stretch", hide_index=True)
        with st.expander("📉 各策略历史盈亏率曲线（回测）"):
            curves = position.latest_positions_series(enabled, last_close)
            if len(curves):
                fig = go.Figure()
                for col in curves.columns:
                    if col != "日期":
                        fig.add_trace(go.Scatter(
                            x=curves["日期"], y=curves[col], name=col,
                            line=dict(width=1.3)))
                fig.add_hline(y=0, line_color="#64748b", line_dash="dash")
                fig.update_layout(height=360, margin=dict(l=40, r=20, t=30, b=30),
                                  legend=dict(orientation="h", y=1.05))
                st.plotly_chart(fig, width="stretch")

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
               "所有接口均为免费公开数据源，若遇限流系统会自动重试并降级。")


if __name__ == "__main__":
    main()
