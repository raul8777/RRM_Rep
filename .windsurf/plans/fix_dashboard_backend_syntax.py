path='/app/app.py'
with open(path,'r',encoding='utf-8') as f:
    lines=f.readlines()
# Find and replace the malformed block
old = '''        _open_unrealized_total = round(sum(float(p.get('unrealized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_context_counts = {
            "legacy": sum(1 for p in open_list if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in open_list if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in open_list if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in open_list if p.get('config_context_state') == 'unknown'),
        }
        result["open_positions"] = {
            "positions": open_list,
            "count": len(open_list),
            "spot_count": _spot_count,
            "futures_count": _fut_count,
            "total_unrealized_pnl": _open_unrealized_total,
        _open_realized_total = round(sum(float(p.get('realized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_total_pnl = round(_open_unrealized_total + _open_realized_total, 4)
        "total_realized_pnl": _open_realized_total,
        "total_pnl": _open_total_pnl,
            "legacy_count": _open_context_counts["legacy"],
            "partial_context_count": _open_context_counts["partial"],
            "current_context_count": _open_context_counts["current"],
            "unknown_context_count": _open_context_counts["unknown"],
        }
'''
new = '''        _open_unrealized_total = round(sum(float(p.get('unrealized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_realized_total = round(sum(float(p.get('realized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_total_pnl = round(_open_unrealized_total + _open_realized_total, 4)
        _open_context_counts = {
            "legacy": sum(1 for p in open_list if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in open_list if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in open_list if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in open_list if p.get('config_context_state') == 'unknown'),
        }
        result["open_positions"] = {
            "positions": open_list,
            "count": len(open_list),
            "spot_count": _spot_count,
            "futures_count": _fut_count,
            "total_unrealized_pnl": _open_unrealized_total,
            "total_realized_pnl": _open_realized_total,
            "total_pnl": _open_total_pnl,
            "legacy_count": _open_context_counts["legacy"],
            "partial_context_count": _open_context_counts["partial"],
            "current_context_count": _open_context_counts["current"],
            "unknown_context_count": _open_context_counts["unknown"],
        }
'''
if old not in ''.join(lines):
    print('old block not found')
    raise SystemExit(1)
text=''.join(lines).replace(old,new,1)
with open(path,'w',encoding='utf-8') as f:
    f.write(text)
print('fixed')
