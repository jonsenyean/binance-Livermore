# -*- coding: utf-8 -*-
"""币安账户只读展示模块（三行置顶标题区）。

需要 config.BINANCE_API_KEY / BINANCE_API_SECRET 及显示名 BINANCE_ACCOUNT_NAME。
只读、不下单 —— 用币安后台「只读」(Enable Reading) 权限的 API Key。

对外接口：
  - fetch_account_info(key, secret)  拉取账户数据（合约 v2 + 现货 v3，时间校准）
  - print_top_banner(acc or None)    打印置顶三行：总资金 / 合约保证金 / 现货价值
  - show_account_if_configured()     入口：启动时的一次性展示（未配置则静默返回）

签名：HmacSHA256(secret, query + timestamp)，先拉 serverTime 校准本机时钟
（本机时钟偏差会导致 -1021 拒绝）。全部请求走 config.WS_PROXY 代理。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import urllib.parse

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

import aiohttp

import config

FAPI_BASE = "https://fapi.binance.com"   # U 本位合约（USDT 永续）
SPOT_BASE = "https://api.binance.com"    # 现货

# 本机时钟与币安服务器的毫秒偏移（首次校准后缓存）
_time_offset: int = 0


async def _server_time(base: str) -> None:
    """拉 serverTime 校准本机与币安的毫秒偏移，写入 _time_offset。"""
    global _time_offset
    try:
        path = "/fapi/v1/time" if "fapi" in base else "/api/v3/time"
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}{path}",
                             proxy=config.WS_PROXY or None, timeout=10) as r:
                d = await r.json()
        _time_offset = int(d["serverTime"]) - int(time.time() * 1000)
    except Exception:
        _time_offset = 0


def _signed(url: str, key: str, secret: str, params: dict | None = None) -> str:
    """构造带签名的 URL（timestamp 用校准后服务器时间，recvWindow 放宽防偏差）。"""
    q = dict(params or {})
    q["timestamp"] = int(time.time() * 1000) + _time_offset
    q["recvWindow"] = 10000
    raw = urllib.parse.urlencode(q)
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{url}?{raw}&signature={sig}"


async def _get_json(url: str, headers: dict) -> dict | list:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers,
                         proxy=config.WS_PROXY or None, timeout=20) as r:
            return await r.json()


def _to_num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------
# 账户数据抓取
# ----------------------------------------------------------------------
async def fetch_account_info(key: str, secret: str) -> dict:
    """拉取合约 v2 账户 + 现货 v3 账户，返回汇总 dict。"""
    auth = {"X-MBX-APIKEY": key}

    # 合约顶层钱包（总钱包 / 可用 / 权益 / 未实现）
    await _server_time(FAPI_BASE)
    futures_wallet = {"wallet": 0.0, "available": 0.0, "equity": 0.0, "pnl": 0.0}
    futures_balance = []
    futures_positions = []
    try:
        acc = await _get_json(_signed(f"{FAPI_BASE}/fapi/v2/account", key, secret), auth)
        if isinstance(acc, dict):
            futures_wallet = {
                "wallet": _to_num(acc.get("totalWalletBalance")),
                "available": _to_num(acc.get("availableBalance")),
                "equity": _to_num(acc.get("totalMarginBalance")),
                "pnl": _to_num(acc.get("totalUnrealizedProfit")),
            }
            futures_balance = [
                {"asset": a.get("asset"), "balance": _to_num(a.get("balance")),
                 "available": _to_num(a.get("availableBalance")),
                 "unrealized": _to_num(a.get("unrealizedProfit"))}
                for a in acc.get("assets", []) if _to_num(a.get("balance")) != 0
            ]
    except Exception:
        pass

    # 合约持仓
    try:
        pr = await _get_json(_signed(f"{FAPI_BASE}/fapi/v2/positionRisk",
                                     key, secret), auth)
        if isinstance(pr, list):
            futures_positions = [
                {"symbol": p.get("symbol"),
                 "side": p.get("positionSide")
                 or ("LONG" if _to_num(p.get("positionAmt")) > 0 else "SHORT"),
                 "amt": _to_num(p.get("positionAmt")),
                 "entry": _to_num(p.get("entryPrice")),
                 "mark": _to_num(p.get("markPrice")),
                 "pnl": _to_num(p.get("unRealizedProfit")),
                 "notional": abs(_to_num(p.get("notional")))}
                for p in pr if _to_num(p.get("positionAmt")) != 0
            ]
    except Exception:
        pass

    # 现货账户 + USDT + 代币
    await _server_time(SPOT_BASE)
    spot_balances, spot_usdt = [], None
    try:
        sp = await _get_json(_signed(f"{SPOT_BASE}/api/v3/account", key, secret), auth)
        if isinstance(sp, dict):
            usdt = None
            for b in sp.get("balances", []):
                if b.get("asset") == "USDT":
                    usdt = {"free": _to_num(b.get("free")),
                            "locked": _to_num(b.get("locked"))}
                if _to_num(b.get("free")) != 0 or _to_num(b.get("locked")) != 0:
                    spot_balances.append(b)
            spot_usdt = usdt
    except Exception:
        pass

    return {
        "futures_wallet": futures_wallet,
        "futures_balance": futures_balance,
        "futures_positions": futures_positions,
        "spot_balances": spot_balances,
        "spot_usdt": spot_usdt,
    }


def _account_summary(acc: dict) -> dict:
    """三大金额：总资金 = 现货价值 + 合约保证金（钱包余额）。"""
    fw = acc["futures_wallet"]
    sp_free = acc["spot_usdt"]["free"] if acc["spot_usdt"] else 0.0
    sp_lock = acc["spot_usdt"]["locked"] if acc["spot_usdt"] else 0.0
    spot_val = sp_free + sp_lock
    futures_margin = fw["wallet"]
    return {
        "total": spot_val + futures_margin,
        "futures": futures_margin,
        "spot": spot_val,
        "name": (config.BINANCE_ACCOUNT_NAME or "").strip() or "币安账户",
    }


# ----------------------------------------------------------------------
# Windows 控制台 ANSI 颜色启用
# ----------------------------------------------------------------------
def enable_vt() -> None:
    """用 ctypes 给输出句柄开启 VT 处理（ENABLE_VIRTUAL_TERMINAL_PROCESSING）。

    cmd 默认不解析 \\033[...m 转义（会原样打成乱码），必须显式开启。
    比 os.system('') 更可靠；无副作用，失败静默。
    """
    if sys.platform != "win32":
        return
    try:
        h = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not h or h == -1:
            return
        mode = wintypes.DWORD()
        ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode))
        ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 置顶三行标题区
# ----------------------------------------------------------------------
def _color(s: str, code: str, use_color: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if use_color else s


def fmt_amt(x: float, nd: int = 2) -> str:
    """自适应小数的金额显示：小额显示足够多有效位，大额保留千分位。

    - |x| >= 1000  → 保留 2 位小数（逗号千分位）
    - |x| >= 1     → 保留 2 位小数
    - |x| <  1     → 显示 8 位小数（如 0.00349973，避免小额被圆整成 0.00）
    """
    if abs(x) < 1e-12:
        return "0"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.8f}"


def print_top_banner(acc: dict | None, use_color: bool = True) -> None:
    """置顶三行：总资金 / 合约保证金 / 现货价值（金色框 + 绿色合约行）。

    main 每次刷新状态时清屏后调用，使三行始终贴在屏幕最顶端。
    acc=None 表示未配置/未拉到，仍打印标题占位。
    """
    enable_vt()   # 每次重绘都确保 ANSI 颜色可用，防止颜色码原样打出来变乱码
    name = (config.BINANCE_ACCOUNT_NAME or "").strip() or "币安账户"
    if acc is None:
        t0 = f"总资金 -- USDT"
        t1 = f"合约保证金 -- USDT"
        t2 = f"现货价值 -- USDT"
    else:
        s = _account_summary(acc)
        t0 = f"总资金 {fmt_amt(s['total'])} USDT"
        t1 = f"合约保证金 {fmt_amt(s['futures'])} USDT"
        t2 = f"现货价值 {fmt_amt(s['spot'])} USDT"

    print(_color("═" * 80, "1;33", use_color))
    print(_color(f"   ★ ★ ★  {name}  ★ ★ ★".center(80), "1;33", use_color))
    print(_color("═" * 80, "1;33", use_color))
    print(f"  {t0}")
    print(_color(f"  {t1}", "1;32", use_color))
    print(f"  {t2}")
    print(_color("─" * 80, "1;33", use_color))


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
async def show_account_if_configured() -> None:
    """一次性展示（启动时）。未配置则静默返回；失败打印原因但不中断主程序。"""
    key = (config.BINANCE_API_KEY or "").strip()
    secret = (config.BINANCE_API_SECRET or "").strip()
    if not key or not secret:
        return
    if sys.platform == "win32":
        os.system("")   # 开启 Windows 控制台 ANSI 支持
    try:
        acc = await fetch_account_info(key, secret)
        print_top_banner(acc)
    except Exception as e:
        from api_client import print_top_banner  # noqa: F401  (防御，避免裸打印)
        print_top_banner(None)
        print(f"[账户] 拉取失败：{type(e).__name__}: {e}（请核对 API Key / 网络）")


# 兼容旧的调用名（banner 一次性全量打印），防止外部引用报错
print_account_banner = print_top_banner