from datetime import UTC, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from freqtrade.enums import RunMode
from freqtrade.strategy.journal import build_entry_snapshot, save_entry_snapshot
from tests.conftest import log_has_re


CANDLE = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _row(**extra) -> pd.Series:
    return pd.Series(
        {
            "date": pd.Timestamp(CANDLE),
            "entry_check__breakout": 1,
            "entry_gap__breakout": 0.0,
            "entry_check__btc_trend": 0,
            "entry_gap__btc_trend": np.nan,
            "close": 1.5,
            "rsi": np.nan,
            **extra,
        }
    )


def test_build_entry_snapshot():
    snapshot = build_entry_snapshot(_row(), "breakout", ["close", "rsi", "missing"])
    assert snapshot == {
        "v": 1,
        "candle": "2026-10-30T12:00:00+00:00",
        "enter_tag": "breakout",
        "checks": {
            "breakout": {"met": True, "gap": 0.0},
            "btc_trend": {"met": False, "gap": None},
        },
        "values": {"close": 1.5, "rsi": None},
    }


def _strategy(runmode: RunMode, candle_dates: list[datetime], timeframe="1h") -> MagicMock:
    strategy = MagicMock()
    strategy.timeframe = timeframe
    strategy.dp.runmode = runmode
    df = pd.DataFrame([_row(date=pd.Timestamp(d)) for d in candle_dates])
    strategy.dp.get_analyzed_dataframe.return_value = (df, candle_dates[-1])
    return strategy


def _trade(open_date: datetime) -> MagicMock:
    trade = MagicMock()
    trade.entry_side = "buy"
    trade.nr_of_successful_entries = 1
    trade.open_date_utc = open_date
    trade.enter_tag = "breakout"
    trade.get_custom_data.return_value = None
    return trade


def _order() -> MagicMock:
    order = MagicMock()
    order.ft_order_side = "buy"
    return order


def _hour(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 10, 30, h, m, s, tzinfo=UTC)


def test_save_entry_snapshot_saves_signal_candle():
    # Order placed at 12:30 inside the 12:00 candle: the signal came from the closed 11:00 candle
    strategy = _strategy(RunMode.DRY_RUN, [_hour(11), _hour(12), _hour(13)])
    trade = _trade(_hour(12, 30))

    save_entry_snapshot(strategy, "BTC/USDT", trade, _order(), ["close"])

    trade.set_custom_data.assert_called_once()
    key, snapshot = trade.set_custom_data.call_args[0]
    assert key == "entry_snapshot"
    assert snapshot["candle"] == "2026-10-30T11:00:00+00:00"
    assert snapshot["values"] == {"close": 1.5}


def test_save_entry_snapshot_late_fill_keeps_signal_candle():
    # 15m limit order placed at 12:00:05, fill seen at 12:15 when the 12:00 candle has closed
    strategy = _strategy(RunMode.LIVE, [_hour(11, 30), _hour(11, 45), _hour(12)], timeframe="15m")
    trade = _trade(_hour(12, 0, 5))

    save_entry_snapshot(strategy, "BTC/USDT", trade, _order(), ["close"])

    _, snapshot = trade.set_custom_data.call_args[0]
    assert snapshot["candle"] == "2026-10-30T11:45:00+00:00"


def test_save_entry_snapshot_no_candle(caplog):
    strategy = _strategy(RunMode.LIVE, [datetime(2026, 10, 30, 13, 0, tzinfo=UTC)])
    trade = _trade(datetime(2026, 10, 30, 12, 30, tzinfo=UTC))

    save_entry_snapshot(strategy, "BTC/USDT", trade, _order(), ["close"])

    trade.set_custom_data.assert_not_called()
    assert log_has_re(r".*entry snapshot", caplog)


def test_save_entry_snapshot_skips_backtest():
    strategy = _strategy(RunMode.BACKTEST, [CANDLE])
    trade = _trade(datetime(2026, 10, 30, 12, 30, tzinfo=UTC))

    save_entry_snapshot(strategy, "BTC/USDT", trade, _order(), ["close"])

    trade.set_custom_data.assert_not_called()


def test_save_entry_snapshot_only_first_entry():
    strategy = _strategy(RunMode.LIVE, [CANDLE])
    trade = _trade(datetime(2026, 10, 30, 12, 30, tzinfo=UTC))
    trade.nr_of_successful_entries = 2
    exit_order = _order()
    exit_order.ft_order_side = "sell"

    save_entry_snapshot(strategy, "BTC/USDT", trade, _order(), ["close"])
    trade.nr_of_successful_entries = 1
    save_entry_snapshot(strategy, "BTC/USDT", trade, exit_order, ["close"])

    trade.set_custom_data.assert_not_called()
