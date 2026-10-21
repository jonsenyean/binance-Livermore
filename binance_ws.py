# -*- coding: utf-8 -*-
"""币安 USDT 永续合约 WebSocket 行情客户端（基于 aiohttp，支持本地代理）。

币安 2026-03-06 升级后，U本位合约 WS 按数据类别拆分到不同地址：
  - Public（高频行情）→ config.WS_PUBLIC_URL  订阅 bookTicker（最优买一/卖一，撮合成交价）
  - Market（常规行情）→ config.WS_MARKET_URL  订阅 markPrice@1s（标记价/资金费/强平/K线累积）
旧地址 stream.binancefuture.com 已下线（2026-04-23），markPrice 不再路由 → 行情冻结根因。

因此建立两条独立连接，各自断线自动重连。每条连接带行情看门狗：
  代理链路可能出现「连接还活着、但行情不推了」——此时 async for 会一直阻塞在等待消息上，
  永不触发 except 重连。用独立任务监控该流最近更新时间，超过 config.WS_STALE_SECONDS 无更新
  即强制 ws.close() 断开，走统一重连逻辑。连接被服务端/代理正常关闭（非异常）时同样会
  通知 on_disconnect 并等待 WS_RECONNECT_SECONDS 后重连。

两条连接只要有任一路在线就视为「已连接」（on_connect）；全部掉线才通知 on_disconnect。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Optional

import aiohttp

import config


class BinanceFuturesWS:
    def __init__(self,
                 symbols: str | list[str],
                 on_update: Callable[[str, dict], None],
                 on_connect: Optional[Callable[[], None]] = None,
                 on_disconnect: Optional[Callable[[], None]] = None):
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = list(symbols)
        # bookTicker → Public（高频）；markPrice@1s → Market（常规）
        self.public_url = config.WS_PUBLIC_URL.format(
            streams="/".join(f"{s.lower()}@bookTicker" for s in symbols))
        self.market_url = config.WS_MARKET_URL.format(
            streams="/".join(f"{s.lower()}@markPrice@1s" for s in symbols))
        self.proxy = config.WS_PROXY or None
        self.on_update = on_update
        self.on_connect = on_connect or (lambda: None)
        self.on_disconnect = on_disconnect or (lambda: None)
        self.running = False
        # 每条流的运行状态（monotonic 时间戳 / 是否被看门狗强制断开 / 是否在线）
        self._last_ts = {"book": 0.0, "mark": 0.0}
        self._stale_closed = {"book": False, "mark": False}
        self._stream_state = {"book": False, "mark": False}
        self._any_connected = False

    # ------------------------------------------------------------------
    # 两条连接
    # ------------------------------------------------------------------
    async def connect(self):
        self.running = True
        tasks = [
            asyncio.create_task(self._run_stream("book", self.public_url)),
            asyncio.create_task(self._run_stream("mark", self.market_url)),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    def _set_stream_state(self, kind: str, state: bool):
        """更新单条流在线状态；任一路在线即整体已连接，全部掉线才通知断开。"""
        self._stream_state[kind] = state
        now = any(self._stream_state.values())
        if now and not self._any_connected:
            self._any_connected = True
            self.on_connect()
        elif not now and self._any_connected:
            self._any_connected = False
            self.on_disconnect()

    async def _run_stream(self, kind: str, url: str):
        """单条流的连接 + 重连循环（kind: "book" / "mark"）。"""
        while self.running:
            self._stale_closed[kind] = False
            reason = "连接断开"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                            url, proxy=self.proxy,
                            heartbeat=20, max_msg_size=2 ** 20) as ws:
                        self._set_stream_state(kind, True)
                        print(f"[ws-{kind}] 已连接（代理: {self.proxy or '无'}）")
                        self._last_ts[kind] = time.monotonic()
                        watchdog = asyncio.create_task(self._watchdog(kind, ws))
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    d = json.loads(msg.data).get("data", {})
                                    self._last_ts[kind] = time.monotonic()
                                    self.on_update(kind, d)
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    reason = f"连接异常({msg.data})"
                                    break
                            else:
                                # async for 正常跑完：连接被服务端/代理正常关闭（非异常）
                                reason = "连接被服务端/代理关闭"
                        finally:
                            watchdog.cancel()
                            try:
                                await watchdog
                            except asyncio.CancelledError:
                                pass
            except asyncio.CancelledError:
                raise                      # stop() 主动取消：直接退出，不再重连
            except Exception as e:
                reason = f"连接异常({e!r})"
            # —— 本次连接已结束（异常 / 正常关闭 / 看门狗强制断开），统一走「通知 → 等待 → 重连」——
            self._set_stream_state(kind, False)
            if not self.running:
                break
            if self._stale_closed[kind]:
                print(f"[ws-{kind}] 看门狗已强制断开，{config.WS_RECONNECT_SECONDS} 秒后重连...")
            else:
                print(f"[ws-{kind}] {reason}，{config.WS_RECONNECT_SECONDS} 秒后重连...")
            await asyncio.sleep(config.WS_RECONNECT_SECONDS)

    async def _watchdog(self, kind: str, ws) -> None:
        """行情看门狗：超过 WS_STALE_SECONDS 无该流更新则强制断开连接。

        独立任务运行：主循环 async for 阻塞等消息期间，靠它发现「连接活着但不推流」。
        """
        interval = min(config.WS_STALE_SECONDS, 5)   # 每 ≤5s 检查一次，及时度足够
        try:
            while True:
                await asyncio.sleep(interval)
                if time.monotonic() - self._last_ts[kind] > config.WS_STALE_SECONDS:
                    self._stale_closed[kind] = True
                    print(f"[ws-{kind}] 行情超过 {config.WS_STALE_SECONDS} 秒无更新，强制断开连接")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False
