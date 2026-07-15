path = '/app/app.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Locate insertion point: after "_sp_esc_n = len(pos_list)" within the per-symbol open positions loop
insert_idx = None
for i, line in enumerate(lines):
    if '_sp_esc_n = len(pos_list)' in line:
        insert_idx = i + 1
        break

if insert_idx is None:
    print('insert marker not found')
    raise SystemExit(1)

new_block = '''            # Realized PnL from partial closes for this symbol (e.g. SSO scale-outs)
            _sp_realized_pnl = 0.0
            try:
                if db_client:
                    _sp_real_rows = db_client.execute_query(
                        "SELECT COALESCE(SUM(pnl), 0) AS realized FROM trades_closed "
                        "WHERE symbol=%s AND metadata->>'partial_close' = 'true'",
                        (symbol,),
                    )
                    if _sp_real_rows:
                        _sp_realized_pnl = float(_sp_real_rows[0].get('realized', 0) or 0)
            except Exception:
                _sp_realized_pnl = 0.0
'''

# Insert block
lines.insert(insert_idx, new_block)

# Now add realized fields into open_list dict. Find line containing '"unrealized_pnl_mxn": round(_sp_total_pnl * _usd_mxn, 2),'
for i in range(insert_idx, len(lines)):
    if '"unrealized_pnl_mxn": round(_sp_total_pnl * _usd_mxn, 2),' in lines[i]:
        # insert after this line
        lines.insert(i+1, '''                "realized_pnl": round(_sp_realized_pnl, 4),
                "realized_pnl_mxn": round(_sp_realized_pnl * _usd_mxn, 2),
                "total_pnl": round(_sp_total_pnl + _sp_realized_pnl, 4),
                "total_pnl_mxn": round((_sp_total_pnl + _sp_realized_pnl) * _usd_mxn, 2),
''')
        break

# Update open_positions summary: add total_realized_pnl and total_pnl after total_unrealized_pnl
for i, line in enumerate(lines):
    if '"total_unrealized_pnl": _open_unrealized_total,' in line:
        # insert after this line
        _open_realized_total = '_open_realized_total = round(sum(float(p.get(\'realized_pnl\', 0.0) or 0.0) for p in open_list), 4)'
        _open_total_pnl = '_open_total_pnl = round(_open_unrealized_total + _open_realized_total, 4)'
        lines.insert(i+1, f'        {_open_realized_total}\n        {_open_total_pnl}\n        "total_realized_pnl": _open_realized_total,\n        "total_pnl": _open_total_pnl,\n')
        break

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('app.py patched')
