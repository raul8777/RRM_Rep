import sys
sys.path.insert(0, '/app')
from init_components import get_components
comp = get_components()
db = comp.db_client

r = db.execute_query("SELECT close_reason, COUNT(*) as n FROM trades_closed WHERE is_corrupted=TRUE GROUP BY close_reason ORDER BY n DESC")
print("Corrupted por razón:")
for x in r:
    print(f"  {x['close_reason']}: {x['n']}")

r2 = db.execute_query("SELECT COUNT(*) as n FROM trades_closed WHERE is_corrupted=TRUE")
r3 = db.execute_query("SELECT COUNT(*) as n FROM trades_closed WHERE is_corrupted=FALSE OR is_corrupted IS NULL")
print(f"\nTotal corrupted: {r2[0]['n']}")
print(f"Total limpios:   {r3[0]['n']}")
