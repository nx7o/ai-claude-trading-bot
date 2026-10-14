"""Offline tests for exchange.py. A fake ccxt client stands in for Bybit."""

import ccxt
import pytest

import exchange

BTC = "BTC/USDT:USDT"
MARKETS = {
    BTC: {
        "id": "BTCUSDT", "symbol": BTC, "swap": True, "linear": True,
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}, "leverage": {"max": 100}},
    },
}
UTA_BALANCE = {"info": {"result": {"list": [{
    "accountType": "UNIFIED", "totalEquity": "10500.5", "totalAvailableBalance": "9800.25",
    "totalWalletBalance": "10400", "totalPerpUPL": "100.5", "totalInitialMargin": "700.25",
    "totalMaintenanceMargin": "35", "coin": [],
}]}}}


def make_position(side="long", contracts=0.01, position_idx="0"):
    return {
        "symbol": BTC, "side": side, "contracts": contracts, "entryPrice": 60000.0,
        "markPrice": 61000.0, "unrealizedPnl": 10.0, "liquidationPrice": 30500.0,
        "leverage": 2.0, "notional": 610.0, "stopLossPrice": 58800.0, "takeProfitPrice": None,
        "info": {"symbol": "BTCUSDT", "positionIdx": position_idx},
    }


class FakeBybit:
    def __init__(self, positions=None, leverage_error=None):
        self.markets = MARKETS
        self.positions = positions or []
        self.leverage_error = leverage_error
        self.orders = []
        self.leverage_calls = []

    def load_markets(self):
        return self.markets

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.1f}"

    def fetch_ticker(self, symbol):
        return {"markPrice": 60000.0, "last": 60010.0}

    def fetch_market_leverage_tiers(self, symbol):
        return [{"maxNotional": 2_000_000, "maintenanceMarginRate": 0.005}]

    def set_leverage(self, leverage, symbol):
        self.leverage_calls.append((leverage, symbol))
        if self.leverage_error:
            raise self.leverage_error

    def create_order(self, symbol, type, side, amount, price, params):
        self.orders.append({"symbol": symbol, "type": type, "side": side, "amount": amount, "params": params})
        return {"id": f"order-{len(self.orders)}"}

    def fetch_positions(self, symbols=None, params=None):
        if params and params.get("settleCoin") == "USDC":
            return []
        return self.positions

    def fetch_balance(self):
        return UTA_BALANCE

    def cancel_all_orders(self, symbol):
        return [{"id": "a"}, {"id": "b"}]


@pytest.fixture
def fake(monkeypatch):
    client = FakeBybit()
    monkeypatch.setattr(exchange, "_client", client)
    monkeypatch.setattr(exchange, "_env_name", "testnet")
    monkeypatch.setattr(exchange, "_risk_tiers", {})
    return client


def write_env(tmp_path, text):
    env_file = tmp_path / ".env"
    env_file.write_text(text)
    return env_file


# --- configuration -----------------------------------------------------------

def test_missing_env_file_raises_config_error(tmp_path):
    with pytest.raises(exchange.ConfigError, match="not found"):
        exchange.load_config(tmp_path / ".env")


def test_placeholder_credentials_rejected(tmp_path):
    env_file = write_env(tmp_path, "BYBIT_API_KEY=your_api_key_here\nBYBIT_API_SECRET=abc\n")
    with pytest.raises(exchange.ConfigError, match="BYBIT_API_KEY"):
        exchange.load_config(env_file)


def test_invalid_env_rejected(tmp_path):
    env_file = write_env(tmp_path, "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\nBYBIT_ENV=prod\n")
    with pytest.raises(exchange.ConfigError, match="BYBIT_ENV"):
        exchange.load_config(env_file)


def test_config_defaults_to_testnet_and_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "from-process-env")
    env_file = write_env(tmp_path, "BYBIT_API_KEY=file-key\nBYBIT_API_SECRET=file-secret\n")
    assert exchange.load_config(env_file) == {"api_key": "file-key", "api_secret": "file-secret", "env": "testnet"}


def test_testnet_client_points_at_testnet():
    client = exchange._build_client({"api_key": "k", "api_secret": "s", "env": "testnet"})
    assert client.urls["api"]["private"] == "https://api-testnet.{hostname}"
    assert client.options["defaultSubType"] == "linear"


def test_demo_client_points_at_demo():
    client = exchange._build_client({"api_key": "k", "api_secret": "s", "env": "demo"})
    assert client.urls["api"]["private"] == "https://api-demo.{hostname}"


# --- error handling ----------------------------------------------------------

@pytest.mark.parametrize("ccxt_error, expected", [
    (ccxt.AuthenticationError("10003 API key is invalid"), exchange.InvalidCredentials),
    (ccxt.PermissionDenied("10010 unmatched IP"), exchange.InvalidCredentials),
    (ccxt.RequestTimeout("timed out"), exchange.ConnectionFailed),
    (ccxt.ExchangeNotAvailable("maintenance"), exchange.ConnectionFailed),
    (ccxt.InsufficientFunds("110007 not enough"), exchange.OrderRejected),
    (ccxt.ExchangeError("something else"), exchange.ExchangeClientError),
])
def test_ccxt_errors_are_translated(fake, monkeypatch, ccxt_error, expected):
    def boom():
        raise ccxt_error
    monkeypatch.setattr(fake, "fetch_balance", boom)
    with pytest.raises(expected):
        exchange.get_balance()


def test_connect_retries_then_fails(fake, monkeypatch):
    calls = []
    def unreachable():
        calls.append(1)
        raise ccxt.NetworkError("connection refused")
    monkeypatch.setattr(fake, "fetch_time", unreachable, raising=False)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    with pytest.raises(exchange.ConnectionFailed, match="after 3 attempts"):
        exchange.connect()
    assert len(calls) == 3


def test_connect_reports_bad_credentials(fake, monkeypatch):
    monkeypatch.setattr(fake, "fetch_time", lambda: 0, raising=False)
    def rejected():
        raise ccxt.AuthenticationError('bybit {"retCode":10003,"retMsg":"API key is invalid."}')
    monkeypatch.setattr(fake, "fetch_balance", rejected)
    with pytest.raises(exchange.InvalidCredentials, match="testnet.bybit.com"):
        exchange.connect()


# --- balance and positions ---------------------------------------------------

def test_get_balance_unified_account(fake):
    balance = exchange.get_balance()
    assert balance["equity"] == 10500.5
    assert balance["available_margin"] == 9800.25
    assert balance["currency"] == "USD"


def test_get_balance_falls_back_to_usdt_coin(fake, monkeypatch):
    raw = {"info": {"result": {"list": [{
        "accountType": "UNIFIED", "totalEquity": "", "totalAvailableBalance": "",
        "coin": [{"coin": "USDT", "equity": "1000", "walletBalance": "990",
                  "totalPositionIM": "100", "totalOrderIM": "40", "locked": "0"}],
    }]}}}
    monkeypatch.setattr(fake, "fetch_balance", lambda: raw)
    balance = exchange.get_balance()
    assert (balance["equity"], balance["available_margin"], balance["currency"]) == (1000.0, 850.0, "USDT")


def test_get_positions_skips_empty_and_maps_fields(fake):
    fake.positions = [make_position(), make_position(contracts=0)]
    positions = exchange.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert (p["symbol"], p["side"], p["size"]) == ("BTCUSDT", "long", 0.01)
    assert (p["entry_price"], p["unrealized_pnl"], p["liquidation_price"]) == (60000.0, 10.0, 30500.0)


# --- orders ------------------------------------------------------------------

def test_sl_tp_prices_long_and_short():
    assert exchange.sl_tp_prices("buy", 100.0, 2, 4) == pytest.approx((98.0, 104.0))
    assert exchange.sl_tp_prices("short", 100.0, 2, 4) == pytest.approx((102.0, 96.0))


def test_place_order_attaches_sl_tp_and_sets_leverage(fake):
    result = exchange.place_order("BTCUSDT", "long", 0.0104, 5, 2.0, 4.0)
    order = fake.orders[0]
    assert fake.leverage_calls == [(5, BTC)]
    assert (order["symbol"], order["type"], order["side"], order["amount"]) == (BTC, "market", "buy", 0.01)
    assert order["params"]["stopLoss"] == {"triggerPrice": 58800.0}
    assert order["params"]["takeProfit"] == {"triggerPrice": 62400.0}
    assert order["params"]["slTriggerBy"] == "MarkPrice"
    assert result["client_order_id"] == order["params"]["clientOrderId"]


def test_place_order_short_prices(fake):
    exchange.place_order("BTC/USDT:USDT", "sell", 0.01, 3, 1.5, 3.0)
    params = fake.orders[0]["params"]
    assert params["stopLoss"] == {"triggerPrice": 60900.0}
    assert params["takeProfit"] == {"triggerPrice": 58200.0}


def test_place_order_ignores_leverage_not_modified(fake):
    fake.leverage_error = ccxt.BadRequest('bybit {"retCode":110043,"retMsg":"leverage not modified"}')
    exchange.place_order("BTCUSDT", "buy", 0.01, 5, 2.0, 4.0)
    assert len(fake.orders) == 1


def test_place_order_rejects_stop_beyond_liquidation(fake):
    # 20x: liquidation ~ (5% - 0.5% MMR) = 4.5% away; with the 80% buffer the max stop is 3.6%.
    with pytest.raises(exchange.OrderRejected, match="liquidation"):
        exchange.place_order("BTCUSDT", "buy", 0.01, 20, 4.0, 8.0)
    assert fake.orders == []


@pytest.mark.parametrize("kwargs, error", [
    ({"side": "up"}, ValueError),
    ({"size": 0}, ValueError),
    ({"size": 0.0001}, exchange.OrderRejected),  # below 0.001 minimum
    ({"leverage": 0.5}, ValueError),
    ({"leverage": 125}, exchange.OrderRejected),  # above 100x maximum
    ({"stop_loss_pct": 0}, ValueError),
    ({"symbol": "BTCUSD"}, ValueError),
])
def test_place_order_validation(fake, kwargs, error):
    args = {"symbol": "BTCUSDT", "side": "buy", "size": 0.01, "leverage": 5,
            "stop_loss_pct": 2.0, "take_profit_pct": 4.0, **kwargs}
    with pytest.raises(error):
        exchange.place_order(**args)
    assert fake.orders == []


def test_close_position_sends_reduce_only_opposite_side(fake):
    fake.positions = [make_position(side="short", contracts=0.02)]
    closed = exchange.close_position("BTCUSDT")
    order = fake.orders[0]
    assert (order["side"], order["amount"], order["params"]) == ("buy", 0.02, {"reduceOnly": True})
    assert closed[0]["closed_position_side"] == "short"


def test_close_position_hedge_mode_targets_leg(fake):
    fake.positions = [make_position(side="long", position_idx="1")]
    exchange.close_position("BTCUSDT")
    assert fake.orders[0]["params"] == {"reduceOnly": True, "positionIdx": 1}


def test_close_position_with_nothing_open(fake):
    assert exchange.close_position("BTCUSDT") == []
    assert fake.orders == []


def test_cancel_all_orders_returns_ids(fake):
    assert exchange.cancel_all_orders("BTCUSDT") == ["a", "b"]
