# -*- coding: utf-8 -*-
"""交易对筛选器 + 高波动时段统计。

  - 从币安拉取全部 USDT 永续 24h 行情，取交易量前 VOLUME_POOL 名作为流动池
  - 拉这些交易对的历史 1h K 线，计算每对的「30 天平均小时振幅」作为稳定波动率
  - 在流动池里按波动率从高到低选 NUM_PAIRS 个，强制包含 REQUIRE_SYMBOLS（如 ETH）
  - 对选中的交易对统计每小时平均振幅，用滑动窗口找高波动时段（UTC）
"""
from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp

import config


async def _get_json(session, url, **kw):
    async with session.get(url, proxy=config.WS_PROXY or None, timeout=20, **kw) as r:
        return await r.json()


async def get_24h_tickers() -> list[dict]:
    """拉取所有 USDT 永续合约 24h 行情。"""
    async with aiohttp.ClientSession() as s:
        data = await _get_json(s, "https://fapi.binance.com/fapi/v1/ticker/24hr")
    tickers = []
    for t in data:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        quote_vol = float(t.get("quoteVolume") or 0)
        high = float(t.get("highPrice") or 0)
        low = float(t.get("lowPrice") or 0)
        last = float(t.get("lastPrice") or 0)
        if quote_vol <= 0 or high <= 0 or low <= 0:
            continue
        tickers.append({
            "symbol": symbol,
            "quote_volume": quote_vol,
            "last": last,
            "amplitude": (high - low) / low,          # 24h 振幅（仅展示）
            "change_pct": float(t.get("priceChangePercent") or 0),
        })
    return tickers


async def fetch_precision(symbols: list[str]) -> dict:
    """拉取币安官方 exchangeInfo 中各交易对的精度与最小下单，返回 {symbol: {...}}。"""
    async with aiohttp.ClientSession() as s:
        info = await _get_json(s, "https://fapi.binance.com/fapi/v1/exchangeInfo")
    prec = {}
    syms = set(symbols)
    for sym in info.get("symbols", []):
        if sym["symbol"] not in syms:
            continue
        d = {}
        for f in sym.get("filters", []):
            ft = f["filterType"]
            if ft == "PRICE_FILTER":
                d["tick_size"] = float(f["tickSize"])
            elif ft == "LOT_SIZE":
                d["step_size"] = float(f["stepSize"])
            elif ft == "MIN_NOTIONAL":
                d["min_notional"] = float(f["notional"])
        if d:
            prec[sym["symbol"]] = d
    return prec


async def fetch_klines_many(symbols: list[str], interval: str = "1h",
                            limit: int = 1500) -> dict:
    """并发拉取多个交易对 K 线，返回 {symbol: [row,...]}（失败的对跳过）。

    interval: 周期（如 "1h" 用于波动率统计，"1m" 用于趋势策略预载）。
    limit:    每对拉取数量（币安上限 1500）。
    """
    rows_map: dict = {}
    async with aiohttp.ClientSession() as s:
        tasks = {sym: _get_json(s, "https://fapi.binance.com/fapi/v1/klines",
                                params={"symbol": sym, "interval": interval, "limit": limit})
                 for sym in symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for sym, data in zip(tasks, results):
        if isinstance(data, Exception):
            continue
        rows = []
        for k in data:
            rows.append({"ts": int(k[0]) / 1000, "high": float(k[2]),
                         "low": float(k[3]), "close": float(k[4])})
        rows_map[sym] = rows
    return rows_map


def avg_hourly_amplitude(rows: list[dict]) -> float:
    """该交易对 30 天每小时平均振幅 (high-low)/close，作为稳定波动率。"""
    amps = [(r["high"] - r["low"]) / r["close"] for r in rows if r["close"] > 0]
    return statistics.mean(amps) if amps else 0.0


def select_pairs(tickers: list[dict], volatility: dict[str, float],
                 kline_counts: dict[str, int] | None = None,
                 num: int = config.NUM_PAIRS,
                 require: list[str] | None = None,
                 pool_size: int = config.VOLUME_POOL,
                 min_days: int = config.POOL_MIN_DAYS) -> list[dict]:
    """在交易量前 pool_size 名的流动池中，按 30 天波动率从高到低选 num 个，强制含 require。

    仅保留上市足够久（kline 数 ≥ min_days*24）的交易对，排除刚上线的新币。
    """
    require = require or config.REQUIRE_SYMBOLS
    pool = sorted(tickers, key=lambda x: x["quote_volume"], reverse=True)[:pool_size]
    if kline_counts:
        min_klines = min_days * 24
        pool = [t for t in pool if kline_counts.get(t["symbol"], 0) >= min_klines]
    by_vol = sorted(pool, key=lambda t: volatility.get(t["symbol"], 0.0), reverse=True)
    chosen, chosen_syms = [], set()
    # 强制包含
    for sym in require:
        cand = next((t for t in pool if t["symbol"] == sym), None)
        if cand:
            chosen.append(cand)
            chosen_syms.add(sym)
    # 按波动率补足
    for t in by_vol:
        if len(chosen) >= num:
            break
        if t["symbol"] not in chosen_syms:
            chosen.append(t)
            chosen_syms.add(t["symbol"])
    for t in chosen:
        t["volatility"] = volatility.get(t["symbol"], 0.0)
    return chosen[:num]


def volatility_by_hour(rows_map: dict, symbols: list[str]) -> dict:
    """统计每个 UTC 小时的平均振幅，按指定交易对聚合。"""
    hourly: dict = defaultdict(list)
    for sym in symbols:
        for r in rows_map.get(sym, []):
            if r["close"] <= 0:
                continue
            hour = datetime.fromtimestamp(r["ts"], tz=timezone.utc).hour
            hourly[hour].append((r["high"] - r["low"]) / r["close"])
    return {h: statistics.mean(v) for h, v in hourly.items() if v}


def high_volatility_window(avg_by_hour: dict,
                           window_hours: int = config.VOLATILITY_WINDOW_HOURS):
    """滑动窗口找平均波动最高的连续 window_hours 小时（UTC）。"""
    if not avg_by_hour:
        return None
    best_start, best_avg = None, -1.0
    for start in range(24):
        window = [avg_by_hour.get((start + i) % 24, 0.0) for i in range(window_hours)]
        avg = sum(window) / window_hours
        if avg > best_avg:
            best_avg, best_start = avg, start
    end = (best_start + window_hours - 1) % 24
    return {"start_utc": best_start, "end_utc": end,
            "avg_amplitude": best_avg, "hours": [best_start, end]}


def window_active(hours: list, current_utc_hour: int) -> bool:
    """当前 UTC 小时是否在[开始,结束]高波动时段内（支持跨天）。"""
    start, end = hours
    if start <= end:
        return start <= current_utc_hour <= end
    return current_utc_hour >= start or current_utc_hour <= end


def describe_window(win: dict) -> str:
    """把高波动时段翻译成易读文字（北京时间为 UTC+8）。"""
    s, e = win["start_utc"], win["end_utc"]
    bj_s, bj_e = (s + 8) % 24, (e + 8) % 24
    return (f"UTC {s:02d}:00 ~ {e:02d}:00（北京时间 {bj_s:02d}:00 ~ {bj_e:02d}:00），"
            f"平均小时振幅 {win['avg_amplitude'] * 100:.2f}%")