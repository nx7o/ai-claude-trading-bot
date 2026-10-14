"""
Trade execution logic.

Responsibilities (to be implemented):
- Turn signals (from scanner.py or Telegram commands) into concrete orders
- Pre-trade risk checks: position size, leverage, total exposure, daily loss limit
- Round size and price to exchange precision; reject orders below minimums
- Place, cancel and amend orders via exchange.py
- Track order state and fills; reconcile with the exchange on startup
- Use client order IDs so a retried request cannot open a duplicate position
- Dry-run mode (log intended orders without sending them)
- Kill switch: halt new orders and optionally flatten all positions
"""

# TODO: imports

# TODO: risk checks

# TODO: order sizing / precision helpers

# TODO: open / close / cancel position functions

# TODO: dry-run and kill-switch handling
