"""Verify BNB reserve and portfolio balance after commission fix."""
import sys
sys.path.insert(0, '/app')

from binance_commission import estimate_bnb_buffer_needed, get_bnb_reserve_usdt
from config import Settings

s = Settings()

spot_buf = estimate_bnb_buffer_needed(s, 'spot', s.bnb_buffer_days)
fut_buf = estimate_bnb_buffer_needed(s, 'futures', s.bnb_buffer_days)
reserve = get_bnb_reserve_usdt(s)

print(f"spot buffer:      ${spot_buf:.2f}")
print(f"futures buffer:   ${fut_buf:.2f}")
print(f"total reserve:    ${reserve['total_reserve_usdt']:.2f}")
print()

from init_components import get_components
c = get_components()
pm = c.portfolio_manager
bal = pm.get_total_balance(use_cache=False)
print(f"usdt_available:              ${bal['usdt_available']:.2f}")
print(f"bnb_reserve_usdt:            ${bal['bnb_reserve_usdt']:.2f}")
print(f"usdt_available_after_reserve:${bal['usdt_available_after_reserve']:.2f}")
print()

if bal['usdt_available_after_reserve'] > 0:
    print("PASS: usdt_available_after_reserve > 0 — predictions will be non-zero")
else:
    print("FAIL: still zero — investigate further")
