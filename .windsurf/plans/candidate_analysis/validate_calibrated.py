import json, math, statistics, collections

path = '/app/Dev/benchmarks/forced_regime_sweep/evidence_gate_shadow_log.jsonl'
recs = [json.loads(l) for l in open(path) if l.strip()]
print('total', len(recs))
by = collections.defaultdict(list)
for r in recs:
    by[r['forced_regime']].append(r)
for reg, lst in by.items():
    posts = [r['posterior'] for r in lst]
    pnls = [r['pnl_usdt'] for r in lst]
    posts.sort()
    print(f"{reg}: n={len(lst)} post min/max/med={posts[0]:.3f}/{posts[-1]:.3f}/{posts[len(posts)//2]:.3f} mean_pnl={statistics.mean(pnls):.2f}")
    if reg == 'SPOT_BULL':
        thr = 0.60
    elif reg == 'RANGE_SCALP':
        thr = 0.52
    else:
        continue
    sel = [r for r in lst if r['posterior'] >= thr]
    if sel:
        wins = sum(1 for r in sel if r['pnl_usdt'] > 0)
        mean_pnl = statistics.mean([r['pnl_usdt'] for r in sel])
        print(f"  thr={thr} n={len(sel)} wr={wins/len(sel):.3f} mean={mean_pnl:.2f}")
    else:
        print(f"  thr={thr} n=0")
