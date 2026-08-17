# -*- coding: utf-8 -*-
"""
策略引擎模块（PRD 第 3 章）

四大策略，各自独立回测模拟并输出买卖信号：
  1. traditional      传统技术面+基本面（MA/MACD/KDJ 金叉死叉 + 估值/业绩）
  2. martingale       马丁策略（越跌越买，亏损加仓，目标微利平仓）
  3. anti_martingale  反马丁策略（赢冲输缩，顺势而为）
  4. dca              定投策略（定期定额 + 智能低吸 + 目标/估值止盈）

统一返回 StrategyResult：
  - signals:   DataFrame[日期, 价格, 方向(buy/sell), 原因]  ← 用于 K 线打点
  - positions: DataFrame[日期, 持仓数, 成本价, 投入资金, 市值, 盈亏率%, 可用资金] ← 持仓模拟
  - status / status_tag：当前信号看板文案与颜色语义
  - metrics：当前持仓摘要（供"持仓模拟计算器"面板）
  - warnings：策略风险提示
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core import indicators as ind


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class StrategyResult:
    name: str
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    status: str = ""
    status_tag: str = "neutral"      # bull / bear / neutral / hold / wait / danger
    metrics: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    capital: float = 0.0            # 初始资金（马丁/反马丁，用于累计收益率计算）
    total_invest: float = 0.0       # 累计投入（定投，用于累计收益率计算）
    _rows: list = field(default_factory=list, repr=False, init=False)

    def add_signal(self, date, price, action, reason, qty=None, pct=None):
        """
        记录一条买卖信号。
        qty：本次成交股数；pct：本次交易金额占当时总权益的百分比（仓位）。
        仅出信号不计仓位的策略（如传统指标）不填 qty/pct，展示为"—"。
        """
        # 先追加到列表，最后一次性建表，避免逐条 pd.concat 的 O(n^2) 开销
        self._rows.append({"日期": date, "价格": price, "方向": action, "原因": reason,
                           "数量": qty, "仓位%": pct})

    def finalize_signals(self):
        """把累积的信号行转为 DataFrame 并返回。"""
        if self._rows:
            self.signals = pd.DataFrame(self._rows)
            self._rows = []
        return self.signals

    @property
    def n_buy(self):
        return sum(1 for r in self._rows if r["方向"] == "buy")

    @property
    def n_sell(self):
        return sum(1 for r in self._rows if r["方向"] == "sell")

    def summarize_metrics(self, last_close):
        """
        汇总当前持仓摘要。收益率按累计口径计算：
          - 马丁/反马丁（设置了 capital）：累计收益率 = (总权益 - 初始资金) / 初始资金
          - 定投（未设置 capital）：收益率 = (市值 - 投入) / 投入
        这样平仓落袋的利润（体现在可用资金里）也计入结果，与买卖信号一一对应。
        """
        if len(self.positions) == 0:
            return
        p = self.positions.iloc[-1]
        shares = float(p["持仓数"])
        value = float(p["市值"])
        cash = float(p["可用资金"])
        invest = float(p["投入资金"])
        equity = float(p.get("权益", value + cash))
        cost = float(p["成本价"]) if shares > 0 else 0.0
        if self.capital and self.capital > 0:
            base = self.capital
        elif self.total_invest and self.total_invest > 0:
            base = self.total_invest
        else:
            base = 0.0
        if base > 0:
            pnl_amount = equity - base
            pnl_pct = pnl_amount / base * 100
        else:
            pnl_amount = value - invest
            pnl_pct = (value / invest - 1) * 100 if invest > 0 else 0.0
        self.metrics = {
            "shares": shares,
            "cost": cost,
            "invest": invest,
            "value": value,
            "cash": cash,
            "equity": equity,
            "pnl_pct": float(pnl_pct),
            "pnl_amount": float(pnl_amount),
            "position_ratio": (value / equity * 100) if equity > 0 else 0.0,
        }


# --------------------------------------------------------------------------
# 1. 传统技术面 + 基本面分析（PRD 3.1）
# --------------------------------------------------------------------------
def run_traditional(df, pe_pct_map=None, growth_pct=None):
    df = ind.add_all(df)
    res = StrategyResult(name="传统指标")

    # --- 均线：MA5 上穿 MA20 金叉买 / 下穿死叉卖 ---
    for d, price, r in _iter_signals(
            ind.cross_above(df["MA5"], df["MA20"]), df, "MA金叉(5上穿20)"):
        res.add_signal(d, price, "buy", r)
    for d, price, r in _iter_signals(
            ind.cross_below(df["MA5"], df["MA20"]), df, "MA死叉(5下穿20)"):
        res.add_signal(d, price, "sell", r)

    # --- MACD：DIF 上穿 DEA 买 / 下穿卖 ---
    for d, price, r in _iter_signals(
            ind.cross_above(df["DIF"], df["DEA"]), df, "MACD金叉(DIF上穿DEA)"):
        res.add_signal(d, price, "buy", r)
    for d, price, r in _iter_signals(
            ind.cross_below(df["DIF"], df["DEA"]), df, "MACD死叉(DIF下穿DEA)"):
        res.add_signal(d, price, "sell", r)

    # --- KDJ：20 以下超卖金叉买 / 80 以上超买死叉卖 ---
    kdj_buy = ind.cross_above(df["K"], df["D"]) & (df["D"] < 20)
    for d, price, r in _iter_signals(kdj_buy, df, "KDJ超卖金叉(D<20)"):
        res.add_signal(d, price, "buy", r)
    kdj_sell = ind.cross_below(df["K"], df["D"]) & (df["K"] > 80)
    for d, price, r in _iter_signals(kdj_sell, df, "KDJ超买死叉(K>80)"):
        res.add_signal(d, price, "sell", r)

    # --- 基本面：估值百分位 + 业绩增长（基于最新数据，作为当期买卖投票）---
    votes = {"buy": [], "sell": []}
    last = df.iloc[-1]
    latest_pct = None
    if pe_pct_map:
        latest_pct = pe_pct_map.get(last["日期"])
    if latest_pct is not None:
        if latest_pct < 0.5 and growth_pct is not None and growth_pct > 0:
            votes["buy"].append("估值低(PE百分位%.0f%%)+业绩增长" % (latest_pct * 100))
        elif latest_pct < 0.3:
            votes["buy"].append("估值偏低(PE百分位%.0f%%)" % (latest_pct * 100))
        if latest_pct > 0.8:
            votes["sell"].append("估值偏高(PE百分位%.0f%%)" % (latest_pct * 100))
    if growth_pct is not None and growth_pct < 0:
        votes["sell"].append("净利润下滑(%.1f%%)" % growth_pct)
    if votes["buy"]:
        res.add_signal(last["日期"], last["收盘"], "buy", "基本面:" + ";".join(votes["buy"]))
    if votes["sell"]:
        res.add_signal(last["日期"], last["收盘"], "sell", "基本面:" + ";".join(votes["sell"]))

    # --- 综合状态：买卖票数对比 ---
    buy_n, sell_n = res.n_buy, res.n_sell
    if buy_n > sell_n:
        res.status = f"看多（买点{buy_n} > 卖点{sell_n}）"
        res.status_tag = "bull"
    elif sell_n > buy_n:
        res.status = f"看空（卖点{sell_n} > 买点{buy_n}）"
        res.status_tag = "bear"
    else:
        res.status = "中性（买卖信号均衡，观望为主）"
        res.status_tag = "neutral"

    res.finalize_signals()
    res.summarize_metrics(df["收盘"].iloc[-1])
    return res


def _iter_signals(mask, df, reason):
    """将 bool Series 中 True 的位置转成 (日期, 价格, 原因) 迭代器。"""
    idx = np.where(mask.to_numpy())[0]
    for i in idx:
        row = df.iloc[i]
        yield row["日期"], float(row["收盘"]), reason


# --------------------------------------------------------------------------
# 2. 马丁策略（PRD 3.2）—— 越跌越买，亏损加仓
# --------------------------------------------------------------------------
def run_martingale(df, params):
    capital = float(params["capital"])
    drop_step = float(params["drop_step"])
    multiply = float(params["multiply"])
    max_adds = int(params["max_adds"])
    target_profit = float(params["target_profit"])
    stop_loss = float(params["stop_loss"])
    init_ratio = float(params["init_ratio"])

    res = StrategyResult(name="马丁策略")
    res.capital = capital
    res.warnings.append(
        "⚠️ 马丁策略在单边下跌行情中资金消耗呈指数级增长，极易爆仓。"
        f"本系统已强制设置：最大加仓次数 {max_adds} 次、总资金止损线 {stop_loss*100:.0f}%。")

    cash = capital
    shares = 0.0
    cost = 0.0            # 加权平均成本
    invest = 0.0          # 持仓累计投入
    last_amount = 0.0     # 最近一次买入金额（用于计算下次加仓金额）
    adds = 0              # 本周期已加仓次数
    ref_price = 0.0       # 本周期初始建仓价
    cycle = 0             # 建仓-平仓周期计数
    stopped = False
    rows = []

    def _snapshot():
        equity = cash + shares * close
        return (date, shares, cost, invest, shares * close, cash, equity)

    for i, bar in df.iterrows():
        date, close = bar["日期"], float(bar["收盘"])

        if stopped:
            rows.append(_snapshot())
            continue

        # --- 建仓：空仓时在当期收盘价买入初始仓位 ---
        if shares <= 0:
            amount = cash * init_ratio
            if amount <= 0 or close <= 0:
                rows.append(_snapshot())
                continue
            new_shares = amount / close
            pct = amount / (cash + shares * close) * 100 if cash > 0 else 0.0
            shares = new_shares
            cost = close
            invest = amount
            cash -= amount
            last_amount = amount
            ref_price = close
            adds = 0
            cycle += 1
            res.add_signal(date, close, "buy",
                           f"马丁初始建仓（第{cycle}轮，投入{amount:.0f}元）",
                           qty=new_shares, pct=pct)

        # --- 加仓：跌破 初始建仓价×(1 - n×间距) 时，金额×倍数加仓 ---
        for n in range(adds + 1, max_adds + 1):
            if close <= ref_price * (1 - drop_step * n):
                amount = last_amount * multiply
                # 现金不足时按剩余现金加仓
                amount = min(amount, cash)
                if amount <= 0:
                    break
                new_shares = amount / close
                pct = amount / (cash + shares * close) * 100 if (cash + shares * close) > 0 else 0.0
                shares += new_shares
                cost = (cost * (shares - new_shares) + close * new_shares) / shares
                invest += amount
                cash -= amount
                last_amount = amount
                adds = n
                res.add_signal(date, close, "buy",
                               f"马丁加仓(第{n}次)：较建仓价下跌{n*drop_step*100:.0f}%，"
                               f"投入{amount:.0f}元",
                               qty=new_shares, pct=pct)
                if n == max_adds:
                    res.warnings.append(
                        f"{date} 已达最大加仓次数 {max_adds} 次，若继续下跌资金将迅速耗尽，"
                        "请严格执行止损纪律。")
                break

        # --- 平仓判定：目标微利 / 总资金止损 ---
        equity = cash + shares * close
        pnl_ratio = (equity - capital) / capital
        if pnl_ratio >= target_profit:
            res.add_signal(date, close, "sell",
                           f"马丁目标微利平仓（总盈利{pnl_ratio*100:.1f}%≥{target_profit*100:.0f}%）",
                           qty=shares, pct=shares * close / equity * 100 if equity > 0 else 0.0)
            cash = equity
            shares, cost, invest, last_amount, adds, ref_price = 0, 0, 0, 0, 0, 0
        elif pnl_ratio <= stop_loss:
            res.add_signal(date, close, "sell",
                           f"⚠️ 马丁止损清仓（总亏损{pnl_ratio*100:.1f}%，触发{stop_loss*100:.0f}%止损线）",
                           qty=shares, pct=shares * close / equity * 100 if equity > 0 else 0.0)
            cash = equity
            shares, cost, invest, last_amount, adds, ref_price = 0, 0, 0, 0, 0, 0
            stopped = True
            res.status = f"已触发止损（-{stop_loss*100:.0f}%），交易已停止，建议观望"
            res.status_tag = "danger"

        rows.append(_snapshot())

    res.positions = pd.DataFrame(
        rows, columns=["日期", "持仓数", "成本价", "投入资金", "市值", "可用资金", "权益"])
    res.positions["收益率%"] = np.where(
        res.positions["权益"] > 0,
        (res.positions["权益"] - capital) / capital * 100,
        0.0)
    res.positions["成本价"] = res.positions["成本价"].replace(0, np.nan)

    # --- 当前状态（累计口径） ---
    last = res.positions.iloc[-1]
    total_ret = float(last["收益率%"])
    if last["持仓数"] <= 0:
        if not stopped:
            res.status = (f"空仓（累计{'+' if total_ret >= 0 else ''}{total_ret:.1f}%），"
                          f"等待{'下一轮' if cycle > 0 else '首次'}建仓")
            res.status_tag = "wait"
    else:
        if total_ret < 0:
            res.status = f"持有中（累计{total_ret:.1f}%），等待加仓/止损"
            res.status_tag = "hold"
        else:
            res.status = f"持有中（累计{total_ret:+.1f}%），接近目标微利"
            res.status_tag = "bull"
    if adds == max_adds and last["持仓数"] > 0:
        res.status += "｜⚠️已达最大加仓次数"

    res.finalize_signals()
    res.summarize_metrics(df["收盘"].iloc[-1])
    return res


# --------------------------------------------------------------------------
# 3. 反马丁策略（PRD 3.3）—— 赢冲输缩，顺势而为
# --------------------------------------------------------------------------
def run_anti_martingale(df, params):
    capital = float(params["capital"])
    base_ratio = float(params["base_ratio"])
    multiply = float(params["multiply"])
    max_adds = int(params["max_adds"])
    stop_loss = float(params["stop_loss"])
    lookback = int(params["lookback"])

    res = StrategyResult(name="反马丁策略")
    res.capital = capital
    df = ind.add_all(df)

    cash = capital
    shares = 0.0
    cost = 0.0
    invest = 0.0
    last_amount = 0.0
    adds = 0
    entry_price = 0.0
    rows = []

    def _snapshot():
        equity = cash + shares * close
        return (date, shares, cost, invest, shares * close, cash, equity)

    # 均线多头排列（MA5>MA10>MA20）
    bull_align = (df["MA5"] > df["MA10"]) & (df["MA10"] > df["MA20"]) & df["MA20"].notna()
    # 前 lookback 日最高价（不含当日），作为关键阻力
    prior_high = df["最高"].rolling(lookback).max().shift(1)

    for i, bar in df.iterrows():
        date, close, high = bar["日期"], float(bar["收盘"]), float(bar["最高"])

        if shares <= 0:
            # 空仓：均线多头排列时顺势建仓
            if bool(bull_align.iloc[i]):
                amount = cash * base_ratio
                if amount > 0:
                    new_shares = amount / close
                    pct = amount / (cash + shares * close) * 100 if cash > 0 else 0.0
                    shares = new_shares
                    cost = close
                    invest = amount
                    cash -= amount
                    last_amount = amount
                    entry_price = close
                    adds = 0
                    res.add_signal(date, close, "buy",
                                   "反马丁顺势建仓（MA5>MA10>MA20 多头排列）",
                                   qty=new_shares, pct=pct)
        else:
            pnl_ratio = (close - cost) / cost
            ph = prior_high.iloc[i]
            # 加仓：突破关键阻力 + 持仓盈利 + 未超上限
            if (pd.notna(ph) and close > ph and pnl_ratio > 0
                    and adds < max_adds):
                amount = last_amount * multiply
                amount = min(amount, cash)
                if amount > 0:
                    new_shares = amount / close
                    pct = amount / (cash + shares * close) * 100 if (cash + shares * close) > 0 else 0.0
                    shares += new_shares
                    cost = (cost * (shares - new_shares) + close * new_shares) / shares
                    invest += amount
                    cash -= amount
                    last_amount = amount
                    adds += 1
                    res.add_signal(date, close, "buy",
                                   f"反马丁盈利加仓(第{adds}次)：突破{lookback}日新高，"
                                   f"持仓盈利{pnl_ratio*100:.1f}%",
                                   qty=new_shares, pct=pct)
            # 离场：单笔止损 或 跌破短期均线（趋势反转）
            elif pnl_ratio <= stop_loss:
                res.add_signal(date, close, "sell",
                               f"反马丁止损离场（浮亏{pnl_ratio*100:.1f}%，"
                               f"触及{stop_loss*100:.0f}%止损线）",
                               qty=shares,
                               pct=shares * close / (cash + shares * close) * 100
                               if (cash + shares * close) > 0 else 0.0)
                cash += shares * close
                shares, cost, invest, last_amount, adds, entry_price = 0, 0, 0, 0, 0, 0
            elif pd.notna(df["MA5"].iloc[i]) and close < df["MA5"].iloc[i]:
                res.add_signal(date, close, "sell",
                               f"反马丁趋势反转离场（跌破MA5，落袋{pnl_ratio*100:.1f}%）",
                               qty=shares,
                               pct=shares * close / (cash + shares * close) * 100
                               if (cash + shares * close) > 0 else 0.0)
                cash += shares * close
                shares, cost, invest, last_amount, adds, entry_price = 0, 0, 0, 0, 0, 0

        rows.append(_snapshot())

    res.positions = pd.DataFrame(
        rows, columns=["日期", "持仓数", "成本价", "投入资金", "市值", "可用资金", "权益"])
    res.positions["收益率%"] = np.where(
        res.positions["权益"] > 0,
        (res.positions["权益"] - capital) / capital * 100,
        0.0)
    res.positions["成本价"] = res.positions["成本价"].replace(0, np.nan)

    last = res.positions.iloc[-1]
    total_ret = float(last["收益率%"])
    if last["持仓数"] <= 0:
        res.status = f"空仓（累计{'+' if total_ret >= 0 else ''}{total_ret:.1f}%），等待均线多头排列"
        res.status_tag = "wait"
    else:
        res.status = (f"持仓中（累计{'+' if total_ret >= 0 else ''}{total_ret:.1f}%），"
                      f"顺势持有，止损线{stop_loss*100:.0f}%")
        res.status_tag = "bull" if total_ret >= 0 else "hold"
    res.warnings.append("反马丁策略适合单边上涨趋势，震荡行情中会频繁止损，需配合趋势过滤器使用。")
    res.finalize_signals()
    res.summarize_metrics(df["收盘"].iloc[-1])
    return res


# --------------------------------------------------------------------------
# 4. 定投策略（PRD 3.4）—— 定期定额 + 智能低吸 + 止盈
# --------------------------------------------------------------------------
def run_dca(df, params, pe_pct_map=None):
    base_amount = float(params["base_amount"])
    weekday = int(params["weekday"])
    pe_low = float(params["pe_low"])
    pe_high = float(params["pe_high"])
    dip_boost = float(params["dip_boost"])
    target_profit = float(params["target_profit"])

    res = StrategyResult(name="定投策略")
    df = ind.add_all(df)

    shares = 0.0
    cost = 0.0          # 加权平均成本
    invest = 0.0        # 持仓成本（随买卖变化）
    total_invest = 0.0  # 累计投入（所有买入金额之和）
    realized = 0.0      # 落袋现金（历次止盈卖出所得）
    sold_half = False   # 是否已完成首次分批止盈
    rows = []
    last_mult = 1.0
    last_status_note = ""

    def _snapshot():
        value = shares * close
        return (date, shares, cost, invest, value, realized,
                value + realized, total_invest)

    for i, bar in df.iterrows():
        date, close = bar["日期"], float(bar["收盘"])
        date_ts = pd.Timestamp(date)

        mult = 1.0
        reasons = []
        # --- 智能定投（低吸）：估值百分位 & 均线偏离 ---
        pct = pe_pct_map.get(date) if pe_pct_map else None
        if pct is not None:
            if pct < pe_low:
                mult *= 1.5
                reasons.append(f"PE百分位{pct*100:.0f}%<50%，加倍买入")
            elif pct > pe_high:
                mult *= 0.5
                reasons.append(f"PE百分位{pct*100:.0f}%>80%，减半定投")
        if pd.notna(df["MA20"].iloc[i]) and close < df["MA20"].iloc[i]:
            mult *= (1 + dip_boost)
            reasons.append(f"低于20日均线，低吸加码×{1+dip_boost:.1f}")

        # --- 定期买入：每周四 ---
        if date_ts.weekday() == weekday:
            amount = base_amount * mult
            if amount > 0:
                new_shares = amount / close
                equity_after = shares * close + realized + amount
                pct = amount / equity_after * 100 if equity_after > 0 else 0.0
                shares += new_shares
                cost = (cost * (shares - new_shares) + close * new_shares) / shares
                invest += amount
                total_invest += amount
                res.add_signal(date, close, "buy",
                               "智能定投买入" + (f"（{'；'.join(reasons)}）" if reasons else "（常规）"),
                               qty=new_shares, pct=pct)
            last_mult = mult
            last_status_note = "；".join(reasons) if reasons else "常规定投"

        # --- 止盈：目标收益率分批/全部止盈；估值过高部分赎回 ---
        if shares > 0 and invest > 0:
            pnl_ratio = (shares * close - invest) / invest
            equity_now = shares * close + realized
            if pnl_ratio >= target_profit * 1.5:
                res.add_signal(date, close, "sell",
                               f"定投全部止盈（持仓收益率{pnl_ratio*100:.1f}%"
                               f"≥{target_profit*150:.0f}%）",
                               qty=shares,
                               pct=shares * close / equity_now * 100 if equity_now > 0 else 0.0)
                realized += shares * close
                shares, cost, invest = 0, 0, 0
            elif pnl_ratio >= target_profit and not sold_half:
                res.add_signal(date, close, "sell",
                               f"定投分批止盈（持仓收益率{pnl_ratio*100:.1f}%"
                               f"≥{target_profit*100:.0f}%，先卖一半落袋）",
                               qty=shares * 0.5,
                               pct=shares * close * 0.5 / equity_now * 100 if equity_now > 0 else 0.0)
                realized += shares * close * 0.5
                shares *= (1 - 0.5)
                invest *= (1 - 0.5)
                sold_half = True
            elif pct is not None and pct > pe_high and shares > 0:
                res.add_signal(date, close, "sell",
                               f"估值止盈（PE百分位{pct*100:.0f}%>80%，部分赎回20%）",
                               qty=shares * 0.2,
                               pct=shares * close * 0.2 / equity_now * 100 if equity_now > 0 else 0.0)
                realized += shares * close * 0.2
                shares *= 0.8
                invest *= 0.8

        rows.append(_snapshot())

    res.positions = pd.DataFrame(
        rows, columns=["日期", "持仓数", "成本价", "投入资金", "市值", "可用资金",
                       "权益", "累计投入"])
    # 累计收益率 = (总权益 - 累计投入) / 累计投入，含落袋现金
    res.positions["收益率%"] = np.where(
        res.positions["累计投入"] > 0,
        (res.positions["权益"] - res.positions["累计投入"])
        / res.positions["累计投入"] * 100,
        0.0)
    res.positions["成本价"] = res.positions["成本价"].replace(0, np.nan)
    res.total_invest = total_invest

    last = res.positions.iloc[-1]
    pnl = float(last["收益率%"])
    res.status = (f"累计投入{total_invest:.0f}元，当前{'盈利' if pnl >= 0 else '亏损'}"
                  f"{pnl:.1f}%（含落袋）")
    res.status_tag = "bull" if pnl >= 0 else "hold"

    # 当期定投建议（PRD 4.4 文案示例："定投策略：本期建议加倍买入"）
    if last_mult >= 1.5:
        res.status = "本期建议加倍买入（" + last_status_note + "）｜" + res.status
        res.status_tag = "bull"
    elif last_mult <= 0.5:
        res.status = "本期建议减半定投（估值过高）｜" + res.status
        res.status_tag = "bear"
    res.warnings.append("定投止盈目标与智能定投倍数均为演示参数，可在代码 core/config.py 中调整。")
    res.finalize_signals()
    res.summarize_metrics(df["收盘"].iloc[-1])
    return res


# --------------------------------------------------------------------------
# 汇总：合并所有策略信号（供 K 线打点）
# --------------------------------------------------------------------------
def merge_signals(results):
    parts = []
    for r in results:
        if len(r.signals) > 0:
            s = r.signals.copy()
            s["策略"] = r.name
            parts.append(s)
    if not parts:
        return pd.DataFrame(columns=["日期", "价格", "方向", "原因", "策略"])
    return pd.concat(parts, ignore_index=True).sort_values("日期", kind="stable")
