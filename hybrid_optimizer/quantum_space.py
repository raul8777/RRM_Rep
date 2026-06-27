"""Quantum-inspired optimization for backtest parameter space."""
from typing import Dict, List, Tuple
import random
import math


class SimulatedQuantumAnnealing:
    """
    Refine a promising region of the parameter space using a simulated quantum
    annealing flavor. Keeps a population of configurations (superposition) and
    allows "tunneling" (large jumps) early, then converges to the best measured
    state. This is a classical approximation; it does NOT require a real QPU.
    """

    def __init__(self, param_space: Dict[str, Tuple[float, float]], seed: int = 42):
        self.param_space = param_space
        self.keys = list(param_space.keys())
        random.seed(seed)

    def _random_config(self) -> Dict[str, float]:
        return {
            k: random.uniform(lo, hi)
            for k, (lo, hi) in self.param_space.items()
        }

    def _neighbour(self, cfg: Dict[str, float], tunnel: float) -> Dict[str, float]:
        """Return a neighbour. tunnel∈[0,1] controls jump size (1=big, 0=small)."""
        new = dict(cfg)
        for k in self.keys:
            lo, hi = self.param_space[k]
            span = hi - lo
            # Mix small Gaussian step + possible large tunnel jump
            if random.random() < tunnel:
                new[k] = random.uniform(lo, hi)
            else:
                sigma = span * 0.05
                new[k] = max(lo, min(hi, random.gauss(cfg[k], sigma)))
        return new

    def optimize(
        self,
        evaluator,  # callable(cfg) -> score (higher is better)
        initial_population: List[Dict[str, float]] = None,
        population_size: int = 20,
        iterations: int = 50,
        start_tunnel: float = 0.30,
        end_tunnel: float = 0.02,
    ) -> Dict:
        """Run SQA and return best configuration + history."""
        if initial_population:
            pop = [dict(c) for c in initial_population[:population_size]]
            while len(pop) < population_size:
                pop.append(self._random_config())
        else:
            pop = [self._random_config() for _ in range(population_size)]

        scores = [evaluator(c) for c in pop]
        best_idx = max(range(population_size), key=lambda i: scores[i])
        best_cfg = dict(pop[best_idx])
        best_score = scores[best_idx]

        for it in range(iterations):
            tunnel = start_tunnel * (1 - it / iterations) + end_tunnel * (it / iterations)
            new_pop = []
            new_scores = []
            for i, cfg in enumerate(pop):
                cand = self._neighbour(cfg, tunnel)
                cand_score = evaluator(cand)
                # Accept if better, else probabilistic (quantum-fluctuation-like)
                if cand_score >= scores[i]:
                    new_pop.append(cand)
                    new_scores.append(cand_score)
                else:
                    delta = scores[i] - cand_score
                    temp = max(1e-9, (1 - it / iterations))
                    prob = math.exp(-delta / temp)
                    if random.random() < prob:
                        new_pop.append(cand)
                        new_scores.append(cand_score)
                    else:
                        new_pop.append(cfg)
                        new_scores.append(scores[i])

                if cand_score > best_score:
                    best_score = cand_score
                    best_cfg = dict(cand)

            pop = new_pop
            scores = new_scores

        return {
            "best_config": best_cfg,
            "best_score": best_score,
            "final_population": pop,
            "final_scores": scores,
        }


def qio_refine_best(
    param_space: Dict[str, Tuple[float, float]],
    evaluator,
    top_configs: List[Dict[str, float]],
    seed: int = 42,
) -> Dict:
    """Convenience wrapper: take top configs from LHS and refine with SQA."""
    sqa = SimulatedQuantumAnnealing(param_space, seed=seed)
    return sqa.optimize(
        evaluator,
        initial_population=top_configs,
        population_size=max(20, len(top_configs)),
        iterations=50,
    )
