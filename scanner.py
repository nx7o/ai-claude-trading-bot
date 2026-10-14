"""
Funding rate and market scanner.

Responsibilities (to be implemented):
- Fetch funding rates across perpetual markets on a schedule
- Normalise rates to a common basis (funding interval differs by exchange/market)
- Filter out illiquid markets: volume, open interest, spread, order book depth
- Rank opportunities (e.g. annualised funding net of fees)
- Track rate history to avoid acting on one-off spikes
- Emit signals to trader.py and alerts to telegram_bot.py
"""

# TODO: imports

# TODO: funding rate fetch + normalisation

# TODO: liquidity / quality filters

# TODO: ranking and thresholds (from config)

# TODO: scan loop / scheduled job
