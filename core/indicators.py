# -*- coding: utf-8 -*-
"""
技术指标模块（PRD 3.1 / 4.2 / 4.3）
纯 Pandas 实现：MA / MACD / KDJ / RSI / BOLL，输入 K 线 DataFrame，
在原始 df 上追加指标列并返回。
"""
import numpy as np
import pandas as pd


def add_ma(df, windows=(5, 10, 20, 60)):
    """均线系统：MA5 / MA10 / MA20 / MA60。"""
    for w in windows:
        df[f"MA{w}"] = df["收盘"].rolling(w).mean()
    return df


def add_macd(df, fast=12, slow=26, signal=9):
    """
    MACD：DIF = EMA(fast) - EMA(slow)；DEA = EMA(DIF, signal)；柱 = 2*(DIF-DEA)。
    """
    ema_fast = df["收盘"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["收盘"].ewm(span=slow, adjust=False).mean()
    df["DIF"] = ema_fast - ema_slow
    df["DEA"] = df["DIF"].ewm(span=signal, adjust=False).mean()
    df["MACD"] = 2 * (df["DIF"] - df["DEA"])
    return df


def add_kdj(df, n=9, m1=3, m2=3):
    """
    KDJ：RSV = (C - LLV(L,n)) / (HHV(H,n) - LLV(L,n)) * 100；
    K = SMA(RSV, m1)；D = SMA(K, m2)；J = 3K - 2D。
    """
    low_n = df["最低"].rolling(n).min()
    high_n = df["最高"].rolling(n).max()
    rsv = (df["收盘"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    df["K"] = rsv.ewm(com=m1 - 1, adjust=False).mean()
    df["D"] = df["K"].ewm(com=m2 - 1, adjust=False).mean()
    df["J"] = 3 * df["K"] - 2 * df["D"]
    return df


def add_rsi(df, periods=(6, 12, 24)):
    """RSI（Wilder 平滑）：RSI = 100 * 平均涨幅 / (平均涨幅 + 平均跌幅)。"""
    delta = df["收盘"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    for p in periods:
        avg_gain = gain.ewm(alpha=1 / p, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df[f"RSI{p}"] = (100 - 100 / (1 + rs)).fillna(50)
    return df


def add_boll(df, n=20, k=2):
    """布林带：中轨 MA20，上/下轨 ±2 倍标准差。"""
    mid = df["收盘"].rolling(n).mean()
    std = df["收盘"].rolling(n).std(ddof=0)
    df["BOLL_MID"] = mid
    df["BOLL_UP"] = mid + k * std
    df["BOLL_LOW"] = mid - k * std
    return df


def add_all(df):
    """一次性叠加全部指标（PRD 4.2/4.3）。"""
    df = df.copy()
    add_ma(df)
    add_macd(df)
    add_kdj(df)
    add_rsi(df)
    add_boll(df)
    return df


def cross_above(a, b):
    """a 上穿 b（当日 a>b 且前一日 a<=b），返回 bool Series。"""
    prev_a, prev_b = a.shift(1), b.shift(1)
    return (a > b) & (prev_a <= prev_b)


def cross_below(a, b):
    """a 下穿 b。"""
    prev_a, prev_b = a.shift(1), b.shift(1)
    return (a < b) & (prev_a >= prev_b)
