"""Nobitex exchange subclass"""

import logging

from freqtrade.exchange import Exchange
from freqtrade.exchange.ccxt_nobitex import register
from freqtrade.exchange.exchange_types import FtHas


logger = logging.getLogger(__name__)

# Nobitex is not part of ccxt - make our implementation available as ccxt.nobitex
register()


class Nobitex(Exchange):
    """
    Nobitex exchange class. Contains adjustments needed for Freqtrade to work
    with this exchange.

    Please note that this exchange is not included in the list of exchanges
    officially supported by the Freqtrade development team. So some features
    may still not work as expected.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 500,
        # Candles above 1h are built from hourly candles - max. 500 of those per request
        "ohlcv_candle_limit_per_timeframe": {
            "2h": 250,
            "4h": 125,
            "6h": 83,
            "12h": 41,
            "1d": 20,
        },
        # The orderbook endpoint returns about 24 levels - request (at most) 20
        "l2_limit_range": [20],
        # Only the latest 20 public trades are available
        "trades_has_history": False,
        "stoploss_on_exchange": False,
    }
