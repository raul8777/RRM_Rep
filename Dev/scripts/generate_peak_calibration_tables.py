"""Generate calibration tables from signal_peak_outcomes.jsonl.

Produces:
  - Dev/benchmarks/super_mimic/predictive_sweep/peak_calibration_tables.md
  - Dev/benchmarks/super_mimic/predictive_sweep/peak_calibration_summary.json
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

OUT_DIR = Path("Dev/benchmarks/super_mimic/predictive_sweep")
OUT_JSONL = OUT_DIR / "signal_peak_outcomes.jsonl"
OUT_MD = OUT_DIR / "peak_calibration_tables.md"
OUT_SUMMARY = OUT_DIR / "peak_calibration_summary.json"


def pct(values: List[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def classify_by_peak(r: Dict[str, Any]) -> str:
    return r.get("peak_label", "REJECT")


def main() -> None:
    records: List[Dict[str, Any]] = []
    for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))

    by_reg: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in records:
        by_reg[r["regime"]][classify_by_peak(r)].append(r)

    features = [
        "expected_pnl",
        "success_probability",
        "confluence",
        "range_score",
        "range_position",
        "rsi_4h",
        "vol_spike",
        "short_term_score",
        "atr_pct",
        "peak_quality",
    ]

    md_lines = ["# Tablas de calibración A/B/C por régimen\n"]
    md_lines.append(
        "Clasificación por pico a 12h: A ≥ 5%, B 2.5-5%, C 1-2.5%, Rechazo < 1%.\n"
    )

    summary: Dict[str, Any] = {
        "total_signals": len(records),
        "classification_rule": "A>=0.05 B>=0.025 C>=0.01 else REJECT",
        "regimes": {},
    }

    for regime in sorted(by_reg.keys()):
        groups = by_reg[regime]
        counts = {cls: len(groups.get(cls, [])) for cls in ["A", "B", "C", "REJECT"]}
        total = sum(counts.values())
        summary["regimes"][regime] = {"counts": counts}

        md_lines.append(f"## Régimen: {regime}\n")
        md_lines.append(f"Total señales: {total}\n")

        md_lines.append("### Distribución de picos por clase\n")
        md_lines.append(
            "| clase | count | % | mean peak_12h | p25 peak_12h | p50 peak_12h | p75 peak_12h | mean peak_4h | p75 peak_4h |\n"
        )
        md_lines.append("|---|---|---|---|---|---|---|---|---|\n")
        for cls in ["A", "B", "C", "REJECT"]:
            arr = groups.get(cls, [])
            if not arr:
                continue
            p12 = [r["peak_12h"] for r in arr]
            p4 = [r["peak_4h"] for r in arr]
            md_lines.append(
                f"| {cls} | {len(arr)} | {len(arr)/total*100:.1f} | "
                f"{sum(p12)/len(p12):.4f} | {pct(p12,25):.4f} | {pct(p12,50):.4f} | "
                f"{pct(p12,75):.4f} | {sum(p4)/len(p4):.4f} | {pct(p4,75):.4f} |\n"
            )

        md_lines.append("\n### Percentiles de features por clase\n")
        md_lines.append("| feature | clase | p10 | p25 | p50 | p75 | p90 |\n")
        md_lines.append("|---|---|---|---|---|---|---|\n")
        for feat in features:
            for cls in ["A", "B", "C", "REJECT"]:
                vals = [
                    r.get(feat)
                    for r in groups.get(cls, [])
                    if r.get(feat) is not None
                ]
                vals = [v for v in vals if isinstance(v, (int, float))]
                if not vals:
                    continue
                md_lines.append(
                    f"| {feat} | {cls} | {pct(vals,10):.4f} | {pct(vals,25):.4f} | "
                    f"{pct(vals,50):.4f} | {pct(vals,75):.4f} | {pct(vals,90):.4f} |\n"
                )
        md_lines.append("\n")

    # Proposed peak_quality score thresholds per regime based on class medians.
    proposed: Dict[str, Any] = {}
    for regime in sorted(by_reg.keys()):
        groups = by_reg[regime]
        a_pq = [r["peak_quality"] for r in groups.get("A", [])]
        b_pq = [r["peak_quality"] for r in groups.get("B", [])]
        c_pq = [r["peak_quality"] for r in groups.get("C", [])]

        proposed[regime] = {
            "peak_quality": {
                "A_min": round(pct(a_pq, 25) if a_pq else 0.0, 4),
                "B_min": round(pct(b_pq, 25) if b_pq else 0.0, 4),
                "C_min": round(pct(c_pq, 10) if c_pq else 0.0, 4),
            },
            "percentile_recommendation": {
                "A": "score >= p85",
                "B": "score p50-p85",
                "C": "score p15-p50",
                "REJECT": "score < p15",
            },
        }

    summary["proposed_thresholds"] = proposed

    md_lines.append("## Umbrales propuestos (borrador)\n")
    md_lines.append(
        "Basados en p25 de A, p25 de B y p10 de C usando `peak_quality`.\n\n"
    )
    for regime, th in proposed.items():
        md_lines.append(f"### {regime}\n")
        md_lines.append("| perfil | peak_quality min | recomendación percentil |\n")
        md_lines.append("|---|---|---|\n")
        md_lines.append(
            f"| A | {th['peak_quality']['A_min']} | {th['percentile_recommendation']['A']} |\n"
        )
        md_lines.append(
            f"| B | {th['peak_quality']['B_min']} | {th['percentile_recommendation']['B']} |\n"
        )
        md_lines.append(
            f"| C | {th['peak_quality']['C_min']} | {th['percentile_recommendation']['C']} |\n"
        )
        md_lines.append(
            f"| REJECT | < {th['peak_quality']['C_min']} | {th['percentile_recommendation']['REJECT']} |\n"
        )
        md_lines.append("\n")

    md_lines.append("## Notas\n")
    md_lines.append(
        "- `peak_quality` combina `atr_pct`, `expected_pnl`, categoría, calidad por símbolo y momentum condicionado a régimen.\n"
    )
    md_lines.append(
        "- A es la clase más pequeña (~10% de señales) y representa los picos ≥5% a 12h.\n"
    )
    md_lines.append(
        "- El score separa mejor la media de pico que la clase exacta; se usa para rankear y concentrar capital.\n"
    )

    OUT_MD.write_text("".join(md_lines), encoding="utf-8")
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_SUMMARY}")


if __name__ == "__main__":
    main()
