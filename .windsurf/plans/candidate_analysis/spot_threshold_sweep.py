import json, statistics

recs = [json.loads(l) for l in open('/app/Dev/benchmarks/forced_regime_sweep/evidence_gate_shadow_log.jsonl') if l.strip()]
rows = [r for r in recs if r['forced_regime'] == 'SPOT_BULL']
for thr in [0.50, 0.51, 0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58, 0.59, 0.60]:
    sel = [r for r in rows if r['posterior'] >= thr]
    if not sel:
        print(thr, 0)
        continue
    wins = sum(1 for r in sel if r['pnl_usdt'] > 0)
    mean = statistics.mean([r['pnl_usdt'] for r in sel])
    print(f'thr={thr:.2f} n={len(sel):4} wr={wins/len(sel):.3f} mean={mean:.2f}')
