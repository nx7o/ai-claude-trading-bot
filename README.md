# ai-claude-trading-bot

## Exchange layer (`exchange.py`)

Bybit USDT/USDC linear perpetuals via [ccxt](https://github.com/ccxt/ccxt). Testnet by default.

### Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your keys; .env is git-ignored
python exchange.py     # read-only connection test
```

Create testnet keys at <https://testnet.bybit.com> → API Management, with Contract **Orders** and
**Positions** permissions. Mainnet keys do not work on testnet. Set `BYBIT_ENV=demo` to use Bybit
Demo Trading (mainnet prices, fake funds) with keys from the Demo Trading tab on bybit.com.

Credentials are read only from `.env`; process environment variables are ignored.

### API

| Function | Returns |
| --- | --- |
| `connect()` | Checks reachability (retries 3× with backoff) and that the keys are accepted |
| `get_balance()` | `equity`, `available_margin` (USD on a Unified Trading Account) |
| `get_positions()` | Open positions: `side`, `size`, `entry_price`, `unrealized_pnl`, `liquidation_price`, … |
| `place_order(symbol, side, size, leverage, stop_loss_pct, take_profit_pct)` | Market order with SL/TP attached |
| `close_position(symbol)` | Reduce-only market close of all open positions on `symbol` |
| `cancel_all_orders(symbol)` | Ids of the cancelled orders |

Units: `size` is in the base coin (`0.01` = 0.01 BTC). `stop_loss_pct=2.0` means a 2% **price**
move from entry, not 2% of margin. `place_order` rejects a stop-loss that would trigger after the
estimated liquidation price for the chosen leverage.

Errors are raised as `ConfigError`, `ConnectionFailed`, `InvalidCredentials` or `OrderRejected`
(all subclasses of `ExchangeClientError`); bad arguments raise `ValueError`.

`cancel_all_orders` can also remove the TP/SL orders protecting an open position on that symbol.

### Testing

```bash
python -m pytest                          # offline unit tests, no keys needed
python exchange.py --order-test BTCUSDT   # opens and closes a minimum-size position (testnet/demo only)
```
