"""
_mark_corrupted2.py — Marca trades obsoletos como is_corrupted=TRUE en trades_closed.
Ejecutar: docker compose exec -w /app app python3 Dev/scripts/_mark_corrupted2.py
"""
import sys
sys.path.insert(0, '/app')

from init_components import get_components
comp = get_components()
db = comp.db_client

print("=" * 65)
print("MARCADO DE TRADES OBSOLETOS — is_corrupted=TRUE")
print("=" * 65)

def q(sql):
    try:
        return db.execute_query(sql)
    except Exception as e:
        print(f"  QUERY ERROR: {e}")
        return []

def u(sql):
    try:
        return db.execute_update(sql)
    except Exception as e:
        print(f"  UPDATE ERROR: {e}")
        return 0

# 1. Verificar columna y schema
cols = q("SELECT column_name FROM information_schema.columns WHERE table_name='trades_closed' ORDER BY ordinal_position")
col_names = [c['column_name'] for c in cols]
print(f"Columnas en trades_closed: {col_names}")

if 'is_corrupted' not in col_names:
    print("Agregando columna is_corrupted...")
    u("ALTER TABLE trades_closed ADD COLUMN IF NOT EXISTS is_corrupted BOOLEAN DEFAULT FALSE")
    print("Columna agregada.")
else:
    r = q("SELECT COUNT(*) as n FROM trades_closed WHERE is_corrupted = TRUE")
    print(f"Ya marcados como corrupted: {r[0]['n'] if r else 0}")

# 2. Ver columnas de close_reason para identificar la correcta
print(f"\nTotal trades_closed: {q('SELECT COUNT(*) as n FROM trades_closed')[0]['n']}")

# Detectar nombre correcto de columna de razón de cierre
reason_col = None
for candidate in ['close_reason', 'exit_reason', 'reason', 'close_type']:
    if candidate in col_names:
        reason_col = candidate
        break

if not reason_col:
    print(f"ERROR: No se encontró columna de razón. Columnas: {col_names}")
    sys.exit(1)
print(f"Columna de razón de cierre: '{reason_col}'")

# 3. Ver columna de tiempo de salida
time_col = None
for candidate in ['exit_time', 'closed_at', 'close_time', 'updated_at', 'created_at']:
    if candidate in col_names:
        time_col = candidate
        break
print(f"Columna de tiempo de cierre: '{time_col}'")

# 4. Distribución de razones
reasons = q(f"SELECT {reason_col}, COUNT(*) as n FROM trades_closed GROUP BY {reason_col} ORDER BY n DESC")
print("\nDistribución de close_reason:")
for r in reasons:
    print(f"  {r[reason_col]}: {r['n']}")

# 5. Contar targets
if time_col:
    n_rebal = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} = 'portfolio_rebalancing' AND {time_col} < '2026-06-15 20:40:00'")[0]['n']
    n_replace = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} = 'active_replacement' AND {time_col} < '2026-06-17 00:00:00'")[0]['n']
    n_manual = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital') AND {time_col} < '2026-06-18 00:00:00'")[0]['n']
    n_b6 = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} = 'stop_loss' AND {time_col} < '2026-06-17 18:00:00' AND symbol IN ('TRXUSDT','TONUSDT','UUSDT')")[0]['n']
else:
    n_rebal = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} = 'portfolio_rebalancing'")[0]['n']
    n_replace = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} = 'active_replacement'")[0]['n']
    n_manual = q(f"SELECT COUNT(*) as n FROM trades_closed WHERE {reason_col} IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital')")[0]['n']
    n_b6 = 0

total = n_rebal + n_replace + n_manual + n_b6
print(f"\nTargets a marcar:")
print(f"  Rebalanceos pre-fix (< 2026-06-15 20:40): {n_rebal}")
print(f"  Active replacements pre-fix (<2026-06-17): {n_replace}")
print(f"  Manuales pre-producción:                   {n_manual}")
print(f"  B6 bug SL TRX/TON/U:                       {n_b6}")
print(f"  TOTAL:                                     {total}")

if total == 0:
    print("\nNada que marcar.")
    sys.exit(0)

# 6. Ejecutar marcado
print("\nEjecutando marcado...")
if time_col:
    m1 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} = 'portfolio_rebalancing' AND {time_col} < '2026-06-15 20:40:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m2 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} = 'active_replacement' AND {time_col} < '2026-06-17 00:00:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m3 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital') AND {time_col} < '2026-06-18 00:00:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m4 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} = 'stop_loss' AND {time_col} < '2026-06-17 18:00:00' AND symbol IN ('TRXUSDT','TONUSDT','UUSDT') AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
else:
    m1 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} = 'portfolio_rebalancing' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m2 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} = 'active_replacement' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m3 = u(f"UPDATE trades_closed SET is_corrupted = TRUE WHERE {reason_col} IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital') AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
    m4 = 0

print(f"  Rebalanceos marcados:    {m1}")
print(f"  Active replacements:     {m2}")
print(f"  Manuales:                {m3}")
print(f"  B6 bug:                  {m4}")

# 7. Resultado final
rf = q("SELECT COUNT(*) as n FROM trades_closed WHERE is_corrupted = TRUE")
rc = q("SELECT COUNT(*) as n FROM trades_closed WHERE (is_corrupted IS NULL OR is_corrupted = FALSE)")
n_corrupted = rf[0]['n'] if rf else 0
n_clean = rc[0]['n'] if rc else 0

print(f"\n{'='*65}")
print(f"  is_corrupted=TRUE (excluidos del predictor): {n_corrupted}")
print(f"  Trades limpios para aprendizaje:             {n_clean}")
print(f"{'='*65}")

# 8. Mostrar trades limpios para análisis
clean_by_reason = q(f"SELECT {reason_col}, COUNT(*) as n FROM trades_closed WHERE (is_corrupted IS NULL OR is_corrupted = FALSE) GROUP BY {reason_col} ORDER BY n DESC")
print("\nTrades limpios por razón:")
for r in clean_by_reason:
    print(f"  {r[reason_col]}: {r['n']}")

# 9. WR de trades limpios
pnl_col = None
for candidate in ['pnl', 'pnl_usdt', 'realized_pnl', 'profit']:
    if candidate in col_names:
        pnl_col = candidate
        break
if pnl_col:
    wr_data = q(f"SELECT COUNT(*) as total, SUM(CASE WHEN {pnl_col} > 0 THEN 1 ELSE 0 END) as wins, SUM({pnl_col}) as total_pnl, AVG({pnl_col}) as avg_pnl FROM trades_closed WHERE (is_corrupted IS NULL OR is_corrupted = FALSE)")
    if wr_data:
        d = wr_data[0]
        total = d['total'] or 1
        wins = d['wins'] or 0
        wr = wins / total * 100
        print(f"\nEstadísticas trades limpios:")
        print(f"  Total: {total} | Wins: {wins} | WR: {wr:.1f}%")
        print(f"  PnL total: ${float(d['total_pnl'] or 0):.2f} | Avg: ${float(d['avg_pnl'] or 0):.4f}")

print("\nSiguiente: POST /predictor/reset-corrupted para invalidar cache predictor")
