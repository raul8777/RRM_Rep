"""
mark_corrupted.py — Marca trades obsoletos como is_corrupted=TRUE en DB.
Ejecutar: docker compose exec app python3 Dev/scripts/mark_corrupted.py
"""
import os
import sys

try:
    from init_components import get_components
    comp = get_components()
    db = comp.db_client
    if db is None:
        raise RuntimeError("db_client is None — check DATABASE_URL env var")
except Exception as e:
    print(f"ERROR init: {e}")
    sys.exit(1)

print("=" * 60)
print("MARCADO DE TRADES OBSOLETOS — is_corrupted=TRUE")
print("=" * 60)

def q(sql, params=None):
    rows = db.execute_query(sql, params or [])
    return rows

def u(sql, params=None):
    return db.execute_update(sql, params or [])

# 1. Verificar columna
try:
    rows = q('SELECT COUNT(*) as n FROM closed_trades WHERE is_corrupted = TRUE')
    print(f"Ya marcados como corrupted: {rows[0]['n'] if rows else 0}")
except Exception as e:
    print(f"Columna is_corrupted no existe o error: {e}")
    print("Intentando agregar columna...")
    try:
        u("ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS is_corrupted BOOLEAN DEFAULT FALSE")
        print("Columna agregada.")
    except Exception as e2:
        print(f"No se pudo agregar columna: {e2}")
        sys.exit(1)

# 2. Contar targets antes de marcar
r2 = q("SELECT COUNT(*) as n FROM closed_trades WHERE close_reason = 'portfolio_rebalancing' AND exit_time < '2026-06-15 20:40:00'")
n_rebal = r2[0]['n'] if r2 else 0
print(f"\nRebalanceos pre-fix (antes 2026-06-15 20:40): {n_rebal}")

r3 = q("SELECT COUNT(*) as n FROM closed_trades WHERE close_reason = 'active_replacement' AND exit_time < '2026-06-17 00:00:00'")
n_replace = r3[0]['n'] if r3 else 0
print(f"Active replacements pre-fix (antes 2026-06-17): {n_replace}")

r4 = q("SELECT COUNT(*) as n FROM closed_trades WHERE close_reason = 'stop_loss' AND exit_time < '2026-06-17 18:00:00' AND symbol IN ('TRXUSDT','TONUSDT','UUSDT')")
n_b6 = r4[0]['n'] if r4 else 0
print(f"B6 bug trades TRX/TON/U (SL con pct casi-cero): {n_b6}")

r5 = q("SELECT COUNT(*) as n FROM closed_trades WHERE close_reason IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital') AND exit_time < '2026-06-18 00:00:00'")
n_manual = r5[0]['n'] if r5 else 0
print(f"Cierres manuales pre-producción (manual_rebalance, SL_BROKEN, etc): {n_manual}")

total_to_mark = n_rebal + n_replace + n_b6 + n_manual
print(f"\nTOTAL A MARCAR: {total_to_mark} de 155 trades cerrados")

if total_to_mark == 0:
    print("Nada que marcar. Saliendo.")
    sys.exit(0)

# 3. Ejecutar UPDATE
print("\nEjecutando marcado...")

n1 = u("UPDATE closed_trades SET is_corrupted = TRUE WHERE close_reason = 'portfolio_rebalancing' AND exit_time < '2026-06-15 20:40:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
print(f"  Rebalanceos marcados: {n1}")

n2 = u("UPDATE closed_trades SET is_corrupted = TRUE WHERE close_reason = 'active_replacement' AND exit_time < '2026-06-17 00:00:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
print(f"  Active replacements marcados: {n2}")

n3 = u("UPDATE closed_trades SET is_corrupted = TRUE WHERE close_reason = 'stop_loss' AND exit_time < '2026-06-17 18:00:00' AND symbol IN ('TRXUSDT','TONUSDT','UUSDT') AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
print(f"  B6 bug trades marcados: {n3}")

n4 = u("UPDATE closed_trades SET is_corrupted = TRUE WHERE close_reason IN ('manual_rebalance', 'SL_AT_LIMIT_MANUAL', 'SL_BROKEN_EXCL', 'SL_BROKEN_MANUAL', 'manual_liquidation_idle_capital') AND exit_time < '2026-06-18 00:00:00' AND (is_corrupted IS NULL OR is_corrupted = FALSE)")
print(f"  Manuales pre-producción marcados: {n4}")

# 4. Verificar resultado final
rf = q('SELECT COUNT(*) as n FROM closed_trades WHERE is_corrupted = TRUE')
n_corrupted = rf[0]['n'] if rf else 0

rc = q('SELECT COUNT(*) as n FROM closed_trades WHERE (is_corrupted IS NULL OR is_corrupted = FALSE)')
n_clean = rc[0]['n'] if rc else 0

print(f"\n{'='*60}")
print(f"Trades marcados is_corrupted=TRUE: {n_corrupted}")
print(f"Trades limpios para aprendizaje:   {n_clean}")
print(f"{'='*60}")
print("\nSiguiente paso: POST /predictor/reset-corrupted para limpiar cache del predictor")
print("Luego: POST /learning/run para recalibrar con datos limpios")
