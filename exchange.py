"""Bybit exchange client built on ccxt.

Credentials are read from the .env file next to this module and nowhere else
(see .env.example). The default environment is Bybit testnet.

Conventions used by every function in this module:
- symbol: Bybit id ("BTCUSDT") or ccxt unified symbol ("BTC/USDT:USDT").
  Only linear (USDT/USDC-settled) perpetuals are supported.
- size: quantity in the base coin (0.01 means 0.01 BTC), not USD.
- stop_loss_pct / take_profit_pct: percent price move from the entry
  reference price, so 2.0 means 2%. They are NOT percent of margin (ROE).
- One-way position mode (Bybit's default) is assumed when opening positions.

Run `python exchange.py` for a read-only connection test.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import ccxt
from dotenv import dotenv_values

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parent / ".env"
VALID_ENVS = ("testnet", "demo", "mainnet")
SETTLE_COINS = ("USDT", "USDC")
CONNECT_RETRIES = 3
# Used when the risk-limit tiers can't be fetched. Bybit's tier-1 MMR is
# 0.5% for BTC/ETH and higher for most altcoins, so 1% is conservative.
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.01
# A stop-loss must trigger within this fraction of the estimated distance to
# liquidation, leaving room for slippage and fees.
LIQUIDATION_BUFFER = 0.8
# Bybit error code for "leverage not modified" (leverage already at target).
LEVERAGE_NOT_MODIFIED = "110043"


class ExchangeClientError(Exception):
    """Base class for every error raised by this module."""


class ConfigError(ExchangeClientError):
    """The .env file is missing or incomplete."""


class ConnectionFailed(ExchangeClientError):
    """Bybit could not be reached (network, timeout, outage, rate limit)."""


class InvalidCredentials(ExchangeClientError):
    """Bybit rejected the API key, signature, permissions or IP."""


class OrderRejected(ExchangeClientError):
    """The order failed validation or was refused by Bybit."""


_client: ccxt.bybit | None = None
_env_name: str | None = None
_risk_tiers: dict[str, list[dict]] = {}


def load_config(env_file: Path = ENV_FILE) -> dict[str, str]:
    """Read and validate Bybit settings from the .env file only."""
    if not env_file.is_file():
        raise ConfigError(
            f"{env_file} not found. Copy .env.example to .env and fill in "
            "your Bybit testnet API key and secret."
        )
    values = dotenv_values(env_file)
    api_key = (values.get("BYBIT_API_KEY") or "").strip()
    api_secret = (values.get("BYBIT_API_SECRET") or "").strip()
    env = (values.get("BYBIT_ENV") or "testnet").strip().lower()

    missing = [
        name
        for name, value in (("BYBIT_API_KEY", api_key), ("BYBIT_API_SECRET", api_secret))
        if not value or value.lower().startswith("your_")
    ]
    if missing:
        raise ConfigError(f"Missing or placeholder value for {', '.join(missing)} in {env_file}")
    if env not in VALID_ENVS:
        raise ConfigError(f"BYBIT_ENV must be one of {', '.join(VALID_ENVS)}, got {env!r}")
    return {"api_key": api_key, "api_secret": api_secret, "env": env}


def _build_client(config: dict[str, str]) -> ccxt.bybit:
    client = ccxt.bybit({
        "apiKey": config["api_key"],
        "secret": config["api_secret"],
        "enableRateLimit": True,
        "timeout": 15_000,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",
            # Sync with server time so signed requests aren't rejected for clock drift.
            "adjustForTimeDifference": True,
            "recvWindow": 10_000,
            # Only load linear markets; spot/inverse/options add seconds to startup.
            "fetchMarkets": {"types": ["linear"]},
        },
    })
    env = config["env"]
    if env == "testnet":
        client.set_sandbox_mode(True)  # api-testnet.bybit.com
    elif env == "demo":
        client.enable_demo_trading(True)  # api-demo.bybit.com
    else:
        log.warning("BYBIT_ENV=mainnet: orders will use REAL funds")
    return client


def _get_client() -> ccxt.bybit:
    global _client, _env_name
    if _client is None:
        config = load_config()
        _client = _build_client(config)
        _env_name = config["env"]
    return _client


def _credentials_hint(error: Exception) -> str:
    where = {
        "testnet": "testnet.bybit.com (mainnet keys do not work on testnet)",
        "demo": "the Demo Trading tab of your bybit.com account",
        "mainnet": "bybit.com",
    }.get(_env_name or "testnet")
    return (
        f"Bybit rejected the API credentials: {error}. Check that BYBIT_API_KEY and "
        f"BYBIT_API_SECRET in .env were created on {where}, have not expired, have "
        "Contract 'Orders' and 'Positions' permissions, and that any IP whitelist "
        "includes this machine."
    )


def _describe(error: Exception) -> str:
    """ccxt network errors only name the URL; append the underlying cause."""
    cause = error.__cause__ or error.__context__
    return f"{error} ({cause})" if cause else str(error)


def _translate_errors(func):
    """Convert ccxt exceptions into this module's exception types."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ExchangeClientError:
            raise
        except ccxt.AuthenticationError as e:  # also covers PermissionDenied, AccountSuspended
            raise InvalidCredentials(_credentials_hint(e)) from e
        except ccxt.InvalidNonce as e:
            raise ConnectionFailed(
                f"Bybit rejected the request timestamp; check the system clock: {e}"
            ) from e
        except ccxt.NetworkError as e:  # timeouts, DNS, outages, rate limits
            raise ConnectionFailed(f"Could not reach Bybit {_env_name}: {_describe(e)}") from e
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:
            raise OrderRejected(str(e)) from e
        except ccxt.BaseError as e:
            raise ExchangeClientError(str(e)) from e

    return wrapper


def _num(value: Any) -> float | None:
    """Parse Bybit's numeric strings; empty strings become None."""
    if value in (None, ""):
        return None
    return float(value)


def _resolve_market(client: ccxt.bybit, symbol: str) -> dict:
    client.load_markets()
    market = client.markets.get(symbol)
    if market is None:
        market = next(
            (m for m in client.markets.values() if m["id"] == symbol.upper() and m.get("swap")),
            None,
        )
    if market is None or not (market.get("swap") and market.get("linear")):
        raise ValueError(f"Unknown or unsupported symbol {symbol!r}; expected a linear perpetual like BTCUSDT")
    return market


@_translate_errors
def connect(retries: int = CONNECT_RETRIES) -> dict:
    """Verify Bybit is reachable and the API credentials are accepted.

    Reachability is checked with the public server-time endpoint (retried with
    exponential backoff), then credentials are checked with a signed
    wallet-balance request. Raises ConfigError, ConnectionFailed or
    InvalidCredentials on failure.
    """
    client = _get_client()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            server_time = client.fetch_time()
            break
        except ccxt.NetworkError as e:
            last_error = e
            log.warning("Bybit %s unreachable (attempt %d/%d): %s", _env_name, attempt, retries, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    else:
        raise ConnectionFailed(
            f"Bybit {_env_name} unreachable after {retries} attempts: {_describe(last_error)}"
        ) from last_error

    client.load_markets()
    balance = _parse_balance(client.fetch_balance())  # signed request: proves the credentials
    log.info("Connected to Bybit %s (%d linear markets)", _env_name, len(client.markets))
    return {
        "env": _env_name,
        "api_url": client.urls["api"]["private"].replace("{hostname}", client.hostname),
        "server_time": client.iso8601(server_time),
        "clock_skew_ms": server_time - client.milliseconds(),
        "linear_markets": len(client.markets),
        "equity": balance["equity"],
    }


def _parse_balance(raw: dict) -> dict:
    accounts = ((raw.get("info") or {}).get("result") or {}).get("list") or []
    account = accounts[0] if accounts else {}
    equity = _num(account.get("totalEquity"))
    available = _num(account.get("totalAvailableBalance"))
    if equity is not None and available is not None:
        # Unified Trading Account: account-wide totals, valued in USD.
        return {
            "currency": "USD",
            "equity": equity,
            "available_margin": available,
            "wallet_balance": _num(account.get("totalWalletBalance")),
            "unrealized_pnl": _num(account.get("totalPerpUPL")),
            "initial_margin": _num(account.get("totalInitialMargin")),
            "maintenance_margin": _num(account.get("totalMaintenanceMargin")),
            "account_type": account.get("accountType"),
        }

    # Account-level totals are blank (e.g. isolated margin mode): fall back to the USDT coin.
    usdt = next((c for c in account.get("coin") or [] if c.get("coin") == "USDT"), None)
    if usdt is None:
        raise ExchangeClientError(f"Unrecognised Bybit balance response: {raw.get('info')}")
    wallet = _num(usdt.get("walletBalance")) or 0.0
    used = sum(_num(usdt.get(k)) or 0.0 for k in ("totalPositionIM", "totalOrderIM", "locked"))
    return {
        "currency": "USDT",
        "equity": _num(usdt.get("equity")) or 0.0,
        "available_margin": max(wallet - used, 0.0),
        "wallet_balance": wallet,
        "unrealized_pnl": _num(usdt.get("unrealisedPnl")),
        "initial_margin": _num(usdt.get("totalPositionIM")),
        "maintenance_margin": _num(usdt.get("totalPositionMM")),
        "account_type": account.get("accountType"),
    }


@_translate_errors
def get_balance() -> dict:
    """Return current equity and available margin.

    On a Unified Trading Account the figures are account-wide in USD
    (totalEquity / totalAvailableBalance); otherwise they come from the USDT wallet.
    """
    return _parse_balance(_get_client().fetch_balance())


def _format_position(position: dict) -> dict:
    return {
        "symbol": position["info"].get("symbol"),
        "unified_symbol": position["symbol"],
        "side": position["side"],
        "size": position["contracts"],
        "entry_price": position["entryPrice"],
        "mark_price": position["markPrice"],
        "unrealized_pnl": position["unrealizedPnl"] or 0.0,
        # None when Bybit reports no liquidation price (e.g. cross margin with ample equity).
        "liquidation_price": position["liquidationPrice"],
        "leverage": position["leverage"],
        "notional": position["notional"],
        "stop_loss": position.get("stopLossPrice") or None,
        "take_profit": position.get("takeProfitPrice") or None,
    }


@_translate_errors
def get_positions() -> list[dict]:
    """Return all open linear positions with entry price, size, P&L and liquidation price."""
    client = _get_client()
    positions = []
    for settle in SETTLE_COINS:
        positions += client.fetch_positions(params={"settleCoin": settle})
    return [_format_position(p) for p in positions if (p.get("contracts") or 0) > 0]


def _normalize_side(side: str) -> str:
    normalized = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}.get(str(side).lower())
    if normalized is None:
        raise ValueError(f"side must be 'buy'/'long' or 'sell'/'short', got {side!r}")
    return normalized


def _reference_price(client: ccxt.bybit, market: dict) -> float:
    ticker = client.fetch_ticker(market["symbol"])
    # Mark price: SL/TP trigger on it, and it resists testnet's thin-book spikes.
    price = _num(ticker.get("markPrice")) or _num(ticker.get("last"))
    if not price:
        raise ExchangeClientError(f"No price available for {market['symbol']}")
    return price


def _maintenance_margin_rate(client: ccxt.bybit, market: dict, notional: float) -> float:
    symbol = market["symbol"]
    if symbol not in _risk_tiers:
        try:
            _risk_tiers[symbol] = client.fetch_market_leverage_tiers(symbol)
        except ccxt.BaseError as e:
            log.warning(
                "Could not fetch risk tiers for %s (%s); assuming %.2f%% maintenance margin",
                symbol, e, DEFAULT_MAINTENANCE_MARGIN_RATE * 100,
            )
            return DEFAULT_MAINTENANCE_MARGIN_RATE
    tiers = _risk_tiers[symbol]
    for tier in tiers:
        if tier.get("maxNotional") is None or notional <= tier["maxNotional"]:
            return tier.get("maintenanceMarginRate") or DEFAULT_MAINTENANCE_MARGIN_RATE
    return (tiers[-1].get("maintenanceMarginRate") if tiers else None) or DEFAULT_MAINTENANCE_MARGIN_RATE


def _set_leverage(client: ccxt.bybit, market: dict, leverage: float) -> None:
    try:
        client.set_leverage(leverage, market["symbol"])
    except ccxt.BadRequest as e:
        if LEVERAGE_NOT_MODIFIED not in str(e):
            raise


def sl_tp_prices(side: str, entry: float, stop_loss_pct: float, take_profit_pct: float) -> tuple[float, float]:
    """Return (stop_loss, take_profit) prices for a percent move from entry."""
    sl_move, tp_move = stop_loss_pct / 100, take_profit_pct / 100
    if _normalize_side(side) == "buy":
        return entry * (1 - sl_move), entry * (1 + tp_move)
    return entry * (1 + sl_move), entry * (1 - tp_move)


@_translate_errors
def place_order(
    symbol: str,
    side: str,
    size: float,
    leverage: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> dict:
    """Open a market position with an exchange-side stop-loss and take-profit.

    The stop-loss and take-profit are attached to the order itself, so the
    position is never open without protection. Both are priced from the
    current mark price and trigger on the mark price. Raises ValueError for bad
    arguments and OrderRejected if the order is unsafe or Bybit refuses it.

    Network errors are NOT retried here: a timeout can hide an order that did
    fill, so check get_positions() before resubmitting.
    """
    order_side = _normalize_side(side)
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    if leverage < 1:
        raise ValueError(f"leverage must be at least 1, got {leverage}")
    for name, pct in (("stop_loss_pct", stop_loss_pct), ("take_profit_pct", take_profit_pct)):
        if not 0 < pct < 100:
            raise ValueError(f"{name} must be between 0 and 100 (percent), got {pct}")

    client = _get_client()
    market = _resolve_market(client, symbol)
    unified = market["symbol"]

    max_leverage = (market.get("limits", {}).get("leverage") or {}).get("max")
    if max_leverage and leverage > max_leverage:
        raise OrderRejected(f"{unified} allows at most {max_leverage}x leverage, got {leverage}x")

    amount = float(client.amount_to_precision(unified, size))
    min_amount = (market.get("limits", {}).get("amount") or {}).get("min")
    if amount <= 0 or (min_amount and amount < min_amount):
        raise OrderRejected(f"size {size} is below the {unified} minimum of {min_amount}")

    price = _reference_price(client, market)
    notional = amount * price
    min_cost = (market.get("limits", {}).get("cost") or {}).get("min")
    if min_cost and notional < min_cost:
        raise OrderRejected(f"order value {notional:.2f} is below the {unified} minimum of {min_cost}")

    # Isolated-margin estimate of the price move that liquidates the position.
    liquidation_pct = (1 / leverage - _maintenance_margin_rate(client, market, notional)) * 100
    if stop_loss_pct >= liquidation_pct * LIQUIDATION_BUFFER:
        raise OrderRejected(
            f"stop_loss_pct {stop_loss_pct}% would trigger at or beyond the estimated "
            f"liquidation move of ~{max(liquidation_pct, 0):.2f}% at {leverage}x; "
            "lower the leverage or tighten the stop"
        )

    stop_loss, take_profit = sl_tp_prices(order_side, price, stop_loss_pct, take_profit_pct)
    stop_loss = float(client.price_to_precision(unified, stop_loss))
    take_profit = float(client.price_to_precision(unified, take_profit))

    _set_leverage(client, market, leverage)
    client_order_id = f"bot-{uuid.uuid4().hex[:24]}"
    order = client.create_order(unified, "market", order_side, amount, None, {
        "stopLoss": {"triggerPrice": stop_loss},
        "takeProfit": {"triggerPrice": take_profit},
        "slTriggerBy": "MarkPrice",
        "tpTriggerBy": "MarkPrice",
        "clientOrderId": client_order_id,
    })
    log.info(
        "Placed %s %s %s @ ~%s (%sx) SL=%s TP=%s id=%s",
        order_side, amount, unified, price, leverage, stop_loss, take_profit, order.get("id"),
    )
    return {
        "order_id": order.get("id"),
        "client_order_id": client_order_id,
        "symbol": market["id"],
        "side": order_side,
        "size": amount,
        "leverage": leverage,
        "reference_price": price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


@_translate_errors
def close_position(symbol: str) -> list[dict]:
    """Close every open position on symbol with reduce-only market orders.

    Returns one entry per closing order; an empty list means nothing was open.
    """
    client = _get_client()
    market = _resolve_market(client, symbol)
    unified = market["symbol"]
    open_positions = [p for p in client.fetch_positions([unified]) if (p.get("contracts") or 0) > 0]
    if not open_positions:
        log.info("No open position on %s", unified)
        return []

    closed = []
    for position in open_positions:
        close_side = "sell" if position["side"] == "long" else "buy"
        params: dict[str, Any] = {"reduceOnly": True}
        position_idx = str(position["info"].get("positionIdx", "0"))
        if position_idx != "0":  # hedge mode: target the long (1) or short (2) leg
            params["positionIdx"] = int(position_idx)
        order = client.create_order(unified, "market", close_side, position["contracts"], None, params)
        log.info("Closed %s %s %s (order %s)", position["side"], position["contracts"], unified, order.get("id"))
        closed.append({
            "order_id": order.get("id"),
            "symbol": market["id"],
            "side": close_side,
            "size": position["contracts"],
            "closed_position_side": position["side"],
        })
    return closed


@_translate_errors
def cancel_all_orders(symbol: str) -> list[str]:
    """Cancel all open orders on symbol and return the cancelled order ids.

    Warning: with no order filter, Bybit's linear cancel-all also targets
    conditional and TP/SL orders, so this can strip the stop-loss from an open
    position on the same symbol.
    """
    client = _get_client()
    market = _resolve_market(client, symbol)
    cancelled = client.cancel_all_orders(market["symbol"])
    ids = [o["id"] for o in cancelled if o.get("id")]
    log.info("Cancelled %d order(s) on %s", len(ids), market["symbol"])
    return ids


def _run_order_test(symbol: str) -> None:
    """Open and immediately close the minimum-size position (testnet/demo only)."""
    if _env_name == "mainnet":
        raise ExchangeClientError("--order-test is disabled on mainnet")
    market = _resolve_market(_get_client(), symbol)
    size = market["limits"]["amount"]["min"]
    print(f"\nOrder test: buying {size} {market['id']} at 2x with SL 2% / TP 4%")
    print("  placed:", place_order(symbol, "buy", size, 2, 2.0, 4.0))
    print("  positions:", [p for p in get_positions() if p["symbol"] == market["id"]])
    print("  closed:", close_position(symbol))
    print("  cancelled:", cancel_all_orders(symbol))


def _run_connection_test(order_test_symbol: str | None = None) -> int:
    try:
        info = connect()
        print(f"Connected to Bybit {info['env']} at {info['api_url']}")
        print(f"  server time {info['server_time']} (clock skew {info['clock_skew_ms']} ms), "
              f"{info['linear_markets']} linear markets")
        balance = get_balance()
        print(f"Balance ({balance['account_type']}, {balance['currency']}): "
              f"equity={balance['equity']:.2f} available_margin={balance['available_margin']:.2f}")
        positions = get_positions()
        print(f"Open positions: {len(positions)}")
        for p in positions:
            print(f"  {p['symbol']} {p['side']} size={p['size']} entry={p['entry_price']} "
                  f"pnl={p['unrealized_pnl']} liq={p['liquidation_price']}")
        if order_test_symbol:
            _run_order_test(order_test_symbol)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2
    except InvalidCredentials as e:
        print(f"INVALID CREDENTIALS: {e}")
        return 3
    except ConnectionFailed as e:
        print(f"CONNECTION FAILED: {e}")
        return 4
    except (ExchangeClientError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1
    print("\nConnection test PASSED")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bybit connection test (read-only by default).")
    parser.add_argument(
        "--order-test", metavar="SYMBOL",
        help="also open and close a minimum-size position on SYMBOL (testnet/demo only)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(_run_connection_test(args.order_test))
