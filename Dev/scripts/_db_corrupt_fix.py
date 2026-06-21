"""
Marca como is_corrupted=TRUE las posiciones futures cerradas que son artefactos
de reinicios de Docker: duración exacta ~6h + sl_price/tp_price NULL.
Luego llama POST /predictor/reset-corrupted para excluirlas del modelo.
"""
import sqlite3
import urllib.request
import json

conn = sqlite3.connect('/app/trades.db')
cur = conn.cursor()

# 1. Verificar columna
cur.execute("PRAGMA table_info(trades_closed)")
cols = [r[1] for r in cur.fetchall()]
print("Columnas trades_closed:", cols)
has_corrupted = 'is_corrupted' in cols

if not has_corrupted:
    print("WARN: columna is_corrupted no existe — agregando...")
    cur.execute("ALTER TABLE trades_closed ADD COLUMN is_corrupted INTEGER DEFAULT 0")
    conn.commit()
    print("Columna is_corrupted agregada")

# 2. Identificar candidatas
q_view = """
SELECT id, symbol, side, pnl,
       ROUND((julianday(closed_at)-julianday(opened_at))*24, 2) as dur_h,
       sl_price, tp_price, opened_at, closed_at
FROM trades_closed
WHERE trade_type='futures'
  AND (sl_price IS NULL OR tp_price IS NULL)
  AND ROUND((julianday(closed_at)-julianday(opened_at))*24, 1) = 6.0
ORDER BY closed_at DESC
"""
cur.execute(q_view)
rows = cur.fetchall()
print(f"\nCandidatas a marcar como corruptas: {len(rows)}")
for r in rows:
    print(f"  ID={r[0]} {r[1]} {r[2]} pnl={r[3]} dur={r[4]}h sl={r[5]} tp={r[6]}")
    print(f"     opened={r[7]} closed={r[8]}")

if not rows:
    print("Sin candidatas. Verificando con rango ampliado (5.8-6.2h)...")
    q_wide = """
    SELECT id, symbol, side, pnl,
           ROUND((julianday(closed_at)-julianday(opened_at))*24, 2) as dur_h,
           sl_price, tp_price
    FROM trades_closed
    WHERE trade_type='futures'
      AND (sl_price IS NULL OR tp_price IS NULL)
    ORDER BY closed_at DESC LIMIT 20
    """
    cur.execute(q_wide)
    rows2 = cur.fetchall()
    print(f"Futures con sl/tp NULL (sin filtro duración): {len(rows2)}")
    for r in rows2:
        print(f"  ID={r[0]} {r[1]} {r[2]} pnl={r[3]} dur={r[4]}h sl={r[5]} tp={r[6]}")
    conn.close()
    exit(0)

# 3. Marcar como corruptas
ids = [r[0] for r in rows]
id_list = ','.join(str(i) for i in ids)
q_update = f"UPDATE trades_closed SET is_corrupted=1 WHERE id IN ({id_list})"
cur.execute(q_update)
conn.commit()
print(f"\nMarcados como is_corrupted=1: {cur.rowcount} registros (IDs: {id_list})")

# 4. Verificar
cur.execute("SELECT COUNT(*) FROM trades_closed WHERE is_corrupted=1")
total_corrupted = cur.fetchone()[0]
print(f"Total is_corrupted=1 en DB: {total_corrupted}")

conn.close()

# 5. Llamar POST /predictor/reset-corrupted
print("\nLlamando POST /predictor/reset-corrupted...")
try:
    req = urllib.request.Request(
        'http://localhost:8000/predictor/reset-corrupted',
        data=b'',
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        print(f"reset-corrupted result: {result}")
except Exception as e:
    print(f"reset-corrupted ERROR: {e}")

print("\nDone.")
