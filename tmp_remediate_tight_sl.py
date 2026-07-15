import psycopg2, os, json
from datetime import datetime, timezone

url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')
conn = psycopg2.connect(url)
cur = conn.cursor()

# Category thresholds from canonical config (must match config.py / config_helpers.py)
CATEGORY_THRESHOLDS = {
    "LARGE_CAP":  {"stop_loss_pct": 3.5},
    "MIDCAP_ALT": {"stop_loss_pct": 3.5},
    "NARRATIVE":  {"stop_loss_pct": 3.5},
    "MEME":       {"stop_loss_pct": 2.5},
}
DEFAULT_SL_PCT = 3.36

# Token categories (subset; extend if needed)
TOKEN_CATEGORIES = {
    "BTCUSDT": "LARGE_CAP",
    "ETHUSDT": "LARGE_CAP",
    "BNBUSDT": "LARGE_CAP",
    "XRPUSDT": "LARGE_CAP",
    "SOLUSDT": "LARGE_CAP",
    "DOGEUSDT": "MIDCAP_ALT",
    "RENDERUSDT": "MIDCAP_ALT",
    "TAOUSDT": "NARRATIVE",
    "FETUSDT": "MIDCAP_ALT",
    "AVAXUSDT": "MIDCAP_ALT",
    "ADAUSDT": "LARGE_CAP",
    "DOTUSDT": "MIDCAP_ALT",
    "SUIUSDT": "MIDCAP_ALT",
    "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME",
    "LINKUSDT": "MIDCAP_ALT",
    "NEARUSDT": "MIDCAP_ALT",
    "WLDUSDT": "MIDCAP_ALT",
    "INJUSDT": "MIDCAP_ALT",
    "AGIXUSDT": "NARRATIVE",
    "ONDOUSDT": "NARRATIVE",
    "JUPUSDT": "MIDCAP_ALT",
    "OPUSDT": "MIDCAP_ALT",
    "HBARUSDT": "MIDCAP_ALT",
    "AAVEUSDT": "MIDCAP_ALT",
    "ARBUSDT": "MIDCAP_ALT",
    "MATICUSDT": "MIDCAP_ALT",
    "ATOMUSDT": "MIDCAP_ALT",
    "GRTUSDT": "MIDCAP_ALT",
    "AIOZUSDT": "MIDCAP_ALT",
    "SEIUSDT": "MIDCAP_ALT",
    "SUSHIUSDT": "MIDCAP_ALT",
    "SENTUSDT": "MIDCAP_ALT",
    "ZECUSDT": "MIDCAP_ALT",
}

cur.execute("""
    SELECT id, symbol, price, stop_loss, quantity
    FROM trades_open
    WHERE side='BUY'
""")
rows = cur.fetchall()
print(f"Open SPOT long positions: {len(rows)}")

for pos_id, symbol, entry_price, stop_loss, qty in rows:
    if not entry_price or entry_price <= 0:
        continue
    cat = TOKEN_CATEGORIES.get(symbol, 'MIDCAP_ALT')
    sl_pct = CATEGORY_THRESHOLDS.get(cat, {}).get('stop_loss_pct', DEFAULT_SL_PCT) / 100.0
    floor_sl_price = float(entry_price) * (1 - sl_pct)
    if stop_loss is None or float(stop_loss) > floor_sl_price:
        # Stored SL is tighter than category floor -> widen it
        cur.execute(
            "UPDATE trades_open SET stop_loss = %s WHERE id = %s",
            (floor_sl_price, pos_id)
        )
        cur.execute(
            """
            INSERT INTO trades_audit (ts, symbol, trade_type, event, detail)
            VALUES (NOW(), %s, 'spot', 'emergency_sl_remediate', %s)
            """,
            (symbol,
             json.dumps({
                 "position_id": str(pos_id),
                 "old_sl": float(stop_loss) if stop_loss else None,
                 "new_sl": floor_sl_price,
                 "category": cat,
                 "category_sl_pct": sl_pct,
                 "reason": "Stored SL was tighter than category floor; widened to prevent micro-stop"
             }))
        )
        print(f"  {symbol}: SL widened {stop_loss} -> {floor_sl_price:.6f} ({sl_pct*100}% below {entry_price})")
    else:
        print(f"  {symbol}: SL already wider than floor ({stop_loss} <= {floor_sl_price:.6f})")

conn.commit()
conn.close()
print("Done")
