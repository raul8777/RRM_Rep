from init_components import get_components
import json
from datetime import datetime, timezone

comp = get_components()
closed = comp.position_manager.get_closed_positions(limit=200)
results = []
for c in closed:
    exit_time = getattr(c, 'exit_time', None)
    if not exit_time:
        continue
    if isinstance(exit_time, datetime):
        et = exit_time
    elif isinstance(exit_time, str):
        et = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
    else:
        continue
    if et >= datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc) and et < datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc):
        results.append({
            'symbol': c.symbol,
            'qty': float(getattr(c, 'quantity', 0)),
            'entry_price': float(getattr(c, 'entry_price', 0)),
            'exit_price': float(getattr(c, 'exit_price', 0)),
            'pnl': float(getattr(c, 'pnl', 0)),
            'pnl_pct': float(getattr(c, 'pnl_pct', 0)),
            'close_reason': getattr(c, 'close_reason', None),
            'entry_time': str(getattr(c, 'entry_time', None)),
            'exit_time': str(getattr(c, 'exit_time', None)),
            'metadata': getattr(c, 'metadata', None),
        })

print(json.dumps({
    'count': len(results),
    'total_pnl': round(sum(r['pnl'] for r in results), 2),
    'trades': results
}, default=str, indent=2))
