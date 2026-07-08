#!/usr/bin/env python3
"""Resume new entry trading after a pause.

Sets trading_runtime_state.pause_new_entries to paused=false and records
an operator-driven resume reason. Useful after audits, deployments, or
manual operator pauses.

Usage inside Docker container:
    python /app/Dev/tools/resume_entries.py

Optional arguments:
    --reason TEXT  override the reason stored in the state row
"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg


DEFAULT_DSN = (
    "postgresql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.getenv("DB_USER", "crypto_agent"),
        password=os.getenv("DB_PASSWORD", "crypto_agent_password"),
        host=os.getenv("DB_HOST", "crypto-agent-db"),
        port=os.getenv("DB_PORT", "5432"),
        db=os.getenv("DB_NAME", "crypto_agent"),
    )
)


async def resume_entries(reason: str) -> dict:
    conn = await asyncpg.connect(DEFAULT_DSN)
    try:
        value = json.dumps(
            {
                "paused": False,
                "reason": reason,
                "started_at": None,
                "resumed_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        await conn.execute(
            "UPDATE trading_runtime_state SET value = $1::jsonb, updated_at = now() "
            "WHERE key = $2",
            value,
            "pause_new_entries",
        )
        row = await conn.fetchrow(
            "SELECT key, value, updated_at FROM trading_runtime_state WHERE key = $1",
            "pause_new_entries",
        )
        return dict(row)
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Resume new trading entries")
    parser.add_argument(
        "--reason",
        default="operator resume via Dev/tools/resume_entries.py",
        help="reason stored in trading_runtime_state",
    )
    args = parser.parse_args()
    result = asyncio.run(resume_entries(args.reason))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
