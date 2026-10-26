import base64
import json
from unittest.mock import MagicMock

import ccxt
import ccxt.async_support as ccxt_async
import pytest

from freqtrade.exchange import Bitpin, ccxt_bitpin
from freqtrade.exchange.ccxt_bitpin import BitpinTokenInvalid
from tests.conftest import get_patched_exchange


MARKETS = [
    {
        "symbol": "BTC_USDT",
        "name": "Bitcoin/Tether",
        "base": "BTC",
        "quote": "USDT",
        "tradable": True,
        "suspended": False,
        "price_precision": 2,
        "base_amount_precision": 8,
        "quote_amount_precision": 2,
    },
    {
        "symbol": "USDT_IRT",
        "name": "Tether/Toman",
        "base": "USDT",
        "quote": "IRT",
        "tradable": True,
        "suspended": True,
        "price_precision": 0,
        "base_amount_precision": 2,
        "quote_amount_precision": 0,
    },
]


def make_jwt(exp: float) -> str:
    def enc(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp})}.signature"


@pytest.fixture(autouse=True)
def clear_token_cache():
    ccxt_bitpin._TOKEN_CACHE.clear()
    yield
    ccxt_bitpin._TOKEN_CACHE.clear()


@pytest.fixture
def api():
    ex = ccxt.bitpin({"apiKey": "key", "secret": "secret", "enableRateLimit": False})
    ex.markets = None
    ex.set_markets([ex.parse_market(m) for m in MARKETS])
    return ex


def mock_fetch(ex, routes: dict):
    """Route ccxt fetch() calls by "METHOD path" to canned responses (or callables)."""
    calls = []

    def _fetch(url, method="GET", headers=None, body=None):
        path = url.split("/api/v1/", 1)[1].split("?", 1)[0]
        calls.append({"method": method, "path": path, "url": url, "headers": headers, "body": body})
        result = routes[f"{method} {path}"]
        return result(calls[-1]) if callable(result) else result

    ex.fetch = MagicMock(side_effect=_fetch)
    return calls


def test_bitpin_registered_in_ccxt():
    assert "bitpin" in ccxt.exchanges
    assert "bitpin" in ccxt_async.exchanges
    assert ccxt.bitpin is ccxt_bitpin.bitpin
    assert ccxt_async.bitpin is ccxt_bitpin.bitpin_async
    # Registering again must not duplicate entries
    ccxt_bitpin.register()
    assert ccxt.exchanges.count("bitpin") == 1


def test_bitpin_exchange_class(default_conf, mocker):
    exchange = get_patched_exchange(mocker, default_conf, exchange="bitpin")
    assert isinstance(exchange, Bitpin)
    assert exchange.get_option("tickers_have_bid_ask") is False
    assert exchange.get_option("ohlcv_candle_limit") == 1000


def test_parse_market(api):
    market = api.market("BTC/USDT")
    assert market["id"] == "BTC_USDT"
    assert market["spot"] is True
    assert market["active"] is True
    assert market["precision"] == {"amount": 1e-08, "price": 0.01, "cost": 0.01}
    # Suspended markets are inactive
    assert api.market("USDT/IRT")["active"] is False


def test_parse_datetime():
    assert ccxt_bitpin.BitpinBase._parse_datetime("2024-07-29T14:07:34.176795+03:30") == (
        1722249454176
    )
    assert ccxt_bitpin.BitpinBase._parse_datetime(None) is None
    assert ccxt_bitpin.BitpinBase._parse_datetime("garbage") is None


def test_ohlcv_request(api, time_machine):
    time_machine.move_to("2025-01-01 00:00:00 +00:00", tick=False)
    market = api.market("BTC/USDT")
    # Timeframes above 30m are requested as 30m candles
    req = api._ohlcv_request(market, "1h", 1735689600000, 10, {})
    assert req == {"symbol": "BTC_USDT", "res": "30m", "from": 1735689600, "to": 1735725599}
    # since is aligned down to the timeframe
    req = api._ohlcv_request(market, "1h", 1735689600000 + 600_000, 10, {})
    assert req["from"] == 1735689600
    # Limit is capped to what fits into 10000 30m candles
    req = api._ohlcv_request(market, "1d", 1735689600000, 1000, {})
    assert req["to"] - req["from"] + 1 == 208 * 86400
    req = api._ohlcv_request(market, "5m", None, 12, {})
    assert req["res"] == "5m"
    assert req["to"] == 1735689600
    assert req["from"] == 1735689600 - 11 * 300


def candle(ts: int, o, h, low, c, v) -> dict:
    return {"open": o, "close": c, "low": str(low), "high": str(h), "volume": str(v), "ts": ts}


def test_fetch_ohlcv_aggregates_30m(api):
    base = 1735689600
    calls = mock_fetch(
        api,
        {
            "GET mkt/candles/": [
                candle(base, 10.0, 12, 9, 11.0, 1),
                candle(base + 1800, 11.0, 15, 10, 14.0, 2),
                candle(base + 3600, 14.0, 14, 8, 9.0, 3),
                # Current, incomplete hour
                candle(base + 7200, 9.0, 10, 9, 10.0, 1),
            ]
        },
    )
    res = api.fetch_ohlcv("BTC/USDT", "1h", since=base * 1000, limit=3)
    assert res == [
        [base * 1000, 10.0, 15.0, 9.0, 14.0, 3.0],
        [(base + 3600) * 1000, 14.0, 14.0, 8.0, 9.0, 3.0],
        [(base + 7200) * 1000, 9.0, 10.0, 9.0, 10.0, 1.0],
    ]
    assert "res=30m" in calls[0]["url"]
    assert "symbol=BTC_USDT" in calls[0]["url"]
    assert "Authorization" not in calls[0]["headers"]


def test_fetch_ohlcv_native_timeframe(api):
    mock_fetch(api, {"GET mkt/candles/": [candle(1735689600, 1019.0, 1023, 1017.5, 1020.0, 1.5)]})
    res = api.fetch_ohlcv("BTC/USDT", "15m", since=1735689600000, limit=2)
    assert res == [[1735689600000, 1019.0, 1023.0, 1017.5, 1020.0, 1.5]]


def test_aggregate_ohlcv_daily():
    day = 86_400_000
    half_hour = 1_800_000
    candles = [[day + i * half_hour, 1.0 + i, 2.0 + i, 0.5, 1.5 + i, 1.0] for i in range(48)]
    candles.append([2 * day, 5.0, 6.0, 4.0, 5.5, 2.0])
    res = ccxt_bitpin.BitpinBase._aggregate_ohlcv(candles, day)
    assert res == [[day, 1.0, 49.0, 0.5, 48.5, 48.0], [2 * day, 5.0, 6.0, 4.0, 5.5, 2.0]]


def test_fetch_ticker_adds_orderbook(api):
    mock_fetch(
        api,
        {
            "GET mkt/tickers/": [
                {
                    "symbol": "BTC_USDT",
                    "price": "100.5",
                    "daily_change_price": -1.5,
                    "low": "99",
                    "high": "101",
                    "timestamp": 1722860054.189,
                },
                {"symbol": "UNKNOWN_USDT", "price": "1", "timestamp": 1722860054.189},
            ],
            "GET mth/orderbook/BTC_USDT/": {
                "asks": [["100.6", "2"], ["101", "1"]],
                "bids": [["100.4", "3"], ["100", "1"]],
            },
        },
    )
    assert list(api.fetch_tickers().keys()) == ["BTC/USDT"]
    ticker = api.fetch_ticker("BTC/USDT")
    assert ticker["last"] == 100.5
    assert ticker["bid"] == 100.4
    assert ticker["ask"] == 100.6
    assert ticker["percentage"] == -1.5
    assert ticker["timestamp"] == 1722860054189


def test_token_handling(api, time_machine):
    now = 1_800_000_000
    time_machine.move_to(now, tick=False)
    access_1 = make_jwt(now + 900)
    access_2 = make_jwt(now + 2000)
    refresh = make_jwt(now + 7 * 86400)
    calls = mock_fetch(
        api,
        {
            "POST usr/authenticate/": {"access": access_1, "refresh": refresh},
            "POST usr/refresh_token/": {"access": access_2},
            "GET wlt/wallets/": [],
        },
    )
    api.fetch_balance()
    assert [c["path"] for c in calls] == ["usr/authenticate/", "wlt/wallets/"]
    assert json.loads(calls[0]["body"]) == {"api_key": "key", "secret_key": "secret"}
    assert calls[1]["headers"]["Authorization"] == f"Bearer {access_1}"

    # Token is cached - also across instances sharing the same key
    other = ccxt.bitpin({"apiKey": "key", "secret": "secret", "enableRateLimit": False})
    other.set_markets(api.markets)
    other_calls = mock_fetch(other, {"GET wlt/wallets/": []})
    other.fetch_balance()
    assert [c["path"] for c in other_calls] == ["wlt/wallets/"]

    # Access token about to expire -> refresh token is used
    time_machine.move_to(now + 900 - 30, tick=False)
    calls.clear()
    api.fetch_balance()
    assert [c["path"] for c in calls] == ["usr/refresh_token/", "wlt/wallets/"]
    assert json.loads(calls[0]["body"]) == {"refresh": refresh}
    assert calls[1]["headers"]["Authorization"] == f"Bearer {access_2}"


def test_token_invalid_retries_once(api, time_machine):
    now = 1_800_000_000
    time_machine.move_to(now, tick=False)
    tokens = iter([make_jwt(now + 900), make_jwt(now + 901)])
    attempts = []

    def wallets(call):
        attempts.append(call["headers"]["Authorization"])
        if len(attempts) == 1:
            raise BitpinTokenInvalid("bitpin token_not_valid")
        return [{"id": 1, "asset": "USDT", "balance": "10", "frozen": "0", "service": "main"}]

    mock_fetch(
        api,
        {
            "POST usr/authenticate/": lambda _: {"access": next(tokens)},
            "GET wlt/wallets/": wallets,
        },
    )
    balance = api.fetch_balance()
    assert balance["USDT"]["free"] == 10
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]


def test_fetch_balance_pagination(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    ccxt_bitpin._TOKEN_CACHE[api._token_cache_key()] = {
        "access": "tok",
        "access_exp": 1_800_000_000 + 900,
    }
    page_1 = [
        {"id": 500 - i, "asset": f"C{i}", "balance": "1", "frozen": "0", "service": "main"}
        for i in range(200)
    ]
    page_2 = [
        {"id": 10, "asset": "IRT", "balance": "1500", "frozen": "500", "service": "main"},
        {"id": 9, "asset": "USDT", "balance": "5", "frozen": "0", "service": "other"},
    ]
    calls = mock_fetch(
        api, {"GET wlt/wallets/": lambda c: page_2 if "offset=301" in c["url"] else page_1}
    )
    balance = api.fetch_balance()
    assert len(calls) == 2
    assert balance["IRT"] == {"free": 1500.0, "used": 500.0, "total": 2000.0}
    assert "USDT" not in balance
    assert "C0" in balance


@pytest.mark.parametrize(
    "state,amount,filled,type_,expected",
    [
        ("initial", "1", "0", "limit", "open"),
        ("active", "1", "0.5", "limit", "open"),
        ("closed", "1", "1", "limit", "closed"),
        ("closed", "1", "0.4", "limit", "canceled"),
        ("closed", "1", "0", "limit", "canceled"),
        ("closed", "1", "0.99", "market", "closed"),
    ],
)
def test_parse_order_status(api, state, amount, filled, type_, expected):
    order = api.parse_order(
        {
            "id": 1,
            "symbol": "BTC_USDT",
            "type": type_,
            "side": "buy",
            "base_amount": amount,
            "price": "100",
            "state": state,
            "dealed_base_amount": filled,
            "dealed_quote_amount": "0",
        }
    )
    assert order["status"] == expected


def test_parse_order(api):
    order = api.parse_order(
        {
            "id": 535179385,
            "symbol": "BTC_USDT",
            "type": "limit",
            "side": "sell",
            "price": "1956",
            "stop_price": None,
            "oco_target_price": None,
            "base_amount": "63.0",
            "quote_amount": "123228",
            "identifier": "abc",
            "state": "closed",
            "closed_at": "2024-07-29T14:08:34.176795+03:30",
            "created_at": "2024-07-29T14:07:34.176795+03:30",
            "dealed_base_amount": "63.0",
            "dealed_quote_amount": "123228",
            "req_to_cancel": False,
            "commission": "1000",
        }
    )
    assert order["id"] == "535179385"
    assert order["clientOrderId"] == "abc"
    assert order["symbol"] == "BTC/USDT"
    assert order["status"] == "closed"
    assert order["amount"] == 63.0
    assert order["filled"] == 63.0
    assert order["remaining"] == 0.0
    assert order["cost"] == 123228.0
    assert order["timestamp"] == 1722249454176
    # Sell fees are charged in the quote currency
    assert order["fee"] == {"cost": 1000.0, "currency": "USDT"}


def test_create_order_request(api):
    market = api.market("BTC/USDT")
    req = api._create_order_request(market, "limit", "buy", 0.123456789, 100.123, {})
    assert req == {
        "symbol": "BTC_USDT",
        "type": "limit",
        "side": "buy",
        "base_amount": "0.12345678",
        "price": "100.12",
    }
    req = api._create_order_request(market, "market", "sell", 1, None, {"clientOrderId": "x"})
    assert req == {
        "symbol": "BTC_USDT",
        "type": "market",
        "side": "sell",
        "base_amount": "1",
        "identifier": "x",
    }
    req = api._create_order_request(market, "limit", "sell", 1, 90, {"triggerPrice": 95})
    assert req["type"] == "stop_limit"
    assert req["stop_price"] == "95"
    assert req["price"] == "90"
    with pytest.raises(ccxt.InvalidOrder):
        api._create_order_request(market, "limit", "buy", 1, None, {})


def test_create_and_cancel_order(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    ccxt_bitpin._TOKEN_CACHE[api._token_cache_key()] = {
        "access": "tok",
        "access_exp": 1_800_000_000 + 900,
    }
    order_resp = {
        "id": 12345,
        "symbol": "BTC_USDT",
        "type": "limit",
        "side": "buy",
        "base_amount": "0.01",
        "price": "100",
        "state": "active",
        "created_at": "2024-06-15T10:00:00.000000+03:30",
        "dealed_base_amount": "0",
        "dealed_quote_amount": "0",
        "commission": "0",
    }
    calls = mock_fetch(
        api,
        {
            "POST odr/orders/": order_resp,
            "DELETE odr/orders/12345/": None,
            "GET odr/orders/12345/": order_resp,
        },
    )
    order = api.create_order("BTC/USDT", "limit", "buy", 0.01, 100)
    assert order["id"] == "12345"
    assert order["status"] == "open"
    assert json.loads(calls[0]["body"])["price"] == "100"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"

    cancelled = api.cancel_order("12345", "BTC/USDT")
    assert cancelled["id"] == "12345"
    assert calls[1]["method"] == "DELETE"

    assert api.fetch_order("12345", "BTC/USDT")["fee"]["currency"] == "BTC"


def test_fetch_my_trades(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    ccxt_bitpin._TOKEN_CACHE[api._token_cache_key()] = {
        "access": "tok",
        "access_exp": 1_800_000_000 + 900,
    }
    mock_fetch(
        api,
        {
            "GET odr/fills/": [
                {
                    "id": 65375268,
                    "symbol": "BTC_USDT",
                    "base_amount": "63.0",
                    "quote_amount": "123228",
                    "price": "1956",
                    "created_at": "2024-07-29T14:06:56.095051+03:30",
                    "commission": "0.1",
                    "side": "buy",
                    "commission_currency": "BTC",
                    "order_id": 535178666,
                }
            ]
        },
    )
    trades = api.fetch_my_trades("BTC/USDT")
    assert len(trades) == 1
    assert trades[0]["order"] == "535178666"
    assert trades[0]["fee"] == {"cost": 0.1, "currency": "BTC"}
    assert trades[0]["amount"] == 63.0


@pytest.mark.parametrize(
    "maker,taker",
    [
        # Live API returns fractions
        (0.0025, 0.003),
        # Docs show percentages
        (0.25, 0.3),
    ],
)
def test_parse_trading_fees(api, maker, taker):
    fees = api.parse_trading_fees(
        [{"market": 1, "symbol": "BTC_USDT", "maker": maker, "taker": taker}]
    )
    assert fees["BTC/USDT"]["maker"] == 0.0025
    assert fees["BTC/USDT"]["taker"] == 0.003


@pytest.mark.parametrize(
    "code,body,url,exception",
    [
        (401, {"detail": "x", "code": "token_not_valid"}, "/odr/orders/", BitpinTokenInvalid),
        (401, {"detail": "ip not allowed"}, "/wlt/wallets/", ccxt.AuthenticationError),
        (
            406,
            {"code": "api_credential_wrong", "detail": "x"},
            "/usr/authenticate/",
            ccxt.AuthenticationError,
        ),
        (406, {"detail": "wrong ip 1.2.3.4"}, "/usr/authenticate/", ccxt.AuthenticationError),
        (403, {"detail": "x"}, "/wlt/wallets/", ccxt.PermissionDenied),
        (429, {"detail": "x"}, "/mkt/tickers/", ccxt.RateLimitExceeded),
        (404, {"detail": "Not found."}, "/odr/orders/1/", ccxt.OrderNotFound),
        (406, {"detail": "not allowed"}, "/odr/orders/1/", ccxt.InvalidOrder),
        (400, {"detail": "insufficient balance"}, "/odr/orders/", ccxt.InsufficientFunds),
        (400, {"price": ["invalid"]}, "/odr/orders/", ccxt.InvalidOrder),
        (400, {"detail": "too many candles"}, "/mkt/candles/", ccxt.BadRequest),
    ],
)
def test_handle_errors(api, code, body, url, exception):
    with pytest.raises(exception):
        api.handle_errors(code, "", url, "GET", {}, json.dumps(body), body, {}, None)


def test_handle_errors_success(api):
    assert api.handle_errors(200, "", "/mkt/markets/", "GET", {}, "[]", [], {}, None) is None
    assert api.handle_errors(204, "", "/odr/orders/1/", "DELETE", {}, "", None, {}, None) is None


async def test_async_token_and_orders(time_machine):
    now = 1_800_000_000
    time_machine.move_to(now, tick=False)
    ex = ccxt_async.bitpin({"apiKey": "key", "secret": "secret", "enableRateLimit": False})
    try:
        ex.set_markets([ex.parse_market(m) for m in MARKETS])
        calls = []
        access = make_jwt(now + 900)

        async def _fetch(url, method="GET", headers=None, body=None):
            path = url.split("/api/v1/", 1)[1].split("?", 1)[0]
            calls.append((method, path, headers))
            if path == "usr/authenticate/":
                return {"access": access, "refresh": make_jwt(now + 86400)}
            return []

        ex.fetch = _fetch
        assert await ex.fetch_open_orders("BTC/USDT") == []
        assert [c[1] for c in calls] == ["usr/authenticate/", "odr/orders/"]
        assert calls[1][2]["Authorization"] == f"Bearer {access}"
    finally:
        await ex.close()
