"""
Exchange API connection.

Responsibilities (to be implemented):
- Create an authenticated exchange client from config (ccxt / ccxt.async_support)
- Testnet / sandbox toggle driven by config
- Load and cache market metadata (symbols, precision, min order size, contract size)
- Market data: tickers, order books, OHLCV, funding rates, open interest
- Account data: balances, positions, open orders, fills
- Order primitives: create, cancel, fetch status (used by trader.py)
- Retry, rate-limit and error handling around every API call
- Close the client session on shutdown
"""

# TODO: imports

# TODO: client factory (build exchange instance from config)

# TODO: market data wrappers

# TODO: account data wrappers

# TODO: order wrappers

# TODO: error handling / retry helpers
