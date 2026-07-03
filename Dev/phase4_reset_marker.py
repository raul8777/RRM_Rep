#!/usr/bin/env python3
import json
import uuid
from datetime import datetime, timezone
import sys

sys.path.insert(0, '/app')
from db_client import DatabaseClient

DB_URL = 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent'
KEY = 'performance_changepoint_PERF_3Y60K_PRIORITY_2026-07-02'


def main():
    db = DatabaseClient(DB_URL)

    rows = db.execute_query(
        "SELECT key, value, updated_at FROM trading_runtime_state "
        "WHERE key IN (%s, %s) ORDER BY key",
        ('pause_new_entries', KEY),
    )
    print('before:', rows)

    payload_obj = {
        'changepoint_id': 'PERF_3Y60K_PRIORITY_2026-07-02-reset',
        'base_run_id': 61556,
        'root_session_id': str(uuid.uuid4()),
        'reset_reason': 'post_gate1_gate2_remediation',
        'reset_at': datetime.now(timezone.utc).isoformat(),
    }
    payload = json.dumps(payload_obj)
    db.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (KEY, payload, payload),
    )

    after = db.execute_query(
        "SELECT key, value, updated_at FROM trading_runtime_state "
        "WHERE key IN (%s, %s) ORDER BY key",
        ('pause_new_entries', KEY),
    )
    print('after:', after)


if __name__ == '__main__':
    main()
