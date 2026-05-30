"""Metrics + tabular reports for a backtest run."""
from collections import defaultdict
from dataclasses import asdict

import pandas as pd

from trader.backtest.engine import ClosedTrade


def trades_to_df(trades: list[ClosedTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=[
            "symbol","direction","setup","qty","entry_price","exit_price",
            "opened_at","closed_at","exit_reason","gross_pnl","total_costs","net_pnl","reason"
        ])
    return pd.DataFrame([asdict(t) for t in trades])


def summary_by_setup(trades: list[ClosedTrade]) -> pd.DataFrame:
    """Per-setup aggregate metrics."""
    if not trades:
        return pd.DataFrame()
    df = trades_to_df(trades)
    rows = []
    for setup, g in df.groupby("setup"):
        wins   = g[g["net_pnl"] > 0]
        losses = g[g["net_pnl"] <= 0]
        avg_win  = wins["net_pnl"].mean()  if len(wins)   else 0.0
        avg_loss = losses["net_pnl"].mean() if len(losses) else 0.0
        rows.append({
            "setup":         setup,
            "trades":        len(g),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate_%":    round(100 * len(wins) / len(g), 1),
            "avg_win_inr":   round(avg_win,  2),
            "avg_loss_inr":  round(avg_loss, 2),
            "net_pnl_inr":   round(g["net_pnl"].sum(),     2),
            "gross_pnl_inr": round(g["gross_pnl"].sum(),   2),
            "total_costs":   round(g["total_costs"].sum(), 2),
            "max_win_inr":   round(g["net_pnl"].max(), 2),
            "max_loss_inr":  round(g["net_pnl"].min(), 2),
        })
    return pd.DataFrame(rows).sort_values("net_pnl_inr", ascending=False)


def daily_pnl(trades: list[ClosedTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = trades_to_df(trades)
    df["day"] = pd.to_datetime(df["closed_at"]).dt.date
    daily = df.groupby("day")["net_pnl"].agg(["sum", "count"]).reset_index()
    daily.columns = ["day", "net_pnl_inr", "trades"]
    daily["cumulative_inr"] = daily["net_pnl_inr"].cumsum()
    return daily


def max_drawdown(daily_df: pd.DataFrame) -> tuple[float, float]:
    """Return (max_drawdown_inr, max_drawdown_pct_of_peak)."""
    if daily_df.empty:
        return 0.0, 0.0
    cum = daily_df["cumulative_inr"]
    peak = cum.cummax()
    dd = cum - peak
    max_dd = float(dd.min())
    peak_at_max_dd = float(peak.loc[dd.idxmin()])
    dd_pct = (max_dd / peak_at_max_dd * 100.0) if peak_at_max_dd else 0.0
    return max_dd, dd_pct


def overall_summary(trades: list[ClosedTrade], capital: float) -> dict:
    if not trades:
        return {"trades": 0, "note": "no trades produced"}
    df = trades_to_df(trades)
    daily = daily_pnl(trades)
    total_net  = float(df["net_pnl"].sum())
    total_gross = float(df["gross_pnl"].sum())
    total_costs = float(df["total_costs"].sum())
    wins  = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] <= 0]
    win_rate = 100 * len(wins) / len(df)
    avg_win  = float(wins["net_pnl"].mean())  if len(wins)   else 0.0
    avg_loss = float(losses["net_pnl"].mean()) if len(losses) else 0.0
    # Expectancy per trade
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss
    max_dd_inr, max_dd_pct = max_drawdown(daily)
    return {
        "trades":          len(df),
        "win_rate_%":      round(win_rate, 1),
        "avg_win_inr":     round(avg_win,  2),
        "avg_loss_inr":    round(avg_loss, 2),
        "expectancy_inr":  round(expectancy, 2),
        "total_net_inr":   round(total_net, 2),
        "total_gross_inr": round(total_gross, 2),
        "total_costs":     round(total_costs, 2),
        "return_on_capital_%":   round(total_net / capital * 100, 2),
        "max_drawdown_inr":      round(max_dd_inr, 2),
        "max_drawdown_pct":      round(max_dd_pct, 2),
        "trading_days":          int(daily["day"].nunique()),
        "trades_per_day_avg":    round(len(df) / max(daily["day"].nunique(), 1), 2),
    }
