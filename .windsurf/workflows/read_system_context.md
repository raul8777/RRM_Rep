---
description: Mandatory context reading before every crypto-agent session
---

# Read System Context Workflow

This workflow is mandatory. Run it at the start of every session before analysis, verification, or code changes.

## 1. Read `SYSTEM_REFERENCE.md` first

```bash
cat /app/SYSTEM_REFERENCE.md
```

- This file contains the current system state, recent changes, known bugs, and pending sweeps.
- It is the operational snapshot of the crypto-agent workspace.

## 2. Read `.windsurf/rules.md`

```bash
cat /app/.windsurf/rules.md
```

- This file is the persisted memory of the workspace (`Memoria persistida del workspace`).
- It defines the hierarchy of rules (hard/guide/context), the 6 mandatory questions before any relevant change, module responsibilities, position lifecycle, and configuration layers.

## 3. Confirm active regime/profile (quick audit)

```bash
# Active regime/profile from DB
docker exec -e PGPASSWORD=crypto_agent_password crypto-agent-db psql -U crypto_agent -d crypto_agent -c "SELECT key, value FROM trading_runtime_state WHERE key IN ('smart_total_active_mode', 'regime_profile_active_RANGE_SCALP', 'regime_profile_active_SPOT_BULL', 'regime_profile_selector_config') ORDER BY key;"

# Latest effective runtime snapshot
docker exec -e PGPASSWORD=crypto_agent_password crypto-agent-db psql -U crypto_agent -d crypto_agent -c "SELECT value FROM trading_runtime_state WHERE key='effective_runtime_selection_snapshot' ORDER BY updated_at DESC LIMIT 1;"
```

## 4. Check dashboard context

Open `http://localhost:8000` and verify:

- `market_regime.trading_mode` matches the selected mode.
- `effective_runtime.mode` matches DB.
- `effective_runtime.profile_label` matches `regime_profile_active_<MODE>` in DB.

If the dashboard profile label does not match DB, the API `settings` instance in `app.py` is stale; the trading cycle `comp.settings` is the source of truth.

## 5. Apply rules before changes

Before proposing any relevant change, answer the 6 questions from `.windsurf/rules.md`:

1. What is the exact problem or improvement?
2. Which files/modules are affected?
3. Does it change risk, position sizing, or stop-loss behavior?
4. What is the rollback plan?
5. How will it be tested?
6. Does it require updating `SYSTEM_REFERENCE.md` or `rules.md`?
