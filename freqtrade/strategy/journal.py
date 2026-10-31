"""
Trade journal: save which entry checks were met when a trade's entry filled.
Strategies call save_entry_snapshot() from their order_filled() callback.
The snapshot is display data only - it never changes a trade.
"""

import logging
import math
from typing import TYPE_CHECKING, Any

import pandas as pd

from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_prev_date


if TYPE_CHECKING:
    from freqtrade.persistence import Order, Trade
    from freqtrade.strategy.interface import IStrategy


logger = logging.getLogger(__name__)

ENTRY_SNAPSHOT_KEY = "entry_snapshot"
_CHECK_PREFIX = "entry_check__"
_GAP_PREFIX = "entry_gap__"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_entry_snapshot(row: pd.Series, enter_tag: str | None, value_cols: list[str]) -> dict:
    """
    Snapshot of the candle that gave the entry signal.
    checks come from the entry_check__<name> / entry_gap__<name> columns, values from value_cols
    (columns the row doesn't have are left out). NaN becomes None.
    """
    checks = {}
    for col in row.index:
        if not col.startswith(_CHECK_PREFIX):
            continue
        name = col[len(_CHECK_PREFIX) :]
        checks[name] = {
            "met": bool(_number(row[col])),
            "gap": _number(row.get(f"{_GAP_PREFIX}{name}")),
        }

    return {
        "v": 1,
        "candle": pd.Timestamp(row["date"]).isoformat(),
        "enter_tag": enter_tag,
        "checks": checks,
        "values": {col: _number(row[col]) for col in value_cols if col in row.index},
    }


def save_entry_snapshot(
    strategy: "IStrategy", pair: str, trade: "Trade", order: "Order", value_cols: list[str]
) -> None:
    """
    Store the entry snapshot on the trade when its first entry order fills.
    Only in live and dry-run. Any failure is logged and ignored.
    """
    try:
        if strategy.dp.runmode not in (RunMode.LIVE, RunMode.DRY_RUN):
            return
        if order.ft_order_side != trade.entry_side or trade.nr_of_successful_entries != 1:
            return
        if trade.get_custom_data(ENTRY_SNAPSHOT_KEY) is not None:
            return

        df, _ = strategy.dp.get_analyzed_dataframe(pair, strategy.timeframe)
        # The signal came from the last candle that had closed when the order was placed.
        # The fill can be seen candles later, so the newest candle is not necessarily it.
        order_candle = timeframe_to_prev_date(strategy.timeframe, trade.open_date_utc)
        candles = df.loc[df["date"] < order_candle] if len(df) else df
        if candles.empty:
            logger.warning(f"No candle for the entry snapshot of {pair} (trade {trade.id}).")
            return

        snapshot = build_entry_snapshot(candles.iloc[-1], trade.enter_tag, value_cols)
        trade.set_custom_data(ENTRY_SNAPSHOT_KEY, snapshot)
    except Exception:
        logger.warning(f"Could not save the entry snapshot of {pair}.", exc_info=True)
