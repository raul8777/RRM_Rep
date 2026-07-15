import psycopg2, json
from datetime import datetime, timezone

url = 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent'
conn = psycopg2.connect(url)
cur = conn.cursor()

print('=== OPEN POSITIONS ===')
cur.execute("""
    SELECT id, symbol, side, quantity, price, stop_loss, take_profit,
           entry_time, status, metadata
    FROM trades_open
    WHERE side='BUY'
    ORDER BY entry_time DESC
""")
for row in cur.fetchall():
    pos_id, symbol, side, qty, price, sl, tp, entry_time, status, metadata = row
    meta = json.loads(metadata) if metadata and isinstance(metadata, str) else (metadata or {})
    age_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
    print(f"\n{symbol} entry={price:.4f} qty={qty} SL={sl:.4f} TP={tp:.4f} age_h={age_h:.1f}")
    print(f"  metadata keys: {sorted(meta.keys())}")
    sm = meta.get('_smart_meta', {})
    print(f"  trailing_active={meta.get('trailing_active', sm.get('trailing_active'))}")
    print(f"  last_peak_price={meta.get('last_peak_price')}")
    print(f"  breakeven_done={meta.get('breakeven_done', sm.get('breakeven_done'))}")
    print(f"  scale_out_done={meta.get('scale_out_done', sm.get('scale_out_done'))}")
    print(f"  tp_extensions={meta.get('tp_extensions', sm.get('tp_extensions_count'))}")
    print(f"  sso_pnl_snapshot={meta.get('sso_pnl_snapshot')}")
    print(f"  unified_governance_last_decision={json.dumps(meta.get('unified_governance_last_decision'), default=str)}")
    print(f"  unified_governance_loss_state keys={list(meta.get('unified_governance_loss_state', {}).keys())}")
    print(f"  bounce_gate: fired={meta.get('bounce_gate_fired')}, blocked_count={meta.get('bounce_gate_blocked_count')}")

fix_time = '2026-07-15 17:26:00+00'
print('\n=== CLOSED SINCE FIX by reason ===')
cur.execute("""
    SELECT close_reason, COUNT(*), ROUND(SUM(pnl)::numeric, 2), ROUND(AVG(pnl)::numeric, 4), ROUND(AVG(pnl_percentage)::numeric, 4)
    FROM trades_closed
    WHERE exit_time >= %s
    GROUP BY close_reason
    ORDER BY COUNT(*) DESC
""", (fix_time,))
for r in cur.fetchall():
    print(r)

print('\n=== AUDIT EVENTS SINCE FIX ===')
cur.execute("""
    SELECT event, trade_type, COUNT(*)
    FROM trades_audit
    WHERE ts >= %s
    GROUP BY event, trade_type
    ORDER BY COUNT(*) DESC
""", (fix_time,))
for r in cur.fetchall():
    print(r)

for event in ['breakeven_activated', 'sso_close', 'sso_partial', 'profit_realization_supervisor_full',
              'profit_realization_supervisor_partial', 'profit_predexit_capture',
              'unified_governance_close_triggered', 'unified_governance_decision',
              'trailing_stop_activated', 'bounce_gate_fired', 'rescue_entry_fired']:
    cur.execute("""
        SELECT ts, symbol, event, detail
        FROM trades_audit
        WHERE event=%s AND ts >= %s
        ORDER BY ts DESC
        LIMIT 3
    """, (event, fix_time))
    rows = cur.fetchall()
    if rows:
        print(f"\n--- {event} last 3 ---")
        for r in rows:
            print(r[0], r[1], r[2], json.dumps(r[3], default=str)[:250])

conn.close()
