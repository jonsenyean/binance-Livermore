# -*- coding: utf-8 -*-
"""组合账户：管理 NUM_PAIRS 个交易对，每个交易对独立的资金池（PER_PAIR_BALANCE）与自动策略。

每个交易对的数据流（bookTicker / markPrice）路由到对应对的上下文：
  - PairContext 持有该对的独立 PaperAccount、自动策略与实时行情
  - 自动策略按 config.STRATEGY 选择：利弗莫尔关键点滚仓（默认）/ 原趋势加仓
  - 止盈止损按该对仓位保证金(本金)计算
  - 组合风控闸门（Governor）：持仓数/保证金占用/净方向敞口/当日熔断，防止相关性一锅端
  - 汇总统计：每对盈亏 + 组合总盈亏 + 策略战绩（胜率/盈亏比）
"""
from __future__ import annotations

import time

import config
from paper_account import CashPool, PaperAccount
from strategy import LivermoreRollStrategy, TrendPyramidStrategy

# config.STRATEGY → 策略类（保留两个可切换，便于 A/B 对比）
STRATEGY_CLASSES = {
    "trend_pyramid": TrendPyramidStrategy,
    "livermore_roll": LivermoreRollStrategy,
}


def _make_strategy():
    cls = STRATEGY_CLASSES.get(config.STRATEGY, LivermoreRollStrategy)
    return cls()


def _print_big_banner(lines: list[str]):
    """控制台特大字横幅连打三遍。只用 GBK 安全字符（█ ★ ！），防中文控制台乱码/报错；打印失败绝不影响交易。"""
    bar = "█" * 62
    text = "\n".join(["", bar, "█", *lines, "█", bar, ""])
    for _ in range(3):
        try:
            print(text, flush=True)
        except Exception:
            pass


def _print_target_banner(profit: float, target: float):
    """止盈目标达成横幅。"""
    _print_big_banner([
        "█        ★★★★★     目  标  达  成  ！     ★★★★★",
        "█",
        f"█          今日盈利金额： +{profit:.2f} USDT（目标 {target:.2f}）",
        "█",
        "█          全部交易对已停止开仓，重启前不再进场",
    ])


def _print_loss_banner(loss: float, limit: float):
    """止损金额触发横幅。"""
    _print_big_banner([
        "█        ▓▓▓▓▓▓     止  损  触  发  ！     ▓▓▓▓▓▓",
        "█",
        f"█          今日亏损金额： -{loss:.2f} USDT（上限 {limit:.2f}）",
        "█",
        "█          全部交易对已停止开仓，重启前不再进场",
    ])


class PairContext:
    """单个交易对的运行上下文（同时充当策略 on_eval 的 app 参数）。"""
    def __init__(self, symbol: str, cashpool: CashPool | None = None):
        self.symbol = symbol
        self.account = PaperAccount(symbol, cashpool=cashpool)
        self.strategy = _make_strategy()
        self.bid: float | None = None
        self.ask: float | None = None
        self.mark: float | None = None
        self.last_price: float | None = None
        self.funding_rate: float | None = None
        self.next_funding_time: int | None = None
        # 组合风控闸门（Portfolio.on_eval 每轮刷新；默认全放行，单独使用策略时不受限）
        self.can_enter = True
        self.can_enter_long = True
        self.can_enter_short = True
        self.can_add = True
        self.gate_reason = ""

    def switch_symbol(self, symbol: str):
        self.symbol = symbol
        self.account.set_symbol(symbol)
        self.strategy.reset()
        self.bid = self.ask = self.mark = self.last_price = None
        self.funding_rate = None
        self.next_funding_time = None


class Portfolio:
    def __init__(self, pairs: list[str], window: dict | None = None,
                 precision: dict | None = None, shared_cash: bool = True):
        self.pairs = list(pairs)
        self.window = window or {"hours": [0, 23], "avg_amplitude": 0.0}
        self.shared_cash = shared_cash
        if shared_cash:
            # 所有交易对共享一个现金池（初始总额 = TOTAL_BALANCE）
            self.cash_pool = CashPool(config.TOTAL_BALANCE)
            self.ctx = {sym: PairContext(sym, cashpool=self.cash_pool) for sym in self.pairs}
        else:
            # 每对独立现金池（初始总额 = PER_PAIR_BALANCE）
            self.cash_pool = None
            self.ctx = {sym: PairContext(sym, cashpool=CashPool(config.PER_PAIR_BALANCE))
                        for sym in self.pairs}
        if precision:
            for sym, d in precision.items():
                if sym in self.ctx:
                    self.ctx[sym].account.set_precision(**d)
        # ---- 组合风控（Governor）状态 ----
        self.day_key = ""            # 当前 UTC 日期（日切检测）
        self.day_start_equity = 0.0  # UTC 日初权益（当日回撤熔断基准）
        self.halted_day = ""         # 触发当日熔断的日期（去重打印/状态显示）
        # ---- 止盈目标：当日盈利达标 → 全组合停止开仓（重启前不再恢复） ----
        self.profit_target = 0.0     # 目标金额 USDT；0=不启用（main.py 启动时询问后写入）
        self.target_hit = False      # 达标锁存：一旦触发，跨日切也不恢复，仅重启清零
        # ---- 止损金额：当日亏损达额 → 全组合停止开仓（重启前不再恢复，与止盈目标对称） ----
        self.loss_limit = 0.0        # 上限金额 USDT；0=不启用
        self.loss_hit = False        # 触发锁存：一旦触发，跨日切也不恢复，仅重启清零

    # ------------------------------------------------------------------
    # 行情路由
    # ------------------------------------------------------------------
    def update_market(self, kind: str, data: dict):
        if kind == "book":
            sym = data.get("s")
            if sym not in self.ctx:
                return
            b, a = float(data["b"]), float(data["a"])
            if b > 0:
                self.ctx[sym].bid = b
            if a > 0:
                self.ctx[sym].ask = a
            self.ctx[sym].last_price = (self.ctx[sym].bid + self.ctx[sym].ask) / 2
        elif kind == "mark":
            sym = data.get("s")
            if sym not in self.ctx:
                return
            ctx = self.ctx[sym]
            ctx.mark = float(data["p"])
            new_rate = float(data["r"])
            new_time = int(data["T"])
            # 资金费结算：T 跳变说明刚发生一次结算
            if (ctx.next_funding_time is not None and new_time != ctx.next_funding_time
                    and ctx.funding_rate is not None):
                amt = ctx.account.apply_funding(ctx.funding_rate, ctx.mark)
                print(f"[资金费 {sym}] 按费率 {ctx.funding_rate * 100:.4f}% 结算 {amt:+.2f} USDT")
            ctx.funding_rate = new_rate
            ctx.next_funding_time = new_time
            # 强平监控
            if ctx.account.check_liquidation(ctx.mark):
                print(f"[强平 {sym}] 标记价触及强平价，仓位已被强平！")

    # ------------------------------------------------------------------
    # 组合风控闸门（Governor）
    # ------------------------------------------------------------------
    def _totals(self) -> dict:
        """组合实时总览：权益/已用保证金/未实现/净方向名义/持仓对数。

        净方向名义 = Σ(多仓位名义) - Σ(空仓位名义)：加密币相关性极高，
        该值才是「实际押了同一个方向多少钱」的度量。
        """
        used_margin = 0.0
        unrealized = 0.0
        net_notional = 0.0
        n_pos = 0
        for ctx in self.ctx.values():
            p = ctx.account.position
            mark = ctx.mark or ctx.last_price or 0.0
            if p is not None and mark > 0:
                n_pos += 1
                used_margin += p.margin
                unrealized += p.unrealized_pnl(mark)
                net_notional += p.qty * mark * (1.0 if p.side == "LONG" else -1.0)
        if self.shared_cash:
            equity = self.cash_pool.total + used_margin + unrealized
        else:
            cash = sum(c.account.cash for c in self.ctx.values())
            equity = cash + used_margin + unrealized
        return {"equity": equity, "used_margin": used_margin, "unrealized": unrealized,
                "net_notional": net_notional, "n_pos": n_pos}

    def _roll_day(self):
        """UTC 日切：重置当日回撤基准与熔断标记（新的一天重新允许开仓）。"""
        key = time.strftime("%Y-%m-%d", time.gmtime())
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = self._totals()["equity"]

    def _gate_for(self, sym: str) -> dict:
        """计算某交易对当前的风控闸门。返回 dict(can_enter/long_ok/short_ok/can_add/reason)。

        只限制自动策略（can_enter/can_add），手动命令不受影响；现有仓位的离场管理永不拦截。
        """
        t = self._totals()
        eq = t["equity"]
        ctx = self.ctx[sym]
        lev = max(ctx.account.leverage, 1)
        base_notional = max(getattr(ctx.strategy, "base_margin_usd", 0.0), 0.0) * lev
        has_pos = ctx.account.position is not None

        # 当日熔断：权益自 UTC 日初回撤超限 → 全组合暂停新开仓/加仓至次日
        daily_halt = False
        if self.day_start_equity > 0:
            dd = (self.day_start_equity - eq) / self.day_start_equity
            if dd >= config.RISK_DAILY_MAX_LOSS_PCT:
                daily_halt = True
                if self.halted_day != self.day_key:
                    self.halted_day = self.day_key
                    print(f"[风控] ⚠ 当日权益回撤 {dd * 100:.1f}% ≥ 熔断线 "
                          f"{config.RISK_DAILY_MAX_LOSS_PCT * 100:.0f}%，暂停新开仓至 UTC 次日"
                          f"（现有仓位照常管理）")

        # 止盈目标：当日盈利（权益 − 日初权益）达标 → 全组合停止开仓/加仓。
        # 与当日熔断不同：达标即锁存，跨 UTC 日切也不恢复，仅程序重启清零；现有仓位离场管理照常。
        if self.profit_target > 0 and not self.target_hit and self.day_start_equity > 0:
            profit = eq - self.day_start_equity
            if profit >= self.profit_target:
                self.target_hit = True
                _print_target_banner(profit, self.profit_target)
        if self.target_hit:
            return {"can_enter": False, "long_ok": False, "short_ok": False,
                    "can_add": False, "reason": "止盈目标达成"}

        # 止损金额：当日亏损（日初权益 − 权益）达额 → 全组合停止开仓/加仓（锁存至重启，与止盈目标对称）。
        if self.loss_limit > 0 and not self.loss_hit and self.day_start_equity > 0:
            loss = self.day_start_equity - eq
            if loss >= self.loss_limit:
                self.loss_hit = True
                _print_loss_banner(loss, self.loss_limit)
        if self.loss_hit:
            return {"can_enter": False, "long_ok": False, "short_ok": False,
                    "can_add": False, "reason": "止损上限触发"}

        if has_pos:
            can_add = (not daily_halt
                       and t["used_margin"] < eq * config.RISK_MAX_MARGIN_PCT
                       and abs(t["net_notional"]) < config.RISK_MAX_NET_NOTIONAL_USD)
            return {"can_enter": False, "long_ok": False, "short_ok": False,
                    "can_add": can_add, "reason": ""}

        count_ok = t["n_pos"] < config.RISK_MAX_POSITIONS
        margin_ok = t["used_margin"] + base_notional / lev <= eq * config.RISK_MAX_MARGIN_PCT
        cap = config.RISK_MAX_NET_NOTIONAL_USD
        long_ok = (not daily_halt) and count_ok and margin_ok and (t["net_notional"] + base_notional <= cap)
        short_ok = (not daily_halt) and count_ok and margin_ok and (t["net_notional"] - base_notional >= -cap)
        if daily_halt:
            reason = "当日熔断"
        elif not count_ok:
            reason = "持仓数满"
        elif not margin_ok:
            reason = "保证金占用高"
        elif not long_ok and not short_ok:
            reason = "双向敞口满"
        elif not long_ok:
            reason = "多头敞口满"
        elif not short_ok:
            reason = "空头敞口满"
        else:
            reason = ""
        return {"can_enter": long_ok or short_ok, "long_ok": long_ok, "short_ok": short_ok,
                "can_add": False, "reason": reason}

    # ------------------------------------------------------------------
    # 策略评估（每个交易对独立，评估前先刷新该对的风控闸门）
    # ------------------------------------------------------------------
    def on_eval(self):
        self._roll_day()
        for sym, ctx in self.ctx.items():
            g = self._gate_for(sym)   # 逐对实时计算：前面的对刚开仓，后面的对立即受限
            ctx.can_enter = g["can_enter"]
            ctx.can_enter_long = g["long_ok"]
            ctx.can_enter_short = g["short_ok"]
            ctx.can_add = g["can_add"]
            ctx.gate_reason = g["reason"]
            try:
                ctx.strategy.on_eval(ctx)
            except Exception as e:
                print(f"[策略 {sym}] 错误: {e}")

    def is_in_window(self, utc_hour: int) -> bool:
        start, end = self.window["hours"]
        if start <= end:
            return start <= utc_hour <= end
        return utc_hour >= start or utc_hour <= end

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        rows = []
        total_margin = 0.0
        total_unrealized = 0.0
        total_realized = 0.0
        total_position_value = 0.0
        total_fees = 0.0
        total_funding = 0.0
        for sym in self.pairs:
            ctx = self.ctx[sym]
            mark = ctx.mark if ctx.mark is not None else 0.0
            s = ctx.account.summary(mark)
            pos = ctx.account.position
            strat = ctx.strategy
            lev = pos.leverage if pos else ctx.account.leverage
            if pos:
                total_position_value += pos.qty * mark
            sl_price = None
            if pos and pos.entry_price > 0:
                giveback = getattr(strat, "trail_giveback", None)   # 利弗莫尔回吐峰值利润；趋势策略无该属性
                be_armed = getattr(strat, "breakeven_armed", False)
                be_buf = getattr(strat, "breakeven_buffer", 0.0)
                peak_pnl = getattr(strat, "peak_pnl", 0.0)
                if pos.side == "LONG":
                    sl_price = pos.entry_price * (1 - strat.sl / pos.leverage)
                    if be_armed:   # 含费保本位（高于保证金止损价时生效）
                        sl_price = max(sl_price, pos.entry_price * (1 + be_buf))
                    if strat.trail_active:
                        if giveback is not None and peak_pnl > 0:
                            # 回吐峰值利润：移动止盈价 = 入场价 + 峰值利润×(1-回吐比例)/数量
                            sl_price = max(sl_price, pos.entry_price + peak_pnl * (1 - giveback) / pos.qty)
                        elif peak_pnl > 0:
                            sl_price = max(sl_price, strat.trail_peak * (1 - strat.trail_stop))
                else:
                    sl_price = pos.entry_price * (1 + strat.sl / pos.leverage)
                    if be_armed:
                        sl_price = min(sl_price, pos.entry_price * (1 - be_buf))
                    if strat.trail_active:
                        if giveback is not None and peak_pnl > 0:
                            sl_price = min(sl_price, pos.entry_price - peak_pnl * (1 - giveback) / pos.qty)
                        elif peak_pnl > 0:
                            sl_price = min(sl_price, strat.trail_peak * (1 + strat.trail_stop))
            pivot = None
            piv_fn = getattr(strat, "pivots", None)
            if callable(piv_fn):
                piv_h, piv_l = piv_fn()
                if piv_h:
                    pivot = f"H {piv_h:.2f} / L {piv_l:.2f}"
            if self.shared_cash:
                total_cash = self.cash_pool.total
                cash_display = total_cash if sym == self.pairs[0] else "↗"
                row_equity = total_cash + s["margin"] + s["unrealized"]
                row_net = s["realized"] + s["unrealized"] - s["fees"] - s["funding"]
                roll_display = str(strat.add_count)
            else:
                cash_display = s["cash"]
                row_equity = s["equity"]
                row_net = s["net"]
                roll_display = f"{strat.add_count}/{strat.max_adds}"
            rows.append({
                "symbol": sym,
                "mark": ctx.mark,
                "bid": ctx.bid,
                "ask": ctx.ask,
                "cash": cash_display,
                "margin": s["margin"],
                "unrealized": s["unrealized"],
                "equity": row_equity,
                "net": row_net,
                "realized": s["realized"],
                "position": (f"{'多' if pos.side == 'LONG' else '空'} "
                             f"{pos.qty:.4f}@{pos.entry_price:.2f}" if pos else "-"),
                "leverage": f"{lev}x",
                "roll": roll_display,
                "trend": strat.trend(),
                "pivot": pivot,
                "sl_price": sl_price,
                "liq_price": pos.liquidation_price() if pos else None,
            })
            total_margin += s["margin"]
            total_unrealized += s["unrealized"]
            total_realized += s["realized"]
            total_fees += s["fees"]
            total_funding += s["funding"]
        if self.shared_cash:
            total_cash = self.cash_pool.total
            total_equity = total_cash + total_margin + total_unrealized
            initial = config.TOTAL_BALANCE
        else:
            total_cash = sum(r["cash"] for r in rows if isinstance(r["cash"], (int, float)))
            total_equity = sum(r["equity"] for r in rows)
            initial = len(self.pairs) * config.PER_PAIR_BALANCE
        return {
            "rows": rows,
            "total_equity": total_equity,
            "total_net": total_equity - initial,
            "total_realized": total_realized,
            "total_position_value": total_position_value,
            "total_fees": total_fees,
            "total_funding": total_funding,
            "initial": initial,
            "window": self.window,
            "shared_cash": self.shared_cash,
        }

    def stats_summary(self) -> dict:
        """聚合全部交易对的策略战绩 + 当前风控敞口（供 stats 命令与状态栏）。"""
        agg = {"cycles": 0, "wins": 0, "losses": 0, "gross_win": 0.0,
               "gross_loss": 0.0, "max_loss_streak": 0}
        for ctx in self.ctx.values():
            st = getattr(ctx.strategy, "stats", None)
            if not st:
                continue
            agg["cycles"] += st["cycles"]
            agg["wins"] += st["wins"]
            agg["losses"] += st["losses"]
            agg["gross_win"] += st["gross_win"]
            agg["gross_loss"] += st["gross_loss"]
            agg["max_loss_streak"] = max(agg["max_loss_streak"], st["max_loss_streak"])
        agg.update(self._totals())
        agg["margin_pct"] = (agg["used_margin"] / agg["equity"]) if agg["equity"] > 0 else 0.0
        agg["halted"] = bool(self.halted_day) and self.halted_day == self.day_key
        # 止盈目标进度（profit_today = 权益 − 日初权益；日初未建立时按 0 计）
        agg["profit_today"] = (agg["equity"] - self.day_start_equity) if self.day_start_equity > 0 else 0.0
        agg["profit_target"] = self.profit_target
        agg["target_hit"] = self.target_hit
        # 止损金额进度（loss_today = 日初权益 − 权益，正数=在亏）
        agg["loss_today"] = (self.day_start_equity - agg["equity"]) if self.day_start_equity > 0 else 0.0
        agg["loss_limit"] = self.loss_limit
        agg["loss_hit"] = self.loss_hit
        return agg