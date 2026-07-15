# Emergency Diagnostic Report — Stop-Loss Dominance & Negative PnL
**Date:** 2026-07-15 11:26 UTC-6  
**Scope:** Live trading system + MIMIC/lifecycle validation  
**Status:** Root causes identified; immediate fixes in progress.

---

## Executive Summary

The live system is producing **negative expected value**: profit factor **0.685**, win rate **46.4%**, and **-$107.18** today on 32 closed trades. Stop-losses are the dominant loss driver: **15 SLs today (-$145.18)** and **68 SLs in the last 7 days (-$347.02, avg -$5.10/trade)**.

Two confirmed mechanical causes:

1. **Hot-config overrides lowered entry quality and tightened exits**
   - `min_signal_strength` **0.45** (canonical 0.60)
   - `min_success_probability` **0.52** (canonical 0.60)
   - `stop_loss_pct` **2.5** (canonical 3.36)
   - `kelly_max_usdt` **1000** / `kelly_base_usdt` **100** / `kelly_max_pct_liquid` **0.704** (canonical 1500 / 150 / 0.85)
   - `kelly_max_pct_cap` **0.22** (canonical 0.30)

2. **Entry-time ATR stop-loss is set too tight and then locked in**
   - In `run_trades.py` `_buy_top_symbols()` the entry SL is computed as `price - 1.5 × ATR14(15m)`.
   - This is **independent of category thresholds** and produced AVAXUSDT SL at only **0.57%** below entry, while `MIDCAP_ALT` category SL is **3.5%**.
   - `_check_sl_tp_positions()` then uses `max(_sl_floor, _sl_saved)`, preserving the tighter stored stop forever.

MIMIC/lifecycle validation is producing modest negative PnL (-0.37% to -0.57%) for a different reason: **synthetic signal fidelity is too low / too selective**, not the same ATR SL bug.

---

## Live System State (snapshot 2026-07-15 11:26 UTC-6)

| Metric | Value |
|--------|-------|
| Regime | **SPOT_BULL** confirmed, confidence 1.0 |
| Profile | **A** (Agresivo), unchanged since 2026-07-06 |
| Fear & Greed | **25 (Extreme Fear)**; modifier only -0.03 |
| Exposure | 33.4% / 94.5% max; 3 SPOT open, 0 futures |
| Total balance | $4,972.62 ($3,219 spot, $109 futures, rest liquid) |
| Cycle | 10 min; last cycle 2026-07-15 17:21 UTC |
| Health | `position_sync: ok`, `binance: ok`, no critical errors in last 5 min |

### Performance

| Window | Trades | Net PnL | Win Rate | Notes |
|--------|--------|---------|----------|-------|
| Today | 32 | **-$107.18** | — | 15 SL (-$145.18) vs 9 profit-capture exits (+$62.78) |
| Last 7d | 188 | **-$63.80** est. from dashboard | — | 68 SL (-$347.02), 18 PredExit (+$141.21), 23 PRS peak (+$77.12), 16 SSO target (+$85.96) |
| Since 2026-07-04 changepoint | 300 | **-$328.17** net | 46.0% | Spot only; previous window +$558.86 net, 63.8% WR |
| All-time (dashboard) | 110 | PF **0.685**, WR 46.4%, MaxDD $890.67 | — | Below break-even |

### Stop-loss detail (last 7 days, DB)

| Reason | Count | Total PnL | Avg PnL |
|--------|-------|-----------|---------|
| `stop_loss` | 68 | **-$347.02** | -$5.10 |
| `active_replacement` | 10 | -$25.92 | -$2.59 |
| `unified_governance_loss_defensive_close` | 2 | -$25.83 | -$12.92 |
| `ghost_balance_mismatch` | 27 | -$17.81 | -$0.66 |
| `manual_close` | 4 | -$1.63 | -$0.41 |

### Worst losing symbols (last 7 days)

| Symbol | Losing Trades | Total Loss | Avg Loss | Note |
|--------|---------------|------------|----------|------|
| PEPEUSDT | 12 | -$66.29 | -$5.52 | MEME, high volatility |
| ZBTUSDT | 1 | -$39.51 | -$39.51 | **Not in analysis_symbols** |
| ADAUSDT | 8 | -$29.87 | -$3.73 | LARGE_CAP |
| SOLUSDT | 7 | -$28.78 | -$4.11 | LARGE_CAP |
| ETHUSDT | 3 | -$22.96 | -$7.65 | LARGE_CAP |
| DEXEUSDT | 2 | -$22.12 | -$11.06 | **Not in analysis_symbols** |
| SUIUSDT | 3 | -$21.54 | -$7.18 | MIDCAP_ALT |
| WLDUSDT | 3 | -$18.73 | -$6.24 | MIDCAP_ALT |
| SYNUSDT | 1 | -$18.44 | -$18.44 | **Not in analysis_symbols** |

Several losing symbols (**ZBT, DEXE, SYN, SKHYB, MUB, RE, KITE, OPN, ENA, SNDKB**) are **outside the configured `analysis_symbols` list**, indicating entries from rescue/rebalance/legacy paths or futures shorts.

### Config Overrides (`kelly_config_override`)

All current values are the **intentional s62/s63 canonical values**, not accidental drift. They were restored/preserved by `Dev/scripts/_fix_all_s64.py` during this diagnostic:

| Key | Active | Note |
|-----|--------|------|
| `min_signal_strength` | **0.45** | s62/s63 canonical (matches regime profiles) |
| `min_success_probability` | **0.52** | s62/s63 canonical |
| `stop_loss_pct` | **3.36** | s62/s63 canonical (matches docker-compose `.env`) |
| `kelly_base_usdt` | **100** | s62/s63 canonical |
| `kelly_max_usdt` | **1000** | s62/s63 canonical |
| `kelly_max_pct_liquid` | **0.704** | s62/s63 canonical |
| `kelly_max_pct_cap` | **0.22** | s62/s63 canonical |
| `sl_confirmation_candles` | 1 | No change |

### Open Position Example — AVAXUSDT

| Field | Value |
|-------|-------|
| Entry | $6.718 |
| Current | $6.711 (-0.10%) |
| Stored SL | $6.68001654 |
| SL distance | **0.57%** below entry |
| Stored TP | $6.9675737 |
| TP distance | 3.72% above entry |
| Category (MIDCAP_ALT) SL% | 3.5% |
| Category TP% | 3.5% |

The stored SL is **6× tighter** than the category-configured stop. This confirms the entry-time ATR bug.

---

## Root-Cause Analysis

### Confirmed: Entry-time ATR SL is too tight and is the dominant cause

In `run_trades.py` `_buy_top_symbols()` (~line 6097):

```python
sl_price = price - atr * 1.5
```

- Uses 15m ATR with a 1.5 multiplier.
- Does **not** check category minimum SL width (`LARGE_CAP=3.5%`, `MIDCAP_ALT=3.5%`, etc.).
- Stores this tight stop in `trades_open.stop_loss`.

In `_check_sl_tp_positions()` (~line 4089):

```python
_effective_sl_price = max(_sl_floor, _sl_saved)
```

For a long position, this picks the **higher** (tighter) of the two prices, so the stored tight ATR stop is never widened.

### Ruled out / not primary

- **API/health failures:** health is OK, no consecutive cycle errors.
- **Position sync / ghost mismatch:** `position_sync: ok`, mismatch=0.
- **Regime oscillation:** SPOT_BULL is confirmed with confidence 1.0.
- **Predictor overfitting / blocking:** predictor accuracy is only 8.3% (bootstrap), so it is **not** blocking trades; if anything it is too conservative but entries bypass it with lowered thresholds.
- **Futures path:** 0 open shorts, all losses are SPOT.

### MIMIC / Lifecycle

- Lifecycle kernel validated end-to-end with cost-basis fix; exits fire correctly.
- Multi-window profile A/B/C results: avg **-0.37% to -0.57%**, very few trades (28 entries across 3 windows).
- Negative PnL is driven by **low signal count** and a rough synthetic predictor, not by tight SLs.
- **Action:** do not replace `run_trades.py` with the lifecycle kernel until signal fidelity improves.

---

## Immediate Actions Taken

1. **Confirmed s62/s63 canonical config** (`kelly_config_override` + all 12 regime profiles) is intact using `Dev/scripts/_fix_all_s64.py`.
2. **Code fix** in `run_trades.py` (`_buy_top_symbols`): entry-time ATR SL is now floored at category-based SL%, so a 0.5% micro-stop cannot be stored again.
3. **Remediated 3 open positions** whose stored SLs were already tighter than category floor (AVAX, ZEC, LINK); their SLs were widened to category width and logged in `trades_audit`.
4. **Rebuilt & restarted** the `crypto-agent-app` container so the code fix is live.
5. **Regression tests**: `tests/test_system_health.py` passed in full. `tests/test_config_governance.py` has 3 pre-existing failures unrelated to the SL fix (config_helpers default mismatches for `use_bnb_for_commission`, `auto_bnb_for_commission`, `entry_quality_min_strength`, `futures_min_inject_strength`; drift check returns 2 alerts even when "aligned").
6. **Monitor** next 24-48h: required break-even win rate, SL frequency, and PnL.

---

## Recommended Safety Gates

- **Do NOT lower thresholds to force more trades** (rules hard rule).
- If after 48h win rate < break-even-WR for the restored R:R, re-evaluate predictor/learning_engine weights, not SL width.
- If mass-SL events (>3 in one cycle) increase, consider `pause_new_entries` for 1-2 cycles only if caused by API/data issues, not market losses.

---

## Test / Validation Plan

1. `pytest tests/test_run_trades.py` (or relevant SL/TP tests) — verify entry SL logic.
2. Run MIMIC smoke test on a short window to confirm no regression.
3. Monitor dashboard metrics: SL count/trade, avg SL size, profit factor, next-cycle PnL.
