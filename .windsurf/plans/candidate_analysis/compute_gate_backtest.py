#!/usr/bin/env python3
"""Back-test the calibrated evidence gate on the latest shadow log."""
import json
import statistics
import collections

SHADOW_LOG = "/app/Dev/benchmarks/forced_regime_sweep/evidence_gate_shadow_log.jsonl"
THRESHOLDS = {
    "SPOT_BULL": 0.60,
    "RANGE_SCALP": 0.52,
    "FUTURES_SHORT": 0.80,
    "PRESERVATION": 0.80,
}

recs = [json.loads(l) for l in open(SHADOW_LOG) if l.strip()]
by_reg = collections.defaultdict(lambda: {"all": [], "passed": []})
for r in recs:
    reg = r["forced_regime"]
    pnl = r["pnl_usdt"]
    post = r["posterior"]
    by_reg[reg]["all"].append(pnl)
    if post >= THRESHOLDS.get(reg, 1.0):
        by_reg[reg]["passed"].append(pnl)

print(f"{'Regime':15} {'All n':>8} {'All mean':>10} {'Pass n':>8} {'Pass mean':>10} {'Pass WR':>8}")
all_pnls = []
passed_pnls = []
for reg, data in sorted(by_reg.items()):
    all_n = len(data["all"])
    all_mean = statistics.mean(data["all"]) if all_n else 0.0
    pass_n = len(data["passed"])
    pass_mean = statistics.mean(data["passed"]) if pass_n else 0.0
    pass_wr = sum(1 for p in data["passed"] if p > 0) / pass_n if pass_n else 0.0
    all_pnls.extend(data["all"])
    passed_pnls.extend(data["passed"])
    print(f"{reg:15} {all_n:8} {all_mean:10.2f} {pass_n:8} {pass_mean:10.2f} {pass_wr:8.1%}")

print("-" * 70)
print(f"{'TOTAL':15} {len(all_pnls):8} {statistics.mean(all_pnls):10.2f} {len(passed_pnls):8} {statistics.mean(passed_pnls):10.2f}")
print(f"Baseline PnL total: {sum(all_pnls):.2f} USDT")
print(f"Gated PnL total:    {sum(passed_pnls):.2f} USDT")
