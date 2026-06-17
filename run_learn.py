import sys
sys.path.insert(0, '/app')
from init_components import get_components
from learning_engine import LearningEngine

engine = LearningEngine(db_client=get_components().db_client)
loaded = engine.load_synthetic_trades('/app/historical_data/synthetic_trades_full_20260617_155237.json')
print(f'Loaded: {len(loaded)} trades')
result = engine.run_with_synthetic(loaded)
print(f'Status: {result["status"]}')
print(f'Trades analyzed: {result["trades_analyzed"]}')
print(f'  Synthetic: {result["synthetic_count"]}')
print(f'  Real: {result["real_count"]}')
print(f'Symbols updated: {len(result["symbols_updated"])}')
print(f'Symbols: {result["symbols_updated"]}')
acc = result.get("accuracy", {})
print(f'Win rate (synthetic): {acc.get("win_rate", 0):.1%}')
