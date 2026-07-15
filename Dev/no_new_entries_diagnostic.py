#!/usr/bin/env python3
"""Diagnose why no new positions are opened.

This script builds an evidence matrix from recent runtime logs without
changing strategy, mode, or live configuration.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAST_2H = ROOT / "last_2h_logs.txt"
DEFAULT_CONTEXT = ROOT / "last_logs_context.txt"
DEFAULT_JSON = ROOT / "Dev" / "no_new_entries_diagnostic.json"
DEFAULT_MD = ROOT / "Dev" / "no_new_entries_diagnostic.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback for mixed encodings from terminal captures.
        return data.decode("latin-1", errors="replace")


@dataclass(frozen=True)
class CounterSpec:
    key: str
    patterns: Tuple[re.Pattern[str], ...]


SPECS: Tuple[CounterSpec, ...] = (
    CounterSpec(
        "paused",
        (
            re.compile(r"pause_new_entries\s*=\s*True", re.IGNORECASE),
            re.compile(r"monitor\s+only", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "empty_candidates",
        (
            re.compile(r"Top buy symbols:\s*\[\]", re.IGNORECASE),
            re.compile(r"No buy signals available", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "nonempty_candidates",
        (
            re.compile(r"Top buy symbols:\s*\[(?!\])", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "prediction_ok",
        (
            re.compile(r"Prediction OK for", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "prediction_zero",
        (
            re.compile(r"Prediction for\s+\w+\s*:\s*0\.00\s*USDT", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "cycle_error",
        (
            re.compile(r"Error in cycle:", re.IGNORECASE),
            re.compile(r"name '\w+' is not defined", re.IGNORECASE),
            re.compile(r"cannot access local variable", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "entry_blocked",
        (
            re.compile(r"SKIPPED", re.IGNORECASE),
            re.compile(r"max entries per symbol reached", re.IGNORECASE),
        ),
    ),
    CounterSpec(
        "entry_executed",
        (
            re.compile(r"opened\s+position", re.IGNORECASE),
            re.compile(r"BUY order", re.IGNORECASE),
            re.compile(r"entry_order_id", re.IGNORECASE),
        ),
    ),
)


def _sample_matches(lines: List[str], patterns: Tuple[re.Pattern[str], ...], limit: int = 3) -> List[str]:
    out: List[str] = []
    for line in lines:
        if any(p.search(line) for p in patterns):
            out.append(line.strip())
            if len(out) >= limit:
                break
    return out


def _count(lines: List[str], patterns: Tuple[re.Pattern[str], ...]) -> int:
    return sum(1 for line in lines if any(p.search(line) for p in patterns))


def _segment(lines: List[str]) -> Dict[str, List[str]]:
    mid = len(lines) // 2
    return {
        "first_half": lines[:mid],
        "second_half": lines[mid:],
    }


def classify(counters: Dict[str, int]) -> str:
    # Dominant blocker priority: pause > no-candidate > cycle-error > gate-rejection.
    if counters.get("paused", 0) >= 3:
        return "pausa_operativa"
    if counters.get("empty_candidates", 0) >= 3 and counters.get("nonempty_candidates", 0) == 0:
        return "falta_estructural_de_candidatos"
    if counters.get("cycle_error", 0) >= 3:
        return "excepcion_de_ciclo"
    if counters.get("entry_blocked", 0) >= 3:
        return "rechazos_por_gates"
    return "indeterminado"


def analyze_log(lines: List[str]) -> Dict[str, object]:
    counters: Dict[str, int] = {}
    evidence: Dict[str, List[str]] = {}
    for spec in SPECS:
        counters[spec.key] = _count(lines, spec.patterns)
        evidence[spec.key] = _sample_matches(lines, spec.patterns)

    by_segment: Dict[str, Dict[str, int]] = {}
    for name, segment_lines in _segment(lines).items():
        by_segment[name] = {spec.key: _count(segment_lines, spec.patterns) for spec in SPECS}

    current_window_category = classify(by_segment.get("second_half", {}))

    return {
        "line_count": len(lines),
        "counters": counters,
        "evidence": evidence,
        "by_segment": by_segment,
        "dominant_category": classify(counters),
        "current_window_category": current_window_category,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose no-new-entries behavior from logs")
    parser.add_argument("--last-2h", type=Path, default=DEFAULT_LAST_2H)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    last_2h_text = _read_text_auto(args.last_2h)
    context_text = _read_text_auto(args.context)

    last_2h_lines = [ln for ln in last_2h_text.splitlines() if ln.strip()]
    context_lines = [ln for ln in context_text.splitlines() if ln.strip()]

    a = analyze_log(last_2h_lines)
    b = analyze_log(context_lines)

    result = {
        "generated_at": utc_now(),
        "scope": {
            "mode": "SPOT_BULL",
            "changes_applied": False,
            "diagnosis_only": True,
        },
        "inputs": {
            "last_2h": str(args.last_2h.relative_to(ROOT)).replace("\\", "/"),
            "context": str(args.context.relative_to(ROOT)).replace("\\", "/"),
        },
        "analysis": {
            "last_2h": a,
            "context": b,
        },
        "current_focus": {
            "last_2h_current_window": a["current_window_category"],
            "context_current_window": b["current_window_category"],
        },
        "root_cause_rank": [
            {"category": a["current_window_category"], "source": "last_2h_second_half"},
            {"category": b["current_window_category"], "source": "context_second_half"},
            {"category": a["dominant_category"], "source": "last_2h_all"},
            {"category": b["dominant_category"], "source": "context_all"},
        ],
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")

    md_lines = [
        "# No New Entries Diagnostic",
        "",
        f"Generated: {result['generated_at']}",
        "",
        "## Dominant Categories",
        f"- last_2h: {a['dominant_category']}",
        f"- context: {b['dominant_category']}",
        f"- last_2h (current window): {a['current_window_category']}",
        f"- context (current window): {b['current_window_category']}",
        "",
        "## last_2h Counters",
    ]
    for key, value in a["counters"].items():
        md_lines.append(f"- {key}: {value}")
    md_lines.extend(["", "## context Counters"])
    for key, value in b["counters"].items():
        md_lines.append(f"- {key}: {value}")

    md_lines.extend(["", "## Evidence Samples (last_2h)"])
    for key, samples in a["evidence"].items():
        md_lines.append(f"- {key}:")
        if samples:
            for sample in samples:
                md_lines.append(f"  - {sample}")
        else:
            md_lines.append("  - (none)")

    args.out_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(json.dumps({
        "out_json": str(args.out_json.relative_to(ROOT)).replace("\\", "/"),
        "out_md": str(args.out_md.relative_to(ROOT)).replace("\\", "/"),
        "dominant_last_2h": a["dominant_category"],
        "dominant_context": b["dominant_category"],
        "current_last_2h": a["current_window_category"],
        "current_context": b["current_window_category"],
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
