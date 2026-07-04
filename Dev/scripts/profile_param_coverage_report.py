#!/usr/bin/env python3
"""Report mode/profile parameterization coverage and optionally enforce critical wiring."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set


CRITICAL_FIELDS = {
    "futures_max_new_per_cycle",
    "futures_confirm_red_candle",
    "futures_no_short_btc_4h_threshold",
    "futures_no_short_btc_3h_threshold",
    "futures_no_short_btc_1h_threshold",
    "futures_mass_sl_threshold",
    "futures_mass_sl_cooldown_hours",
    "max_positions_spot_bull",
    "max_positions_range_scalp",
    "max_positions_preservation",
    "max_positions_futures_short",
    "max_total_exposure_pct",
    "max_total_exposure_spot_bull_fear",
    "smart_tp_enabled",
    "smart_tp_max_extensions",
    "smart_tp_extend_score_threshold",
    "scale_out_enabled",
    "scale_out_first_close_pct",
    "futures_smart_tp_enabled",
    "futures_smart_tp_extend_score_threshold",
    "fng_kelly_boost_enabled",
    "sso_target_daily_usd",
    "circuit_breaker_enabled",
    "circuit_breaker_consecutive_failures",
    "circuit_breaker_pause_minutes",
    "screener_min_vol_usdt",
    "screener_futures_min_vol",
    "screener_max_pairs",
    "screener_max_futures_pairs",
    "screener_cache_minutes",
    "screener_snapshot_max_age_minutes",
}


@dataclass
class CoverageReport:
    whitelist_count: int
    scalar_map_count: int
    config_fields_count: int
    overlap_count: int
    whitelist_only: List[str]
    scalar_only: List[str]
    critical_missing_in_whitelist: List[str]
    critical_missing_in_scalar_map: List[str]

    def as_dict(self) -> Dict:
        return {
            "whitelist_count": self.whitelist_count,
            "scalar_map_count": self.scalar_map_count,
            "config_fields_count": self.config_fields_count,
            "overlap_count": self.overlap_count,
            "whitelist_only": self.whitelist_only,
            "scalar_only": self.scalar_only,
            "critical_missing_in_whitelist": self.critical_missing_in_whitelist,
            "critical_missing_in_scalar_map": self.critical_missing_in_scalar_map,
            "ok": not self.critical_missing_in_whitelist and not self.critical_missing_in_scalar_map,
        }


def _extract_set_literal(text: str, marker: str) -> Set[str]:
    idx = text.find(marker)
    if idx < 0:
        return set()
    start = text.find("{", idx)
    if start < 0:
        return set()

    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return set()

    payload = text[start : end + 1]
    try:
        value = ast.literal_eval(payload)
    except Exception:
        return set()

    out: Set[str] = set()
    if isinstance(value, (set, list, tuple)):
        for item in value:
            if isinstance(item, str):
                out.add(item)
    return out


def _extract_scalar_map_keys(text: str) -> Set[str]:
    marker = "_scalar_map = {"
    idx = text.find(marker)
    if idx < 0:
        return set()

    start = text.find("{", idx)
    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return set()

    payload = text[start : end + 1]
    try:
        value = ast.literal_eval(payload)
    except Exception:
        return set()

    out: Set[str] = set()
    if isinstance(value, dict):
        for key in value.keys():
            if isinstance(key, str):
                out.add(key)
    return out


def _extract_config_fields(text: str) -> Set[str]:
    # Matches: name: type = Field(
    matches = re.findall(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*[^=\n]+\s*=\s*Field\(", text, re.MULTILINE)
    return set(matches)


def generate_report(repo_root: Path) -> CoverageReport:
    app_path = repo_root / "app.py"
    run_path = repo_root / "run_trades.py"
    cfg_path = repo_root / "config.py"

    app_text = app_path.read_text(encoding="utf-8")
    run_text = run_path.read_text(encoding="utf-8")
    cfg_text = cfg_path.read_text(encoding="utf-8")

    whitelist = _extract_set_literal(app_text, "_KELLY_EDITABLE_FIELDS")
    scalar_map_keys = _extract_scalar_map_keys(run_text)
    config_fields = _extract_config_fields(cfg_text)

    overlap = whitelist.intersection(scalar_map_keys)
    whitelist_only = sorted(whitelist - scalar_map_keys)
    scalar_only = sorted(scalar_map_keys - whitelist)

    critical_missing_in_whitelist = sorted(CRITICAL_FIELDS - whitelist)
    critical_missing_in_scalar_map = sorted(CRITICAL_FIELDS - scalar_map_keys)

    return CoverageReport(
        whitelist_count=len(whitelist),
        scalar_map_count=len(scalar_map_keys),
        config_fields_count=len(config_fields),
        overlap_count=len(overlap),
        whitelist_only=whitelist_only,
        scalar_only=scalar_only,
        critical_missing_in_whitelist=critical_missing_in_whitelist,
        critical_missing_in_scalar_map=critical_missing_in_scalar_map,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate mode/profile parameterization coverage report")
    parser.add_argument("--repo-root", default=".", help="Repository root path")
    parser.add_argument("--output", help="Write JSON report to this path")
    parser.add_argument("--enforce-critical", action="store_true", help="Return non-zero if critical fields are not fully wired")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    report = generate_report(repo_root)
    payload = report.as_dict()

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=True))

    if args.enforce_critical and (payload["critical_missing_in_whitelist"] or payload["critical_missing_in_scalar_map"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
