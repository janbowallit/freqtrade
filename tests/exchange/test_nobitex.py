import base64
import json
from unittest.mock import MagicMock

import ccxt
import ccxt.async_support as ccxt_async
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from freqtrade.exchange import Nobitex, ccxt_nobitex
from tests.conftest import get_patched_exchange


OPTIONS = {
    "status": "ok",
    "nobitex": {
        "activeCurrencies": ["rls", "btc", "usdt", "eth"],
        "amountPrecisions": {
            "BTCUSDT": "0.000001",
            "BTCIRT": "0.000001",
            "USDTIRT": "0.01",
            "ETHUSDT": "0.00001",
            "DOGEUSDT": "1",
            "WEIRDEUR": "1",
        },
        "pricePrecisions": {
            "BTCUSDT": "0.01",
            "BTCIRT": "10",
            "USDTIRT": "10",
            "ETHUSDT": "0.01",
            "DOGEUSDT": "0.0000001",
        },
        "minOrders": {"rls": "500000", "usdt": "0.5"},
        "maxOrders": {"rls": "50000000000", "usdt": "200000"},
        "tradingFees": {
            "makerFees": ["0.25", "0.17"],
            "takerFees": ["0.25", "0.2"],
            "makerFeesTether": ["0.1", "0.095"],
            "takerFeesTether": ["0.13", "0.12"],
        },
    },
}


def make_secret() -> tuple[str, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes_raw()
    return base64.urlsafe_b64encode(raw).decode(), key


@pytest.fixture
def keypair():
    return make_secret()


@pytest.fixture
def api(keypair):
    ex = ccxt.nobitex({"apiKey": "public-key", "secret": keypair[0], "enableRateLimit": False})
    ex.markets = None
    ex.set_markets(ex.parse_markets_from_options(OPTIONS))
    return ex


def mock_fetch(ex, routes: dict):
    """Route ccxt fetch() calls by "METHOD path" to canned responses (or callables)."""
    calls = []

    def _fetch(url, method="GET", headers=None, body=None):
        path = url.split("nobitex.ir/", 1)[1].split("?", 1)[0]
        calls.append({"method": method, "path": path, "url": url, "headers": headers, "body": body})
        result = routes[f"{method} {path}"]
        return result(calls[-1]) if callable(result) else result

    ex.fetch = MagicMock(side_effect=_fetch)
    return calls


def test_nobitex_registered_in_ccxt():
    assert "nobitex" in ccxt.exchanges
    assert "nobitex" in ccxt_async.exchanges
    assert ccxt.nobitex is ccxt_nobitex.nobitex
    assert ccxt_async.nobitex is ccxt_nobitex.nobitex_async
    # Registering again must not duplicate entries
    ccxt_nobitex.register()
    assert ccxt.exchanges.count("nobitex") == 1


def test_nobitex_exchange_class(default_conf, mocker):
    exchange = get_patched_exchange(mocker, default_conf, exchange="nobitex")
    assert isinstance(exchange, Nobitex)
    assert exchange.get_option("ohlcv_candle_limit") == 500
    assert exchange.ohlcv_candle_limit("1d", "spot") == 20
    assert exchange.get_option("stoploss_on_exchange") is False


def test_parse_markets_from_options(api):
    btc = api.market("BTC/USDT")
    assert btc["id"] == "BTCUSDT"
    assert (btc["baseId"], btc["quoteId"]) == ("btc", "usdt")
    assert btc["precision"] == {"amount": 1e-06, "price": 0.01}
    assert btc["limits"]["cost"] == {"min": 0.5, "max": 200000.0}
    assert (btc["maker"], btc["taker"]) == (0.001, 0.0013)
    assert btc["active"] is True
    # IRT markets are priced in Rial - exposed as RLS like the wallet
    rls = api.market("BTC/RLS")
    assert rls["id"] == "BTCIRT"
    assert rls["quoteId"] == "rls"
    assert rls["limits"]["cost"]["min"] == 500000
    assert (rls["maker"], rls["taker"]) == (0.0025, 0.0025)
    assert api.market("USDT/RLS")["baseId"] == "usdt"
    # Currencies that aren't active are kept as inactive markets
    assert api.market("DOGE/USDT")["active"] is False
    # Unknown quote currencies are skipped
    assert "WEIRD/EUR" not in api.markets


def test_split_and_build_market_ids():
    split = ccxt_nobitex.NobitexBase._split_market_id
    assert split("BTCIRT") == ("btc", "rls")
    assert split("USDTIRT") == ("usdt", "rls")
    assert split("ETHUSDT") == ("eth", "usdt")
    assert split("USDT") is None
    assert split("BTCEUR") is None
    assert ccxt_nobitex.NobitexBase._market_id("btc", "rls") == "BTCIRT"
    assert ccxt_nobitex.NobitexBase._market_id("BTC", "USDT") == "BTCUSDT"
    assert ccxt_nobitex.NobitexBase._market_id("btc", "eur") is None


def test_sign_private_request(api, keypair, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    public = keypair[1].public_key()

    req = api.sign("market/orders/add", "private", "POST", {"type": "buy", "amount": "1"})
    assert req["url"] == "https://apiv2.nobitex.ir/market/orders/add"
    assert req["body"] == '{"type":"buy","amount":"1"}'
    headers = req["headers"]
    assert headers["Nobitex-Key"] == "public-key"
    assert headers["Nobitex-Timestamp"] == "1800000000"
    signature = base64.urlsafe_b64decode(headers["Nobitex-Signature"])
    # Raises if the signature doesn't match timestamp + METHOD + path + body
    public.verify(signature, b"1800000000POST/market/orders/add" + req["body"].encode())

    # GET requests sign the path including the query string
    req = api.sign("market/orders/list", "private", "GET", {"status": "open", "details": 2})
    assert req["url"].endswith("/market/orders/list?status=open&details=2")
    assert req["body"] is None
    signature = base64.urlsafe_b64decode(req["headers"]["Nobitex-Signature"])
    public.verify(signature, b"1800000000GET/market/orders/list?status=open&details=2")


def test_sign_public_request(api):
    req = api.sign("v3/orderbook/{symbol}", "public", "GET", {"symbol": "BTCUSDT"})
    assert req["url"] == "https://apiv2.nobitex.ir/v3/orderbook/BTCUSDT"
    assert "Nobitex-Signature" not in req["headers"]
    assert api.userAgent == {"User-Agent": "TraderBot/freqtrade"}


def test_sign_key_formats(keypair):
    # A 64 byte key (seed + public key) and unpadded base64 are accepted
    key = keypair[1]
    raw = key.private_bytes_raw() + key.public_key().public_bytes_raw()
    secret = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    ex = ccxt.nobitex({"apiKey": "k", "secret": secret})
    assert ex._signing_key().private_bytes_raw() == key.private_bytes_raw()

    for secret in ("not base64!!", base64.urlsafe_b64encode(b"short").decode()):
        ex = ccxt.nobitex({"apiKey": "k", "secret": secret})
        with pytest.raises(ccxt.AuthenticationError):
            ex._signing_key()


def test_ohlcv_request(api, time_machine):
    time_machine.move_to("2026-01-01 00:00:00 +00:00", tick=False)
    market = api.market("BTC/USDT")
    since = 1767225600  # 2026-01-01 00:00 UTC
    # 1h and above use the hourly candles, which open 30 minutes earlier
    req = api._ohlcv_request(market, "1h", since * 1000, 10, {})
    assert req == {
        "symbol": "BTCUSDT",
        "resolution": "60",
        "from": since - 1800,
        "to": since - 1800 + 10 * 3600 - 1,
    }
    # since is aligned down to the timeframe
    assert api._ohlcv_request(market, "1h", since * 1000 + 600_000, 10, {})["from"] == since - 1800
    # Limit is capped to what fits into 500 hourly candles
    req = api._ohlcv_request(market, "1d", since * 1000, 100, {})
    assert req["to"] - req["from"] + 1 == 20 * 86400
    # until limits the range
    req = api._ohlcv_request(market, "1h", since * 1000, 10, {"until": (since + 7200) * 1000})
    assert req["to"] == since + 7200 - 1800
    # Without since: the latest candles, with one extra bucket when aggregating
    req = api._ohlcv_request(market, "4h", None, 10, {})
    assert req == {"symbol": "BTCUSDT", "resolution": "60", "to": since - 1800, "countback": 44}
    # Minute timeframes are UTC-aligned and requested natively
    req = api._ohlcv_request(market, "5m", None, 12, {})
    assert req == {"symbol": "BTCUSDT", "resolution": "5", "to": since, "countback": 12}


def udf(candles: list) -> dict:
    return {
        "s": "ok",
        "t": [c[0] for c in candles],
        "o": [c[1] for c in candles],
        "h": [c[2] for c in candles],
        "l": [c[3] for c in candles],
        "c": [c[4] for c in candles],
        "v": [c[5] for c in candles],
    }


def test_fetch_ohlcv_shifts_hourly(api):
    base = 1767225600 - 1800  # native candle 23:30 -> 00:00 UTC
    response = udf([[base + i * 3600, 10 + i, 20 + i, 5 + i, 11 + i, 1] for i in range(3)])
    calls = mock_fetch(api, {"GET market/udf/history": response})
    candles = api.fetch_ohlcv("BTC/USDT", "1h", since=1767225600000)
    assert [c[0] for c in candles] == [1767225600000, 1767229200000, 1767232800000]
    assert candles[0] == [1767225600000, 10, 20, 5, 11, 1]
    assert "resolution=60" in calls[0]["url"]


def test_fetch_ohlcv_aggregates_and_drops_partial_bucket(api):
    day = 86400
    start = 1767225600 + day  # 2026-01-02
    # Native hourly candles from 2026-01-01 21:30, i.e. shifted to 22:00, 23:00, 00:00 ...
    hourly = [
        [start - 2 * 3600 - 1800 + i * 3600, 100 + i, 200 + i, 50 + i, 101 + i, 2]
        for i in range(26)
    ]
    mock_fetch(api, {"GET market/udf/history": udf(hourly)})
    candles = api.fetch_ohlcv("BTC/USDT", "1d")
    # The 2026-01-01 bucket only holds 22:00 and 23:00 - incomplete, so dropped
    assert [c[0] for c in candles] == [start * 1000]
    assert candles[0] == [start * 1000, 102, 225, 52, 126, 48]


def test_aggregate_ohlcv_same_size():
    candles = [[0, 1, 2, 0.5, 1.5, 3], [3_600_000, 1.5, 2.5, 1, 2, 4]]
    assert ccxt_nobitex.NobitexBase._aggregate_ohlcv(candles, 3_600_000, 3_600_000) == candles


def test_fetch_ohlcv_no_data_and_errors(api):
    mock_fetch(api, {"GET market/udf/history": {"s": "no_data"}})
    assert api.fetch_ohlcv("BTC/USDT", "5m") == []
    with pytest.raises(ccxt.BadRequest):
        api.handle_errors(
            200, "", "https://apiv2.nobitex.ir/market/udf/history", "GET", {}, "", {"s": "error"},
            {}, None,
        )  # fmt: skip


def test_fetch_tickers(api):
    stats = {
        "status": "ok",
        "stats": {
            "btc-usdt": {
                "isClosed": False,
                "bestSell": "79121.11",
                "bestBuy": "79000.01",
                "volumeSrc": "19.5",
                "volumeDst": "1567053.7",
                "latest": "79000",
                "dayLow": "78475",
                "dayHigh": "81647",
                "dayOpen": "81095.33",
                "dayChange": "-2.58",
            },
            "btc-rls": {"latest": "214330000010", "bestBuy": "1", "bestSell": "2"},
            "xyz-usdt": {"latest": "1"},
        },
        "global": {"binance": {}},
    }
    calls = mock_fetch(api, {"GET market/stats": stats})
    tickers = api.fetch_tickers()
    assert set(tickers) == {"BTC/USDT", "BTC/RLS"}
    ticker = tickers["BTC/USDT"]
    assert (ticker["bid"], ticker["ask"], ticker["last"]) == (79000.01, 79121.11, 79000)
    assert (ticker["baseVolume"], ticker["quoteVolume"]) == (19.5, 1567053.7)
    assert ticker["percentage"] == -2.58
    assert api.fetch_ticker("BTC/RLS")["last"] == 214330000010
    assert "srcCurrency=btc&dstCurrency=rls" in calls[-1]["url"]


def test_fetch_ticker_unknown(api):
    mock_fetch(api, {"GET market/stats": {"status": "ok", "stats": {}}})
    with pytest.raises(ccxt.BadSymbol):
        api.fetch_ticker("BTC/USDT")


def test_fetch_order_book_and_trades(api):
    book = {
        "status": "ok",
        "lastUpdate": 1793311307489,
        "lastTradePrice": "79000",
        "bids": [["79002", "0.001"], ["79001", "0.002"]],
        "asks": [["79121.11", "0.00044"], ["79122", "1"]],
    }
    trades = {
        "status": "ok",
        "trades": [
            {"time": 1793311276148, "price": "79000", "volume": "0.000455", "type": "sell"},
            {"time": 1793311263896, "price": "79001", "volume": "0.0033", "type": "buy"},
        ],
    }
    mock_fetch(api, {"GET v3/orderbook/BTCUSDT": book, "GET v2/trades/BTCUSDT": trades})
    orderbook = api.fetch_order_book("BTC/USDT", 1)
    assert orderbook["bids"] == [[79002, 0.001]]
    assert orderbook["asks"] == [[79121.11, 0.00044]]
    assert orderbook["timestamp"] == 1793311307489
    result = api.fetch_trades("BTC/USDT")
    # Sorted oldest first
    assert [t["timestamp"] for t in result] == [1793311263896, 1793311276148]
    assert result[1]["side"] == "sell"
    assert result[1]["amount"] == 0.000455
    assert result[1]["cost"] == pytest.approx(79000 * 0.000455)


def test_fetch_balance(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    response = {
        "status": "ok",
        "wallets": [
            {"currency": "rls", "balance": "1000", "blockedBalance": "100", "activeBalance": "900"},
            {"currency": "usdt", "balance": "50.5", "blockedBalance": "0", "activeBalance": "50.5"},
        ],
    }
    calls = mock_fetch(api, {"POST users/wallets/list": response})
    balance = api.fetch_balance()
    assert balance["RLS"] == {"free": 900, "used": 100, "total": 1000}
    assert balance["USDT"]["free"] == 50.5
    assert json.loads(calls[0]["body"]) == {"type": "spot"}
    assert "Nobitex-Signature" in calls[0]["headers"]


ORDER = {
    "id": 5684,
    "type": "buy",
    "execution": "Limit",
    "tradeType": "Spot",
    "market": "BTC-USDT",
    "srcCurrency": "Bitcoin",
    "dstCurrency": "Tether",
    "price": "79000",
    "amount": "0.01",
    "totalPrice": "395",
    "totalOrderPrice": "790",
    "matchedAmount": "0.005",
    "unmatchedAmount": "0.005",
    "status": "Active",
    "partial": True,
    "fee": "0.000005",
    "created_at": "2026-10-29T11:32:38.326809+00:00",
    "averagePrice": "79000",
    "clientOrderId": "ft-1",
}


@pytest.mark.parametrize(
    "status,expected",
    [
        ("New", "open"),
        ("Active", "open"),
        ("Inactive", "open"),
        ("Done", "closed"),
        ("Canceled", "canceled"),
        (None, None),
    ],
)
def test_parse_order_status(api, status, expected):
    assert api.parse_order_status(status) == expected


def test_parse_order(api):
    order = api.parse_order(ORDER)
    assert order["id"] == "5684"
    assert order["clientOrderId"] == "ft-1"
    assert order["symbol"] == "BTC/USDT"
    assert (order["type"], order["side"], order["status"]) == ("limit", "buy", "open")
    assert (order["price"], order["amount"], order["filled"]) == (79000, 0.01, 0.005)
    assert (order["remaining"], order["cost"], order["average"]) == (0.005, 395, 79000)
    assert order["timestamp"] == 1793273558326
    # Buy fees are paid in the base currency
    assert order["fee"] == {"cost": 0.000005, "currency": "BTC"}

    market_sell = api.parse_order(
        {
            **ORDER,
            "type": "sell",
            "execution": "Market",
            "market": "USDT-RLS",
            "price": "market",
            "averagePrice": "0",
            "status": "Canceled",
            "fee": "27257.7",
        }
    )
    assert market_sell["symbol"] == "USDT/RLS"
    assert market_sell["type"] == "market"
    # "market" is no price - ccxt fills it in from the average fill price
    assert market_sell["price"] == market_sell["average"] == 79000
    assert market_sell["status"] == "canceled"
    # Sell fees are paid in the quote currency
    assert market_sell["fee"] == {"cost": 27257.7, "currency": "RLS"}

    stop = api.parse_order({**ORDER, "execution": "StopLimit", "param1": "78000"})
    assert stop["type"] == "limit"
    assert stop["triggerPrice"] == 78000


def test_create_order_request(api):
    market = api.market("BTC/USDT")
    req = api._create_order_request(
        market, "limit", "buy", 0.0123456789, 79000.123, {"clientOrderId": "ft-1"}
    )
    assert req == {
        "type": "buy",
        "srcCurrency": "btc",
        "dstCurrency": "usdt",
        "amount": "0.012345",
        "execution": "limit",
        "price": "79000.12",
        "clientOrderId": "ft-1",
    }
    req = api._create_order_request(market, "market", "sell", 0.01, None, {})
    assert req == {
        "type": "sell",
        "srcCurrency": "btc",
        "dstCurrency": "usdt",
        "amount": "0.01",
        "execution": "market",
    }
    # Market orders may carry a price to limit the fill range
    assert api._create_order_request(market, "market", "buy", 0.01, 79000, {})["price"] == "79000"
    req = api._create_order_request(market, "limit", "sell", 0.01, 77000, {"stopPrice": 78000})
    assert (req["execution"], req["stopPrice"], req["price"]) == ("stop_limit", "78000", "77000")
    req = api._create_order_request(market, "market", "sell", 0.01, None, {"triggerPrice": 78000})
    assert req["execution"] == "stop_market"
    with pytest.raises(ccxt.InvalidOrder):
        api._create_order_request(market, "limit", "buy", 0.01, None, {})


def test_create_cancel_and_fetch_order(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    calls = mock_fetch(
        api,
        {
            "POST market/orders/add": {"status": "ok", "order": ORDER},
            "POST market/orders/status": {"status": "ok", "order": {**ORDER, "status": "Done"}},
            "POST market/orders/update-status": {
                "status": "ok",
                "updatedStatus": "Canceled",
                "order": {**ORDER, "status": "Canceled"},
            },
        },
    )
    order = api.create_order("BTC/USDT", "limit", "buy", 0.01, 79000)
    assert order["id"] == "5684"
    assert json.loads(calls[0]["body"])["execution"] == "limit"
    assert api.fetch_order("5684", "BTC/USDT")["status"] == "closed"
    assert json.loads(calls[1]["body"]) == {"id": 5684}
    assert api.cancel_order("5684", "BTC/USDT")["status"] == "canceled"
    assert json.loads(calls[2]["body"]) == {"order": 5684, "status": "canceled"}


def test_fetch_orders(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    calls = mock_fetch(
        api, {"GET market/orders/list": {"status": "ok", "orders": [ORDER], "hasNext": False}}
    )
    orders = api.fetch_open_orders("BTC/USDT")
    assert [o["id"] for o in orders] == ["5684"]
    assert calls[0]["url"].endswith(
        "market/orders/list?status=open&details=2&srcCurrency=btc&dstCurrency=usdt&pageSize=100"
    )
    api.fetch_closed_orders(limit=500)
    assert "status=close&details=2&pageSize=100" in calls[1]["url"]
    api.fetch_orders()
    assert "status=all" in calls[2]["url"]


def test_fetch_my_trades(api, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    response = {
        "status": "ok",
        "trades": [
            {
                "id": 123412,
                "orderId": 1231222,
                "srcCurrency": "Tether",
                "dstCurrency": "﷼",
                "market": "USDT-RLS",
                "timestamp": "2022-07-05T09:57:38.560820+00:00",
                "type": "sell",
                "price": "316800",
                "amount": "57.3605",
                "total": "18171806.4",
                "fee": "27257.7096",
            }
        ],
        "hasNext": False,
    }
    calls = mock_fetch(api, {"GET market/trades/list": response})
    trades = api.fetch_my_trades("USDT/RLS")
    assert calls[0]["url"].endswith("srcCurrency=usdt&dstCurrency=rls&pageSize=100")
    trade = trades[0]
    assert (trade["id"], trade["order"], trade["symbol"]) == ("123412", "1231222", "USDT/RLS")
    assert (trade["amount"], trade["cost"]) == (57.3605, 18171806.4)
    assert trade["fee"] == {"cost": 27257.7096, "currency": "RLS"}


@pytest.mark.parametrize(
    "code,body,url,exception",
    [
        (429, {"status": "failed", "code": "TooManyRequests", "backOff": 12}, "/market/stats",
         ccxt.RateLimitExceeded),
        (200, {"status": "failed", "code": "TooManyRequests"}, "/market/stats",
         ccxt.RateLimitExceeded),
        (401, {"detail": "Invalid signature"}, "/users/wallets/list", ccxt.AuthenticationError),
        (403, {"detail": "Permission denied"}, "/market/orders/add", ccxt.PermissionDenied),
        (404, {"detail": "Not found"}, "/market/orders/status", ccxt.OrderNotFound),
        (200, {"status": "failed", "code": "OverValueOrder"}, "/market/orders/add",
         ccxt.InsufficientFunds),
        (200, {"status": "failed", "code": "SmallOrder"}, "/market/orders/add", ccxt.InvalidOrder),
        (400, {"status": "failed", "code": "InvalidSymbol"}, "/v3/orderbook/XX", ccxt.BadSymbol),
        (200, {"status": "failed", "updatedStatus": "Done", "order": {}},
         "/market/orders/update-status", ccxt.InvalidOrder),
        (200, {"status": "failed", "code": "OrderNotFound"}, "/market/orders/status",
         ccxt.OrderNotFound),
        (200, {"status": "failed", "code": "Unknown"}, "/market/orders/list", ccxt.ExchangeError),
        (422, {"status": "failed"}, "/users/wallets/list", ccxt.ExchangeError),
    ],
)  # fmt: skip
def test_handle_errors(api, code, body, url, exception):
    with pytest.raises(exception):
        api.handle_errors(code, "", url, "POST", {}, json.dumps(body), body, {}, None)


def test_handle_errors_success(api):
    ok = {"status": "ok"}
    assert api.handle_errors(200, "", "/market/stats", "GET", {}, "{}", ok, {}, None) is None
    assert api.handle_errors(200, "", "/x", "GET", {}, "{}", {"s": "ok"}, {}, None) is None
    # 5xx are left to ccxt's http status handling
    assert api.handle_errors(502, "", "/market/stats", "GET", {}, "", None, {}, None) is None


async def test_async_orders_and_ohlcv(keypair, time_machine):
    time_machine.move_to(1_800_000_000, tick=False)
    ex = ccxt_async.nobitex({"apiKey": "k", "secret": keypair[0], "enableRateLimit": False})
    try:
        ex.set_markets(ex.parse_markets_from_options(OPTIONS))
        calls = []

        async def _fetch(url, method="GET", headers=None, body=None):
            path = url.split("nobitex.ir/", 1)[1].split("?", 1)[0]
            calls.append((method, path, headers))
            if path == "market/orders/list":
                return {"status": "ok", "orders": [ORDER]}
            if path == "market/orders/add":
                return {"status": "ok", "order": ORDER}
            return {"s": "ok", "t": [1_799_998_200], "o": [1], "h": [2], "l": [0.5], "c": [1.5],
                    "v": [3]}  # fmt: skip

        ex.fetch = _fetch
        assert [o["id"] for o in await ex.fetch_open_orders("BTC/USDT")] == ["5684"]
        assert (await ex.create_order("BTC/USDT", "limit", "buy", 0.01, 79000))["id"] == "5684"
        candles = await ex.fetch_ohlcv("BTC/USDT", "1h")
        assert candles == [[1_800_000_000_000, 1, 2, 0.5, 1.5, 3]]
        assert calls[0][2]["Nobitex-Key"] == "k"
        assert "Nobitex-Signature" not in calls[2][2]
    finally:
        await ex.close()
