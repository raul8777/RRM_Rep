import psycopg2, os, json
url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')
conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'")
row = cur.fetchone()
override = row[0] if isinstance(row[0], dict) else json.loads(row[0])
keys = ['min_signal_strength','min_success_probability','stop_loss_pct','take_profit_pct','kelly_base_usdt','kelly_max_usdt','kelly_max_pct_liquid','kelly_max_pct_cap']
print('=== kelly_config_override active values ===')
for k in keys:
    v = override.get(k)
    if isinstance(v, dict):
        v = v.get('new')
    print(f"{k}: {v}")
conn.close()
