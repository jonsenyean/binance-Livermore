# -*- coding: utf-8 -*-
"""代理/行情连通性诊断工具：验证币安「新分类 WebSocket 地址」能否收到数据。

币安 2026-03-06 升级：U本位合约 WS 按数据类别拆分
  Public（bookTicker 等高频行情）→ wss://fstream.binance.com/public
  Market（markPrice 等常规行情） → wss://fstream.binance.com/market
旧地址 stream.binancefuture.com 已下线：markPrice 不再路由 → 行情冻结根因。
本工具直接连新地址测帧数，快速判断代理/地址是否正常。

用法：
  python check_proxy.py                  # 默认测试 ETHUSDT, BTCUSDT
  python check_proxy.py ETHUSDT SOLUSDT  # 指定交易对
  python check_proxy.py --seconds 8      # 每路测 8 秒（默认 5）
"""
from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

import config


async def probe(name: str, url: str, seconds: float, proxy: str | None):
    """连接 url 测 seconds 秒，返回收到的数据帧数；异常返回 -1。"""
    n = 0
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, proxy=proxy or None, heartbeat=20,
                                    max_msg_size=2 ** 20) as ws:
                t0 = time.monotonic()
                while time.monotonic() - t0 < seconds:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=2)
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        n += 1
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"[:80]
    return n, "ok"


async def main():
    # 解析参数：跳过 --seconds 及其数值，其余作为交易对
    argv = list(sys.argv[1:])
    seconds = 5
    if "--seconds" in argv:
        i = argv.index("--seconds")
        try:
            seconds = int(argv[i + 1])
            argv.pop(i + 1)
        except (ValueError, IndexError):
            pass
        argv.pop(i)
    symbols = [a.upper() for a in argv] or ["ETHUSDT", "BTCUSDT"]
    proxy = config.WS_PROXY or None

    print(f"代理: {proxy or '直连(无)'}   每路测试 {seconds}s    交易对: {', '.join(symbols)}")
    print("=" * 84)
    for sym in symbols:
        s = sym.lower()
        book_url = config.WS_PUBLIC_URL.format(streams=f"{s}@bookTicker")
        mark_url = config.WS_MARKET_URL.format(streams=f"{s}@markPrice@1s")
        for name, url in ((f"book {sym}", book_url), (f"mark {sym}", mark_url)):
            n, err = await probe(name, url, seconds, proxy)
            if n < 0:
                result = f"FAIL {err}"
            elif n > 3:
                result = f"OK   {n} 帧/{seconds}s"
            elif n > 0:
                result = f"OK?  {n} 帧/{seconds}s（偏少）"
            else:
                result = f"FAIL 0 帧/{seconds}s"
            print(f"{name:<12}{url:<54}{result}")
    print("=" * 84)
    print("判断：book/mark 均 OK = 代理与新地址正常；mark 全 FAIL = 见下方排查")
    print("  1) 代理是否启用：config.WS_PROXY 指向本地代理（如 Clash 7897）且已开启")
    print("  2) 新地址是否可达：旧地址(stream.binancefuture.com)已于 2026-04-23 下线")
    print("  3) 若直连可换 WS_PROXY=\"\" 试直连；仍不行多半是网络/防火墙问题")
    await asyncio.sleep(0.2)   # 留时间处理连接清理，避免 Windows 退出时 proactor 报错


if __name__ == "__main__":
    asyncio.run(main())
