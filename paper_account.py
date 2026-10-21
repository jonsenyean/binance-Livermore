# -*- coding: utf-8 -*-
"""虚拟账户模块：模拟撮合成交、持仓管理、保证金/盈亏/资金费/强平计算。

盈亏与保证金全部按币安官方规则：
  - 未实现盈亏 = (标记价 - 入场价) × 数量（多）/ (入场价 - 标记价) × 数量（空）
  - 已实现盈亏 = (出场价 - 入场价) × 数量 - 开平仓手续费
  - 手续费 = 名义价值 × 费率（吃单 0.05% / 挂单 0.02%），官方标准费率
  - 资金费 = 标记价 × 数量 × 资金费率（正费率多头付费给空头，负费率反向）
  - 初始保证金 = 名义价值 / 有效杠杆（杠杆按档位最大杠杆封顶）
  - 强平价（孤立保证金，币安官方）：多 (入场名义-已锁保证金)/(数量×(1-维持保证金率))
                                     空 (入场名义+已锁保证金)/(数量×(1+维持保证金率))
    已锁保证金含加仓锁定的部分（加仓越多/降杠杆加仓，强平价越远离现价）；
    维持保证金率按持仓名义价值查官方分档表
  - 下单精度：数量向下取整到 step_size，价格舍入到 tick_size
  - 最小下单名义价值 = 币安官方要求（如 BTCUSDT = 50 USDT）
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import config


class CashPool:
    """所有交易对共享的现金池，取代每对独立 PER_PAIR_BALANCE 的限制。"""
    def __init__(self, total: float):
        self.total = total      # 当前可用余额（共享）


def mmr_tier(notional: float):
    """按名义价值(USDT)查币安官方维持保证金率档位，返回 (维持保证金率, 该档最大杠杆)。"""
    for upper, mmr, max_lev in config.MMR_TIERS:
        if notional <= upper:
            return mmr, max_lev
    mmr, max_lev = config.MMR_TIERS[-1][1], config.MMR_TIERS[-1][2]
    return mmr, max_lev


@dataclass
class Position:
    side: str           # "LONG" / "SHORT"
    qty: float          # 持仓数量（基础币，已按 step_size 取整）
    entry_price: float  # 开仓均价（已按 tick_size 舍入）
    leverage: int       # 有效杠杆（已按档位封顶）
    margin: float       # 已锁定的保证金
    opened_at: float    # 开仓时间戳

    @property
    def notional(self) -> float:
        return self.entry_price * self.qty

    def unrealized_pnl(self, mark_price: float) -> float:
        """未实现盈亏（币安官方：基于标记价，不含手续费）。"""
        if self.side == "LONG":
            return (mark_price - self.entry_price) * self.qty
        return (self.entry_price - mark_price) * self.qty

    def maintenance_margin(self, price: float) -> float:
        """按当前价格的名义价值对应的档位计算维持保证金。"""
        mmr = mmr_tier(price * self.qty)[0]
        return price * self.qty * mmr

    def liquidation_price(self) -> float:
        """孤立保证金模式下的强平价（币安官方公式，含累计加仓保证金）。

        多： (入场名义 - 已锁保证金) / (数量 × (1 - 维持保证金率))
        空： (入场名义 + 已锁保证金) / (数量 × (1 + 维持保证金率))
        已锁保证金包含加仓锁定的部分：加仓越多、或降杠杆后加仓，强平价越远离现价（与币安一致）。
        维持保证金率按名义价值查官方分档表（沿用入场名义所在档位）。
        """
        mmr = mmr_tier(self.notional)[0]
        qty = self.qty
        if self.side == "LONG":
            return (self.notional - self.margin) / (qty * (1 - mmr))
        return (self.notional + self.margin) / (qty * (1 + mmr))


@dataclass
class TradeRecord:
    ts: float
    action: str     # OPEN / CLOSE / FUNDING / LIQUIDATION
    side: str
    qty: float
    price: float
    realized: float  # 已实现盈亏（平仓时有效，含手续费）
    fee: float
    reason: str = ""


class PaperAccount:
    def __init__(self, symbol: str = config.SYMBOL,
                 cashpool: CashPool | None = None):
        self.cashpool = cashpool or CashPool(config.PER_PAIR_BALANCE)
        self.initial_balance = self.cashpool.total   # 初始资金快照（仅用于 net_profit）
        self.leverage = config.LEVERAGE             # 当前目标杠杆（新仓位生效，按档位封顶）
        self.position: Optional[Position] = None
        self.realized_pnl = 0.0                     # 累计已实现盈亏（含手续费）
        self.fees_paid = 0.0                        # 累计手续费
        self.funding_paid = 0.0                     # 累计资金费（>0 表示净支出）
        self.trades: list[TradeRecord] = []
        self.symbol = symbol.upper()
        self.step_size, self.tick_size, self.min_notional = self._precision(self.symbol)

    @property
    def cash(self) -> float:
        """读取共享现金池余额。"""
        return self.cashpool.total

    @cash.setter
    def cash(self, value: float):
        self.cashpool.total = value


    @staticmethod
    def _precision(symbol: str):
        prec = config.PRECISION.get(symbol, config.DEFAULT_PRECISION)
        return prec["step_size"], prec["tick_size"], prec["min_notional"]

    def set_symbol(self, symbol: str):
        """切换交易对：更新精度/最小下单（不重置资金与持仓）。"""
        self.symbol = symbol.upper()
        self.step_size, self.tick_size, self.min_notional = self._precision(self.symbol)

    def set_precision(self, step_size=None, tick_size=None, min_notional=None):
        """用币安官方 exchangeInfo 精度覆盖当前精度（组合模式下按实际交易对设置）。"""
        if step_size:
            self.step_size = step_size
        if tick_size:
            self.tick_size = tick_size
        if min_notional:
            self.min_notional = min_notional

    # ------------------------------------------------------------------
    # 撮合与费用（币安官方精度规则）
    # ------------------------------------------------------------------
    def _round_qty(self, qty: float) -> float:
        """数量向下取整到 step_size（币安官方对数量的处理）。"""
        return math.floor(qty / self.step_size + 1e-9) * self.step_size

    def _round_price(self, price: float) -> float:
        """价格舍入到 tick_size。"""
        return round(price / self.tick_size) * self.tick_size

    def _fill_price(self, direction: str, bid: float, ask: float) -> float:
        """本地撮合价（币安官方）：买入单吃卖一，卖出单吃买一，加滑点后舍入到 tick。

        direction: "buy" / "sell"（订单方向，与持仓方向无关）。
        """
        if direction == "buy":
            return self._round_price(ask * (1 + config.SLIPPAGE_PCT))
        return self._round_price(bid * (1 - config.SLIPPAGE_PCT))

    def _fee(self, notional: float, rate: Optional[float] = None) -> float:
        """手续费 = 名义价值 × 费率，舍入到 8 位小数（官方）。"""
        return round(notional * (rate if rate is not None else config.TAKER_FEE), 8)

    # ------------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------------
    def open_position(self, side: str, qty: float, bid: float, ask: float,
                      limit_price: float | None = None, is_maker: bool = False):
        """开仓/加仓。自动处理反向仓位（对冲/减仓）。返回 (成功, 消息)。

        limit_price 给定则按限价成交（模拟限价单，DCA 用）；否则按盘口吃单。
        is_maker=True 且限价成交时，手续费按 maker 费率。
        """
        qty = self._round_qty(qty)
        if qty <= 0:
            return False, f"数量 {qty} 不足一个步长 {self.step_size}"

        # 成交价：限价单按限价（舍入到 tick），吃单按盘口（舍入到 tick）
        if limit_price is not None:
            price = self._round_price(limit_price)
            fee_rate = config.MAKER_FEE if is_maker else config.TAKER_FEE
        else:
            # 开仓：LONG=买入→吃卖一，SHORT=卖出→吃买一（币安官方）
            price = self._fill_price("buy" if side == "LONG" else "sell", bid, ask)
            fee_rate = config.TAKER_FEE
        pos = self.position

        # 1) 反向仓位处理：先抵扣
        if pos and pos.side != side:
            if qty < pos.qty:
                self._realize_close(qty, price, reason="REVERSE")
                return True, f"反向减仓 {qty:.4f}（原仓位剩余 {pos.qty - qty:.4f}）"
            old_qty = pos.qty  # _realize_close 会清零 pos.qty，必须先缓存
            self._realize_close(old_qty, price, reason="REVERSE")
            qty -= old_qty
            if qty <= 1e-12:
                return True, "反向平掉全部仓位"
            qty = self._round_qty(qty)
            pos = self.position  # 旧仓位已清空，重新读取（此时应为 None）

        if qty <= 0:
            return False, "数量无效"

        # 2) 最小下单名义价值（币安官方）
        notional = price * qty
        if notional < self.min_notional:
            return False, f"下单名义价值 {notional:.2f} USDT 低于最小下单 {self.min_notional} USDT"

        # 3) 有效杠杆：按档位最大杠杆封顶
        mmr, max_lev = mmr_tier(notional)
        lev = min(self.leverage, max_lev)

        # 4) 检查余额（保证金 + 手续费）
        fee = self._fee(notional, fee_rate)
        margin = notional / lev
        total_cost = margin + fee
        if total_cost > self.cash:
            return False, (f"余额不足：需要 {total_cost:.2f} USDT（保证金 {margin:.2f}"
                           f" + 手续费 {fee:.2f}），可用 {self.cash:.2f}")

        # 5) 开仓/加仓
        if pos is None:
            self.position = Position(side=side, qty=qty, entry_price=price,
                                     leverage=lev, margin=margin, opened_at=time.time())
        else:
            old = pos
            new_qty = old.qty + qty
            old.entry_price = (old.entry_price * old.qty + price * qty) / new_qty
            old.entry_price = self._round_price(old.entry_price)
            old.qty = self._round_qty(new_qty)
            old.margin += margin

        self.cash -= total_cost
        # 开仓手续费计入累计费用，并从已实现盈亏中扣除，保证 权益=本金+已实现+浮盈 恒成立
        self.fees_paid += fee
        self.realized_pnl -= fee
        self.trades.append(TradeRecord(time.time(), "OPEN", side, qty, price, 0.0, fee, "MANUAL"))
        return True, f"开仓 {side} {qty:.4f} @ {price:.2f}（保证金 {margin:.2f} USDT）"

    # ------------------------------------------------------------------
    # 平仓
    # ------------------------------------------------------------------
    def close_position(self, qty: Optional[float], bid: float, ask: float,
                       reason: str = "MANUAL"):
        """平仓。qty 为空表示全平。返回 (成功, 消息)。"""
        pos = self.position
        if pos is None:
            return False, "当前无持仓"
        if qty is None:
            qty = pos.qty
        qty = self._round_qty(min(qty, pos.qty))
        if qty <= 0:
            return False, "数量无效"
        # 平仓：LONG=卖出→吃买一，SHORT=买入→吃卖一（币安官方）
        price = self._fill_price("sell" if pos.side == "LONG" else "buy", bid, ask)
        realized = self._realize_close(qty, price, reason)
        return True, f"平仓 {pos.side} {qty:.4f} @ {price:.2f}（已实现 {realized:+.4f} USDT）"

    def _realize_close(self, qty: float, price: float, reason: str = "MANUAL") -> float:
        """内部平仓结算：已实现盈亏 = 价差×数量 - 手续费。返回净已实现。"""
        pos = self.position
        side = pos.side
        if side == "LONG":
            realized = (price - pos.entry_price) * qty
        else:
            realized = (pos.entry_price - price) * qty
        fee = self._fee(price * qty)
        margin_return = (pos.margin / pos.qty) * qty

        self.cash += margin_return + realized - fee
        pos.qty -= qty
        pos.margin -= margin_return
        if pos.qty <= 1e-12:
            self.position = None

        net = round(realized - fee, 8)
        self.realized_pnl += net
        self.fees_paid += fee
        self.trades.append(TradeRecord(time.time(), "CLOSE", side, qty, price, net, fee, reason))
        return net

    # ------------------------------------------------------------------
    # 资金费 / 强平（币安官方）
    # ------------------------------------------------------------------
    def apply_funding(self, rate: float, mark: float) -> float:
        """资金费 = 标记价 × 数量 × 资金费率。正费率多头付费给空头；负费率反向。"""
        pos = self.position
        if pos is None:
            return 0.0
        notional = mark * pos.qty
        amt = round(notional * rate, 8)
        if pos.side == "LONG":
            self.cash -= amt
            self.funding_paid += amt
        else:
            self.cash += amt
            self.funding_paid -= amt
        self.trades.append(TradeRecord(time.time(), "FUNDING", pos.side, pos.qty, mark, -amt, 0.0, "FUNDING"))
        return amt

    def check_liquidation(self, mark: float) -> bool:
        """标记价触及强平价则强制平仓。返回是否发生强平。"""
        pos = self.position
        if pos is None:
            return False
        liq = pos.liquidation_price()
        hit = (pos.side == "LONG" and mark <= liq) or (pos.side == "SHORT" and mark >= liq)
        if hit:
            self._realize_close(pos.qty, liq, reason="LIQUIDATION")
            return True
        return False

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def equity(self, mark: float) -> float:
        """账户权益 = 可用余额 + 已锁保证金 + 未实现盈亏。"""
        if self.position:
            return self.cash + self.position.margin + self.position.unrealized_pnl(mark)
        return self.cash

    def net_profit(self, mark: float) -> float:
        return self.equity(mark) - self.initial_balance

    def summary(self, mark: float) -> dict:
        pos = self.position
        margin = pos.margin if pos else 0.0
        unrealized = pos.unrealized_pnl(mark) if pos else 0.0
        return {
            "cash": self.cash,
            "margin": margin,
            "unrealized": unrealized,
            "equity": self.equity(mark),
            "net": self.net_profit(mark),
            "realized": self.realized_pnl,
            "fees": self.fees_paid,
            "funding": self.funding_paid,
        }
