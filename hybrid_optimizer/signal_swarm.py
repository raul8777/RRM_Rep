"""Multi-agent swarm for validating trading signals and configurations."""
from typing import Dict, List
from dataclasses import dataclass


@dataclass
class RobotVote:
    name: str
    score: float  # 0..1
    weight: float
    reason: str


class SignalSwarm:
    """
    Ensemble of independent robots that vote on a signal or config.
    Robots: momentum, risk, correlation, regime.
    """

    def __init__(self, settings=None):
        self.settings = settings

    def evaluate_signal(
        self,
        symbol: str,
        signal: Dict,
        regime: str,
        open_positions: List[Dict],
    ) -> Dict:
        """Return swarm consensus for a single signal."""
        votes = []

        # Robot: momentum
        strength = float(signal.get("strength", 0))
        vol_spike = float(signal.get("vol_spike", 0))
        momentum_score = 0.0
        if strength >= 0.70 and vol_spike >= 1.5:
            momentum_score = 1.0
        elif strength >= 0.55 and vol_spike >= 1.0:
            momentum_score = 0.7
        elif strength >= 0.40:
            momentum_score = 0.4
        votes.append(RobotVote("momentum", momentum_score, 1.0, f"strength={strength}, vol={vol_spike}"))

        # Robot: risk (Kelly sizing sanity)
        risk_score = 0.0
        final_usdt = float(signal.get("final_usdt", 0))
        if self.settings:
            kelly_max = getattr(self.settings, "kelly_max_usdt", 500)
            if final_usdt <= kelly_max * 1.2:
                risk_score = 1.0
            elif final_usdt <= kelly_max * 1.5:
                risk_score = 0.6
            else:
                risk_score = 0.2
        votes.append(RobotVote("risk", risk_score, 0.8, f"final_usdt={final_usdt}"))

        # Robot: correlation (avoid same-sector overload)
        same_symbol_count = sum(1 for p in open_positions if p.get("symbol") == symbol)
        correlation_score = 1.0 if same_symbol_count == 0 else max(0.0, 1.0 - same_symbol_count * 0.3)
        votes.append(RobotVote("correlation", correlation_score, 0.6, f"same_symbol={same_symbol_count}"))

        # Robot: regime
        regime_score = 0.0
        if regime in ("SPOT_BULL", "RANGE_SCALP") and signal.get("side") == "BUY":
            regime_score = 1.0
        elif regime == "FUTURES_SHORT" and signal.get("side") in ("SELL", "SHORT"):
            regime_score = 1.0
        elif regime == "PRESERVATION":
            regime_score = 0.3
        else:
            regime_score = 0.5
        votes.append(RobotVote("regime", regime_score, 1.0, f"regime={regime}, side={signal.get('side')}"))

        total_weight = sum(v.weight for v in votes)
        consensus = sum(v.score * v.weight for v in votes) / total_weight if total_weight else 0

        return {
            "consensus": round(consensus, 4),
            "votes": [v.__dict__ for v in votes],
            "threshold": 0.55,
            "pass": consensus >= 0.55,
        }

    def evaluate_config(self, config: Dict, regime: str, recent_pnl: float = 0) -> Dict:
        """Evaluate a parameter configuration across market scenarios."""
        votes = []

        # Range sanity
        sl = config.get("stop_loss_pct", 0.025)
        tp = config.get("take_profit_pct", 0.035)
        risk_reward = tp / sl if sl > 0 else 0
        rr_score = 1.0 if risk_reward >= 1.2 else 0.5
        votes.append(RobotVote("risk_reward", rr_score, 1.0, f"RR={risk_reward:.2f}"))

        # Regime alignment
        if regime == "FUTURES_SHORT":
            fut_sl = config.get("futures_sl_pct", 0.03)
            fut_tp = config.get("futures_tp_pct", 0.07)
            fut_rr = fut_tp / fut_sl if fut_sl > 0 else 0
            regime_score = 1.0 if fut_rr >= 1.5 else 0.5
        else:
            regime_score = 0.7
        votes.append(RobotVote("regime", regime_score, 0.8, f"regime={regime}"))

        # Recent performance
        perf_score = 1.0 if recent_pnl >= 0 else 0.5
        votes.append(RobotVote("recent_perf", perf_score, 0.5, f"pnl={recent_pnl:.2f}"))

        total_weight = sum(v.weight for v in votes)
        consensus = sum(v.score * v.weight for v in votes) / total_weight if total_weight else 0
        return {
            "consensus": round(consensus, 4),
            "votes": [v.__dict__ for v in votes],
            "pass": consensus >= 0.60,
        }
