# -*- coding: utf-8 -*-
"""
数据获取模块（PRD 第 2 章）

数据源（均免费、无需 API Key，任一失败自动降级/重试）：
  - 历史日/周/月 K 线：东方财富 push2his（akshare.stock_zh_a_hist）
  - 实时行情 + 五档盘口：新浪 hq.sinajs.cn（直连接口，瞬时响应）
  - 分时/分钟数据：新浪 quotes.sina.cn KLineData
  - PE/PB 及历史百分位：百度股市通估值（akshare.stock_zh_valuation_baidu）
  - 公司资料（行业/上市日期）：巨潮 cninfo（akshare.stock_profile_cninfo）
  - 净利润增长率：新浪财务指标（akshare.stock_financial_analysis_indicator）

风控（PRD 2.3）：
  - 全局请求节流：相邻两次外部请求至少间隔 REQUEST_INTERVAL 秒
  - 失败自动重试（指数退避）+ 多 User-Agent 轮换
  - 数据落盘缓存（CSV + 时间戳），避免重复请求、加速页面刷新
"""
import ast
import json
import os
import random
import re
import threading
import time

import pandas as pd
import requests

from core import config

_lock = threading.Lock()
_last_request_ts = 0.0


# --------------------------------------------------------------------------
# 基础工具：节流 / 重试 / 请求
# --------------------------------------------------------------------------
def _throttle():
    """全局请求节流：保证任意两次外部请求间隔 >= REQUEST_INTERVAL。"""
    global _last_request_ts
    with _lock:
        now = time.time()
        wait = config.REQUEST_INTERVAL - (now - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.time()


def _http_get(url, headers=None, timeout=None, encoding=None):
    """带节流 + 重试 + UA 轮换的 GET 请求。encoding 指定后按该编码解析响应体。"""
    last_err = None
    for attempt in range(config.MAX_RETRY):
        _throttle()
        try:
            h = {
                "User-Agent": random.choice(config.USER_AGENTS),
                "Referer": "https://finance.sina.com.cn",
                "Accept": "*/*",
            }
            if headers:
                h.update(headers)
            resp = requests.get(url, headers=h,
                                timeout=timeout or config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            else:
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < config.MAX_RETRY - 1:
                time.sleep(config.RETRY_BACKOFF ** attempt)
    raise RuntimeError(f"请求失败({config.MAX_RETRY}次重试后): {url[:80]}... {last_err}")


# --------------------------------------------------------------------------
# 磁盘缓存
# --------------------------------------------------------------------------
def _cache_path(key):
    return os.path.join(config.CACHE_DIR, f"{key}.csv")


def _cache_meta_path(key):
    return os.path.join(config.CACHE_DIR, f"{key}.meta.json")


def _cache_load(key, ttl_seconds):
    """缓存未过期则读取，返回 DataFrame；否则返回 None。"""
    csv_path, meta_path = _cache_path(key), _cache_meta_path(key)
    if not (os.path.exists(csv_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if time.time() - meta.get("ts", 0) > ttl_seconds:
            return None
        return pd.read_csv(csv_path, dtype={"日期": str})
    except Exception:  # noqa: BLE001
        return None


def _cache_save(key, df):
    try:
        df.to_csv(_cache_path(key), index=False, encoding="utf-8-sig")
        with open(_cache_meta_path(key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 股票代码规范化
# --------------------------------------------------------------------------
def normalize_symbol(symbol):
    """
    兼容多种输入：'600519' / 'sh600519' / 'SH600519' / '000001' / '300750'
    返回 (sina_code, em_code)，如 ('sh600519', '600519')。
    北交所代码（4/8/9 开头）使用 bj 前缀。
    """
    s = str(symbol).strip().lower().replace(".", "")
    m = re.match(r"^(sh|sz|bj)?(\d{6})$", s)
    if not m:
        raise ValueError(f"无法识别股票代码: {symbol}")
    prefix, code = m.group(1), m.group(2)
    if not prefix:
        prefix = "sh" if code.startswith("6") else "sz"
    return f"{prefix}{code}", code


def _norm_name(s):
    """股票名称规范化：全角字母/数字转半角、去除所有空白，便于模糊匹配。"""
    if not s:
        return ""
    s = str(s).strip()
    out = []
    for c in s:
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E:      # 全角 -> 半角
            out.append(chr(o - 0xFEE0))
        else:
            out.append(c)
    return "".join(out).replace(" ", "").replace("　", "")


def code_name_map(refresh=False):
    """全市场 代码->名称 映射（用于名称搜索），带磁盘缓存。
    代码统一补齐为 6 位字符串（避免 CSV 往返丢失前导零，如 000001）。"""
    key = "code_name"
    if not refresh:
        df = _cache_load(key, config.CACHE_TTL["code_name"])
        if df is not None and len(df) > 100:
            return {str(c).zfill(6): str(n) for c, n in zip(df["代码"], df["名称"])}
    mapping = {}
    # 优先：东财全量（快）；失败降级：新浪全市场（约 40 秒，仅首次）
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        mapping = {str(c).zfill(6): str(n) for c, n in zip(df["code"], df["name"])}
    except Exception:  # noqa: BLE001
        try:
            df = ak.stock_zh_a_spot()
            mapping = {}
            for _, r in df.iterrows():
                c = str(r["代码"])
                c = c[2:] if c.startswith(("sh", "sz", "bj")) else c
                mapping[str(c).zfill(6)] = str(r["名称"])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"获取股票列表失败: {e}")
    _cache_save(key, pd.DataFrame(sorted(mapping.items()), columns=["代码", "名称"]))
    return mapping


def resolve_symbol(user_input):
    """
    输入股票代码或名称，返回 (sina_code, em_code, name)。
    输入为中文名称时自动查表（如 '贵州茅台'、'平安银行'）。
    """
    user_input = user_input.strip()
    try:
        return normalize_symbol(user_input) + ("",)
    except ValueError:
        pass
    mapping = code_name_map()
    # 用规范化后的名称做匹配（容忍全角/空格差异，如 "万 科Ａ" -> "万科A"）
    q = _norm_name(user_input)
    norm_map = {c: _norm_name(n) for c, n in mapping.items()}
    hit = [c for c, n in norm_map.items() if n == q]
    if not hit:
        hit = [c for c, n in norm_map.items() if q and q in n]
    if not hit:
        raise ValueError(
            f"未找到股票: {user_input}。请检查代码（如 600519）或名称（如 贵州茅台）是否正确。")
    sina_code, em_code = normalize_symbol(hit[0])
    return sina_code, em_code, mapping[hit[0]]


# --------------------------------------------------------------------------
# 历史 K 线（日 / 周 / 月，前复权）
# --------------------------------------------------------------------------
def get_hist(symbol, period="daily", adjust="qfq", start="", end=""):
    """
    历史 K 线。period: daily/weekly/monthly。
    返回 DataFrame: 日期/开盘/收盘/最高/最低/成交量/成交额（日期为字符串）。
    """
    _, code = normalize_symbol(symbol)
    cache_key = f"hist_{code}_{period}_{adjust}_{start}_{end}"
    df = _cache_load(cache_key, config.CACHE_TTL.get("hist_" + period, 3600))
    if df is not None and len(df) > 0:
        return df

    import akshare as ak
    kw = {"symbol": code, "period": period, "adjust": adjust}
    if start:
        kw["start_date"] = start.replace("-", "")
    if end:
        kw["end_date"] = end.replace("-", "")
    last_err = None
    df = None
    for attempt in range(config.MAX_RETRY):
        try:
            _throttle()
            df = ak.stock_zh_a_hist(**kw)
            if df is not None and len(df) > 0:
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            df = None
            time.sleep(config.RETRY_BACKOFF ** attempt)
    if df is None or len(df) == 0:
        # 降级：新浪日线（日K直取；周/月K由日线重采样生成）
        try:
            daily = _sina_kline(symbol, scale=240)
            if period == "daily":
                df = daily
            else:
                rule = "W-FRI" if period == "weekly" else "M"
                d2 = daily.copy()
                d2["__d"] = pd.to_datetime(d2["日期"])
                df = (d2.set_index("__d")
                      .resample(rule)
                      .agg({"开盘": "first", "收盘": "last", "最高": "max",
                            "最低": "min", "成交量": "sum", "成交额": "sum"})
                      .dropna()
                      .reset_index())
                df["日期"] = df["__d"].dt.strftime("%Y-%m-%d")
                df = df.drop(columns=["__d"])
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(f"获取{period}行情失败: {last_err or '未知错误'} / 降级源异常: {e2}")
    if df is None or len(df) == 0:
        raise RuntimeError("未获取到行情数据，请检查代码或稍后重试")

    if "日期" in df.columns:
        df["日期"] = df["日期"].astype(str)
        df = df[["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]]
    _cache_save(cache_key, df)
    return df


def _sina_kline(symbol, scale=240, datalen=800):
    """新浪 K 线兜底：scale=240 日线 / 60 小时线 / 5 分钟线。"""
    sina_code, _ = normalize_symbol(symbol)
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_t=/CN_MarketDataService."
           f"getKLineData?symbol={sina_code}&scale={scale}&ma=no&datalen={datalen}")
    text = _http_get(url).text
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise RuntimeError("新浪K线解析失败")
    rows = json.loads(m.group(1))
    df = pd.DataFrame(rows)
    df["日期"] = df["day"].str[:10]
    df["开盘"] = df["open"].astype(float)
    df["收盘"] = df["close"].astype(float)
    df["最高"] = df["high"].astype(float)
    df["最低"] = df["low"].astype(float)
    df["成交量"] = df["volume"].astype(float)
    df["成交额"] = (df["amount"].astype(float)
                    if "amount" in df.columns else 0.0)
    df = df[["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]]
    df = df[df["日期"].str.len() == 10].reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# 实时行情 + 五档盘口（新浪，PRD 2.2）
# --------------------------------------------------------------------------
def _parse_rt_lists(data):
    """CSV 缓存往返后，把五档盘口字符串还原为 [(量,价)x5] 列表。"""
    for k in ("买盘", "卖盘"):
        v = data.get(k)
        if isinstance(v, str):
            try:
                data[k] = ast.literal_eval(v)
            except Exception:  # noqa: BLE001
                data[k] = []
    return data


def _rt_to_row(data):
    """实时行情 dict -> 可缓存行（盘口序列化为 JSON 字符串）。"""
    row = {k: v for k, v in data.items() if k not in ("买盘", "卖盘")}
    row["买盘"] = json.dumps(data["买盘"])
    row["卖盘"] = json.dumps(data["卖盘"])
    return pd.DataFrame([row])


def get_realtime(symbol):
    """
    返回 dict：最新价/涨跌幅/昨收/今开/最高/最低/成交量/成交额/时间戳/五档盘口。
    盘口结构: {'买盘': [(量,价)x5], '卖盘': [(量,价)x5]}
    """
    sina_code, _ = normalize_symbol(symbol)
    cache_key = f"realtime_{sina_code}"
    df = _cache_load(cache_key, config.CACHE_TTL["realtime"])
    if df is not None and len(df) > 0:
        return _parse_rt_lists(df.iloc[0].to_dict())

    url = f"https://hq.sinajs.cn/list={sina_code}"
    text = _http_get(url, encoding="gbk").text
    m = re.search(r'="(.*)"', text)
    if not m or not m.group(1):
        raise RuntimeError(f"实时行情为空（{sina_code}），可能已停牌或代码错误")
    f = m.group(1).split(",")
    if len(f) < 32:
        raise RuntimeError(f"实时行情字段异常: {sina_code}")

    def num(x):
        try:
            return float(x)
        except (ValueError, TypeError):
            return 0.0

    bids = [(num(f[10 + i * 2]), num(f[11 + i * 2])) for i in range(5)]   # 买一~买五 (量,价)
    asks = [(num(f[20 + i * 2]), num(f[21 + i * 2])) for i in range(5)]   # 卖一~卖五 (量,价)
    prev_close = num(f[2])
    price = num(f[3])
    data = {
        "名称": f[0],
        "最新价": price,
        "涨跌额": price - prev_close,
        "涨跌幅": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
        "今开": num(f[1]),
        "昨收": prev_close,
        "最高": num(f[4]),
        "最低": num(f[5]),
        "成交量(手)": int(num(f[8]) / 100),
        "成交额(元)": num(f[9]),
        "时间戳": f"{f[30]} {f[31]}",
        "买盘": bids,
        "卖盘": asks,
    }
    _cache_save(cache_key, _rt_to_row(data))
    return data


# --------------------------------------------------------------------------
# 分时数据（新浪，PRD 2.2）
# --------------------------------------------------------------------------
def get_intraday(symbol, scale=1, datalen=240):
    """
    当日分时（scale=1 一分钟线）。返回 DataFrame: 日期/开盘/收盘/最高/最低/成交量。
    """
    sina_code, _ = normalize_symbol(symbol)
    cache_key = f"intraday_{sina_code}_{scale}"
    df = _cache_load(cache_key, config.CACHE_TTL["intraday"])
    if df is not None and len(df) > 0:
        return df
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_t=/CN_MarketDataService."
           f"getKLineData?symbol={sina_code}&scale={scale}&ma=no&datalen={datalen}")
    text = _http_get(url).text
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise RuntimeError("分时数据解析失败")
    rows = json.loads(m.group(1))
    if not rows:
        raise RuntimeError("今日暂无分时数据（非交易时段或休市）")
    df = pd.DataFrame(rows)
    df["日期"] = df["day"]
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = df[col].astype(float)
    df = df.rename(columns={"open": "开盘", "close": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量"})
    df = df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]
    _cache_save(cache_key, df)
    return df


# --------------------------------------------------------------------------
# 估值：PE(TTM) / 市净率 历史序列（百度股市通）
# --------------------------------------------------------------------------
def get_valuation(symbol, indicator="市盈率(TTM)", period="近一年"):
    """
    返回 DataFrame: 日期/value。可用于计算当前估值及历史百分位。
    """
    _, code = normalize_symbol(symbol)
    cache_key = f"valuation_{code}_{indicator}_{period}"
    df = _cache_load(cache_key, config.CACHE_TTL["valuation"])
    if df is not None and len(df) > 0:
        return df
    import akshare as ak
    df = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period=period)
    _throttle()
    if df is None or len(df) == 0:
        raise RuntimeError(f"未获取到{indicator}数据")
    df = df.rename(columns={"date": "日期", "value": "value"})
    df["日期"] = df["日期"].astype(str)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).reset_index(drop=True)
    _cache_save(cache_key, df)
    return df


def valuation_percentile(series, latest):
    """最新值在历史序列中的百分位（0~1）。series: 历史值列表。"""
    if not len(series):
        return None
    return float((series <= latest).mean())


# --------------------------------------------------------------------------
# 公司资料（巨潮 cninfo）：行业 / 上市日期 / 主营业务
# --------------------------------------------------------------------------
def get_profile(symbol):
    """返回 dict: 公司名称/所属行业/上市日期/主营业务/注册地址。"""
    _, code = normalize_symbol(symbol)
    cache_key = f"profile_{code}"
    df = _cache_load(cache_key, config.CACHE_TTL["profile"])
    if df is not None and len(df) > 0:
        return df.iloc[0].to_dict()
    import akshare as ak
    df = ak.stock_profile_cninfo(symbol=code)
    _throttle()
    if df is None or len(df) == 0:
        raise RuntimeError("未获取到公司资料")
    r = df.iloc[0]
    data = {
        "公司名称": r.get("公司名称", ""),
        "所属行业": r.get("所属行业", ""),
        "上市日期": r.get("上市日期", ""),
        "主营业务": str(r.get("主营业务", "") or "")[:200],
        "注册地址": r.get("注册地址", ""),
    }
    _cache_save(cache_key, pd.DataFrame([data]))
    return data


# --------------------------------------------------------------------------
# 财务：净利润增长率（新浪财务指标，PRD 3.1 基本面）
# --------------------------------------------------------------------------
def get_growth(symbol):
    """返回最近一期净利润增长率（%），获取失败返回 None（不影响主流程）。"""
    _, code = normalize_symbol(symbol)
    cache_key = f"growth_{code}"
    df = _cache_load(cache_key, config.CACHE_TTL["growth"])
    if df is not None and len(df) > 0:
        return df.iloc[0].to_dict()
    try:
        import akshare as ak
        year = str(pd.Timestamp.now().year - 1)
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=year)
        _throttle()
        col = None
        for c in df.columns:
            if "净利润增长率" in str(c) and "扣除非" not in str(c):
                col = c
                break
        if col is None:
            return None
        val = df[col].dropna().iloc[-1]
        data = {"净利润增长率(%)": float(val)}
    except Exception:  # noqa: BLE001
        return None
    _cache_save(cache_key, pd.DataFrame([data]))
    return data


# --------------------------------------------------------------------------
# 历史行情日期序列（周K/月K 需自动生成，用于估值百分位对齐）
# --------------------------------------------------------------------------
def build_pe_pct_series(pe_series, hist_df):
    """
    将估值历史序列（可能频率更低/日期不同）对齐到 K 线日期上。
    返回 dict: {K线日期: 当日最新可得 PE 百分位(0~1)}
    """
    if pe_series is None or len(pe_series) == 0 or hist_df is None or len(hist_df) == 0:
        return {}
    pe = pe_series.copy()
    pe["日期"] = pd.to_datetime(pe["日期"])
    hist = hist_df.copy()
    hist["__d"] = pd.to_datetime(hist["日期"])
    merged = pd.merge_asof(
        hist.sort_values("__d"), pe.sort_values("日期"),
        left_on="__d", right_on="日期", direction="backward")
    out = {}
    # 按 K 线顺序滑动计算历史百分位（仅用当日及之前的数据，避免未来函数）
    vals = []
    for _, row in merged.iterrows():
        v = row.get("value")
        if pd.notna(v):
            vals.append(v)
            pct = (pd.Series(vals) <= v).mean()
        else:
            pct = 0.5
        out[str(row["日期_x"])[:10]] = float(pct)
    return out


# --------------------------------------------------------------------------
# 缓存直读（供 UI"启动即用/切换股票"从本地缓存读取，不触发网络请求）
# 返回的 DataFrame 可能为过期数据，由界面显示更新时间并后台刷新。
# --------------------------------------------------------------------------
def read_cached_df_any(key):
    """读取缓存文件（不过期判定），不存在或损坏返回 None。"""
    csv_path = _cache_path(key)
    if not os.path.exists(csv_path):
        return None
    try:
        return pd.read_csv(csv_path, dtype={"日期": str})
    except Exception:  # noqa: BLE001
        return None


def cache_timestamp(key):
    """返回缓存写入时间戳（epoch 秒），无缓存返回 None。"""
    meta_path = _cache_meta_path(key)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f).get("ts")
    except Exception:  # noqa: BLE001
        return None


def hist_cache_key(symbol, period="daily", adjust="qfq", start="", end=""):
    _, code = normalize_symbol(symbol)
    return f"hist_{code}_{period}_{adjust}_{start}_{end}"


def get_hist_from_cache(symbol, period="daily", adjust="qfq", start="", end=""):
    """历史K线缓存直读（任意时效），无缓存返回 None。"""
    return read_cached_df_any(hist_cache_key(symbol, period, adjust, start, end))


def get_realtime_from_cache(symbol):
    """实时行情缓存直读（任意时效），无缓存返回 None。"""
    sina_code, _ = normalize_symbol(symbol)
    df = read_cached_df_any(f"realtime_{sina_code}")
    if df is not None and len(df) > 0:
        return _parse_rt_lists(df.iloc[0].to_dict())
    return None


def get_valuation_from_cache(symbol, indicator="市盈率(TTM)", period="近一年"):
    _, code = normalize_symbol(symbol)
    return read_cached_df_any(f"valuation_{code}_{indicator}_{period}")


def get_profile_from_cache(symbol):
    _, code = normalize_symbol(symbol)
    df = read_cached_df_any(f"profile_{code}")
    if df is not None and len(df) > 0:
        return df.iloc[0].to_dict()
    return None


def get_growth_from_cache(symbol):
    _, code = normalize_symbol(symbol)
    df = read_cached_df_any(f"growth_{code}")
    if df is not None and len(df) > 0:
        return df.iloc[0].to_dict()
    return None


def get_intraday_from_cache(symbol, scale=1):
    sina_code, _ = normalize_symbol(symbol)
    return read_cached_df_any(f"intraday_{sina_code}_{scale}")


def delete_stock_cache(symbol):
    """删除指定股票的全部缓存文件（历史K线/实时/分时/估值/资料/业绩）。"""
    sina_code, code = normalize_symbol(symbol)
    prefixes = [f"hist_{code}_", f"realtime_{sina_code}",
                f"intraday_{sina_code}_", f"valuation_{code}_",
                f"profile_{code}", f"growth_{code}"]
    removed = 0
    for f in os.listdir(config.CACHE_DIR):
        if not f.endswith((".csv", ".meta.json")):
            continue
        base = f.split(".")[0]
        if any(base.startswith(p) for p in prefixes):
            try:
                os.remove(os.path.join(config.CACHE_DIR, f))
                removed += 1
            except OSError:
                pass
    return removed
