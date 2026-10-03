"""Provenance metadata for exchange filter snapshots captured at decision time."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List


def describe_filter_snapshot(
    filters: List[Dict[str, Any]],
    *,
    venue: str,
    symbol: str,
    source: str,
) -> Dict[str, str]:
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "filter_snapshot_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "filter_snapshot_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "filter_snapshot_venue": venue,
        "filter_snapshot_symbol": symbol.upper(),
        "filter_snapshot_source": source,
    }