"""Candle-replay backtest engine.

Iterates symbols' 5-min candles in chronological order, calls each setup's
.detect() at every bar, opens trades subject to risk caps, manages SL/TP/
square-off, and emits a closed-trades list.

Costs (slippage + Zerodha equity intraday charges) are applied at fill time
so reported PnL is net of friction.
"""
from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import Optional
import logging
import math

import pandas as pd

from trader.backtest.config import (
    CAPITAL_INR, MAX_RISK_PER_TRADE_INR, MAX_CONCURRENT_POSITIONS,
    DAILY_LOSS_CIRCUIT_INR, DAILY_PROFIT_LOCK_INR, PROFIT_LOCK_TIME,
    SQUARE_OFF, EARLIEST_ENTRY, LATEST_ENTRY,
    BROKERAGE_PER_ORDER, STT_PCT, EXCHANGE_TXN_PCT, GST_PCT,
    STAMP_DUTY_PCT, SEBI_CHARGES_PCT, SLIPPAGE_PCT,
)
from trader.backtest.setups.base import Setup, Signal

logger = logging.getLogger(__name__)


# ── Trade bookkeeping ────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    symbol: str
    direction: str               # 'long' | 'short'
    entry_price: float           # actual fill (post-slippage)
    qty: int
    stop_loss: float
    take_profit: float
    opened_at: datetime
    setup: str
    reason: str
    entry_costs: float           # brokerage + stamp + exchange on entry leg


@dataclass
class ClosedTrade:
    symbol: str
    direction: str
    setup: str
    qty: int
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str             # 'SL' | 'TP' | 'square_off' | 'time_stop'
    gross_pnl: float
    total_costs: float
    net_pnl: float
    reason: str                  # entry signal reason


# ── Cost model ───────────────────────────────────────────────────────────

def _leg_costs(side: str, price: float, qty: int) -> float:
    """One leg of an intraday MIS trade. side = 'buy' | 'sell'."""
    turnover = price * qty
    brokerage = min(BROKERAGE_PER_ORDER, 0.0003 * turnover)  # Zerodha: lower of ₹20 or 0.03%
    exchange  = turnover * EXCHANGE_TXN_PCT
    sebi      = turnover * SEBI_CHARGES_PCT
    stt       = turnover * STT_PCT if side == "sell" else 0.0
    stamp     = turnover * STAMP_DUTY_PCT if side == "buy" else 0.0
    gst       = (brokerage + exchange) * GST_PCT
    return brokerage + exchange + sebi + stt + stamp + gst


def _apply_slippage(price: float, side: str) -> float:
    """Pessimistic fill: buys fill slightly above quoted, sells slightly below."""
    return price * (1 + SLIPPAGE_PCT) if side == "buy" else price * (1 - SLIPPAGE_PCT)


# ── Engine ──────────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(
        self,
        symbol_data: dict[str, pd.DataFrame],
        setup_classes: list[type[Setup]],
        market_index_df: Optional[pd.DataFrame] = None,
        capital: float = CAPITAL_INR,
        allowed_directions: tuple[str, ...] = ("long", "short"),
    ):
        """
        symbol_data: {tradingsymbol: DataFrame with [open,high,low,close,volume]
                      indexed by datetime in IST}
        setup_classes: list of Setup subclasses to evaluate. Engine creates
                       one instance per (symbol, setup).
        market_index_df: optional NIFTY 50 OHLCV (same index format) — used by
                         setups that need market context (e.g., SECTOR_LEADER).
        allowed_directions: ("long",) for long-only, ("short",) for short-only,
                            ("long","short") for both.
        """
        self.symbol_data = symbol_data
        self.market_df   = market_index_df
        self.capital     = capital
        self.allowed_directions = set(allowed_directions)

        # Build setup instances: setups[symbol][setup_name] = Setup
        self.setups: dict[str, dict[str, Setup]] = {
            sym: {cls.name: cls(sym) for cls in setup_classes}
            for sym in symbol_data
        }

        self.open_positions: dict[str, OpenPosition] = {}   # keyed by symbol
        self.closed_trades: list[ClosedTrade] = []
        # Per-day realized PnL — used for circuit breaker / profit lock
        self.daily_pnl: dict[date, float] = {}

    # ── Position management ────────────────────────────────────────────

    def _per_trade_risk(self, today: date, now_time: time) -> float:
        """Profit-lock: if up >= ₹4k by 1 PM, halve risk for the rest of day."""
        pnl_today = self.daily_pnl.get(today, 0.0)
        if pnl_today >= DAILY_PROFIT_LOCK_INR and now_time >= PROFIT_LOCK_TIME:
            return MAX_RISK_PER_TRADE_INR * 0.5
        return MAX_RISK_PER_TRADE_INR

    def _can_open(self, today: date, now_time: time) -> tuple[bool, str]:
        if len(self.open_positions) >= MAX_CONCURRENT_POSITIONS:
            return False, "max concurrent positions"
        if not (EARLIEST_ENTRY <= now_time < LATEST_ENTRY):
            return False, "outside entry window"
        if self.daily_pnl.get(today, 0.0) <= -DAILY_LOSS_CIRCUIT_INR:
            return False, "daily loss circuit"
        return True, ""

    def _size_position(self, sig: Signal, today: date, now_time: time) -> int:
        """Compute share qty from per-trade risk and SL distance."""
        sl_distance = abs(sig.entry_price - sig.stop_loss)
        if sl_distance <= 0:
            return 0
        risk_inr = self._per_trade_risk(today, now_time)
        raw_qty = risk_inr / sl_distance
        return max(0, math.floor(raw_qty))

    def _open_trade(self, sig: Signal):
        if sig.direction not in self.allowed_directions:
            return  # direction filter (e.g. long-only mode)
        ts = sig.triggered_at
        today = ts.date()
        now_time = ts.time()
        ok, reason = self._can_open(today, now_time)
        if not ok:
            return
        if sig.symbol in self.open_positions:
            return  # one position per symbol at a time
        qty = self._size_position(sig, today, now_time)
        if qty <= 0:
            return

        # Apply slippage to entry
        side = "buy" if sig.direction == "long" else "sell"
        fill_price = _apply_slippage(sig.entry_price, side)
        entry_costs = _leg_costs(side, fill_price, qty)

        self.open_positions[sig.symbol] = OpenPosition(
            symbol=sig.symbol, direction=sig.direction,
            entry_price=fill_price, qty=qty,
            stop_loss=sig.stop_loss, take_profit=sig.take_profit,
            opened_at=ts, setup=sig.setup, reason=sig.reason,
            entry_costs=entry_costs,
        )

    def _close_trade(self, pos: OpenPosition, exit_price: float,
                     exit_time: datetime, exit_reason: str):
        side = "sell" if pos.direction == "long" else "buy"
        fill = _apply_slippage(exit_price, side)
        exit_costs = _leg_costs(side, fill, pos.qty)

        # Gross PnL
        if pos.direction == "long":
            gross = (fill - pos.entry_price) * pos.qty
        else:
            gross = (pos.entry_price - fill) * pos.qty

        total_costs = pos.entry_costs + exit_costs
        net = gross - total_costs

        self.closed_trades.append(ClosedTrade(
            symbol=pos.symbol, direction=pos.direction, setup=pos.setup,
            qty=pos.qty, entry_price=pos.entry_price, exit_price=fill,
            opened_at=pos.opened_at, closed_at=exit_time,
            exit_reason=exit_reason, gross_pnl=gross,
            total_costs=total_costs, net_pnl=net,
            reason=pos.reason,
        ))
        d = exit_time.date()
        self.daily_pnl[d] = self.daily_pnl.get(d, 0.0) + net
        del self.open_positions[pos.symbol]

    def _check_exits_on_bar(self, symbol: str, bar: pd.Series):
        """SL/TP hit checks using bar high/low. Conservative: if both could
        fire in the same bar, assume SL fires first (worst case)."""
        if symbol not in self.open_positions:
            return
        pos = self.open_positions[symbol]
        ts = bar.name
        high = float(bar["high"]); low = float(bar["low"])

        if pos.direction == "long":
            sl_hit = low <= pos.stop_loss
            tp_hit = high >= pos.take_profit
            if sl_hit:
                self._close_trade(pos, pos.stop_loss, ts, "SL")
            elif tp_hit:
                self._close_trade(pos, pos.take_profit, ts, "TP")
        else:  # short
            sl_hit = high >= pos.stop_loss
            tp_hit = low <= pos.take_profit
            if sl_hit:
                self._close_trade(pos, pos.stop_loss, ts, "SL")
            elif tp_hit:
                self._close_trade(pos, pos.take_profit, ts, "TP")

    def _square_off_all(self, at_time: datetime, exit_prices: dict[str, float]):
        """Force-close all open positions at given prices (used at 3:15 PM)."""
        for sym in list(self.open_positions.keys()):
            pos = self.open_positions[sym]
            price = exit_prices.get(sym, pos.entry_price)
            self._close_trade(pos, price, at_time, "square_off")

    # ── Replay loop ────────────────────────────────────────────────────

    def _market_return_at(self, ts: datetime) -> Optional[float]:
        if self.market_df is None:
            return None
        day_idx = self.market_df[(self.market_df.index.date == ts.date())
                                 & (self.market_df.index <= ts)]
        if day_idx.empty:
            return None
        open_price = float(day_idx.iloc[0]["open"])
        cur_price  = float(day_idx.iloc[-1]["close"])
        return (cur_price - open_price) / open_price

    def run(self):
        # Build a master timeline of unique timestamps across all symbols
        all_ts = pd.Index([], dtype="datetime64[ns]")
        for df in self.symbol_data.values():
            all_ts = all_ts.union(df.index)
        all_ts = all_ts.sort_values()
        logger.info(f"Replaying {len(all_ts)} unique timestamps across {len(self.symbol_data)} symbols")

        last_date = None
        for ts in all_ts:
            today = ts.date()

            # New day boundary: nothing special, daily PnL key handles it
            if today != last_date:
                last_date = today

            # Build market context once per timestamp
            mkt_ret = self._market_return_at(ts)
            ctx = {"market_return_pct": mkt_ret} if mkt_ret is not None else None

            # Square-off pass: if this bar's time crossed 3:15, exit all
            if ts.time() >= SQUARE_OFF and self.open_positions:
                exit_prices = {}
                for sym in list(self.open_positions.keys()):
                    sym_df = self.symbol_data[sym]
                    if ts in sym_df.index:
                        exit_prices[sym] = float(sym_df.loc[ts, "close"])
                    else:
                        exit_prices[sym] = self.open_positions[sym].entry_price
                self._square_off_all(ts, exit_prices)
                continue

            # Exit checks first (so a SL hit in this bar prevents a new entry
            # being opened in same bar — realistic)
            for sym in list(self.open_positions.keys()):
                sym_df = self.symbol_data[sym]
                if ts in sym_df.index:
                    self._check_exits_on_bar(sym, sym_df.loc[ts])

            # Entry signals
            for sym, df in self.symbol_data.items():
                if ts not in df.index:
                    continue
                idx = df.index.get_loc(ts)
                # Pass each setup the history slice up to and including idx
                for setup in self.setups[sym].values():
                    try:
                        if setup.name == "SECTOR_LEADER":
                            sig = setup.detect(df, idx, market_context=ctx)
                        else:
                            sig = setup.detect(df, idx)
                    except Exception as e:
                        logger.warning(f"{sym}/{setup.name} detect error at {ts}: {type(e).__name__}: {e}")
                        sig = None
                    if sig is not None:
                        self._open_trade(sig)

        logger.info(
            f"Replay done: {len(self.closed_trades)} closed trades, "
            f"{len(self.open_positions)} still open at end"
        )
        return self.closed_trades
