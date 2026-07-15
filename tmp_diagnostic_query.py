import psycopg2, os, json
url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')
conn = psycopg2.connect(url)
cur = conn.cursor()

# kelly_config_override
cur.execute("SELECT key, value FROM trading_runtime_state WHERE key='kelly_config_override'")
row = cur.fetchone()
print('=== kelly_config_override ===')
if row:
    val = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    print(json.dumps(val, indent=2))
else:
    print('not found')

# tables in DB
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
print('\n=== tables ===')
for r in cur.fetchall():
    print(r[0])

# config_change_log schema
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='config_change_log' ORDER BY ordinal_position")
print('\n=== config_change_log columns ===')
print([r[0] for r in cur.fetchall()])

# config_change_log last 50
cur.execute("""
    SELECT *
    FROM config_change_log
    ORDER BY changed_at DESC
    LIMIT 50
""")
print('\n=== config_change_log last 50 ===')
for r in cur.fetchall():
    print(r)

# trades_audit recent events
cur.execute("""
    SELECT event, trade_type, COUNT(*)
    FROM trades_audit
    WHERE ts >= NOW() - INTERVAL '7 days'
    GROUP BY event, trade_type
    ORDER BY COUNT(*) DESC
""")
print('\n=== trades_audit events last 7d ===')
for r in cur.fetchall():
    print(r)

# active mode/profile
cur.execute("SELECT key, value FROM trading_runtime_state WHERE key IN ('current_regime_mode','current_regime_profile_label','smart_total_active_mode','smart_total_active_profile_spot_bull')")
print('\n=== active mode/profile ===')
for r in cur.fetchall():
    print(r[0], ':', r[1])

# trades_closed schema
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='trades_closed' ORDER BY ordinal_position")
print('\n=== trades_closed columns ===')
print([r[0] for r in cur.fetchall()])

# recent trades closed by reason
cur.execute("""
    SELECT close_reason, COUNT(*), ROUND(SUM(pnl)::numeric, 2), ROUND(AVG(pnl)::numeric, 4)
    FROM trades_closed
    WHERE exit_time >= NOW() - INTERVAL '7 days'
    GROUP BY close_reason
    ORDER BY SUM(pnl) ASC
""")
print('\n=== trades_closed last 7d by close_reason ===')
for r in cur.fetchall():
    print(r)

# recent losing trades by symbol
cur.execute("""
    SELECT symbol, COUNT(*), ROUND(SUM(pnl)::numeric, 2), ROUND(AVG(pnl)::numeric, 4)
    FROM trades_closed
    WHERE exit_time >= NOW() - INTERVAL '7 days' AND pnl < 0
    GROUP BY symbol
    ORDER BY SUM(pnl) ASC
""")
print('\n=== losing trades last 7d by symbol ===')
for r in cur.fetchall():
    print(r)

# recent stop_loss positions sample
cur.execute("""
    SELECT symbol, entry_price, stop_loss, take_profit, quantity, pnl, close_reason, regime_at_entry, profile_at_entry
    FROM trades_closed
    WHERE exit_time >= NOW() - INTERVAL '2 days' AND pnl < 0
    ORDER BY exit_time DESC
    LIMIT 20
""")
print('\n=== recent 20 losing positions ===')
for r in cur.fetchall():
    print(r)

conn.close()
