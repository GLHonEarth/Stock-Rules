# -*- coding: utf-8 -*-
"""
持仓模拟计算器（PRD 4.4）
基于各策略回测产生的持仓序列，汇总输出当前理论仓位、成本价、累计盈亏等指标。

累计口径说明：
  - 马丁/反马丁：累计收益率 = (总权益 - 初始资金) / 初始资金。
    总权益 = 可用资金 + 当前持仓市值，已包含历次平仓落袋的利润，
    因此与「买卖信号明细」一一对应（每笔卖出的结果都体现在总权益里）。
  - 定投：收益率 = (市值 - 持仓成本) / 持仓成本，为当前持仓的投资回报。
"""
import pandas as pd


def summarize(result, last_close):
    """
    将策略回测结果整理为一行持仓摘要。
    返回 dict：策略/持仓数/成本价/当前价/市值/可用资金/总权益/投入资金/
               累计盈亏额/累计收益率%/仓位占比%/状态
    """
    m = result.metrics
    return {
        "策略": result.name,
        "持仓数": m.get("shares", 0),
        "成本价": m.get("cost", 0),
        "当前价": last_close,
        "市值": m.get("value", 0),
        "可用资金": m.get("cash", 0),
        "总权益": m.get("equity", 0),
        "投入资金": m.get("invest", 0),
        "累计盈亏额": m.get("pnl_amount", 0),
        "累计收益率%": m.get("pnl_pct", 0),
        "仓位占比%": m.get("position_ratio", 0),
        "状态": result.status,
    }


def position_table(results, last_close):
    """所有持仓型策略的摘要表（DataFrame，用于 st.dataframe 展示）。
    跳过无持仓模拟的策略（如传统指标仅输出信号）。"""
    if not results:
        return pd.DataFrame()
    rows = [summarize(r, last_close) for r in results if r.metrics]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    cols = ["策略", "持仓数", "成本价", "当前价", "市值", "可用资金", "总权益",
            "投入资金", "累计盈亏额", "累计收益率%", "仓位占比%", "状态"]
    return df[[c for c in cols if c in df.columns]].round(2)


def latest_positions_series(results, last_close):
    """
    持仓模拟曲线（合并各策略的累计收益率序列），
    返回 DataFrame: 日期 + 各策略累计收益率%。
    累计收益率随每次买卖变化，能直观看出各操作对结果的影响。
    """
    frames = []
    for r in results:
        if len(r.positions) == 0:
            continue
        p = r.positions[["日期", "收益率%"]].copy()
        p = p.rename(columns={"收益率%": r.name})
        frames.append(p)
    if not frames:
        return pd.DataFrame()
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="日期", how="outer")
    return out.sort_values("日期").fillna(0.0)
