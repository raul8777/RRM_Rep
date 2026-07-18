import json, math, statistics, collections

path = '/app/Dev/benchmarks/forced_regime_sweep/evidence_gate_shadow_log.jsonl'
recs = [json.loads(l) for l in open(path) if l.strip()]
print('records', len(recs))
by_reg = collections.defaultdict(list)
for r in recs:
    m = r['entry_metadata']
    p = m.get('predictor_prob')
    ev = m.get('predictor_ev')
    if p is None:
        continue
    x = {
        'prob': float(p),
        'ev': float(ev) if ev is not None else 0.0,
        'profile': r['profile'],
        'pnl': r['pnl_usdt'],
        'positive': 1 if r['pnl_usdt'] > 0 else 0,
        'logit': math.log(max(p, 1e-6) / (1 - max(p, 1e-6))),
    }
    by_reg[r['forced_regime']].append(x)

for reg, rows in by_reg.items():
    if not rows:
        continue
    pos = sum(x['positive'] for x in rows)
    print(f"{reg}: n={len(rows)} winrate={pos/len(rows):.3f} mean_pnl={statistics.mean(x['pnl'] for x in rows):.3f}")
    for fname in ['prob', 'ev', 'logit']:
        vals = [x[fname] for x in rows]
        pnls = [x['pnl'] for x in rows]
        mv = sum(vals) / len(vals)
        mp = sum(pnls) / len(pnls)
        num = sum((v - mv) * (p - mp) for v, p in zip(vals, pnls))
        den = (sum((v - mv) ** 2 for v in vals) * sum((p - mp) ** 2 for p in pnls)) ** 0.5
        corr = num / den if den else 0
        print(f"  {fname}-pnl corr={corr:.3f}")
    rows.sort(key=lambda x: x['prob'])
    n = len(rows)
    for i in range(5):
        chunk = rows[int(i * n / 5):int((i + 1) * n / 5)]
        if not chunk:
            continue
        wins = sum(x['positive'] for x in chunk)
        avg_p = statistics.mean(x['prob'] for x in chunk)
        avg_e = statistics.mean(x['ev'] for x in chunk)
        print(f"  prob_bin {i}: prob={avg_p:.3f} ev={avg_e:.3f} winrate={wins/len(chunk):.3f} n={len(chunk)}")
