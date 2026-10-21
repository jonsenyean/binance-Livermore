# -*- coding: utf-8 -*-
"""看门狗 + 双连接逻辑测试：验证 binance_ws 断线重连修复 + main.py 行情新鲜度暂停。

运行：python _test_watchdog.py
场景：
  1. mark 流「连接活但不推流」→ 看门狗强制断开 → 自动重连；book 流全程在线不受影响
  2. 两条流均被服务端正常关闭（非异常）→ 仍通知 on_disconnect 并重连
  3. App._market_fresh 初始为 False；手动喂价/收到行情后为 True
"""
import asyncio
import time
from unittest import mock

import aiohttp
import config
import binance_ws
from main import App

# 用极短超时加速测试
config.WS_STALE_SECONDS = 1
config.WS_RECONNECT_SECONDS = 0.0

PASS = []


def ok(name):
    PASS.append(name)
    print(f"  [OK] {name}")


# ---------------------------------------------------------------- 假 WS 流
class FakeMsg:
    def __init__(self, stream, payload):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = '{"stream": "%s", "data": %s}' % (stream, payload)


def mk(stream):
    return FakeMsg(stream, '{"s": "ETHUSDT", "b": "1.0", "a": "1.1"}')


class FakeWS:
    """模式：
      msgs=[] + block=True   推完静默阻塞（复现「连接活但不推流」）
      msgs=[] + close=True   推完正常关闭（服务端断开，非异常）
      periodic>0             每 periodic 秒持续推一条（全程在线，看门狗永不触发）
    """

    def __init__(self, msgs=(), block=False, close=False, periodic=0.0):
        self._msgs = list(msgs)
        self._i = 0
        self._closed = False
        self._block = block
        self._close = close
        self._periodic = periodic
        self.closed_by_watchdog = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        if self._i < len(self._msgs):
            m = self._msgs[self._i]
            self._i += 1
            await asyncio.sleep(0.02)
            return m
        if self._periodic > 0:
            await asyncio.sleep(self._periodic)
            return mk("periodic")
        if self._close:
            raise StopAsyncIteration            # 服务端正常关闭（非异常）
        while not self._closed:                 # 静默阻塞：不报错、不推流
            await asyncio.sleep(0.05)
        raise StopAsyncIteration

    async def close(self):
        self._closed = True
        self.closed_by_watchdog = True


def run_ws_test(book_factory, mark_factory, wait: float):
    """启动双连接客户端，等待 wait 秒，返回 (client, calls, attempts, wses)。"""
    calls = {"connect": 0, "disconnect": 0, "updates": {"book": 0, "mark": 0}}
    attempts = {"book": 0, "mark": 0}
    wses = {"book": [], "mark": []}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def ws_connect(self, url, **kw):
            kind = "book" if "public" in url else "mark"
            attempts[kind] += 1
            ws = (book_factory if kind == "book" else mark_factory)(attempts[kind])
            wses[kind].append(ws)

            class Ctx:
                async def __aenter__(self):
                    return ws

                async def __aexit__(self, *a):
                    return False

            return Ctx()

    client = binance_ws.BinanceFuturesWS(
        ["ETHUSDT"],
        on_update=lambda k, d: calls["updates"].__setitem__(k, calls["updates"][k] + 1),
        on_connect=lambda: calls.__setitem__("connect", calls["connect"] + 1),
        on_disconnect=lambda: calls.__setitem__("disconnect", calls["disconnect"] + 1),
    )
    with mock.patch("binance_ws.aiohttp.ClientSession", return_value=FakeSession()):
        task = asyncio.ensure_future(client.connect())
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(wait))
        client.stop()
        task.cancel()
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
    return client, calls, attempts, wses


# ---------------------------------------------------------------- 场景 1
def test_watchdog_stale():
    print("场景 1: mark 流「连接活但不推流」→ 看门狗强制断开重连；book 流不受影响")

    def book_factory(attempt):
        return FakeWS(periodic=0.1)          # 持续推流，全程在线

    def mark_factory(attempt):
        if attempt == 1:
            return FakeWS([mk("mark"), mk("mark")], block=True)   # 推 2 条后静默阻塞
        return FakeWS(periodic=0.1)          # 重连后持续推流

    client, calls, attempts, wses = run_ws_test(book_factory, mark_factory, wait=2.6)

    assert attempts["mark"] >= 2, f"mark 应自动重连，实际连接尝试 {attempts['mark']}"
    ok("mark 流看门狗触发后自动重连（连接尝试 >=2 次）")
    assert wses["mark"][0].closed_by_watchdog, "第一次 mark 连接应由看门狗 close() 强制断开"
    ok("看门狗通过 ws.close() 强制断开")
    assert calls["updates"]["mark"] >= 3, f"mark 重连后应恢复推流，实际 {calls['updates']['mark']}"
    ok("mark 重连后行情恢复更新")
    assert attempts["book"] == 1, "book 流不应被误重连"
    ok("book 流全程在线，未被误重连")
    assert calls["disconnect"] == 0, "任一路在线时不应触发整体断开"
    ok("任一路在线时整体不断开（断线通知语义正确）")


# ---------------------------------------------------------------- 场景 2
def test_normal_close():
    print("场景 2: 两条流均被服务端正常关闭（非异常）→ 仍通知并重连")

    def book_factory(attempt):
        return FakeWS([mk("book")], close=True)

    def mark_factory(attempt):
        return FakeWS([mk("mark")], close=True)

    client, calls, attempts, wses = run_ws_test(book_factory, mark_factory, wait=1.0)

    assert attempts["book"] >= 2 and attempts["mark"] >= 2, "两条流均应自动重连"
    ok("正常关闭后两条流均自动重连")
    assert calls["disconnect"] >= 1, "全部掉线时应触发 on_disconnect"
    ok("全部掉线时通知 on_disconnect")


# ---------------------------------------------------------------- 场景 3
def test_market_fresh():
    print("场景 3: main.App 行情新鲜度判断（策略暂停依据）")
    app = App()
    assert app._market_fresh() is False, "初始(无数据)应判为过期"
    ok("初始无数据 → _market_fresh=False（策略暂停）")
    app.last_update = time.monotonic()
    assert app._market_fresh() is True, "收到行情/手动喂价后应判为新鲜"
    ok("收到行情后 → _market_fresh=True（策略继续）")
    app.last_update = 0.0
    assert app._market_fresh() is False
    ok("行情停推超过阈值 → 重新判为过期")


if __name__ == "__main__":
    test_watchdog_stale()
    test_normal_close()
    test_market_fresh()
    print(f"\n全部通过: {len(PASS)} 项")
