import sys, json
sys.path.insert(0, '/app')
from config import get_settings
from db_client import DatabaseClient

settings = get_settings()
url = settings.db_url or (
    f"postgresql://{settings.db_user}:{settings.db_password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)
print('db url present:', bool(url))
db = DatabaseClient(url)
rows = db.execute_query("SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'")
print('rows', len(rows))
if rows:
    v = rows[0].get('value')
    if isinstance(v, str):
        data = json.loads(v)
    else:
        data = v
    print(json.dumps(data, indent=2, default=str))
else:
    print('no override')
