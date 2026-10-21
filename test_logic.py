# -*- coding: utf-8 -*-
"""离线逻辑自测：不联网验证账户账务（按币安官方规则：精度/分档保证金/手续费/资金费）
与自动策略闭环：
  - 趋势加仓策略（趋势识别→建仓→浮盈回调加仓→止盈/止损/移动止盈/趋势反转）
  - 利弗莫尔战法策略（关键点突破进场→试探仓→有盈利创新高金字塔加仓→关键点止损）

运行：python test_logic.py
"""
from __future__ import annotations

import math
import time

import config
from paper_account import PaperAccount, mmr_tier
from portfolio import Portfolio
from strategy import LivermoreRollStrategy, TrendPyramidStrategy

config.TRADE_JOURNAL = ""   # 离线测试不写 trade_log.csv（交易日志在实盘/模拟盘生效）


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def floor_step(x, step=0.001):
    """数量按币安官方 step_size 向下取整。"""
    return math.floor(x / step + 1e-9) * step


def check(name, cond):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}")
        raise SystemExit(1)


def test_long_round_trip():
    print("测试 1: 做多完整来回（开仓->浮盈->平仓）")
    a = PaperAccount()
    a.leverage = 10  # 测试按 10x 核算，固定杠杆不随全局配置变化
    ok, _ = a.open_position("LONG", 0.5, bid=100.0, ask=101.0)
    check("开多成功", ok)
    assert a.position is not None and a.position.side == "LONG"
    check("成交价为卖一 101（tick舍入）", approx(a.position.entry_price, 101.0))
    margin = 101.0 * 0.5 / 10  # 杠杆10
    fee_open = 101.0 * 0.5 * config.TAKER_FEE
    check("保证金正确", approx(a.position.margin, margin))
    check("可用余额=每对资金100-保证金-手续费", approx(a.cash, 100 - margin - fee_open))

    upnl = a.position.unrealized_pnl(110.0)
    check("未实现盈亏 (110-101)*0.5=4.5", approx(upnl, 4.5))
    check("权益=余额+保证金+浮盈", approx(a.equity(110.0), a.cash + margin + 4.5))

    ok, _ = a.close_position(None, bid=110.0, ask=111.0)
    check("平多成功", ok)
    check("平多成交价为买一 110（卖出吃买一）", approx(a.trades[-1].price, 110.0))
    fee_close = 110.0 * 0.5 * config.TAKER_FEE
    check("已实现=(110-101)*0.5-开平手续费", approx(a.realized_pnl, 4.5 - fee_close - fee_open))
    check("无持仓", a.position is None)
    check("最终净收益=已实现", approx(a.net_profit(111.0), a.realized_pnl))
    print()


def test_short():
    print("测试 2: 做空与平仓")
    a = PaperAccount()
    a.leverage = 10  # 测试按 10x 核算强平价
    ok, _ = a.open_position("SHORT", 1.0, bid=100.0, ask=101.0)
    check("开空成交价为买一 100", ok and approx(a.position.entry_price, 100.0))
    check("做空下跌盈利", approx(a.position.unrealized_pnl(90.0), 10.0))
    check("做空上涨亏损", approx(a.position.unrealized_pnl(110.0), -10.0))
    mmr = mmr_tier(100.0)[0]  # 名义100 → 档位1: MMR 0.4%
    expected_liq = 100.0 * (1 + 1 / 10) / (1 + mmr)
    check(f"空头强平价={expected_liq:.3f}", approx(a.position.liquidation_price(), expected_liq))
    print()


def test_liquidation():
    print("测试 3: 强平（按档位维持保证金率）")
    a = PaperAccount()
    a.leverage = 10  # 测试按 10x 核算强平价
    a.open_position("LONG", 1.0, bid=99.0, ask=100.0)
    margin = a.position.margin
    mmr = mmr_tier(100.0)[0]
    liq = a.position.liquidation_price()
    check(f"强平价={liq:.4f}", approx(liq, 100.0 * (1 - 1 / 10) / (1 - mmr)))
    hit = a.check_liquidation(liq - 0.01)
    check("触及强平价触发强平", hit)
    check("强平后无持仓", a.position is None)
    # 强平后权益 ≈ 每对资金 - 保证金 + 强平价处维持保证金 - 手续费
    leftover = config.PER_PAIR_BALANCE - margin + liq * mmr - a.fees_paid
    check("强平后权益≈初始-保证金+维持保证金-手续费", approx(a.equity(liq), leftover))
    print()


def test_liquidation_added_margin():
    print("测试 3b: 加仓后强平价按累计保证金计算（币安孤立保证金公式）")
    a = PaperAccount()
    a.leverage = 30
    a.open_position("LONG", 1.0, bid=99.0, ask=100.0)   # 首仓名义100@100, 保证金 100/30
    liq30 = a.position.liquidation_price()

    # 中途降杠杆后再加仓 → 加仓锁更多保证金 → 强平价应更远离现价
    a.leverage = 10
    a.open_position("LONG", 1.0, bid=100.0, ask=101.0)  # 加仓1单位@101, 保证金 101/10
    pos = a.position
    mmr = mmr_tier(pos.notional)[0]
    expected = (pos.notional - pos.margin) / (pos.qty * (1 - mmr))
    check("强平价=(入场名义-累计保证金)/(数量×(1-MMR))", approx(pos.liquidation_price(), expected))

    # 旧公式（只看首仓杠杆）偏紧，新公式给更多缓冲
    old_style = pos.entry_price * (1 - 1 / 30) / (1 - mmr)
    check("含累计保证金后强平价更远离现价", pos.liquidation_price() < old_style)

    # 在强平价处权益 = 维持保证金（币安定义）
    eq_at_liq = pos.margin + (pos.liquidation_price() - pos.entry_price) * pos.qty
    check("强平价处权益=维持保证金", approx(eq_at_liq, pos.liquidation_price() * pos.qty * mmr, tol=1e-6))
    print()


def test_funding():
    print("测试 4: 资金费")
    a = PaperAccount()
    a.open_position("LONG", 1.0, bid=99.0, ask=100.0)
    amt = a.apply_funding(0.0001, mark=101.0)
    check("正费率多头付费 101*1*0.0001", approx(amt, 0.0101))
    check("多头现金减少", approx(a.funding_paid, 0.0101))

    a.close_position(None, bid=101.0, ask=102.0)
    a.open_position("SHORT", 1.0, bid=100.0, ask=101.0)
    amt = a.apply_funding(0.0001, mark=100.0)
    check("正费率空头收款 0.01", approx(amt, 0.0100))
    check("空头资金费累计为净支出减少", approx(a.funding_paid, 0.0101 - 0.0100))
    print()


def test_precision():
    print("测试 5: 币安官方精度（最小下单 50 / step取整 / 最低档封顶杠杆）")
    a = PaperAccount()
    ok, msg = a.open_position("LONG", 0.01, bid=100.0, ask=100.0)  # 名义 ~1 USDT
    check("低于最小下单 50 被拦截", not ok and "最小下单" in msg)
    ok, _ = a.open_position("LONG", 0.5123, bid=100.0, ask=100.0)  # 名义~51.2
    check("数量向下取整到 step 0.001", ok and approx(a.position.qty, 0.512))
    a.leverage = 999  # 超过档位最大杠杆，应被封顶
    a.close_position(None, bid=100.0, ask=101.0)
    ok, _ = a.open_position("LONG", 1.0, bid=100.0, ask=101.0)
    check("杠杆按档位封顶（最大125x）", ok and a.position.leverage == 125)
    print()


def test_reverse():
    print("测试 6: 反向减仓/翻仓")
    a = PaperAccount()
    a.open_position("LONG", 1.0, bid=100.0, ask=101.0)
    ok, _ = a.open_position("SHORT", 0.6, bid=100.0, ask=101.0)
    check("反向减仓成功", ok)
    check("剩余多头 0.40", approx(a.position.qty, 0.4) and a.position.side == "LONG")
    ok, _ = a.open_position("SHORT", 1.4, bid=100.0, ask=101.0)
    check("翻仓成功", ok)
    check("已翻为 1.00 空头", approx(a.position.qty, 1.0) and a.position.side == "SHORT")
    print()


def test_insufficient():
    print("测试 7: 余额不足拦截")
    a = PaperAccount()
    a.leverage = 1
    ok, msg = a.open_position("LONG", 100000.0, bid=100.0, ask=100.0)
    check("余额不足应被拦截", not ok)
    print()


# ----------------------------------------------------------------------
# 趋势加仓策略闭环测试
# ----------------------------------------------------------------------
class FakeApp:
    """模拟 App 的最小对象：提供 bid/ask/mark/last_price 与真实账户。"""
    def __init__(self):
        self.bid = 100.0
        self.ask = 101.0
        self.mark = 100.5
        self.last_price = 100.5
        self.account = PaperAccount()
        self.account.leverage = 10   # 固定 10x，对齐 config.LEVERAGE（治本后）


def make_trend():
    s = TrendPyramidStrategy()
    s.base_margin_usd = 10.0         # 首仓名义 = 10×10 = 100 USDT（保证金 10）
    s.tp = 5.0                       # 止盈：盈利达保证金 500%
    s.sl = 0.5                       # 止损：亏损达保证金 50%
    s.time_limit = 60 * 60 * 8
    s.cooldown = 0                   # 测试关闭冷却，便于连续观察
    s.loss_cooldown = 0              # 测试关闭亏损冷却（A+B 机制单独在测试 17 验证）
    s.min_add_usd = 20
    s.max_adds = 3
    s.add_ratio = 1.0
    s.pullback_pct = 0.005           # 回调 0.5% 触发加仓
    s.trail_activate = 0.02          # 浮盈 2% 启动移动止盈
    s.trail_stop = 0.02              # 峰值回撤 2% 平仓
    return s


def move(app, ask, bid=None):
    """让卖一/买一/标记价同步变化，模拟真实行情联动。"""
    app.ask = ask
    app.bid = bid if bid is not None else ask - 0.5
    app.mark = (app.bid + app.ask) / 2
    app.last_price = app.mark


def candles_uptrend(n=60):
    """缓慢上涨的 1m K 线（每根 +0.2%）。"""
    rows = []
    price = 100.0
    for i in range(n):
        price = round(price * 1.002, 2)
        rows.append({"ts": 1_700_000_000 + i * 60, "open": price,
                     "high": price * 1.0005, "low": price * 0.9995, "close": price})
    return rows


def candles_downtrend(n=60):
    """缓慢下跌的 1m K 线（每根 -0.2%）。"""
    rows = []
    price = 110.0
    for i in range(n):
        price = round(price * 0.998, 2)
        rows.append({"ts": 1_700_000_000 + i * 60, "open": price,
                     "high": price * 1.0005, "low": price * 0.9995, "close": price})
    return rows


# ----------------------------------------------------------------------
# 利弗莫尔战法策略闭环测试
# ----------------------------------------------------------------------
def make_livermore():
    """构造利弗莫尔策略：固定参数，便于离线核算。"""
    s = LivermoreRollStrategy()
    s.base_margin_usd = 40.0         # 试探仓保证金 40U（对齐 config.LIVERMORE_PROBE_MARGIN_USD），10x 名义 = 400 USDT
    s.tp = 5.0                       # 止盈：盈利达保证金 500%
    s.sl = 0.5                       # 止损：亏损达保证金 50%
    s.time_limit = 60 * 60 * 8
    s.cooldown = 0                   # 测试关闭冷却，便于连续观察
    s.loss_cooldown = 0              # 测试关闭亏损冷却（A+B 机制单独在测试 17 验证）
    s.min_add_usd = 20
    s.max_adds = 3
    s.add_ratio = 1.0
    s.pullback_pct = 0.005           # 回调 0.5% 触发加仓
    s.add_breakout_pct = 0.003       # 创新高加仓：比参考价再突破 0.3%
    s.trail_activate = 0.02          # 浮盈 2% 启动移动止盈
    s.trail_giveback = 0.35          # 回吐峰值利润 35% 平仓
    s.stop_atr_mult = 0              # 关闭 ATR 止损：离线测试聚焦关键点止损/金字塔原逻辑，避免低波动下 ATR 抢先关键点触发
    s.breakout_pct = 0.001           # 关键点突破缓冲 0.1%
    s.breakout_atr_mult = 0          # 关闭自适应缓冲：保持固定缓冲原语义（测试 19 单独验证）
    s.funding_filter = 0             # 关闭资金费过滤（测试 19 单独验证）
    s.risk_max_loss = 0              # 关闭风险预算仓位：保持试探仓名义固定（测试 22 单独验证）
    s.stagnation_minutes = 0         # 关闭滞涨离场（测试 20 单独验证）
    s.breakeven_buffer = 0.0         # 保本位=入场价（原语义；含费缓冲在测试 21 单独验证）
    s.pivot_period = 30              # 关键点窗口 30 根
    s.require_trend = True           # 均线趋势过滤（测试过滤路径）
    s.use_pivot_stop = True          # 关键点止损
    s.use_higher_tf = False          # 关闭 15m 多周期过滤：离线测试聚焦 1m 突破/关键点止损原逻辑（15m 严格门控会挡掉横盘突破形态）
    return s


def candles_range(n=60, base=100.0):
    """横盘 K 线（窄幅 ±0.15%），用于建立关键点区间。"""
    rows = []
    for i in range(n):
        p = base * (1 + 0.0015 * math.sin(i / 3))
        rows.append({"ts": 1_700_000_000 + i * 60, "open": p,
                     "high": p * 1.001, "low": p * 0.999, "close": p})
    return rows


def candles_uptrend_breakout(n=40, base=100.0):
    """先横盘 n 根，随后单根大涨 4% 突破（趋势转 UP，关键点上沿=突破阳线高点）。"""
    rows = candles_range(n, base)
    ts = rows[-1]["ts"] + 60
    price = base * 1.04
    rows.append({"ts": ts, "open": base, "high": price, "low": base, "close": price})
    return rows


def candles_downtrend_breakout(n=40, base=100.0):
    """先横盘 n 根，随后单根大跌 4% 跌破（趋势转 DOWN，关键点下沿=破位阴线低点）。"""
    rows = candles_range(n, base)
    ts = rows[-1]["ts"] + 60
    price = base * 0.96
    rows.append({"ts": ts, "open": base, "high": base, "low": price, "close": price})
    return rows


def test_trend_entry():
    print("测试 8: 趋势识别与建仓（上涨→多，下跌→空）")
    s = make_trend()
    s.seed_candles(candles_uptrend())
    check("上涨趋势识别为 UP", s.trend() == "UP")

    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    check("上涨趋势建多", app.account.position is not None and app.account.position.side == "LONG")
    check("状态 HOLDING", s.state == "HOLDING")

    s2 = make_trend()
    s2.seed_candles(candles_downtrend())
    check("下跌趋势识别为 DOWN", s2.trend() == "DOWN")
    app2 = FakeApp()
    move(app2, 105.0)
    s2.on_eval(app2)
    check("下跌趋势建空", app2.account.position is not None and app2.account.position.side == "SHORT")
    print()


def test_trend_warmup():
    print("测试 9: 均线未成形前不开仓")
    s = make_trend()
    s.seed_candles(candles_uptrend(10))    # 只有 10 根 < min_candles(21)
    check("K 线不足视为 FLAT", s.trend() == "FLAT")
    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    check("趋势未成形不开仓", app.account.position is None and s.state == "IDLE")
    print()


def test_pyramid_add():
    print("测试 10: 浮盈加仓（有盈利 + 回调 → 用盈利加仓）")
    s = make_trend()
    s.seed_candles(candles_uptrend())
    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    pos = app.account.position
    base_qty = pos.qty
    check("首次建仓成功", pos is not None and pos.side == "LONG")

    # 未创新高的下跌 → 无盈利/无新高，不加仓
    move(app, 104.0)
    s.on_eval(app)
    check("无新高无盈利不加仓", app.account.position.qty == base_qty and s.add_count == 0)

    # 创新高（盈利）后回调 ≥0.5% → 用盈利加仓
    move(app, 110.0)
    s.on_eval(app)
    check("创新高解锁加仓资格", s.arm_for_add)
    move(app, 108.5)                      # 回撤 (110-108.5)/110 ≈ 1.36%
    s.on_eval(app)
    qty_after = app.account.position.qty
    check("回调触发加仓", s.add_count == 1 and qty_after > base_qty)

    # 加仓后同一段继续下跌 → 不再加仓（须再创新高）
    move(app, 108.0)                      # 回撤 (110-108)/110≈1.8%：不触发移动止盈，也未再创新高
    s.on_eval(app)
    check("同一段下跌不再加仓", s.add_count == 1 and app.account.position.qty == qty_after)

    # 再创新高 + 回调 → 第二次加仓
    move(app, 112.0)
    s.on_eval(app)
    move(app, 110.5)                      # 回撤 (112-110.5)/112 ≈ 1.34%
    s.on_eval(app)
    check("第二次加仓", s.add_count == 2)

    # max_adds 上限：只允许 1 次时不再加仓
    s2 = make_trend()
    s2.max_adds = 1
    s2.seed_candles(candles_uptrend())
    app2 = FakeApp()
    move(app2, 105.0)
    s2.on_eval(app2)
    move(app2, 110.0)
    s2.on_eval(app2)
    move(app2, 108.5)
    s2.on_eval(app2)
    check("达到加仓上限后不再加仓", s2.add_count == 1)
    move(app2, 113.0)
    s2.on_eval(app2)
    move(app2, 111.5)
    s2.on_eval(app2)
    check("上限后再次创新高也不加仓", s2.add_count == 1)
    print()


def test_trend_exits():
    print("测试 11: 止损 / 移动止盈 / 趋势反转")
    # 止损：入场 105 → 跌到 98.75，亏损约 -59% 保证金 ≥ 50%
    s = make_trend()
    s.seed_candles(candles_uptrend())
    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    move(app, 99.0)
    s.on_eval(app)
    check("跌破止损平仓", app.account.position is None and s.state == "IDLE")

    # 移动止盈：浮盈超 2% 启动，峰值 112 回撤 2.68% ≥ 2% 平仓
    s = make_trend()
    s.seed_candles(candles_uptrend())
    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    check("启动前移动止盈关闭", not s.trail_active)
    move(app, 112.0)
    s.on_eval(app)
    check("浮盈超阈值启动移动止盈", s.trail_active)
    move(app, 109.0)
    s.on_eval(app)
    check("峰值回撤触发移动止盈平仓", app.account.position is None)

    # 趋势反转：建多后均线转空 → 平多
    s = make_trend()
    s.seed_candles(candles_uptrend())
    app = FakeApp()
    move(app, 105.0)
    s.on_eval(app)
    check("建多成功", app.account.position is not None and app.account.position.side == "LONG")
    s.seed_candles(candles_downtrend())
    move(app, 104.0)
    s.on_eval(app)
    check("趋势反转平多", app.account.position is None and s.state == "IDLE")
    print()


# ----------------------------------------------------------------------
# 利弗莫尔战法策略测试（测试 12~16）
# ----------------------------------------------------------------------
def test_livermore_breakout_entry():
    print("测试 12: 关键点识别与突破进场（超过关键点+缓冲才进场，均线过滤逆势突破）")
    s = make_livermore()
    s.seed_candles(candles_uptrend_breakout())
    piv_h, piv_l = s.pivots()
    check("关键点上沿=突破阳线高点", approx(piv_h, 104.0, tol=0.01))
    check("关键点下沿=横盘区低点", 99.5 <= piv_l <= 100.5)

    # 只超过关键点、未超过突破缓冲 → 不进场
    app = FakeApp()
    move(app, 104.3)            # 超过 104.0 但 < 104.104（104×1.001）
    s.on_eval(app)
    check("超过关键点未超缓冲不进场", app.account.position is None)

    # 突破关键点+缓冲 → 试探建多
    move(app, 104.6)            # mark 104.35 > 104.104
    s.on_eval(app)
    check("突破关键点试探建多", app.account.position is not None and app.account.position.side == "LONG")
    check("状态 HOLDING", s.state == "HOLDING")
    check("首仓为试探仓保证金", approx(app.account.position.margin, 40.0, tol=0.5))

    # 下跌趋势中的多头突破被均线过滤拦截
    s2 = make_livermore()
    s2.seed_candles(candles_downtrend_breakout())
    piv_h2, _ = s2.pivots()
    app2 = FakeApp()
    move(app2, piv_h2 * 1.02)     # 大幅高于关键点上沿（多头突破信号），但趋势 DOWN
    s2.on_eval(app2)
    check("逆势突破被均线过滤拦截", app2.account.position is None)
    print()


def test_livermore_pyramid_add():
    print("测试 13: 利弗莫尔金字塔加仓（有盈利创新高才加仓，回调绝不加仓）")
    s = make_livermore()
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    base_qty = app.account.position.qty
    check("突破试探建多成功", app.account.position is not None and app.account.position.side == "LONG")

    # 创新高（有盈利）超过上次加仓价 0.3% → 顺势加仓（10x 名义 400U，+6% 浮盈 ~24U ≥ 20U 门槛）
    move(app, 111.0)
    s.on_eval(app)
    check("创新高有盈利加仓", s.add_count == 1 and app.account.position.qty > base_qty)

    # 回调 → 不加仓（利弗莫尔绝不接回调）
    qty1 = app.account.position.qty
    move(app, 110.0)                # 回撤 (111-110)/111 ≈ 0.9%，不创新高、浮盈不足以再次加仓
    s.on_eval(app)
    check("回调不加仓（不接回调）", s.add_count == 1 and app.account.position.qty == qty1)

    # 再创新高 → 再次加仓（逐级金字塔）
    move(app, 112.0)
    s.on_eval(app)
    check("再创新高继续加仓", s.add_count == 2 and app.account.position.qty > qty1)
    print()


def test_livermore_add_limit():
    print("测试 14: 利弗莫尔金字塔加仓上限（max_adds 封顶后不再加）")
    s = make_livermore()
    s.max_adds = 1
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    check("突破试探建多成功", app.account.position is not None and app.account.position.side == "LONG")

    # 创新高 → 加仓（用掉唯一一次额度）
    move(app, 111.0)
    s.on_eval(app)
    check("达到上限加仓一次", s.add_count == 1)

    # 再创新高 → 已到上限，不再加仓
    qty = app.account.position.qty
    move(app, 113.0)
    s.on_eval(app)
    check("达到上限后不再加仓", s.add_count == 1 and app.account.position.qty == qty)
    print()


def test_livermore_pivot_stop():
    print("测试 15: 关键点止损（跌破关键点离场，止损位只上移不下移）")
    s = make_livermore()
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    check("突破建多成功", app.account.position is not None and app.account.position.side == "LONG")
    s.on_eval(app)                      # 让关键点止损初始化
    check("关键点止损初始=横盘区低点", 99.0 <= s.pivot_stop <= 101.0)

    # 30 根更高低点的新 K 线 → 关键点止损上移
    last_ts = s.candles[-1]["ts"]
    for i in range(30):
        s.candles.append({"ts": last_ts + (i + 1) * 60, "open": 105.0,
                          "high": 105.5, "low": 104.8, "close": 105.2})
    move(app, 105.2)
    s.on_eval(app)
    check("关键点止损上移", s.pivot_stop >= 104.8)

    # 价格跌破新止损 → 关键点止损离场（未触发保证金止损）
    move(app, 104.0)
    s.on_eval(app)
    check("跌破关键点止损离场", app.account.position is None and s.state == "IDLE")
    print()


def test_livermore_reentry_guard():
    print("测试 16: 试探仓确认 + 平仓后须突破更高关键点才再次进场")
    s = make_livermore()
    check("试探仓保证金对齐配置", approx(s.base_margin_usd, config.LIVERMORE_PROBE_MARGIN_USD))

    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    check("突破试探建多", app.account.position is not None)
    s.on_eval(app)                      # 让关键点止损初始化

    # 大幅回落跌破关键点止损 → 平仓
    move(app, 99.0)
    s.on_eval(app)
    check("跌破关键点后平仓", app.account.position is None and s.state == "IDLE")

    # 同一价位附近再次突破（关键点未抬高）→ 不重复进场
    move(app, 104.5)
    s.on_eval(app)
    check("同一关键点不重复进场", app.account.position is None)
    print()


def candles_wild(n=40, base=100.0):
    """高波动 K 线（每根振幅 10%），用于验证风险预算收敛名义。"""
    rows = []
    for i in range(n):
        rows.append({"ts": 1_700_000_000 + i * 60, "open": base,
                     "high": base * 1.05, "low": base * 0.95, "close": base})
    return rows


def test_risk_governor():
    print("测试 18: 组合风控闸门（持仓数 / 净方向敞口 / 保证金占用 / 当日熔断）")
    saved = (config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT,
             config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT)
    try:
        p = Portfolio(["ETHUSDT", "AAAUSDT", "BBBUSDT"], shared_cash=True)
        for c in p.ctx.values():
            c.mark = c.last_price = 100.0
        p.ctx["ETHUSDT"].account.open_position("LONG", 1.0, 99.0, 100.0)
        p.ctx["AAAUSDT"].account.open_position("LONG", 1.0, 99.0, 100.0)
        t = p._totals()
        check("净敞口=两笔多头名义合计", approx(t["net_notional"], 200.0))
        check("权益=现金+保证金+未实现（扣除开仓手续费）",
              approx(t["equity"], 1000.0 - 2 * 100.0 * config.TAKER_FEE))

        # A. 持仓数上限：已 2 仓 → 第三对不放行
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT = 2, 1.0
        config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = 10 ** 9, 1.0
        g = p._gate_for("BBBUSDT")
        check("持仓数达上限不放行", not g["can_enter"] and "持仓数满" in g["reason"])

        # B. 净方向敞口：多头净敞口 200，上限 250 → 追多超限拦截、反向仍放行
        config.RISK_MAX_POSITIONS, config.RISK_MAX_NET_NOTIONAL_USD = 10, 250.0
        g = p._gate_for("BBBUSDT")
        check("多头敞口超限拦截", not g["long_ok"] and "多头" in g["reason"])
        check("空头方向仍放行", g["short_ok"] and g["can_enter"])

        # C. 保证金占用：上限 1% < 已用 2% → 加仓与新开仓都拦截
        config.RISK_MAX_MARGIN_PCT = 0.01
        g = p._gate_for("ETHUSDT")
        check("保证金占用超限拦截加仓", not g["can_add"])
        g2 = p._gate_for("BBBUSDT")
        check("保证金占用超限拦截开仓", not g2["can_enter"])

        # D. 当日熔断：权益自日初回撤 50% ≥ 5% → 全组合暂停
        config.RISK_MAX_MARGIN_PCT = 1.0
        config.RISK_DAILY_MAX_LOSS_PCT = 0.05
        p.day_start_equity = 2000.0
        g = p._gate_for("ETHUSDT")
        check("当日熔断拦截加仓", not g["can_add"])
        g2 = p._gate_for("BBBUSDT")
        check("当日熔断拦截开仓", not g2["can_enter"] and "当日熔断" in g2["reason"])
    finally:
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT, \
            config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = saved
    print()


def test_profit_target():
    print("测试 24: 止盈目标（当日盈利达标 → 全组合停止开仓，锁存至重启）")
    saved = (config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT,
             config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT)
    try:
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT = 10, 1.0
        config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = 10 ** 9, 1.0
        p = Portfolio(["ETHUSDT", "AAAUSDT"], shared_cash=True)
        for c in p.ctx.values():
            c.mark = c.last_price = 100.0
        p.ctx["ETHUSDT"].account.open_position("LONG", 1.0, 99.0, 100.0)
        p.day_start_equity = 1000.0
        p.profit_target = 5.0

        # 盈利未达标（+~1U < 5U）：正常放行（AAAUSDT 无持仓看开仓闸门，ETHUSDT 有持仓看加仓闸门）
        g = p._gate_for("AAAUSDT")
        check("盈利未达标正常放行", g["can_enter"])
        g2 = p._gate_for("ETHUSDT")
        check("盈利未达标加仓放行", g2["can_add"])
        check("未达标不计命中", not p.target_hit)

        # 行情上涨 → 当日盈利 ≥ 目标 → 全组合拦截（含加仓）
        p.ctx["ETHUSDT"].mark = p.ctx["ETHUSDT"].last_price = 106.0
        g = p._gate_for("AAAUSDT")
        check("盈利达标拦截开仓", not g["can_enter"] and "止盈" in g["reason"])
        g2 = p._gate_for("ETHUSDT")
        check("盈利达标拦截加仓", not g2["can_add"])
        check("达标锁存置位", p.target_hit)

        # 锁存：行情回落盈利转负也不恢复（重启前不再进场）
        p.ctx["ETHUSDT"].mark = p.ctx["ETHUSDT"].last_price = 90.0
        g = p._gate_for("AAAUSDT")
        check("锁存后回落仍拦截", not g["can_enter"] and "止盈" in g["reason"])

        # 不启用（target=0）：无论盈亏都放行
        p2 = Portfolio(["ETHUSDT"], shared_cash=True)
        p2.ctx["ETHUSDT"].mark = p2.ctx["ETHUSDT"].last_price = 100.0
        p2.day_start_equity = 1000.0
        p2.profit_target = 0.0
        g = p2._gate_for("ETHUSDT")
        check("目标为 0 不启用不拦截", g["can_enter"] and not p2.target_hit)
    finally:
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT, \
            config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = saved
    print()


def test_loss_limit():
    print("测试 25: 止损金额（当日亏损达额 → 全组合停止开仓，锁存至重启）")
    saved = (config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT,
             config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT)
    try:
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT = 10, 1.0
        config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = 10 ** 9, 1.0
        p = Portfolio(["ETHUSDT", "AAAUSDT"], shared_cash=True)
        for c in p.ctx.values():
            c.mark = c.last_price = 100.0
        p.ctx["ETHUSDT"].account.open_position("LONG", 1.0, 99.0, 100.0)
        p.day_start_equity = 1000.0
        p.loss_limit = 5.0

        # 亏损未达额（约 -0.05U < 5U）：正常放行（AAAUSDT 无持仓看开仓，ETHUSDT 有持仓看加仓）
        g = p._gate_for("AAAUSDT")
        check("亏损未达额正常放行", g["can_enter"])
        g2 = p._gate_for("ETHUSDT")
        check("亏损未达额加仓放行", g2["can_add"])
        check("未达额不计触发", not p.loss_hit)

        # 行情下跌 → 当日亏损 ≥ 上限 → 全组合拦截（含加仓）
        p.ctx["ETHUSDT"].mark = p.ctx["ETHUSDT"].last_price = 94.0
        g = p._gate_for("AAAUSDT")
        check("亏损达额拦截开仓", not g["can_enter"] and "止损" in g["reason"])
        g2 = p._gate_for("ETHUSDT")
        check("亏损达额拦截加仓", not g2["can_add"])
        check("触发锁存置位", p.loss_hit)

        # 锁存：行情回升亏损收窄也不恢复（重启前不再进场）
        p.ctx["ETHUSDT"].mark = p.ctx["ETHUSDT"].last_price = 100.0
        g = p._gate_for("AAAUSDT")
        check("锁存后回升仍拦截", not g["can_enter"] and "止损" in g["reason"])

        # 不启用（limit=0）：无论盈亏都放行
        p2 = Portfolio(["ETHUSDT"], shared_cash=True)
        p2.ctx["ETHUSDT"].mark = p2.ctx["ETHUSDT"].last_price = 94.0
        p2.day_start_equity = 1000.0
        p2.loss_limit = 0.0
        g = p2._gate_for("ETHUSDT")
        check("上限为 0 不启用不拦截", g["can_enter"] and not p2.loss_hit)
    finally:
        config.RISK_MAX_POSITIONS, config.RISK_MAX_MARGIN_PCT, \
            config.RISK_MAX_NET_NOTIONAL_USD, config.RISK_DAILY_MAX_LOSS_PCT = saved
    print()


def test_livermore_entry_quality():
    print("测试 19: 进场质量（ATR 自适应突破缓冲 + 资金费率过滤）")
    # 自适应缓冲：极大倍数把缓冲抬宽 → 越过固定缓冲但仍不足自适应缓冲时不进场
    s = make_livermore()
    s.breakout_atr_mult = 10.0
    s.seed_candles(candles_uptrend_breakout())
    piv_h, _ = s.pivots()
    buf = s._breakout_buffer()
    check("自适应缓冲宽于固定缓冲", buf > s.breakout_pct)
    app = FakeApp()
    move(app, piv_h * 1.02)
    s.on_eval(app)
    check("缓冲不足不进场", app.account.position is None)

    # 资金费过滤：费率过高不追多，恢复后放行
    s2 = make_livermore()
    s2.funding_filter = 0.0003
    s2.seed_candles(candles_uptrend_breakout())
    app2 = FakeApp()
    app2.funding_rate = 0.0005
    move(app2, 104.6)
    s2.on_eval(app2)
    check("资金费过高拦截追多", app2.account.position is None)
    app2.funding_rate = 0.0
    s2.on_eval(app2)
    check("费率正常恢复进场", app2.account.position is not None)

    # 资金费过低不追空
    s3 = make_livermore()
    s3.funding_filter = 0.0003
    s3.seed_candles(candles_downtrend_breakout())
    app3 = FakeApp()
    app3.funding_rate = -0.0005
    move(app3, 95.4)
    s3.on_eval(app3)
    check("资金费过低拦截追空", app3.account.position is None)
    print()


def test_livermore_stagnation():
    print("测试 20: 滞涨离场（试仓后行情不证明 → 及时认错离场）")
    s = make_livermore()
    s.stagnation_minutes = 45
    s.stagnation_min_pnl = 0.10
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    check("试探建多成功", app.account.position is not None)
    # 持仓 46 分钟仍未触发加仓、峰值浮盈仅约 +2% 保证金（< 10%）→ 滞涨离场
    app.account.position.opened_at -= 46 * 60
    move(app, 104.8)
    s.on_eval(app)
    check("超时未达标滞涨离场", app.account.position is None)

    # 对照：时限内浮盈达标（>10% 保证金）→ 不离场
    s2 = make_livermore()
    s2.stagnation_minutes = 45
    s2.stagnation_min_pnl = 0.10
    s2.seed_candles(candles_uptrend_breakout())
    app2 = FakeApp()
    move(app2, 104.6)
    s2.on_eval(app2)
    app2.account.position.opened_at -= 46 * 60
    move(app2, 108.5)            # +3.75% 价格 ≈ +37% 保证金 ≥ 10%
    s2.on_eval(app2)
    check("浮盈达标不离场", app2.account.position is not None)
    print()


def test_livermore_breakeven_buffer():
    print("测试 21: 保本止损含费用缓冲（回落到入场价+费用缓冲即离场，保本不倒贴手续费）")
    s = make_livermore()
    s.breakeven_buffer = 0.0012
    s.trail_activate = 99.0      # 关闭移动止盈（隔离：聚焦保本位本身）
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)
    entry = app.account.position.entry_price
    move(app, 108.0)
    s.on_eval(app)
    check("浮盈达标保本已武装", s.breakeven_armed)
    move(app, entry * 1.004)    # mark = ask-0.25 仍高于含费保本位
    s.on_eval(app)
    check("仍高于含费保本位不离场", app.account.position is not None)
    move(app, entry * 1.0025)   # mark 落入 (入场价, 入场价+0.12%) 缓冲带
    s.on_eval(app)
    check("跌破含费保本位离场（虽仍在入场价上方）", app.account.position is None)
    print()


def test_livermore_risk_sizing():
    print("测试 22: 风险预算仓位（高波动币自动收敛试探仓名义，止损亏损不超预算）")
    s = make_livermore()
    s.stop_atr_mult = 1.5
    s.risk_max_loss = 25.0
    s.require_trend = False      # 高波动横盘 K 线（均线 FLAT），纯突破进场
    s.seed_candles(candles_wild())
    atr = s._atr()               # 每根振幅 10% → ATR ≈ 10
    check("高波动 ATR 成立", atr > 5.0)
    app = FakeApp()
    move(app, 106.5)             # 突破关键点上沿 105×1.001
    s.on_eval(app)
    pos = app.account.position
    check("突破进场成功", pos is not None)
    if pos is None:
        return
    notional = pos.entry_price * pos.qty
    check("名义被风险预算收敛", notional < 40.0 * 10)
    est_loss = atr * 1.5 * pos.qty
    check("止损亏损≈预算内", est_loss <= 25.0 + 1.0)
    print()


def test_loss_cooldown_and_streak():
    print("测试 17: 亏损冷却 + 连败降频（亏损离场长冷却，连败翻倍，盈利重置，上限封顶）")
    s = make_trend()
    s.cooldown = 60                  # 普通冷却 60 秒（盈利/中性离场）
    s.loss_cooldown = 1200           # 亏损冷却 20 分钟
    s.loss_streak_ban = 2
    s.loss_streak_mult = 2.0
    s.loss_streak_max_cooldown = 60 * 240
    before = time.time()
    s._finish_cycle(pnl=10.0)
    check("盈利离场用普通冷却且清零连败", approx(s.cooldown_until - before, 60, tol=2) and s.loss_streak == 0)
    s._finish_cycle(pnl=-5.0)
    check("首次亏损离场用亏损冷却", approx(s.cooldown_until - time.time(), 1200, tol=2) and s.loss_streak == 1)
    s._finish_cycle(pnl=-5.0)
    check("连败2次冷却翻倍", approx(s.cooldown_until - time.time(), 2400, tol=2) and s.loss_streak == 2)
    s._finish_cycle(pnl=-5.0)
    check("连败3次冷却×4", approx(s.cooldown_until - time.time(), 4800, tol=2))
    s._finish_cycle(pnl=3.0)
    check("盈利离场重置连败", s.loss_streak == 0)
    for _ in range(5):
        s._finish_cycle(pnl=-1.0)    # 连败5次：1200×2^4=19200 秒 → 封顶 4 小时
    check("连败冷却封顶4小时", approx(s.cooldown_until - time.time(), 60 * 240, tol=2) and s.loss_streak == 5)
    s._finish_cycle()                # 无盈亏信息（外部平仓且无记录）→ 按普通冷却、清零连败
    check("无盈亏信息按普通冷却", approx(s.cooldown_until - time.time(), 60, tol=2) and s.loss_streak == 0)
    print()


def test_close_pnl_recorded():
    print("测试 23: 平仓盈亏入账（回归：_do_close 平仓后取盈亏恒为 ±0 的 bug）")
    # 亏损路径：关键点止损离场 → 战绩记 1 败、亏损额为真实负数（含手续费）
    s = make_livermore()
    s.seed_candles(candles_uptrend_breakout())
    app = FakeApp()
    move(app, 104.6)
    s.on_eval(app)                      # 突破建多
    s.on_eval(app)                      # 初始化关键点止损
    move(app, 99.0)
    s.on_eval(app)                      # 跌破关键点止损离场
    st = s.stats
    check("亏损轮计入战绩", st["cycles"] == 1 and st["losses"] == 1 and st["wins"] == 0)
    check("亏损额为真实负数(含费)", st["gross_loss"] < -5.0)
    # 盈利路径：移动止盈（回吐峰值利润 35%）离场 → 战绩记 1 胜、盈利额为真实正数
    s2 = make_livermore()
    s2.seed_candles(candles_uptrend_breakout())
    app2 = FakeApp()
    move(app2, 104.6)
    s2.on_eval(app2)                    # 突破建多 @104.6
    move(app2, 107.2)                   # mark≈106.95，浮盈 2.2% 启动移动止盈，峰值利润 ≈ 9.0U
    s2.on_eval(app2)
    move(app2, 106.2)                   # mark≈105.95，利润回落 ≈5.2U < 峰值×0.65 → 移动止盈
    s2.on_eval(app2)
    st2 = s2.stats
    check("盈利轮计入战绩", st2["cycles"] == 1 and st2["wins"] == 1 and st2["losses"] == 0)
    check("盈利额为真实正数(含费)", st2["gross_win"] > 0.5)
    print()


if __name__ == "__main__":
    test_long_round_trip()
    test_short()
    test_liquidation()
    test_liquidation_added_margin()
    test_funding()
    test_precision()
    test_reverse()
    test_insufficient()
    test_trend_entry()
    test_trend_warmup()
    test_pyramid_add()
    test_trend_exits()
    test_livermore_breakout_entry()
    test_livermore_pyramid_add()
    test_livermore_add_limit()
    test_livermore_pivot_stop()
    test_livermore_reentry_guard()
    test_loss_cooldown_and_streak()
    test_risk_governor()
    test_livermore_entry_quality()
    test_livermore_stagnation()
    test_livermore_breakeven_buffer()
    test_livermore_risk_sizing()
    test_close_pnl_recorded()
    test_profit_target()
    test_loss_limit()
    print("全部自测通过 (ALL PASS)")
