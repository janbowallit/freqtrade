"""
CCXT-compatible implementation of the Nobitex exchange (https://nobitex.ir).

Nobitex is not (yet) part of ccxt. This module provides sync and async ccxt Exchange classes
and registers them as ``ccxt.nobitex`` / ``ccxt.async_support.nobitex`` so the regular
freqtrade exchange machinery can use them.
Registration is skipped if the installed ccxt version ships its own ``nobitex`` implementation.

API documentation: https://apidocs.nobitex.ir
"""

# Method signatures intentionally mirror ccxt (params={}, type, id ...)
# ruff: noqa: A001, A002, B006

import base64
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import ccxt
import ccxt.async_support as ccxt_async
from ccxt.base.decimal_to_precision import TICK_SIZE
from ccxt.base.errors import (
    AuthenticationError,
    BadRequest,
    BadSymbol,
    ExchangeError,
    InsufficientFunds,
    InvalidOrder,
    OrderNotFound,
    PermissionDenied,
    RateLimitExceeded,
)
from ccxt.base.precise import Precise
from ccxt.base.types import Entry
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


logger = logging.getLogger(__name__)

# Candles of 1h and above open at hh:30 UTC (Tehran time, UTC+3:30), while minute candles
# (which are UTC-aligned) are only kept for about a week. Freqtrade needs UTC-aligned candles
# with a long history, so 1h+ candles are built from the native hourly candles, each moved to
# the next full UTC hour (17:30-18:30 becomes the 18:00 candle).
# This never looks ahead: every candle is used 30 minutes after the exchange closed it.
OHLCV_HOURLY_RESOLUTION = "60"
OHLCV_HOURLY_SECONDS = 3600
OHLCV_HOURLY_SHIFT_SECONDS = 1800
# Maximum number of candles Nobitex returns per request.
OHLCV_MAX_CANDLES = 500

# Market ids are BASE + "IRT" / "USDT" (e.g. BTCIRT). IRT markets are priced in Rial and the
# Rial wallet is called "rls" - so they are exposed as BASE/RLS to keep prices and balances in
# the same unit.
QUOTE_SUFFIXES = {"IRT": "rls", "USDT": "usdt"}
QUOTE_ID_TO_SUFFIX = {quote_id: suffix for suffix, quote_id in QUOTE_SUFFIXES.items()}

ORDER_STATUS = {
    "new": "open",
    "active": "open",
    "inactive": "open",
    "done": "closed",
    "canceled": "canceled",
}

# Error codes from failed responses ({"status": "failed", "code": ...})
ERROR_CODES: dict[str, type[ExchangeError]] = {
    "TooManyRequests": RateLimitExceeded,
    "InvalidSymbol": BadSymbol,
    "InvalidMarketPair": BadSymbol,
    "OverValueOrder": InsufficientFunds,
    "InsufficientBalance": InsufficientFunds,
    "SmallOrder": InvalidOrder,
    "InvalidOrderPrice": InvalidOrder,
    "BadPrice": InvalidOrder,
    "PriceConditionFailed": InvalidOrder,
    "DuplicateOrder": InvalidOrder,
    "DuplicateClientOrderId": InvalidOrder,
    "MarketClosed": InvalidOrder,
    "TradingUnavailable": InvalidOrder,
    "FeatureUnavailable": InvalidOrder,
    "NullIdAndClientOrderId": BadRequest,
}


if TYPE_CHECKING:
    # Give the mixin access to the ccxt Exchange helpers for type checking
    from ccxt.base.exchange import Exchange as _MixinBase
else:
    _MixinBase = object


class NobitexBase(_MixinBase):
    """
    Request building, response parsing and authentication helpers shared between
    the sync and async implementations.
    """

    # Implicit API - costs are relative to rateLimit (200ms -> 300 requests per minute).
    public_get_v2_options = Entry("v2/options", "public", "GET", {"cost": 5})
    public_get_market_stats = Entry("market/stats", "public", "GET", {"cost": 15})  # 20/min
    public_get_v3_orderbook_symbol = Entry("v3/orderbook/{symbol}", "public", "GET", {"cost": 1})
    public_get_v2_trades_symbol = Entry("v2/trades/{symbol}", "public", "GET", {"cost": 5})
    public_get_market_udf_history = Entry("market/udf/history", "public", "GET", {"cost": 5})
    private_post_users_wallets_list = Entry(
        "users/wallets/list", "private", "POST", {"cost": 15}
    )  # 20/min
    # Order placement shares 300 / 10 minutes with margin orders
    private_post_market_orders_add = Entry("market/orders/add", "private", "POST", {"cost": 10})
    private_post_market_orders_status = Entry(
        "market/orders/status", "private", "POST", {"cost": 1}
    )
    private_post_market_orders_update_status = Entry(
        "market/orders/update-status", "private", "POST", {"cost": 3.34}
    )  # 90/min
    private_get_market_orders_list = Entry("market/orders/list", "private", "GET", {"cost": 10})
    private_get_market_trades_list = Entry("market/trades/list", "private", "GET", {"cost": 10})

    def describe(self) -> Any:
        return self.deep_extend(
            super().describe(),
            {
                "id": "nobitex",
                "name": "Nobitex",
                "countries": ["IR"],
                # Most market data endpoints allow 300 requests per minute.
                "rateLimit": 200,
                "version": "v2",
                "certified": False,
                "pro": False,
                "hostname": "apiv2.nobitex.ir",
                # Nobitex asks bots to identify themselves as TraderBot/<name>
                "userAgent": {"User-Agent": "TraderBot/freqtrade"},
                "has": {
                    "CORS": None,
                    "spot": True,
                    "margin": False,
                    "swap": False,
                    "future": False,
                    "option": False,
                    "cancelOrder": True,
                    "createOrder": True,
                    "createLimitOrder": True,
                    "createMarketOrder": True,
                    "fetchBalance": True,
                    "fetchClosedOrders": True,
                    "fetchCurrencies": False,
                    "fetchL2OrderBook": True,
                    "fetchMarkets": True,
                    "fetchMyTrades": True,
                    "fetchOHLCV": True,
                    "fetchOpenOrders": True,
                    "fetchOrder": True,
                    "fetchOrderBook": True,
                    "fetchOrders": True,
                    "fetchTicker": True,
                    "fetchTickers": True,
                    "fetchTrades": True,
                    "fetchTradingFees": False,
                },
                "timeframes": {
                    "1m": "1",
                    "5m": "5",
                    "15m": "15",
                    "30m": "30",
                    "1h": "60",
                    "2h": "60",
                    "4h": "60",
                    "6h": "60",
                    "12h": "60",
                    "1d": "60",
                },
                "urls": {
                    "api": {
                        "public": "https://{hostname}",
                        "private": "https://{hostname}",
                    },
                    "test": {
                        "public": "https://testnetapiv2.nobitex.ir",
                        "private": "https://testnetapiv2.nobitex.ir",
                    },
                    "www": "https://nobitex.ir",
                    "doc": "https://apidocs.nobitex.ir",
                },
                # apiKey: the public key, secret: the Ed25519 private key (URL-safe base64)
                "requiredCredentials": {
                    "apiKey": True,
                    "secret": True,
                },
                "precisionMode": TICK_SIZE,
                "fees": {
                    "trading": {
                        "tierBased": True,
                        "percentage": True,
                        # Defaults - per-market fees come from /v2/options (lowest tier)
                        "maker": self.parse_number("0.0025"),
                        "taker": self.parse_number("0.0025"),
                    },
                },
                "options": {
                    "walletType": "spot",
                    "fetchOHLCVLimit": OHLCV_MAX_CANDLES,
                },
            },
        )

    # --- Request signing / error handling ---------------------------------------------

    def _signing_key(self) -> Ed25519PrivateKey:
        """Load the Ed25519 private key from the (URL-safe base64) secret."""
        cached = getattr(self, "_nobitex_signing_key", None)
        if cached is not None and cached[0] == self.secret:
            return cached[1]
        secret = (self.secret or "").strip()
        try:
            raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        except ValueError as e:
            raise AuthenticationError(self.id + " secret is not a base64 Ed25519 key") from e
        if len(raw) == 64:
            # seed + public key
            raw = raw[:32]
        if len(raw) != 32:
            raise AuthenticationError(self.id + " secret is not a 32 byte Ed25519 private key")
        key = Ed25519PrivateKey.from_private_bytes(raw)
        self._nobitex_signing_key = (self.secret, key)
        return key

    def sign(self, path, api="public", method="GET", params={}, headers=None, body=None):
        request_path = "/" + self.implode_params(path, params)
        query = self.omit(params, self.extract_params(path))
        headers = {} if headers is None else headers
        if method == "GET":
            if query:
                request_path += "?" + self.urlencode(query)
        else:
            body = self.json(query)
            headers["Content-Type"] = "application/json"
        url = self.implode_hostname(self.urls["api"][api]) + request_path
        if api == "private":
            self.check_required_credentials()
            # Signature over timestamp + METHOD + path (with query) + raw body
            timestamp = str(self.seconds())
            payload = timestamp + method + request_path + (body or "")
            signature = self._signing_key().sign(self.encode(payload))
            headers["Nobitex-Key"] = self.apiKey
            headers["Nobitex-Timestamp"] = timestamp
            headers["Nobitex-Signature"] = base64.urlsafe_b64encode(signature).decode()
        return {"url": url, "method": method, "body": body, "headers": headers}

    def handle_errors(
        self,
        code,
        reason,
        url,
        method,
        headers,
        body,
        response,
        requestHeaders,
        requestBody,
    ):
        is_dict = isinstance(response, dict)
        failed = is_dict and self.safe_string(response, "status") == "failed"
        if code < 400 and not failed:
            if is_dict and self.safe_string(response, "s") == "error":
                # Candle endpoint (TradingView UDF format)
                raise BadRequest(self.id + " " + str(body))
            return
        message = self.id + " " + str(body)
        error_code = self.safe_string(response, "code") if is_dict else None
        if code == 429 or error_code == "TooManyRequests":
            raise RateLimitExceeded(message)
        if code == 401:
            raise AuthenticationError(message)
        if code == 403:
            # Key without the needed permission, or IP not on the key's whitelist
            raise PermissionDenied(message)
        is_order_endpoint = "/market/orders/" in url
        if code == 404 and is_order_endpoint:
            raise OrderNotFound(message)
        if error_code in ERROR_CODES:
            raise ERROR_CODES[error_code](message)
        if is_order_endpoint and failed and "updatedStatus" in response:
            # Cancel of an order that's no longer open (e.g. already done)
            raise InvalidOrder(message)
        if is_order_endpoint and error_code is not None and "NotFound" in error_code:
            raise OrderNotFound(message)
        if failed or 400 <= code < 500:
            raise ExchangeError(message)
        # 5xx are handled by ccxt's handle_http_status_code.
        return

    # --- Market id helpers --------------------------------------------------------------

    @staticmethod
    def _split_market_id(market_id: str) -> tuple[str, str] | None:
        """BTCIRT -> ("btc", "rls"), BTCUSDT -> ("btc", "usdt")."""
        for suffix, quote_id in QUOTE_SUFFIXES.items():
            if market_id.endswith(suffix) and len(market_id) > len(suffix):
                return market_id[: -len(suffix)].lower(), quote_id
        return None

    @staticmethod
    def _market_id(base_id: str, quote_id: str) -> str | None:
        suffix = QUOTE_ID_TO_SUFFIX.get(quote_id.lower())
        return base_id.upper() + suffix if suffix else None

    def _safe_market_pair(self, pair: str | None, market=None, delimiter="-") -> dict:
        """Find a market from "BTC-RLS" (orders, trades) or "btc-rls" (stats)."""
        market_id = None
        if pair and delimiter in pair:
            base_id, quote_id = pair.split(delimiter, 1)
            market_id = self._market_id(base_id, quote_id)
        return self.safe_market(market_id, market)

    # --- Request builders ---------------------------------------------------------------

    def _ohlcv_request(self, market, timeframe, since, limit, params) -> dict[str, Any]:
        duration = self.parse_timeframe(timeframe)
        hourly = duration >= OHLCV_HOURLY_SECONDS
        base_duration = OHLCV_HOURLY_SECONDS if hourly else duration
        if limit is None:
            limit = self.safe_integer(self.options, "fetchOHLCVLimit", OHLCV_MAX_CANDLES)
        limit = max(1, min(limit, OHLCV_MAX_CANDLES * base_duration // duration))
        count = limit * duration // base_duration
        # Native hourly candles open 30 minutes before the UTC hour they're moved to
        shift = OHLCV_HOURLY_SHIFT_SECONDS if hourly else 0
        until = self.safe_integer(params, "until")
        request: dict[str, Any] = {
            "symbol": market["id"],
            "resolution": OHLCV_HOURLY_RESOLUTION if hourly else self.timeframes[timeframe],
        }
        if since is not None:
            start = since // 1000 // duration * duration - shift
            end = start + count * base_duration - 1
            if until is not None:
                end = min(end, until // 1000 - shift)
            request["from"] = start
            request["to"] = end
        else:
            end = until // 1000 if until is not None else self.seconds()
            # When aggregating, fetch one more bucket - a partial first bucket gets dropped
            extra = duration // base_duration if duration > base_duration else 0
            request["to"] = end - shift
            request["countback"] = min(count + extra, OHLCV_MAX_CANDLES)
        return request

    def _parse_ohlcv_response(self, response, market, timeframe, since, limit) -> list:
        candles = self._parse_udf_candles(response)
        duration = self.parse_timeframe(timeframe)
        if duration < OHLCV_HOURLY_SECONDS:
            return self.filter_by_since_limit(candles, since, limit, 0)
        shift_ms = OHLCV_HOURLY_SHIFT_SECONDS * 1000
        hourly = [[c[0] + shift_ms, *c[1:]] for c in candles]
        aggregated = self._aggregate_ohlcv(hourly, duration * 1000, OHLCV_HOURLY_SECONDS * 1000)
        return self.filter_by_since_limit(aggregated, since, limit, 0)

    def _parse_udf_candles(self, response: dict) -> list:
        if self.safe_string(response, "s") != "ok":
            # "no_data": nothing in the requested range
            return []
        keys = ("t", "o", "h", "l", "c", "v")
        columns = [response.get(k) or [] for k in keys]
        result = []
        for ts, open_, high, low, close, volume in zip(*columns, strict=False):
            result.append(
                [
                    int(ts) * 1000,
                    self.safe_number([open_], 0),
                    self.safe_number([high], 0),
                    self.safe_number([low], 0),
                    self.safe_number([close], 0),
                    self.safe_number([volume], 0),
                ]
            )
        return self.sort_by(result, 0)

    @staticmethod
    def _aggregate_ohlcv(candles: list, duration_ms: int, base_ms: int) -> list:
        """
        Combine sorted base candles into UTC-aligned candles of duration_ms.
        A leading bucket that doesn't start at its first base candle is incomplete and dropped.
        """
        result: list = []
        for ts, open_, high, low, close, volume in candles:
            bucket = ts - ts % duration_ms
            if result and result[-1][0] == bucket:
                last = result[-1]
                last[2] = max(last[2], high)
                last[3] = min(last[3], low)
                last[4] = close
                last[5] = (last[5] or 0) + (volume or 0)
            elif not result and ts != bucket and duration_ms > base_ms:
                continue
            else:
                result.append([bucket, open_, high, low, close, volume])
        return result

    def _create_order_request(self, market, type, side, amount, price, params) -> dict[str, Any]:
        request: dict[str, Any] = {
            "type": side,
            "srcCurrency": market["baseId"],
            "dstCurrency": market["quoteId"],
            "amount": self.amount_to_precision(market["symbol"], amount),
        }
        execution = type
        trigger_price = self.safe_string_2(params, "triggerPrice", "stopPrice")
        if trigger_price is not None:
            execution = "stop_limit" if type == "limit" else "stop_market"
            request["stopPrice"] = self.price_to_precision(market["symbol"], trigger_price)
        request["execution"] = execution
        if type == "limit":
            if price is None:
                raise InvalidOrder(self.id + " limit orders require a price")
            request["price"] = self.price_to_precision(market["symbol"], price)
        elif price is not None:
            # Optional for market orders - limits the price range the order may fill at
            request["price"] = self.price_to_precision(market["symbol"], price)
        client_order_id = self.safe_string(params, "clientOrderId")
        if client_order_id is not None:
            request["clientOrderId"] = client_order_id
        params = self.omit(params, ["triggerPrice", "stopPrice", "clientOrderId"])
        return self.extend(request, params)

    def _orders_request(self, market, limit, status) -> dict[str, Any]:
        request: dict[str, Any] = {"status": status, "details": 2}
        if market is not None:
            request["srcCurrency"] = market["baseId"]
            request["dstCurrency"] = market["quoteId"]
        request["pageSize"] = min(limit, 100) if limit is not None else 100
        return request

    def _my_trades_request(self, market, limit) -> dict[str, Any]:
        request: dict[str, Any] = {}
        if market is not None:
            request["srcCurrency"] = market["baseId"]
            request["dstCurrency"] = market["quoteId"]
        request["pageSize"] = min(limit, 500) if limit is not None else 100
        return request

    # --- Parsers ------------------------------------------------------------------------

    @staticmethod
    def _parse_datetime(value: str | None) -> int | None:
        """Parse a timezone-aware ISO8601 string to a millisecond timestamp."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)

    def _fee_rate(self, fees: dict, key: str):
        """First (lowest volume) tier of a /v2/options fee list, in percent."""
        values = fees.get(key) or []
        if not values:
            return None
        return self.parse_number(Precise.string_div(str(values[0]), "100"))

    def parse_markets_from_options(self, response: dict) -> list:
        options = self.safe_dict(response, "nobitex", {})
        amount_precisions = self.safe_dict(options, "amountPrecisions", {})
        price_precisions = self.safe_dict(options, "pricePrecisions", {})
        min_orders = self.safe_dict(options, "minOrders", {})
        max_orders = self.safe_dict(options, "maxOrders", {})
        active = set(self.safe_list(options, "activeCurrencies", []))
        fees = self.safe_dict(options, "tradingFees", {})
        result = []
        for market_id in sorted(amount_precisions.keys()):
            ids = self._split_market_id(market_id)
            if ids is None:
                continue
            base_id, quote_id = ids
            result.append(
                self.parse_market(
                    {
                        "id": market_id,
                        "baseId": base_id,
                        "quoteId": quote_id,
                        "amountPrecision": amount_precisions.get(market_id),
                        "pricePrecision": price_precisions.get(market_id),
                        "minOrder": min_orders.get(quote_id),
                        "maxOrder": max_orders.get(quote_id),
                        "active": base_id in active,
                        "maker": self._fee_rate(
                            fees, "makerFeesTether" if quote_id == "usdt" else "makerFees"
                        ),
                        "taker": self._fee_rate(
                            fees, "takerFeesTether" if quote_id == "usdt" else "takerFees"
                        ),
                    }
                )
            )
        return result

    def parse_market(self, market: dict) -> dict:
        market_id = self.safe_string(market, "id")
        base_id = self.safe_string(market, "baseId")
        quote_id = self.safe_string(market, "quoteId")
        base = self.safe_currency_code(base_id.upper())
        quote = self.safe_currency_code(quote_id.upper())
        fees = self.fees["trading"]
        maker = market.get("maker")
        taker = market.get("taker")
        return self.safe_market_structure(
            {
                "id": market_id,
                "symbol": base + "/" + quote,
                "base": base,
                "quote": quote,
                "settle": None,
                "baseId": base_id,
                "quoteId": quote_id,
                "settleId": None,
                "type": "spot",
                "spot": True,
                "margin": False,
                "swap": False,
                "future": False,
                "option": False,
                "active": self.safe_bool(market, "active", True),
                "contract": False,
                "linear": None,
                "inverse": None,
                "taker": taker if taker is not None else fees["taker"],
                "maker": maker if maker is not None else fees["maker"],
                "contractSize": None,
                "expiry": None,
                "expiryDatetime": None,
                "strike": None,
                "optionType": None,
                "precision": {
                    "amount": self.safe_number(market, "amountPrecision"),
                    "price": self.safe_number(market, "pricePrecision"),
                },
                "limits": {
                    "leverage": {"min": None, "max": None},
                    "amount": {"min": None, "max": None},
                    "price": {"min": None, "max": None},
                    "cost": {
                        "min": self.safe_number(market, "minOrder"),
                        "max": self.safe_number(market, "maxOrder"),
                    },
                },
                "created": None,
                "info": market,
            }
        )

    def parse_ticker(self, ticker: dict, market=None) -> dict:
        last = self.safe_string(ticker, "latest")
        return self.safe_ticker(
            {
                "symbol": self.safe_string(market, "symbol"),
                "timestamp": None,
                "datetime": None,
                "high": self.safe_string(ticker, "dayHigh"),
                "low": self.safe_string(ticker, "dayLow"),
                "bid": self.safe_string(ticker, "bestBuy"),
                "bidVolume": None,
                "ask": self.safe_string(ticker, "bestSell"),
                "askVolume": None,
                "vwap": None,
                "open": self.safe_string(ticker, "dayOpen"),
                "close": last,
                "last": last,
                "previousClose": None,
                "change": None,
                "percentage": self.safe_string(ticker, "dayChange"),
                "average": None,
                "baseVolume": self.safe_string(ticker, "volumeSrc"),
                "quoteVolume": self.safe_string(ticker, "volumeDst"),
                "info": ticker,
            },
            market,
        )

    def parse_stats(self, response: dict, symbols=None) -> dict:
        """Tickers from /market/stats - keyed by "btc-rls"."""
        result = {}
        for key, stats in self.safe_dict(response, "stats", {}).items():
            if not isinstance(stats, dict):
                continue
            market = self._safe_market_pair(key)
            symbol = market.get("symbol")
            if symbol not in self.markets:
                continue
            result[symbol] = self.parse_ticker(stats, market)
        return self.filter_by_array_tickers(result, "symbol", symbols)

    def parse_trade(self, trade: dict, market=None) -> dict:
        # Public trades carry "time" (ms) and "volume", own trades "timestamp" (ISO) and "amount".
        market = self._safe_market_pair(self.safe_string(trade, "market"), market)
        timestamp = self._parse_datetime(self.safe_string(trade, "timestamp"))
        if timestamp is None:
            timestamp = self.safe_integer(trade, "time")
        side = self.safe_string(trade, "type")
        fee = None
        fee_cost = self.safe_string(trade, "fee")
        if fee_cost is not None:
            # Fees are taken from what's received: base on buys, quote on sells
            fee = {
                "cost": fee_cost,
                "currency": market["base"] if side == "buy" else market["quote"],
            }
        return self.safe_trade(
            {
                "id": self.safe_string(trade, "id"),
                "order": self.safe_string(trade, "orderId"),
                "timestamp": timestamp,
                "datetime": self.iso8601(timestamp),
                "symbol": market["symbol"],
                "type": None,
                "side": side,
                "takerOrMaker": None,
                "price": self.safe_string(trade, "price"),
                "amount": self.safe_string_2(trade, "amount", "volume"),
                "cost": self.safe_string(trade, "total"),
                "fee": fee,
                "info": trade,
            },
            market,
        )

    def parse_order_status(self, status: str | None) -> str | None:
        if status is None:
            return None
        return ORDER_STATUS.get(status.lower(), status.lower())

    def parse_order(self, order: dict, market=None) -> dict:
        market = self._safe_market_pair(self.safe_string(order, "market"), market)
        side = self.safe_string(order, "type")
        execution = (self.safe_string(order, "execution") or "").lower()
        type = "market" if execution in ("market", "stopmarket") else "limit"
        price = self.safe_string(order, "price")
        if price == "market" or Precise.string_eq(price, "0"):
            price = None
        average = self.safe_string(order, "averagePrice")
        if Precise.string_eq(average, "0"):
            average = None
        fee = None
        fee_cost = self.safe_number(order, "fee")
        if fee_cost is not None:
            fee = {
                "cost": fee_cost,
                "currency": market["base"] if side == "buy" else market["quote"],
            }
        trigger_price = self.safe_string(order, "param1")
        timestamp = self._parse_datetime(self.safe_string(order, "created_at"))
        return self.safe_order(
            {
                "id": self.safe_string(order, "id"),
                "clientOrderId": self.safe_string(order, "clientOrderId"),
                "timestamp": timestamp,
                "datetime": self.iso8601(timestamp),
                "lastTradeTimestamp": None,
                "lastUpdateTimestamp": None,
                "status": self.parse_order_status(self.safe_string(order, "status")),
                "symbol": market["symbol"],
                "type": type,
                "timeInForce": "GTC",
                "postOnly": None,
                "side": side,
                "price": price,
                "triggerPrice": trigger_price,
                "stopPrice": trigger_price,
                "amount": self.safe_string(order, "amount"),
                "filled": self.safe_string(order, "matchedAmount"),
                "remaining": self.safe_string(order, "unmatchedAmount"),
                "cost": self.safe_string(order, "totalPrice"),
                "average": average,
                "trades": None,
                "fee": fee,
                "info": order,
            },
            market,
        )

    def parse_balance(self, response: dict) -> dict:
        result: dict[str, Any] = {"info": response}
        for wallet in self.safe_list(response, "wallets", []):
            code = self.safe_currency_code((self.safe_string(wallet, "currency") or "").upper())
            account = self.account()
            account["total"] = self.safe_string(wallet, "balance")
            account["used"] = self.safe_string(wallet, "blockedBalance")
            account["free"] = self.safe_string(wallet, "activeBalance")
            result[code] = account
        return self.safe_balance(result)

    def _slice_order_book(self, response: dict, market: dict, limit) -> dict:
        orderbook = self.parse_order_book(
            response, market["symbol"], self.safe_integer(response, "lastUpdate")
        )
        if limit is not None:
            orderbook["bids"] = orderbook["bids"][:limit]
            orderbook["asks"] = orderbook["asks"][:limit]
        return orderbook

    def _wallets_request(self, params) -> dict:
        return self.extend({"type": self.safe_string(self.options, "walletType", "spot")}, params)


class nobitex(NobitexBase, ccxt.Exchange):
    """Synchronous Nobitex implementation."""

    def fetch_markets(self, params={}):
        response = self.public_get_v2_options(params)
        return self.parse_markets_from_options(response)

    def fetch_tickers(self, symbols=None, params={}):
        self.load_markets()
        symbols = self.market_symbols(symbols)
        response = self.public_get_market_stats(params)
        return self.parse_stats(response, symbols)

    def fetch_ticker(self, symbol, params={}):
        self.load_markets()
        market = self.market(symbol)
        request = {"srcCurrency": market["baseId"], "dstCurrency": market["quoteId"]}
        response = self.public_get_market_stats(self.extend(request, params))
        tickers = self.parse_stats(response, [market["symbol"]])
        if market["symbol"] not in tickers:
            raise BadSymbol(self.id + " fetch_ticker() no ticker for " + symbol)
        return tickers[market["symbol"]]

    def fetch_order_book(self, symbol, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        response = self.public_get_v3_orderbook_symbol(
            self.extend({"symbol": market["id"]}, params)
        )
        return self._slice_order_book(response, market, limit)

    def fetch_trades(self, symbol, since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        response = self.public_get_v2_trades_symbol(self.extend({"symbol": market["id"]}, params))
        return self.parse_trades(self.safe_list(response, "trades", []), market, since, limit)

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        request = self._ohlcv_request(market, timeframe, since, limit, params)
        response = self.public_get_market_udf_history(
            self.extend(request, self.omit(params, "until"))
        )
        return self._parse_ohlcv_response(response, market, timeframe, since, limit)

    def fetch_balance(self, params={}):
        self.load_markets()
        response = self.private_post_users_wallets_list(self._wallets_request(params))
        return self.parse_balance(response)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        request = self._create_order_request(market, type, side, amount, price, params)
        response = self.private_post_market_orders_add(request)
        return self.parse_order(self.safe_dict(response, "order", {}), market)

    def cancel_order(self, id, symbol=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = {"order": int(id), "status": "canceled"}
        response = self.private_post_market_orders_update_status(self.extend(request, params))
        return self.parse_order(self.safe_dict(response, "order", {"id": id}), market)

    def fetch_order(self, id, symbol=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        response = self.private_post_market_orders_status(self.extend({"id": int(id)}, params))
        return self.parse_order(self.safe_dict(response, "order", {}), market)

    def fetch_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_status("all", symbol, since, limit, params)

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_status("open", symbol, since, limit, params)

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_status("close", symbol, since, limit, params)

    def _fetch_orders_by_status(self, status, symbol, since, limit, params):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._orders_request(market, limit, status)
        response = self.private_get_market_orders_list(self.extend(request, params))
        return self.parse_orders(self.safe_list(response, "orders", []), market, since, limit)

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._my_trades_request(market, limit)
        response = self.private_get_market_trades_list(self.extend(request, params))
        return self.parse_trades(self.safe_list(response, "trades", []), market, since, limit)


class nobitex_async(NobitexBase, ccxt_async.Exchange):
    """Asynchronous Nobitex implementation."""

    async def fetch_markets(self, params={}):
        response = await self.public_get_v2_options(params)
        return self.parse_markets_from_options(response)

    async def fetch_tickers(self, symbols=None, params={}):
        await self.load_markets()
        symbols = self.market_symbols(symbols)
        response = await self.public_get_market_stats(params)
        return self.parse_stats(response, symbols)

    async def fetch_ticker(self, symbol, params={}):
        await self.load_markets()
        market = self.market(symbol)
        request = {"srcCurrency": market["baseId"], "dstCurrency": market["quoteId"]}
        response = await self.public_get_market_stats(self.extend(request, params))
        tickers = self.parse_stats(response, [market["symbol"]])
        if market["symbol"] not in tickers:
            raise BadSymbol(self.id + " fetch_ticker() no ticker for " + symbol)
        return tickers[market["symbol"]]

    async def fetch_order_book(self, symbol, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        response = await self.public_get_v3_orderbook_symbol(
            self.extend({"symbol": market["id"]}, params)
        )
        return self._slice_order_book(response, market, limit)

    async def fetch_trades(self, symbol, since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        response = await self.public_get_v2_trades_symbol(
            self.extend({"symbol": market["id"]}, params)
        )
        return self.parse_trades(self.safe_list(response, "trades", []), market, since, limit)

    async def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        request = self._ohlcv_request(market, timeframe, since, limit, params)
        response = await self.public_get_market_udf_history(
            self.extend(request, self.omit(params, "until"))
        )
        return self._parse_ohlcv_response(response, market, timeframe, since, limit)

    async def fetch_balance(self, params={}):
        await self.load_markets()
        response = await self.private_post_users_wallets_list(self._wallets_request(params))
        return self.parse_balance(response)

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        request = self._create_order_request(market, type, side, amount, price, params)
        response = await self.private_post_market_orders_add(request)
        return self.parse_order(self.safe_dict(response, "order", {}), market)

    async def cancel_order(self, id, symbol=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = {"order": int(id), "status": "canceled"}
        response = await self.private_post_market_orders_update_status(self.extend(request, params))
        return self.parse_order(self.safe_dict(response, "order", {"id": id}), market)

    async def fetch_order(self, id, symbol=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        response = await self.private_post_market_orders_status(
            self.extend({"id": int(id)}, params)
        )
        return self.parse_order(self.safe_dict(response, "order", {}), market)

    async def fetch_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_status("all", symbol, since, limit, params)

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_status("open", symbol, since, limit, params)

    async def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_status("close", symbol, since, limit, params)

    async def _fetch_orders_by_status(self, status, symbol, since, limit, params):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._orders_request(market, limit, status)
        response = await self.private_get_market_orders_list(self.extend(request, params))
        return self.parse_orders(self.safe_list(response, "orders", []), market, since, limit)

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._my_trades_request(market, limit)
        response = await self.private_get_market_trades_list(self.extend(request, params))
        return self.parse_trades(self.safe_list(response, "trades", []), market, since, limit)


def register() -> None:
    """
    Make Nobitex available as ``ccxt.nobitex`` and ``ccxt.async_support.nobitex``.
    Does nothing if ccxt already provides a native implementation.
    """
    for module, cls in ((ccxt, nobitex), (ccxt_async, nobitex_async)):
        if "nobitex" in module.exchanges:
            continue
        module.nobitex = cls
        module.exchanges.append("nobitex")
