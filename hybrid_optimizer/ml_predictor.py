"""Lightweight ML ensemble + online bandit for config selection."""
from typing import Dict, List
import math
import random


class LightweightEnsemble:
    """
    Simple ensemble that blends a linear baseline and a tree-like heuristic.
    No external ML libraries required. Predicts PnL of a configuration.
    """

    def __init__(self):
        self.weights = {
            "kelly_max_usdt": 0.001,
            "futures_kelly_max_usdt": 0.0008,
            "risk_reward": 0.5,
            "futures_risk_reward": 0.6,
            "min_signal_strength": 0.3,
        }
        self.bias = 0.0

    def _risk_reward(self, cfg: Dict) -> float:
        sl = cfg.get("stop_loss_pct", 0.025)
        tp = cfg.get("take_profit_pct", 0.035)
        return tp / sl if sl > 0 else 0

    def _futures_risk_reward(self, cfg: Dict) -> float:
        sl = cfg.get("futures_sl_pct", 0.0333)
        tp = cfg.get("futures_tp_pct", 0.0768)
        return tp / sl if sl > 0 else 0

    def predict_pnl(self, cfg: Dict) -> float:
        """Predict daily PnL for a configuration."""
        rr = self._risk_reward(cfg)
        f_rr = self._futures_risk_reward(cfg)
        score = (
            self.weights["kelly_max_usdt"] * cfg.get("kelly_max_usdt", 500)
            + self.weights["futures_kelly_max_usdt"] * cfg.get("futures_kelly_max_usdt", 800)
            + self.weights["risk_reward"] * rr * 100
            + self.weights["futures_risk_reward"] * f_rr * 100
            - self.weights["min_signal_strength"] * (cfg.get("min_signal_strength", 0.55) - 0.40) * 100
            + self.bias
        )
        return round(score, 4)


class ThompsonBandit:
    """
    Multi-armed bandit using Thompson Sampling to choose among candidate configs.
    Each arm is a configuration option; successes/failures updated from live outcomes.
    """

    def __init__(self, configs: List[Dict], seed: int = 42):
        self.configs = configs
        self.arms = {
            i: {"alpha": 1.0, "beta": 1.0, "reward_sum": 0.0, "count": 0}
            for i in range(len(configs))
        }
        random.seed(seed)

    def sample(self) -> int:
        """Select an arm using Thompson Sampling."""
        best_idx = 0
        best_sample = -1
        for i, params in self.arms.items():
            # Beta distribution sample
            sample = self._beta_sample(params["alpha"], params["beta"])
            if sample > best_sample:
                best_sample = sample
                best_idx = i
        return best_idx

    def _beta_sample(self, alpha: float, beta: float) -> float:
        # Marsaglia and Tsang's Gamma sampler for Beta
        x = self._gamma_sample(alpha, 1.0)
        y = self._gamma_sample(beta, 1.0)
        return x / (x + y + 1e-9)

    def _gamma_sample(self, shape: float, scale: float) -> float:
        if shape < 1:
            return self._gamma_sample(shape + 1, scale) * random.random() ** (1 / shape)
        d = shape - 1.0 / 3.0
        c = 1.0 / math.sqrt(9.0 * d)
        while True:
            x = random.gauss(0, 1)
            v = 1.0 + c * x
            if v <= 0:
                continue
            v = v * v * v
            u = random.random()
            if u < 1.0 - 0.0331 * (x * x) * (x * x):
                return d * v * scale
            if math.log(u) < 0.5 * x * x + d * (1.0 - v + math.log(v)):
                return d * v * scale

    def update(self, arm_idx: int, reward: float):
        """Update arm after observing reward."""
        arm = self.arms[arm_idx]
        arm["count"] += 1
        arm["reward_sum"] += reward
        # Normalize reward to success probability estimate
        success = 0.5 + 0.5 * (reward / (abs(reward) + 1.0))
        arm["alpha"] += success
        arm["beta"] += (1 - success)

    def best_arm(self) -> int:
        return max(self.arms, key=lambda i: self.arms[i]["reward_sum"] / max(1, self.arms[i]["count"]))

    def get_stats(self) -> Dict:
        return {
            i: {
                "alpha": arm["alpha"],
                "beta": arm["beta"],
                "mean_reward": arm["reward_sum"] / max(1, arm["count"]),
                "count": arm["count"],
            }
            for i, arm in self.arms.items()
        }
