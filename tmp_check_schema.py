import psycopg2, os
url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')
conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='trades_open' ORDER BY ordinal_position")
print([r[0] for r in cur.fetchall()])
conn.close()
