"""
CCXT-compatible implementation of the Bitpin exchange (https://bitpin.ir).

Bitpin is not (yet) part of ccxt. This module provides sync and async ccxt Exchange classes
and registers them as ``ccxt.bitpin`` / ``ccxt.async_support.bitpin`` so the regular
freqtrade exchange machinery can use them.
Registration is skipped if the installed ccxt version ships its own ``bitpin`` implementation.

API documentation: https://docs.bitpin.ir/v1/docs/Introduction/bitpin-api-documentation
"""

# Method signatures intentionally mirror ccxt (params={}, type, id ...)
# ruff: noqa: A001, A002, B006

import base64
import json
import logging
import threading
import time
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


logger = logging.getLogger(__name__)

# Refresh the access token this many seconds before it expires.
TOKEN_EXPIRY_MARGIN = 60

# Tokens are shared between the sync and async instances (and across instances using the
# same key), as Bitpin only allows 200 token requests per day.
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()


class BitpinTokenInvalid(AuthenticationError):
    """Access token expired or invalid - a fresh token should fix this."""


if TYPE_CHECKING:
    # Give the mixin access to the ccxt Exchange helpers for type checking
    from ccxt.base.exchange import Exchange as _MixinBase
else:
    _MixinBase = object


class BitpinBase(_MixinBase):
    """
    Request building, response parsing and authentication helpers shared between
    the sync and async implementations.
    """

    # Implicit API - costs are relative to rateLimit (200ms -> 300 requests per minute).
    public_get_mkt_markets = Entry("mkt/markets/", "public", "GET", {"cost": 1})
    public_get_mkt_tickers = Entry("mkt/tickers/", "public", "GET", {"cost": 3.75})  # 80/min
    public_get_mkt_candles = Entry("mkt/candles/", "public", "GET", {"cost": 1})
    public_get_mth_orderbook = Entry("mth/orderbook/{symbol}/", "public", "GET", {"cost": 5})
    public_get_mth_matches = Entry("mth/matches/{symbol}/", "public", "GET", {"cost": 5})
    private_get_wlt_wallets = Entry("wlt/wallets/", "private", "GET", {"cost": 5})  # 60/min
    # 1000 / day
    private_get_mkt_commissions = Entry("mkt/commissions/", "private", "GET", {"cost": 5})
    # Order queries share 80 / minute
    private_get_odr_orders = Entry("odr/orders/", "private", "GET", {"cost": 3.75})
    private_get_odr_order = Entry("odr/orders/{order_id}/", "private", "GET", {"cost": 3.75})
    private_get_odr_fills = Entry("odr/fills/", "private", "GET", {"cost": 3.75})
    # Order placement / cancellation: 5400 / hour
    private_post_odr_orders = Entry("odr/orders/", "private", "POST", {"cost": 3.34})
    private_delete_odr_order = Entry("odr/orders/{order_id}/", "private", "DELETE", {"cost": 3.34})
    # Token creation and refresh: 200 / day
    auth_post_usr_authenticate = Entry("usr/authenticate/", "auth", "POST", {"cost": 1})
    auth_post_usr_refresh_token = Entry("usr/refresh_token/", "auth", "POST", {"cost": 1})

    def describe(self) -> Any:
        return self.deep_extend(
            super().describe(),
            {
                "id": "bitpin",
                "name": "Bitpin",
                "countries": ["IR"],
                # Default limit: 300 requests per minute per IP.
                "rateLimit": 200,
                "version": "v1",
                "certified": False,
                "pro": False,
                "hostname": "api.bitpin.org",
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
                    "fetchTradingFees": True,
                },
                "timeframes": {
                    "1m": "1m",
                    "5m": "5m",
                    "15m": "15m",
                    "30m": "30m",
                    "1h": "1h",
                    "3h": "3h",
                    "4h": "4h",
                    "6h": "6h",
                    "1d": "1d",
                    "1w": "1w",
                    "1M": "1M",
                },
                "urls": {
                    "api": {
                        "public": "https://{hostname}/api/v1",
                        "private": "https://{hostname}/api/v1",
                        "auth": "https://{hostname}/api/v1",
                    },
                    "www": "https://bitpin.ir",
                    "doc": "https://docs.bitpin.ir/v1/docs/Introduction/bitpin-api-documentation",
                },
                "requiredCredentials": {
                    "apiKey": True,
                    "secret": True,
                },
                "precisionMode": TICK_SIZE,
                "fees": {
                    "trading": {
                        "tierBased": False,
                        "percentage": True,
                        # Defaults - the real per-market fees are available via fetch_trading_fees
                        "maker": self.parse_number("0.003"),
                        "taker": self.parse_number("0.004"),
                    },
                },
                "options": {
                    # Wallet service to report balances for
                    "walletService": "main",
                    "fetchOHLCVLimit": 1000,
                },
            },
        )

    # --- Authentication ---------------------------------------------------------------

    def _token_cache_key(self) -> str:
        return f"{self.hostname}:{self.apiKey}"

    def _cached_tokens(self) -> dict[str, Any]:
        with _TOKEN_LOCK:
            return dict(_TOKEN_CACHE.get(self._token_cache_key(), {}))

    def _store_tokens(self, response: dict[str, Any]) -> None:
        with _TOKEN_LOCK:
            entry = _TOKEN_CACHE.setdefault(self._token_cache_key(), {})
            for key in ("access", "refresh"):
                token = self.safe_string(response, key)
                if token:
                    entry[key] = token
                    entry[f"{key}_exp"] = self._jwt_expiry(token)

    def _invalidate_access_token(self) -> None:
        with _TOKEN_LOCK:
            entry = _TOKEN_CACHE.get(self._token_cache_key(), {})
            entry.pop("access", None)
            entry.pop("access_exp", None)

    @staticmethod
    def _jwt_expiry(token: str) -> float:
        """Read the "exp" claim of a JWT token. Returns 0 if it can't be decoded."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except (IndexError, KeyError, TypeError, ValueError):
            return 0

    @staticmethod
    def _token_valid(tokens: dict[str, Any], key: str) -> bool:
        return bool(tokens.get(key)) and tokens.get(f"{key}_exp", 0) - TOKEN_EXPIRY_MARGIN > (
            time.time()
        )

    # --- Request signing / error handling ---------------------------------------------

    def sign(self, path, api="public", method="GET", params={}, headers=None, body=None):
        url = self.implode_hostname(self.urls["api"][api]) + "/" + self.implode_params(path, params)
        query = self.omit(params, self.extract_params(path))
        headers = {} if headers is None else headers
        if method in ("GET", "DELETE"):
            if query:
                url += "?" + self.urlencode(query)
        else:
            body = self.json(query)
            headers["Content-Type"] = "application/json"
        if api == "private":
            tokens = self._cached_tokens()
            if not tokens.get("access"):
                raise AuthenticationError(self.id + " no access token available")
            headers["Authorization"] = "Bearer " + tokens["access"]
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
        if code < 400:
            return
        message = self.id + " " + str(body)
        error_code = self.safe_string(response, "code") if isinstance(response, dict) else None
        if error_code == "token_not_valid":
            raise BitpinTokenInvalid(message)
        if code == 401 or error_code == "api_credential_wrong" or (code == 406 and "/usr/" in url):
            # Wrong key/secret or IP not whitelisted - usr/authenticate answers these with 406,
            # e.g. {"code": "api_credential_wrong", ...} or {"detail": "wrong ip 1.2.3.4"}
            raise AuthenticationError(message)
        if code == 403:
            raise PermissionDenied(message)
        if code == 429:
            raise RateLimitExceeded(message)
        is_order_endpoint = "/odr/" in url
        if code == 404 and is_order_endpoint:
            raise OrderNotFound(message)
        if code == 406 and is_order_endpoint:
            # Cancel of an order that's no longer cancellable.
            raise InvalidOrder(message)
        if code == 400:
            lowered = str(body).lower()
            if "balance" in lowered or "موجودی" in str(body):
                raise InsufficientFunds(message)
            if is_order_endpoint:
                raise InvalidOrder(message)
            raise BadRequest(message)
        if 400 < code < 500:
            raise ExchangeError(message)
        # 5xx are handled by ccxt's handle_http_status_code.
        return

    # --- Request builders ---------------------------------------------------------------

    def _ohlcv_request(self, market, timeframe, since, limit, params) -> dict[str, Any]:
        duration = self.parse_timeframe(timeframe)
        if limit is None:
            limit = self.safe_integer(self.options, "fetchOHLCVLimit", 1000)
        until = self.safe_integer(params, "until")
        if since is not None:
            start = since // 1000
            end = start + duration * limit - 1
            if until is not None:
                end = min(end, until // 1000)
        else:
            end = until // 1000 if until is not None else self.seconds()
            start = end - duration * limit
        return {
            "symbol": market["id"],
            "res": self.safe_string(self.timeframes, timeframe, timeframe),
            "from": start,
            "to": end,
        }

    def _create_order_request(self, market, type, side, amount, price, params) -> dict[str, Any]:
        request: dict[str, Any] = {
            "symbol": market["id"],
            "type": type,
            "side": side,
            "base_amount": self.amount_to_precision(market["symbol"], amount),
        }
        trigger_price = self.safe_string_2(params, "triggerPrice", "stopPrice")
        if trigger_price is not None:
            if type != "limit":
                raise InvalidOrder(self.id + " trigger orders are only supported as stop_limit")
            request["type"] = "stop_limit"
            request["stop_price"] = self.price_to_precision(market["symbol"], trigger_price)
        if type == "limit":
            if price is None:
                raise InvalidOrder(self.id + " limit orders require a price")
            request["price"] = self.price_to_precision(market["symbol"], price)
        client_order_id = self.safe_string_2(params, "clientOrderId", "identifier")
        if client_order_id is not None:
            request["identifier"] = client_order_id
        params = self.omit(params, ["triggerPrice", "stopPrice", "clientOrderId", "identifier"])
        return self.extend(request, params)

    def _orders_request(self, market, since, limit, state) -> dict[str, Any]:
        request: dict[str, Any] = {}
        if market is not None:
            request["symbol"] = market["id"]
        if state is not None:
            request["state"] = state
        if since is not None:
            # Bitpin filters by date (YYYY-MM-DD) only - exact filtering happens in parse_orders
            request["start"] = self.iso8601(since)[0:10]
        request["limit"] = min(limit, 100) if limit is not None else 100
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

    @staticmethod
    def _seconds_to_ms(seconds: str | None) -> int | None:
        if seconds is None:
            return None
        return int(float(seconds) * 1000)

    def parse_market(self, market: dict) -> dict:
        market_id = self.safe_string(market, "symbol")
        base_id = self.safe_string(market, "base")
        quote_id = self.safe_string(market, "quote")
        base = self.safe_currency_code(base_id)
        quote = self.safe_currency_code(quote_id)
        tradable = self.safe_bool(market, "tradable", True)
        suspended = self.safe_bool(market, "suspended", False)
        fees = self.fees["trading"]
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
                "active": tradable and not suspended,
                "contract": False,
                "linear": None,
                "inverse": None,
                "taker": fees["taker"],
                "maker": fees["maker"],
                "contractSize": None,
                "expiry": None,
                "expiryDatetime": None,
                "strike": None,
                "optionType": None,
                "precision": {
                    "amount": self.parse_number(
                        self.parse_precision(self.safe_string(market, "base_amount_precision"))
                    ),
                    "price": self.parse_number(
                        self.parse_precision(self.safe_string(market, "price_precision"))
                    ),
                    "cost": self.parse_number(
                        self.parse_precision(self.safe_string(market, "quote_amount_precision"))
                    ),
                },
                "limits": {
                    "leverage": {"min": None, "max": None},
                    "amount": {"min": None, "max": None},
                    "price": {"min": None, "max": None},
                    "cost": {"min": None, "max": None},
                },
                "created": None,
                "info": market,
            }
        )

    def parse_ticker(self, ticker: dict, market=None) -> dict:
        market_id = self.safe_string(ticker, "symbol")
        market = self.safe_market(market_id, market)
        timestamp = self._seconds_to_ms(self.safe_string(ticker, "timestamp"))
        last = self.safe_string(ticker, "price")
        return self.safe_ticker(
            {
                "symbol": market["symbol"],
                "timestamp": timestamp,
                "datetime": self.iso8601(timestamp),
                "high": self.safe_string(ticker, "high"),
                "low": self.safe_string(ticker, "low"),
                "bid": None,
                "bidVolume": None,
                "ask": None,
                "askVolume": None,
                "vwap": None,
                "open": None,
                "close": last,
                "last": last,
                "previousClose": None,
                "change": None,
                "percentage": self.safe_string(ticker, "daily_change_price"),
                "average": None,
                "baseVolume": None,
                "quoteVolume": None,
                "info": ticker,
            },
            market,
        )

    def _add_book_to_ticker(self, ticker: dict, orderbook: dict) -> dict:
        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []
        if bids:
            ticker["bid"], ticker["bidVolume"] = bids[0][0], bids[0][1]
        if asks:
            ticker["ask"], ticker["askVolume"] = asks[0][0], asks[0][1]
        return ticker

    def parse_ohlcv(self, ohlcv, market=None) -> list:
        return [
            self._seconds_to_ms(self.safe_string(ohlcv, "ts")),
            self.safe_number(ohlcv, "open"),
            self.safe_number(ohlcv, "high"),
            self.safe_number(ohlcv, "low"),
            self.safe_number(ohlcv, "close"),
            self.safe_number(ohlcv, "volume"),
        ]

    def parse_trade(self, trade: dict, market=None) -> dict:
        # Public trades (matches) carry "time" in seconds, own trades (fills) "created_at".
        market_id = self.safe_string(trade, "symbol")
        market = self.safe_market(market_id, market)
        timestamp = self._parse_datetime(self.safe_string(trade, "created_at"))
        if timestamp is None:
            timestamp = self._seconds_to_ms(self.safe_string(trade, "time"))
        fee = None
        fee_cost = self.safe_string(trade, "commission")
        if fee_cost is not None:
            fee = {
                "cost": fee_cost,
                "currency": self.safe_currency_code(self.safe_string(trade, "commission_currency")),
            }
        return self.safe_trade(
            {
                "id": self.safe_string(trade, "id"),
                "order": self.safe_string(trade, "order_id"),
                "timestamp": timestamp,
                "datetime": self.iso8601(timestamp),
                "symbol": market["symbol"],
                "type": None,
                "side": self.safe_string(trade, "side"),
                "takerOrMaker": None,
                "price": self.safe_string(trade, "price"),
                "amount": self.safe_string(trade, "base_amount"),
                "cost": self.safe_string(trade, "quote_amount"),
                "fee": fee,
                "info": trade,
            },
            market,
        )

    def parse_order_status(self, order: dict) -> str | None:
        state = self.safe_string(order, "state")
        if state in ("initial", "active"):
            return "open"
        if state == "closed":
            amount = self.safe_string(order, "base_amount")
            filled = self.safe_string(order, "dealed_base_amount", "0")
            if amount is not None and Precise.string_ge(filled, amount):
                return "closed"
            if self.safe_string(order, "type") == "market" and Precise.string_gt(filled, "0"):
                # Market orders may have been placed by quote amount
                return "closed"
            return "canceled"
        return state

    def parse_order(self, order: dict, market=None) -> dict:
        market_id = self.safe_string(order, "symbol")
        market = self.safe_market(market_id, market)
        side = self.safe_string(order, "side")
        type = self.safe_string(order, "type")
        price = self.safe_string(order, "price")
        if Precise.string_eq(price, "0"):
            price = None
        fee = None
        fee_cost = self.safe_number(order, "commission")
        if fee_cost is not None:
            # Bitpin charges buy fees in the base currency and sell fees in the quote currency.
            fee = {
                "cost": fee_cost,
                "currency": market["base"] if side == "buy" else market["quote"],
            }
        trigger_price = self.safe_string(order, "stop_price")
        timestamp = self._parse_datetime(self.safe_string(order, "created_at"))
        return self.safe_order(
            {
                "id": self.safe_string(order, "id"),
                "clientOrderId": self.safe_string(order, "identifier"),
                "timestamp": timestamp,
                "datetime": self.iso8601(timestamp),
                "lastTradeTimestamp": self._parse_datetime(self.safe_string(order, "closed_at")),
                "lastUpdateTimestamp": None,
                "status": self.parse_order_status(order),
                "symbol": market["symbol"],
                "type": "limit" if type == "stop_limit" else type,
                "timeInForce": "GTC",
                "postOnly": None,
                "side": side,
                "price": price,
                "triggerPrice": trigger_price,
                "stopPrice": trigger_price,
                "amount": self.safe_string(order, "base_amount"),
                "filled": self.safe_string(order, "dealed_base_amount"),
                "remaining": None,
                "cost": self.safe_string(order, "dealed_quote_amount"),
                "average": None,
                "trades": None,
                "fee": fee,
                "info": order,
            },
            market,
        )

    def parse_balance(self, response: list) -> dict:
        service = self.safe_string(self.options, "walletService", "main")
        result: dict[str, Any] = {"info": response}
        for wallet in response:
            if self.safe_string(wallet, "service", service) != service:
                continue
            code = self.safe_currency_code(self.safe_string(wallet, "asset"))
            account = self.account()
            account["free"] = self.safe_string(wallet, "balance")
            account["used"] = self.safe_string(wallet, "frozen")
            result[code] = account
        return self.safe_balance(result)

    def parse_trading_fees(self, response: list) -> dict:
        result: dict[str, Any] = {}
        for entry in response:
            market = self.safe_market(self.safe_string(entry, "symbol"))
            # Bitpin returns percentages (0.3 == 0.3%)
            result[market["symbol"]] = {
                "info": entry,
                "symbol": market["symbol"],
                "maker": self.parse_number(
                    Precise.string_div(self.safe_string(entry, "maker"), "100")
                ),
                "taker": self.parse_number(
                    Precise.string_div(self.safe_string(entry, "taker"), "100")
                ),
                "percentage": True,
                "tierBased": False,
            }
        return result

    def _filter_symbol_tickers(self, response: list, symbols) -> dict:
        tickers = [self.parse_ticker(t) for t in response]
        result = {t["symbol"]: t for t in tickers if t["symbol"] in self.markets}
        return self.filter_by_array_tickers(result, "symbol", symbols)


class bitpin(BitpinBase, ccxt.Exchange):
    """Synchronous Bitpin implementation."""

    def _ensure_access_token(self) -> None:
        tokens = self._cached_tokens()
        if self._token_valid(tokens, "access"):
            return
        self.check_required_credentials()
        if self._token_valid(tokens, "refresh"):
            try:
                response = self.auth_post_usr_refresh_token({"refresh": tokens["refresh"]})
                self._store_tokens(response)
                return
            except AuthenticationError:
                logger.info("Bitpin refresh token rejected, re-authenticating.")
        response = self.auth_post_usr_authenticate(
            {"api_key": self.apiKey, "secret_key": self.secret}
        )
        self._store_tokens(response)

    def request(
        self, path, api="public", method="GET", params={}, headers=None, body=None, config={}
    ):
        if api != "private":
            return self.fetch2(path, api, method, params, headers, body, config)
        self._ensure_access_token()
        try:
            return self.fetch2(path, api, method, params, headers, body, config)
        except BitpinTokenInvalid:
            self._invalidate_access_token()
            self._ensure_access_token()
            return self.fetch2(path, api, method, params, headers, body, config)

    def fetch_markets(self, params={}):
        response = self.public_get_mkt_markets(params)
        return [self.parse_market(m) for m in response]

    def fetch_tickers(self, symbols=None, params={}):
        self.load_markets()
        symbols = self.market_symbols(symbols)
        response = self.public_get_mkt_tickers(params)
        return self._filter_symbol_tickers(response, symbols)

    def fetch_ticker(self, symbol, params={}):
        self.load_markets()
        market = self.market(symbol)
        tickers = self.fetch_tickers([symbol], params)
        if symbol not in tickers:
            raise BadSymbol(self.id + " fetch_ticker() no ticker for " + symbol)
        orderbook = self.fetch_order_book(market["symbol"])
        return self._add_book_to_ticker(tickers[symbol], orderbook)

    def fetch_order_book(self, symbol, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        response = self.public_get_mth_orderbook(self.extend({"symbol": market["id"]}, params))
        orderbook = self.parse_order_book(response, market["symbol"])
        if limit is not None:
            orderbook["bids"] = orderbook["bids"][:limit]
            orderbook["asks"] = orderbook["asks"][:limit]
        return orderbook

    def fetch_trades(self, symbol, since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        response = self.public_get_mth_matches(self.extend({"symbol": market["id"]}, params))
        return self.parse_trades(response, market, since, limit)

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        request = self._ohlcv_request(market, timeframe, since, limit, params)
        response = self.public_get_mkt_candles(self.extend(request, self.omit(params, "until")))
        return self.parse_ohlcvs(response, market, timeframe, since, limit)

    def fetch_balance(self, params={}):
        self.load_markets()
        wallets: list = []
        request: dict[str, Any] = {"limit": 200}
        while True:
            page = self.private_get_wlt_wallets(self.extend(request, params))
            wallets.extend(page)
            if len(page) < request["limit"]:
                break
            request["offset"] = min(self.safe_integer(w, "id") for w in page)
        return self.parse_balance(wallets)

    def fetch_trading_fees(self, params={}):
        self.load_markets()
        response = self.private_get_mkt_commissions(params)
        return self.parse_trading_fees(response)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        self.load_markets()
        market = self.market(symbol)
        request = self._create_order_request(market, type, side, amount, price, params)
        response = self.private_post_odr_orders(request)
        return self.parse_order(response, market)

    def cancel_order(self, id, symbol=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        self.private_delete_odr_order(self.extend({"order_id": id}, params))
        return self.safe_order({"id": id, "info": {}}, market)

    def fetch_order(self, id, symbol=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        response = self.private_get_odr_order(self.extend({"order_id": id}, params))
        return self.parse_order(response, market)

    def fetch_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_state(None, symbol, since, limit, params)

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_state("initial,active", symbol, since, limit, params)

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._fetch_orders_by_state("closed", symbol, since, limit, params)

    def _fetch_orders_by_state(self, state, symbol, since, limit, params):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._orders_request(market, since, limit, state)
        response = self.private_get_odr_orders(self.extend(request, params))
        return self.parse_orders(response, market, since, limit)

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params={}):
        self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request: dict[str, Any] = {"limit": 100}
        if market is not None:
            request["symbol"] = market["id"]
        response = self.private_get_odr_fills(self.extend(request, params))
        return self.parse_trades(response, market, since, limit)


class bitpin_async(BitpinBase, ccxt_async.Exchange):
    """Asynchronous Bitpin implementation."""

    async def _ensure_access_token(self) -> None:
        tokens = self._cached_tokens()
        if self._token_valid(tokens, "access"):
            return
        self.check_required_credentials()
        if self._token_valid(tokens, "refresh"):
            try:
                response = await self.auth_post_usr_refresh_token({"refresh": tokens["refresh"]})
                self._store_tokens(response)
                return
            except AuthenticationError:
                logger.info("Bitpin refresh token rejected, re-authenticating.")
        response = await self.auth_post_usr_authenticate(
            {"api_key": self.apiKey, "secret_key": self.secret}
        )
        self._store_tokens(response)

    async def request(
        self, path, api="public", method="GET", params={}, headers=None, body=None, config={}
    ):
        if api != "private":
            return await self.fetch2(path, api, method, params, headers, body, config)
        await self._ensure_access_token()
        try:
            return await self.fetch2(path, api, method, params, headers, body, config)
        except BitpinTokenInvalid:
            self._invalidate_access_token()
            await self._ensure_access_token()
            return await self.fetch2(path, api, method, params, headers, body, config)

    async def fetch_markets(self, params={}):
        response = await self.public_get_mkt_markets(params)
        return [self.parse_market(m) for m in response]

    async def fetch_tickers(self, symbols=None, params={}):
        await self.load_markets()
        symbols = self.market_symbols(symbols)
        response = await self.public_get_mkt_tickers(params)
        return self._filter_symbol_tickers(response, symbols)

    async def fetch_ticker(self, symbol, params={}):
        await self.load_markets()
        market = self.market(symbol)
        tickers = await self.fetch_tickers([symbol], params)
        if symbol not in tickers:
            raise BadSymbol(self.id + " fetch_ticker() no ticker for " + symbol)
        orderbook = await self.fetch_order_book(market["symbol"])
        return self._add_book_to_ticker(tickers[symbol], orderbook)

    async def fetch_order_book(self, symbol, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        response = await self.public_get_mth_orderbook(
            self.extend({"symbol": market["id"]}, params)
        )
        orderbook = self.parse_order_book(response, market["symbol"])
        if limit is not None:
            orderbook["bids"] = orderbook["bids"][:limit]
            orderbook["asks"] = orderbook["asks"][:limit]
        return orderbook

    async def fetch_trades(self, symbol, since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        response = await self.public_get_mth_matches(self.extend({"symbol": market["id"]}, params))
        return self.parse_trades(response, market, since, limit)

    async def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        request = self._ohlcv_request(market, timeframe, since, limit, params)
        response = await self.public_get_mkt_candles(
            self.extend(request, self.omit(params, "until"))
        )
        return self.parse_ohlcvs(response, market, timeframe, since, limit)

    async def fetch_balance(self, params={}):
        await self.load_markets()
        wallets: list = []
        request: dict[str, Any] = {"limit": 200}
        while True:
            page = await self.private_get_wlt_wallets(self.extend(request, params))
            wallets.extend(page)
            if len(page) < request["limit"]:
                break
            request["offset"] = min(self.safe_integer(w, "id") for w in page)
        return self.parse_balance(wallets)

    async def fetch_trading_fees(self, params={}):
        await self.load_markets()
        response = await self.private_get_mkt_commissions(params)
        return self.parse_trading_fees(response)

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        await self.load_markets()
        market = self.market(symbol)
        request = self._create_order_request(market, type, side, amount, price, params)
        response = await self.private_post_odr_orders(request)
        return self.parse_order(response, market)

    async def cancel_order(self, id, symbol=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        await self.private_delete_odr_order(self.extend({"order_id": id}, params))
        return self.safe_order({"id": id, "info": {}}, market)

    async def fetch_order(self, id, symbol=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        response = await self.private_get_odr_order(self.extend({"order_id": id}, params))
        return self.parse_order(response, market)

    async def fetch_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_state(None, symbol, since, limit, params)

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_state("initial,active", symbol, since, limit, params)

    async def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return await self._fetch_orders_by_state("closed", symbol, since, limit, params)

    async def _fetch_orders_by_state(self, state, symbol, since, limit, params):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request = self._orders_request(market, since, limit, state)
        response = await self.private_get_odr_orders(self.extend(request, params))
        return self.parse_orders(response, market, since, limit)

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params={}):
        await self.load_markets()
        market = self.market(symbol) if symbol is not None else None
        request: dict[str, Any] = {"limit": 100}
        if market is not None:
            request["symbol"] = market["id"]
        response = await self.private_get_odr_fills(self.extend(request, params))
        return self.parse_trades(response, market, since, limit)


def register() -> None:
    """
    Make Bitpin available as ``ccxt.bitpin`` and ``ccxt.async_support.bitpin``.
    Does nothing if ccxt already provides a native implementation.
    """
    for module, cls in ((ccxt, bitpin), (ccxt_async, bitpin_async)):
        if "bitpin" in module.exchanges:
            continue
        module.bitpin = cls
        module.exchanges.append("bitpin")
