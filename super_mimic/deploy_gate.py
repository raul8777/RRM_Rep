"""Super MIMIC Deploy Gate.

Evaluates whether an experiment's recommended config is safe to deploy using:
  1. Hard rules (PnL/día, drawdown, trades, attractors).
  2. ML advisor (fingerprint similarity to recent winning configs).

If the gate passes, the deployment can be applied to the live config via
`apply_recommended_config()`, which merges fields into `kelly_config_override`
using the same merge semantics as `POST /config/apply`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from super_mimic.meta_learner import SuperMimicMetaLearner


@dataclass
class DeployGateResult:
    """Outcome of a deploy gate evaluation."""
    passed: bool
    score: float  # 0.0 - 1.0 composite confidence
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hard_rules: Dict[str, Any] = field(default_factory=dict)
    ml_advisor: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "hard_rules": self.hard_rules,
            "ml_advisor": self.ml_advisor,
        }


class DeployGate:
    """Evaluate and record Super MIMIC deployments."""

    # Hard thresholds
    MIN_PNL_PER_DAY = 0.0
    MAX_DRAWDOWN_PCT = 15.0
    MIN_TRADES = 3
    MIN_ATTRACTORS = 1
    MIN_PEAK_CAPTURE_RATIO = 0.60
    MIN_CAPITAL_DEPLOYMENT_RATIO = 0.30

    def __init__(self, db_client=None):
        self.db_client = db_client

    def evaluate(self, artifact: Dict[str, Any]) -> DeployGateResult:
        """Run hard rules + ML advisor on a completed experiment artifact."""
        recommended = (artifact.get("recommended_config") or {})
        metrics = self._extract_metrics(artifact)
        landscape = (artifact.get("landscape") or {})
        attractors = landscape.get("attractors") or []

        hard_ok = True
        hard_rules: Dict[str, Any] = {}
        reasons: List[str] = []
        warnings: List[str] = []

        pnl_day = metrics.get("pnl_per_day_usdt", 0.0)
        drawdown = metrics.get("drawdown_pct", 100.0)
        trades = metrics.get("trades", 0)
        n_attractors = len(attractors)
        peak_capture = metrics.get("peak_capture_ratio", 0.0)
        cdr = metrics.get("capital_deployment_ratio", 0.0)

        checks = [
            ("pnl_per_day", pnl_day > self.MIN_PNL_PER_DAY, f"PnL/día ${pnl_day:.2f} > 0"),
            ("drawdown", drawdown < self.MAX_DRAWDOWN_PCT, f"Drawdown {drawdown:.2f}% < {self.MAX_DRAWDOWN_PCT}%"),
            ("trades", trades >= self.MIN_TRADES, f"trades {trades} >= {self.MIN_TRADES}"),
            ("attractors", n_attractors >= self.MIN_ATTRACTORS, f"attractors {n_attractors} >= {self.MIN_ATTRACTORS}"),
            ("peak_capture_ratio", peak_capture >= self.MIN_PEAK_CAPTURE_RATIO, f"peak_capture {peak_capture:.2f} >= {self.MIN_PEAK_CAPTURE_RATIO}"),
            ("capital_deployment_ratio", cdr >= self.MIN_CAPITAL_DEPLOYMENT_RATIO, f"CDR {cdr:.2f} >= {self.MIN_CAPITAL_DEPLOYMENT_RATIO}"),
        ]
        for key, ok, msg in checks:
            hard_rules[key] = {"ok": ok, "message": msg}
            if ok:
                reasons.append(msg)
            else:
                warnings.append(f"FAIL: {msg}")
                hard_ok = False

        # ML advisor: how similar is current fingerprint to recent winners?
        ml_score = 0.5
        ml_reason = "no fingerprint data"
        try:
            fp = artifact.get("fingerprint")
            mode = (artifact.get("design") or {}).get("mode", "")
            profile = (artifact.get("design") or {}).get("profile", "A")
            if fp and mode:
                from super_mimic.meta_learner import RegimeFingerprint
                meta = SuperMimicMetaLearner(self.db_client)
                current_fp = RegimeFingerprint.from_dict(fp)
                warm = meta.recommend_warm_start(
                    current_fp, mode=mode, profile=profile, top_k=5, min_trades=3
                )
                if warm:
                    best_dist = min(r["distance"] for r in warm)
                    ml_score = max(0.0, 1.0 - best_dist)
                    ml_reason = f"best fingerprint distance {best_dist:.3f}"
                else:
                    ml_score = 0.5
                    ml_reason = "no historical winners to compare"
        except Exception as e:
            ml_reason = f"ml advisor error: {e}"

        ml_advisor = {"score": ml_score, "reason": ml_reason}

        # Composite score: hard rules + ML advisor
        score = 0.7 * (1.0 if hard_ok else 0.0) + 0.3 * ml_score

        # Gate passes only if all hard rules pass (ML advisor never blocks, only warns)
        passed = hard_ok

        return DeployGateResult(
            passed=passed,
            score=round(score, 3),
            reasons=reasons,
            warnings=warnings,
            hard_rules=hard_rules,
            ml_advisor=ml_advisor,
        )

    def apply_recommended_config(
        self,
        experiment_id: str,
        recommended_config: Dict[str, Any],
        target: str = "kelly_config_override",
        operator: Optional[str] = None,
        mode: str = "",
        profile: str = "",
    ) -> Dict[str, Any]:
        """Record deployment audit for a recommended config.

        The actual runtime config merge is delegated to the existing governance
        helpers (e.g. `_apply_kelly_config_patch` in app.py) so we reuse the same
        whitelist, persistence, and audit semantics used by PATCH /config/kelly.
        """
        db = self._get_db()

        # Build rollback payload from current runtime override (best-effort)
        rollback = {}
        try:
            rows = db.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key = %s LIMIT 1",
                (target,),
            )
            if rows:
                _raw = rows[0].get("value") or rows[0].get(0)
                if _raw:
                    rollback = json.loads(_raw) if isinstance(_raw, str) else dict(_raw)
        except Exception:
            pass

        # Record deployment audit (always)
        from super_mimic.core import ExperimentRegistry
        reg = ExperimentRegistry(db)
        reg.record_deployment(
            experiment_id=experiment_id,
            target=target,
            mode=mode,
            profile=profile,
            changes=recommended_config,
            rollback_payload=rollback,
            operator=operator,
        )

        return {
            "ok": True,
            "target": target,
            "recorded": True,
            "rollback_fields": list(rollback.keys()) if isinstance(rollback, dict) else [],
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_db(self):
        if self.db_client is not None:
            return self.db_client
        from init_components import get_components
        return get_components().db_client

    def _extract_metrics(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        """Extract comparable metrics from an artifact dict."""
        raw = artifact.get("engine_results", {}).get("summary", {})
        # First try the best recommendation in engine results
        recs = artifact.get("engine_results", {}).get("recommendations", [])
        best = recs[0] if recs else {}

        def get(*keys):
            for k in keys:
                if k in best:
                    return best[k]
                if k in raw:
                    return raw[k]
            return 0.0

        return {
            "pnl_per_day_usdt": get("best_pnl_per_day_usdt", "pnl_per_day_usdt", "pnl_usdt"),
            "win_rate": get("best_win_rate", "win_rate", "win_rate_pct"),
            "sharpe": get("best_sharpe", "sharpe"),
            "trades": get("best_trades", "trades", "total_trades"),
            "drawdown_pct": get("best_drawdown_pct", "drawdown_pct", "max_drawdown_pct"),
            "peak_capture_ratio": get("peak_capture_ratio", "best_peak_capture_ratio"),
            "capital_deployment_ratio": get("capital_deployment_ratio", "best_cdr", "cdr"),
        }
