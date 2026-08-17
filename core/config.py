# -*- coding: utf-8 -*-
"""
全局配置：请求频率控制、重试策略、User-Agent 轮换、磁盘缓存参数。
"""
import os

# ---------- 路径 ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------- 反爬风控（PRD 2.3）----------
# 每次外部请求的最小间隔（秒），避免被数据源限流封 IP
REQUEST_INTERVAL = 0.6
MAX_RETRY = 3                     # 请求失败自动重试次数
RETRY_BACKOFF = 1.5               # 重试等待基数（秒），指数退避：1.5s/2.25s/3.4s
REQUEST_TIMEOUT = 15              # 单次请求超时（秒）

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# ---------- 磁盘缓存有效期（秒）----------
CACHE_TTL = {
    "hist_daily":   20 * 60,      # 日K：盘中会变化，20 分钟
    "hist_week":    6 * 3600,     # 周K
    "hist_month":   6 * 3600,     # 月K
    "intraday":     60,           # 分时：1 分钟
    "realtime":     20,           # 实时行情：20 秒
    "valuation":    12 * 3600,    # 估值（PE/PB）：半天
    "profile":      7 * 86400,    # 公司资料：7 天
    "growth":       24 * 3600,    # 财务增长数据：1 天
    "code_name":    12 * 3600,    # 代码-名称映射：半天
}

# ---------- 策略默认参数（PRD 第 3 章）----------
STRATEGY_PARAMS = {
    "martingale": {
        "capital": 100000.0,      # 初始资金（元）
        "init_ratio": 0.20,       # 初始建仓比例（占总资金）
        "drop_step": 0.05,        # 加仓间距：每下跌 5% 加仓一次
        "multiply": 2.0,          # 加仓倍数：每次加仓金额翻倍
        "max_adds": 5,            # 最大加仓次数（风控上限）
        "target_profit": 0.06,    # 目标微利：持仓投入盈利 6% 平仓
                                  #   （原按含闲置现金的总权益口径会退化空转，
                                  #     第一轮盈利后每轮建仓立即原价平仓，收益永远卡死）
        "stop_loss": -0.15,       # 总资金止损线：总资金回撤 15% 清仓
        "max_position": 0.55,     # 本周期仓位上限（占资金比例），防止加仓无限放大亏损
        "reentry": True,          # 止损后等价格收复 60 日均线重新入场
    },
    "anti_martingale": {
        "capital": 100000.0,
        "base_ratio": 0.10,       # 基础仓位比例
        "multiply": 2.0,          # 盈利加仓倍数
        "max_adds": 4,            # 最多顺势加仓次数
        "stop_loss": -0.05,       # 单笔止损：较建仓成本回撤 5% 离场
        "lookback": 20,           # 突破前 N 日新高视为关键阻力突破
    },
    "dca": {
        "base_amount": 1000.0,    # 每期基准定投金额（元）
        "weekday": 3,             # 定投日：3=每周四（PRD 示例）
        # ---- 估值智能加码：PE 百分位越低买越多，越高买越少 ----
        "pe_deep": 0.30,          # PE 百分位 < 30%：大幅加码
        "pe_low": 0.50,           # PE 百分位 < 50%：加码
        "pe_high": 0.80,          # PE 百分位 > 80%：减码（近乎暂停）
        "mult_pe_deep": 2.0,      # 深度低估买入倍数
        "mult_pe_low": 1.5,       # 低估买入倍数
        "mult_pe_high": 0.2,      # 高估买入倍数
        # ---- 均线智能低吸 ----
        "mult_ma60": 1.5,         # 跌破 MA60：加码
        "mult_ma20": 1.2,         # 跌破 MA20：轻度加码
        "breakdown_pct": 0.15,    # 跌破 MA60 该幅度以上：深度破位，不加码（防接飞刀）
        "overheat_pct": 0.15,     # 高于 MA20 该幅度以上：过热，减码防追高
        "mult_overheat": 0.5,
        "max_mult": 3.0,          # 单次买入倍数上限
        # ---- 分级止盈：低门槛、分批落袋，避免收益长期不兑现 ----
        "target1": 0.08,          # 持仓收益率 ≥ 8%：卖 30%
        "target2": 0.12,          # ≥ 12%：再卖 30%
        "target3": 0.16,          # ≥ 16%：全部卖出
        "sell_ratio": 0.3,        # 每次分批卖出比例
        "pe_high_sell": 0.15,     # PE>80% 且持仓盈利：赎回 15%
    },
}
