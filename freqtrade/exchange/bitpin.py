"""Bitpin exchange subclass"""

import logging

from freqtrade.exchange import Exchange
from freqtrade.exchange.ccxt_bitpin import register
from freqtrade.exchange.exchange_types import FtHas


logger = logging.getLogger(__name__)

# Bitpin is not part of ccxt - make our implementation available as ccxt.bitpin
register()


class Bitpin(Exchange):
    """
    Bitpin exchange class. Contains adjustments needed for Freqtrade to work
    with this exchange.

    Please note that this exchange is not included in the list of exchanges
    officially supported by the Freqtrade development team. So some features
    may still not work as expected.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1000,
        # Candles above 30m are built from 30m candles - max. 10000 of those per request
        "ohlcv_candle_limit_per_timeframe": {"6h": 800, "1d": 200},
        # Orderbook endpoint always returns 20 levels
        "l2_limit_range": [20],
        # Tickers only contain the last price - no bid/ask or volume
        "tickers_have_bid_ask": False,
        "tickers_have_quoteVolume": False,
        # Only the latest public trades are available
        "trades_has_history": False,
        "stoploss_on_exchange": False,
    }
