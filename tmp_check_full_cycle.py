import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Dev.scripts.mimic_mode_profile_sweep import _load_windows, _run_single_config, _build_profile_overrides
from simulation.mimic_lifecycle_adapter import run_lifecycle_simulation, _default_spot_bull_config

windows = _load_windows(['win_20260415_20260430'])
window = windows[0]

import os
repo_root = os.path.dirname(os.path.abspath(__file__))
all_symbols = window.get('symbols', window.get('baseline_symbols', []))
symbols = [s for s in all_symbols if os.path.exists(os.path.join(repo_root, 'historical_data', f'{s}_1h.parquet'))]

from config import get_settings
from datetime import datetime
settings = get_settings()
settings.dry_run = True
settings.allow_real_trading = False

res = run_lifecycle_simulation(
    settings=settings,
    symbols=symbols,
    interval='1h',
    start=datetime.fromisoformat(window['start']),
    end=datetime.fromisoformat(window['end']),
    initial_usdt=2910.0,
    forced_profile='A',
    lifecycle_config=_default_spot_bull_config(),
    data_dir=os.path.join(repo_root, 'historical_data'),
)
print('cycles:', res['cycles'])
print('final balance:', res['final_balance'])
for i, r in enumerate(res['results']):
    if i in (0, 1, 50, 100, 200, 300, 360):
        s = r['summary']
        print(f"cycle {i}: value={r['total_value']:.2f} signals={s['signals']} entries={len(s['entries'])} exits={len(s['exits'])}")
        if s['entries']:
            for e in s['entries']:
                print(f"  entry: {e['symbol']} qty={e['quantity']:.4f} usdt={e['position_usdt']:.2f}")
        if s['exits']:
            for e in s['exits']:
                print(f"  exit: {e}")
