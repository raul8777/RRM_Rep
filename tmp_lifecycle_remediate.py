import psycopg2, os, json, sys, argparse
from datetime import datetime, timezone

url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')

CATEGORY_THRESHOLDS = {
    "LARGE_CAP":  {"stop_loss_pct": 3.5, "take_profit_pct": 3.5},
    "MIDCAP_ALT": {"stop_loss_pct": 3.5, "take_profit_pct": 3.5},
    "NARRATIVE":  {"stop_loss_pct": 3.5, "take_profit_pct": 7.0},
    "MEME":       {"stop_loss_pct": 2.5, "take_profit_pct": 4.5},
}

TOKEN_CATEGORIES = {
    "BTCUSDT": "LARGE_CAP", "ETHUSDT": "LARGE_CAP", "BNBUSDT": "LARGE_CAP",
    "XRPUSDT": "LARGE_CAP", "SOLUSDT": "MIDCAP_ALT",
    "DOGEUSDT": "MIDCAP_ALT", "RENDERUSDT": "MIDCAP_ALT", "TAOUSDT": "NARRATIVE",
    "FETUSDT": "MIDCAP_ALT", "AVAXUSDT": "MIDCAP_ALT", "ADAUSDT": "MIDCAP_ALT",
    "DOTUSDT": "MIDCAP_ALT", "SUIUSDT": "MIDCAP_ALT", "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME", "LINKUSDT": "MIDCAP_ALT", "NEARUSDT": "MIDCAP_ALT",
    "WLDUSDT": "MIDCAP_ALT", "INJUSDT": "NARRATIVE", "AGIXUSDT": "NARRATIVE",
    "ONDOUSDT": "NARRATIVE", "JUPUSDT": "MIDCAP_ALT", "OPUSDT": "MIDCAP_ALT",
    "HBARUSDT": "MIDCAP_ALT", "AAVEUSDT": "MIDCAP_ALT", "ARBUSDT": "MIDCAP_ALT",
    "MATICUSDT": "MIDCAP_ALT", "ATOMUSDT": "MIDCAP_ALT", "GRTUSDT": "NARRATIVE",
    "AIOZUSDT": "NARRATIVE", "SEIUSDT": "MIDCAP_ALT", "SUSHIUSDT": "MIDCAP_ALT",
    "SENTUSDT": "MIDCAP_ALT", "ZECUSDT": "MIDCAP_ALT", "UNIUSDT": "MIDCAP_ALT",
    "XLMUSDT": "MIDCAP_ALT",
}


def inspect_schema(cur, table):
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return cur.fetchall()


def audit(cur, symbol, event, detail):
    cur.execute(
        """
        INSERT INTO trades_audit (ts, symbol, trade_type, event, detail)
        VALUES (NOW(), %s, 'spot', %s, %s)
        """,
        (symbol, event, json.dumps(detail))
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='apply remediation writes')
    parser.add_argument('--close-link', action='store_true', help='close LINKUSDT zombie (requires --apply)')
    args = parser.parse_args()

    conn = psycopg2.connect(url)
    cur = conn.cursor()

    print("=== trades_open schema ===")
    for col, dtype in inspect_schema(cur, 'trades_open'):
        print(f"  {col}: {dtype}")

    cur.execute("""
        SELECT id, symbol, quantity, price, stop_loss, take_profit, status, metadata
        FROM trades_open
        WHERE side='BUY'
        ORDER BY symbol
    """)
    rows = cur.fetchall()
    print(f"\nOpen BUY positions: {len(rows)}")

    tp_fixes = []
    link_zombie = None

    for pos_id, symbol, qty, entry, sl, tp, status, meta in rows:
        cat = TOKEN_CATEGORIES.get(symbol, 'MIDCAP_ALT')
        cat_tp_pct = CATEGORY_THRESHOLDS.get(cat, {}).get('take_profit_pct', 3.5) / 100.0
        min_tp = float(entry) * (1 + cat_tp_pct)
        meta = meta if isinstance(meta, dict) else (json.loads(meta) if meta else {})

        print(f"\n{symbol}  qty={qty} entry={entry} sl={sl} tp={tp} status={status} cat={cat}")
        if qty is not None and float(qty) <= 1e-12:
            print(f"  -> ZOMBIE DETECTED (qty ~0)")
            link_zombie = (pos_id, symbol, entry)

        if tp is not None and float(tp) < min_tp:
            diff_pct = (min_tp - float(tp)) / float(entry) * 100
            print(f"  -> TP too tight: stored={tp} min_cat={min_tp:.6f} (cat={cat_tp_pct*100}%, diff {diff_pct:.2f}%)")
            tp_fixes.append((pos_id, symbol, tp, min_tp, cat_tp_pct))

    print("\n=== ONDO audit ===")
    cur.execute("""
        SELECT id, symbol, quantity, price, stop_loss, take_profit, status, metadata
        FROM trades_open WHERE symbol='ONDOUSDT' AND side='BUY'
    """)
    for r in cur.fetchall():
        pos_id, symbol, qty, entry, sl, tp, status, meta = r
        meta = meta if isinstance(meta, dict) else (json.loads(meta) if meta else {})
        print(f"  OPEN {symbol} id={pos_id} qty={qty} entry={entry} sl={sl} tp={tp} status={status}")
        print(f"    metadata keys: {sorted(meta.keys())}")
        print(f"    scale_out_done={meta.get('scale_out_done')}")
        print(f"    sso_pnl_snapshot={meta.get('sso_pnl_snapshot')}")
        print(f"    sso_levels_done={meta.get('sso_levels_done')}")
        print(f"    breakeven_done={meta.get('breakeven_done')}")
        print(f"    partial_close={meta.get('partial_close')}")
        print(f"    partial_qty_remaining={meta.get('partial_qty_remaining')}")

    cols = inspect_schema(cur, 'trades_closed')
    col_names = [c for c, _ in cols]
    print(f"  trades_closed columns: {col_names}")
    sel_cols = [c for c in ('id','close_reason','exit_price','pnl','pnl_percentage','exit_time','quantity','exit_commission') if c in col_names]
    order_col = 'exit_time' if 'exit_time' in col_names else 'id'
    if sel_cols:
        cur.execute(
            "SELECT " + ", ".join(sel_cols) + f" FROM trades_closed WHERE symbol='ONDOUSDT' ORDER BY {order_col} DESC LIMIT 5"
        )
        closed = cur.fetchall()
        if not closed:
            print("  No closed ONDO records.")
        for r in closed:
            print(f"  CLOSED " + " | ".join(f"{k}={v}" for k, v in zip(sel_cols, r)))
    else:
        print("  trades_closed has no recognizable columns")

    cur.execute("""
        SELECT ts, event, detail FROM trades_audit
        WHERE symbol='ONDOUSDT'
        ORDER BY ts DESC LIMIT 30
    """)
    print("\n  Recent ONDO audit events:")
    def _detail(d):
        if isinstance(d, dict):
            return d
        if d:
            try:
                return json.loads(d)
            except Exception:
                return {}
        return {}

    for ts, event, detail in cur.fetchall():
        d = _detail(detail)
        print(f"    {ts} {event} | {d.get('action','')} {d.get('reason','')[:80]}")

    print("\n=== LINK zombie audit events ===")
    cur.execute("""
        SELECT ts, event, detail FROM trades_audit
        WHERE symbol='LINKUSDT'
        ORDER BY ts DESC LIMIT 10
    """)
    for ts, event, detail in cur.fetchall():
        d = _detail(detail)
        print(f"  {ts} {event} | {str(d)[:100]}")

    if not args.apply:
        print("\n(DRY RUN - no changes applied. Use --apply [--close-link] to execute.)")
        conn.close()
        return

    print("\n=== APPLYING FIXES ===")
    # TP floor remediation
    for pos_id, symbol, old_tp, new_tp, tp_pct in tp_fixes:
        cur.execute("UPDATE trades_open SET take_profit = %s WHERE id = %s", (new_tp, pos_id))
        audit(cur, symbol, 'tp_floor_remediate', {
            'position_id': str(pos_id),
            'old_tp': float(old_tp),
            'new_tp': float(new_tp),
            'category_tp_pct': tp_pct * 100,
            'reason': 'Stored TP was tighter than category minimum; floored to category TP'
        })
        print(f"  {symbol}: TP updated {old_tp} -> {new_tp:.6f}")

    # LINK zombie cleanup
    if args.close_link and link_zombie:
        pos_id, symbol, entry = link_zombie
        now = datetime.now(timezone.utc)
        # Move zombie to trades_closed and remove from trades_open.
        open_cols = {c for c, _ in inspect_schema(cur, 'trades_open')}
        closed_cols = {c for c, _ in inspect_schema(cur, 'trades_closed')}
        # Fetch the existing open row
        cur.execute(
            "SELECT id, symbol, side, quantity, price, strategy_id, entry_time, entry_commission, pnl_prediction, indicators_at_entry, metadata "
            "FROM trades_open WHERE id = %s", (pos_id,)
        )
        row = cur.fetchone()
        if row:
            _id, _sym, _side, _qty, _entry, _strat, _entry_time, _entry_comm, _pnl_pred, _indic, _meta = row
            _meta = _meta if isinstance(_meta, dict) else (json.loads(_meta) if _meta else {})
            _meta['ghost_cleanup'] = {
                'manual_cleanup_at': now.isoformat(),
                'original_quantity': str(_qty),
                'reason': 'quantity ~0 in trades_open; moved to closed'
            }
            insert_cols = ['symbol','side','quantity','entry_price','exit_price','strategy_id','entry_time','exit_time','pnl','pnl_percentage','close_reason','metadata','entry_commission','exit_commission']
            present = [c for c in insert_cols if c in closed_cols]
            values = [
                _sym, _side, _qty, _entry, _entry, _strat, _entry_time, now,
                0.0, 0.0, 'ghost_balance_mismatch', json.dumps(_meta),
                _entry_comm if _entry_comm is not None else 0.0, 0.0
            ]
            # Add optional fields if present
            if 'pnl_prediction' in closed_cols:
                present.append('pnl_prediction'); values.append(json.dumps(_pnl_pred) if _pnl_pred else None)
            if 'indicators_at_entry' in closed_cols:
                present.append('indicators_at_entry'); values.append(json.dumps(_indic) if _indic else None)
            if 'is_corrupted' in closed_cols:
                present.append('is_corrupted'); values.append(False)
            if 'data_version' in closed_cols:
                present.append('data_version'); values.append(1)
            if 'is_synthetic' in closed_cols:
                present.append('is_synthetic'); values.append(False)
            if 'learning_eligible' in closed_cols:
                present.append('learning_eligible'); values.append(False)
            if 'corrupt' in closed_cols:
                present.append('corrupt'); values.append(False)

            placeholders = ','.join(['%s'] * len(present))
            cur.execute(f"INSERT INTO trades_closed ({','.join(present)}) VALUES ({placeholders})", tuple(values))
            cur.execute("DELETE FROM trades_open WHERE id = %s", (pos_id,))
            audit(cur, symbol, 'ghost_position_closed', {
                'position_id': str(pos_id),
                'quantity': 0,
                'trades_closed_inserted': True,
                'reason': 'quantity ~0 in trades_open; zombie moved to trades_closed'
            })
            print(f"  {symbol}: zombie position id={pos_id} moved to trades_closed and removed from trades_open")
    elif args.close_link:
        print("  --close-link requested but no LINK zombie found")

    conn.commit()
    print("Done.")
    conn.close()


if __name__ == '__main__':
    main()
