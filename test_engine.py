# -*- coding: utf-8 -*-
"""引擎自检脚本：数据获取 -> 指标 -> 四大策略 -> 汇总，全链路跑通验证。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config, data_fetcher, strategies  # noqa: E402


def main():
    t0 = time.time()
    print("=" * 60)
    print("1) 解析股票代码")
    sina, em, name = data_fetcher.resolve_symbol("600519")
    print(f"   600519 -> sina={sina}, em={em}, name={name}")

    print("\n2) 历史日K")
    hist = data_fetcher.get_hist(em, period="daily")
    print(f"   {len(hist)} 根K线, 最新 {hist['日期'].iloc[-1]} 收盘 {hist['收盘'].iloc[-1]:.2f}")

    print("\n3) 实时行情")
    rt = data_fetcher.get_realtime(em)
    print(f"   {rt['名称']} 最新 {rt['最新价']} 涨跌幅 {rt['涨跌幅']}% "
          f"时间 {rt['时间戳']}")
    print(f"   买一 {rt['买盘'][0]}  卖一 {rt['卖盘'][0]}")

    print("\n4) 估值 PE/PB")
    pe = data_fetcher.get_valuation(em)
    pb = data_fetcher.get_valuation(em, indicator="市净率")
    print(f"   PE(TTM)最新 {pe['value'].iloc[-1]:.2f}  PB最新 {pb['value'].iloc[-1]:.2f}")
    pct = data_fetcher.valuation_percentile(pe["value"].to_numpy(), pe["value"].iloc[-1])
    print(f"   PE百分位 {pct*100:.0f}%")

    print("\n5) 公司资料 + 业绩")
    prof = data_fetcher.get_profile(em)
    growth = data_fetcher.get_growth(em)
    print(f"   行业={prof['所属行业']}  上市={prof['上市日期']}")
    print(f"   净利润增长率={growth}")

    print("\n6) 估值百分位对齐到K线日期")
    pe_map = data_fetcher.build_pe_pct_series(pe, hist)
    keys = list(pe_map.keys())
    print(f"   对齐 {len(pe_map)} 天, 示例: {keys[-3:]} -> {[round(pe_map[k],2) for k in keys[-3:]]}")

    print("\n7) 四大策略")
    params = dict(config.STRATEGY_PARAMS)
    results = [
        strategies.run_traditional(hist, pe_map, growth["净利润增长率(%)"]),
        strategies.run_martingale(hist, params["martingale"]),
        strategies.run_anti_martingale(hist, params["anti_martingale"]),
        strategies.run_dca(hist, params["dca"], pe_map),
    ]
    for r in results:
        n_buy = len(r.signals[r.signals["方向"] == "buy"]) if len(r.signals) else 0
        n_sell = len(r.signals[r.signals["方向"] == "sell"]) if len(r.signals) else 0
        print(f"   [{r.name}] 买点{n_buy} 卖点{n_sell} | 状态: {r.status}")
        if r.warnings:
            print(f"        警示: {r.warnings[0][:60]}...")
        if len(r.positions):
            last = r.positions.iloc[-1]
            print(f"        当前持仓: {last['持仓数']:.0f}股 成本{last['成本价']:.2f} "
                  f"盈亏{last['盈亏率%']:.2f}%")

    print("\n8) 信号合并")
    sig = strategies.merge_signals(results)
    print(f"   共 {len(sig)} 条信号, 最近5条:")
    print(sig.tail(5).to_string(index=False))

    print(f"\n✅ 引擎全链路测试通过，耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
