# -*- coding: utf-8 -*-
"""
持仓模拟计算器（PRD 4.4）
基于各策略回测产生的持仓序列，汇总输出当前理论仓位、成本价、盈亏等指标。
"""
import pandas as pd


def summarize(result, last_close):
    """
    将策略回测结果整理为一行持仓摘要。
    返回 dict：策略/持仓数/成本价/当前价/市值/投入资金/盈亏额/盈亏率/仓位比例/状态
    """
    m = result.metrics
    capital = m.get("invest", 0) + m.get("cash", 0)
    position_ratio = (m.get("invest", 0) / capital * 100) if capital > 0 else 0.0
    return {
        "策略": result.name,
        "持仓数": m.get("shares", 0),
        "成本价": m.get("cost", 0),
        "当前价": last_close,
        "市值": m.get("value", 0),
        "投入资金": m.get("invest", 0),
        "盈亏额": m.get("pnl_amount", 0),
        "盈亏率%": m.get("pnl_pct", 0),
        "仓位占比%": position_ratio,
        "状态": result.status,
    }


def position_table(results, last_close):
    """所有策略的持仓摘要表（DataFrame，用于 st.dataframe 展示）。"""
    if not results:
        return pd.DataFrame()
    rows = [summarize(r, last_close) for r in results]
    df = pd.DataFrame(rows)
    cols = ["策略", "持仓数", "成本价", "当前价", "市值", "投入资金",
            "盈亏额", "盈亏率%", "仓位占比%", "状态"]
    return df[[c for c in cols if c in df.columns]].round(2)


def latest_positions_series(results, last_close):
    """
    持仓模拟曲线（合并各策略的 持仓数*价格 序列），
    返回 DataFrame: 日期 + 各策略盈亏率%。
    """
    frames = []
    for r in results:
        if len(r.positions) == 0:
            continue
        p = r.positions[["日期", "盈亏率%"]].copy()
        p = p.rename(columns={"盈亏率%": r.name})
        frames.append(p)
    if not frames:
        return pd.DataFrame()
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="日期", how="outer")
    return out.sort_values("日期").fillna(0.0)
