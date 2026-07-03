"""Show ALL params inside each Profile A and what the runtime config currently provides for exit/sizing."""
from init_components import get_components
import json

db = get_components().db_client
settings = get_components().settings

modes = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]

# 1. What's actually inside each profile A
print("=" * 60)
print("PARAMS STORED IN EACH PROFILE A")
print("=" * 60)
for mode in modes:
    key = f"regime_profile_{mode}_A"
    rows = db.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key = %s LIMIT 1", (key,)
    )
    if not rows:
        print(f"\n{mode}/A: NOT FOUND")
        continue
    v = rows[0]["value"] or {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = {}
    params = v.get("params", {})
    print(f"\n--- {mode} / A (run_id={v.get('meta',{}).get('best_run_id')}, {len(params)} params) ---")
    for k, val in sorted(params.items()):
        print(f"  {k:<50} = {val}")

# 2. Current runtime values for exit/sizing params (from settings, NOT profiles)
print("\n" + "=" * 60)
print("RUNTIME CONFIG (settings) - EXIT/SIZING PARAMS")
print("=" * 60)
exit_sizing_params = [
    "min_hold_minutes", "min_hold_minutes_spot_bull", "min_hold_minutes_range_scalp",
    "min_hold_for_prediction_exit", "prediction_exit_enabled",
    "min_cycles_before_close_shorts",
    "kelly_conviction_max_pct", "kelly_conviction_vol2x", "kelly_conviction_vol3x",
    "kelly_conviction_enabled",
    "smart_tp_enabled", "smart_tp_max_extensions", "smart_tp_max_pnl_pct",
    "smart_tp_extend_score_threshold", "smart_tp_extension_pct",
    "smart_tp_extend_disabled_regimes",
    "scale_out_enabled", "scale_out_first_close_pct",
    "futures_smart_tp_enabled", "futures_smart_tp_max_pnl_pct",
    "futures_smart_tp_max_extensions", "futures_smart_tp_extend_score_threshold",
    "futures_trailing_enabled", "futures_trailing_activation_pct",
    "futures_trailing_distance_pct", "futures_sl_cooldown_hours",
    "futures_min_btc_7d_drop",
]
for p in exit_sizing_params:
    val = getattr(settings, p, "(missing)")
    print(f"  {p:<50} = {val}")


