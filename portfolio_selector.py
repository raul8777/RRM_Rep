"""
portfolio_selector.py — Combinatorial portfolio selection for SPOT/FUTURES.

Selects the K best orthogonal positions from a filtered candidate set by
solving a QUBO (Quadratic Unconstrained Binary Optimization) problem with a
classical Simulated Quantum Annealing (SQA) solver. The objective maximizes
risk-adjusted expected return (Sharpe-like) while penalizing pairwise
correlation.

Regime-aware:
  - SPOT_BULL / RANGE_SCALP: LONG candidates
  - FUTURES_SHORT: SHORT candidates
  - PRESERVATION: no selection (empty result)

The solver is purely classical; no QPU or external API is required.
"""

import logging
import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A selectable position candidate."""
    symbol: str
    expected_pnl: float           # predicted absolute PnL in USDT
    success_probability: float    # 0..1
    risk_score: float             # 0..100 (higher = riskier)
    atr_pct: float = 0.0          # ATR as % of price; used for risk scaling
    peak_quality_score: float = 0.0  # expected_gain from peak_quality predictor
    metadata: Dict = None        # optional extra data (signal, category, etc.)

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class SelectionResult:
    """Result of portfolio selection."""
    selected: List[Candidate]
    excluded: List[Candidate]
    expected_pnl_total: float
    avg_correlation: float
    objective_score: float
    kelly_multiplier: float      # 0.65..1.0 based on avg correlation
    weights: Dict[str, float]    # capital allocation weights (sum to 1)
    solver: str                  # 'sqa' or 'greedy'


# ── QUBO objective ───────────────────────────────────────────────────────────

def _normalize(values: List[float]) -> List[float]:
    """Min-max normalize to [0, 1]; degenerate case returns zeros."""
    lo = min(values) if values else 0.0
    hi = max(values) if values else 1.0
    span = hi - lo
    if span <= 0:
        return [0.0] * len(values)
    return [(v - lo) / span for v in values]


def _build_qubo_evaluator(
    candidates: List[Candidate],
    correlation_matrix: Dict[Tuple[str, str], float],
    k: int,
    lambda_corr: float,
    lambda_size: float,
    lambda_risk: float,
    min_pnl_scale: float = 1.0,
) -> callable:
    """
    Build an evaluator f(x) where x is a binary vector of length N.

    Returns a higher score for better portfolios. The SQA maximizes this score.
    """
    n = len(candidates)
    if n == 0:
        return lambda x: 0.0

    symbols = [c.symbol for c in candidates]
    pnls = [c.expected_pnl for c in candidates]
    risks = [c.risk_score for c in candidates]

    pnl_norm = _normalize([max(0.0, p) if p is not None else 0.0 for p in pnls])
    risk_norm = _normalize(risks)

    # Peak quality scores — strongest predictor (precision_A 92-96%)
    peak_scores = [c.peak_quality_score for c in candidates]
    peak_norm = _normalize([max(0.0, p) for p in peak_scores])

    # Pre-compute correlation pairs
    corr_pairs: List[Tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            r = correlation_matrix.get((symbols[i], symbols[j]), 0.0)
            if r is None or math.isnan(r):
                r = 0.0
            corr_pairs.append((i, j, r))

    def _eval(x: List[int]) -> float:
        if len(x) != n:
            return float('-inf')

        selected_count = sum(x)

        # Revenue term: Peaks dominates (60%) + predictor PnL (40%)
        revenue = sum((0.6 * peak_norm[i] + 0.4 * pnl_norm[i]) * x[i] for i in range(n)) * min_pnl_scale

        # Correlation penalty: penalize selected highly-correlated pairs
        corr_penalty = 0.0
        for i, j, r in corr_pairs:
            if x[i] and x[j]:
                corr_penalty += r

        # Cardinality penalty: encourage exactly K selections
        size_penalty = (selected_count - k) ** 2

        # Risk penalty: penalize high-risk selections
        risk_penalty = sum(risk_norm[i] * x[i] for i in range(n))

        score = (
            revenue
            - lambda_corr * corr_penalty
            - lambda_size * size_penalty
            - lambda_risk * risk_penalty
        )
        return round(score, 6)

    return _eval


# ── Binary Simulated Quantum Annealing ───────────────────────────────────────

class _BinarySQA:
    """
    Classical Simulated Quantum Annealing for binary vectors.
    Maximizes a given evaluator f(x) where x ∈ {0,1}^N.
    """

    def __init__(self, n: int, seed: Optional[int] = None):
        self.n = n
        if seed is not None:
            random.seed(seed)

    def _random_state(self) -> List[int]:
        return [random.choice([0, 1]) for _ in range(self.n)]

    def _neighbour(self, x: List[int], flip_prob: float) -> List[int]:
        new = list(x)
        for i in range(self.n):
            if random.random() < flip_prob:
                new[i] = 1 - new[i]
        return new

    def optimize(
        self,
        evaluator,
        population_size: int = 20,
        iterations: int = 50,
        start_flip_prob: float = 0.30,
        end_flip_prob: float = 0.02,
        initial_state: Optional[List[int]] = None,
    ) -> Dict:
        if self.n == 0:
            return {"best_state": [], "best_score": 0.0}

        if initial_state is not None and len(initial_state) == self.n:
            pop = [list(initial_state) for _ in range(population_size)]
        else:
            pop = [self._random_state() for _ in range(population_size)]

        scores = [evaluator(p) for p in pop]
        best_idx = max(range(population_size), key=lambda i: scores[i])
        best_state = list(pop[best_idx])
        best_score = scores[best_idx]

        for it in range(iterations):
            flip_prob = start_flip_prob * (1 - it / iterations) + end_flip_prob * (it / iterations)
            new_pop = []
            new_scores = []
            for i, state in enumerate(pop):
                cand = self._neighbour(state, flip_prob)
                cand_score = evaluator(cand)
                if cand_score >= scores[i]:
                    new_pop.append(cand)
                    new_scores.append(cand_score)
                else:
                    delta = scores[i] - cand_score
                    temp = max(1e-9, 1.0 - it / iterations)
                    prob = math.exp(-delta / temp)
                    if random.random() < prob:
                        new_pop.append(cand)
                        new_scores.append(cand_score)
                    else:
                        new_pop.append(state)
                        new_scores.append(scores[i])

                if cand_score > best_score:
                    best_score = cand_score
                    best_state = list(cand)

            pop = new_pop
            scores = new_scores

        return {"best_state": best_state, "best_score": best_score}


# ── Greedy exact fallback for small N ────────────────────────────────────────

def _greedy_select(
    candidates: List[Candidate],
    correlation_matrix: Dict[Tuple[str, str], float],
    k: int,
) -> List[int]:
    """
    Deterministic greedy fallback: pick the highest expected_pnl first,
    then add the candidate that maximizes marginal objective gain while
    minimizing added correlation. Used when N is small enough for brute force
    or as baseline for SQA.
    """
    n = len(candidates)
    if n <= k:
        return [1] * n

    selected: set = set()
    # Start with highest expected_pnl
    best_idx = max(range(n), key=lambda i: candidates[i].expected_pnl)
    selected.add(best_idx)

    while len(selected) < k:
        best_gain = float('-inf')
        best_i = -1
        for i in range(n):
            if i in selected:
                continue
            gain = candidates[i].expected_pnl
            corr_cost = 0.0
            for j in selected:
                r = correlation_matrix.get((candidates[i].symbol, candidates[j].symbol), 0.0)
                if r is None or math.isnan(r):
                    r = 0.0
                corr_cost += r * abs(candidates[j].expected_pnl)
            net_gain = gain - corr_cost
            if net_gain > best_gain:
                best_gain = net_gain
                best_i = i
        if best_i < 0:
            break
        selected.add(best_i)

    return [1 if i in selected else 0 for i in range(n)]


def _brute_force_select(
    candidates: List[Candidate],
    evaluator,
    k: int,
) -> List[int]:
    """Brute-force all subsets of size k for very small N."""
    n = len(candidates)
    if n > 15 or k > n:
        return []

    from itertools import combinations
    best_score = float('-inf')
    best_state = [0] * n
    for combo in combinations(range(n), k):
        state = [0] * n
        for i in combo:
            state[i] = 1
        score = evaluator(state)
        if score > best_score:
            best_score = score
            best_state = state
    return best_state


# ── Public API ───────────────────────────────────────────────────────────────

def select_portfolio(
    candidates: List[Candidate],
    correlation_matrix: Dict[Tuple[str, str], float],
    k: int,
    lambda_corr: float = 1.0,
    lambda_size: float = 5.0,
    lambda_risk: float = 0.5,
    solver: str = "sqa",
    population_size: int = 30,
    iterations: int = 80,
    seed: Optional[int] = None,
) -> SelectionResult:
    """
    Select up to K candidates from the provided list using QUBO optimization.

    Args:
        candidates: list of Candidate objects that already passed quality gates
        correlation_matrix: dict {(sym_a, sym_b): |r|} for the candidate universe
        k: desired portfolio cardinality (max)
        lambda_corr: weight for pairwise correlation penalty
        lambda_size: weight for cardinality penalty (keep high to respect k)
        lambda_risk: weight for individual risk penalty
        solver: 'sqa' (default), 'greedy', or 'brute'
        population_size: SQA population size
        iterations: SQA iterations
        seed: random seed for reproducibility

    Returns:
        SelectionResult with selected candidates, weights, and diagnostics.
    """
    n = len(candidates)
    if n == 0 or k <= 0:
        return SelectionResult(
            selected=[], excluded=[],
            expected_pnl_total=0.0, avg_correlation=0.0,
            objective_score=0.0, kelly_multiplier=1.0,
            weights={}, solver="none",
        )

    if k > n:
        k = n

    evaluator = _build_qubo_evaluator(
        candidates, correlation_matrix, k,
        lambda_corr, lambda_size, lambda_risk,
    )

    # Brute force for very small problems (guaranteed optimum)
    if solver == "brute" or (n <= 12 and solver != "greedy"):
        state = _brute_force_select(candidates, evaluator, k)
        used_solver = "brute"
    elif solver == "greedy":
        state = _greedy_select(candidates, correlation_matrix, k)
        used_solver = "greedy"
    else:
        # SQA solver
        sqa = _BinarySQA(n, seed=seed)
        greedy_state = _greedy_select(candidates, correlation_matrix, k)
        sqa_result = sqa.optimize(
            evaluator,
            population_size=population_size,
            iterations=iterations,
            initial_state=greedy_state,
        )
        state = sqa_result["best_state"]
        used_solver = "sqa"

    selected = [candidates[i] for i, v in enumerate(state) if v]
    excluded = [candidates[i] for i, v in enumerate(state) if not v]

    # Compute diagnostics
    expected_pnl_total = sum(c.expected_pnl for c in selected)
    avg_correlation = _avg_pairwise_correlation(selected, correlation_matrix)
    objective_score = evaluator(state)

    # Kelly multiplier based on average correlation of selected set
    kelly_mult = _kelly_reduction(avg_correlation)

    # Capital weights: proportional to expected PnL, adjusted by risk
    weights = _compute_weights(selected)

    return SelectionResult(
        selected=selected,
        excluded=excluded,
        expected_pnl_total=expected_pnl_total,
        avg_correlation=avg_correlation,
        objective_score=objective_score,
        kelly_multiplier=kelly_mult,
        weights=weights,
        solver=used_solver,
    )


def _avg_pairwise_correlation(
    selected: List[Candidate],
    correlation_matrix: Dict[Tuple[str, str], float],
) -> float:
    if len(selected) < 2:
        return 0.0
    pairs = []
    for i in range(len(selected)):
        for j in range(i + 1, len(selected)):
            r = correlation_matrix.get((selected[i].symbol, selected[j].symbol), 0.0)
            if r is None or math.isnan(r):
                r = 0.0
            pairs.append(r)
    return sum(pairs) / len(pairs) if pairs else 0.0


def _kelly_reduction(avg_r: float, corr_floor: float = 0.60, kelly_min: float = 0.65) -> float:
    if avg_r <= corr_floor:
        return 1.0
    reduction = (avg_r - corr_floor) / (1.0 - corr_floor)
    return round(max(kelly_min, 1.0 - reduction * (1.0 - kelly_min)), 3)


def _compute_weights(selected: List[Candidate]) -> Dict[str, float]:
    if not selected:
        return {}
    # Weight proportional to expected PnL / (1 + risk_score/100)
    # This favors high-return, low-risk candidates.
    raw = []
    for c in selected:
        pnl = max(0.0, c.expected_pnl)
        denom = 1.0 + c.risk_score / 100.0
        raw.append(pnl / denom)
    total = sum(raw)
    if total <= 0:
        # Equal weight if no positive PnL
        w = 1.0 / len(selected)
        return {c.symbol: round(w, 6) for c in selected}
    weights = {selected[i].symbol: round(raw[i] / total, 6) for i in range(len(selected))}
    return weights


# ── Regime-aware wrappers ────────────────────────────────────────────────────

def get_max_k_for_regime(settings, regime_mode: str) -> int:
    """Return the maximum number of positions for the active regime/profile."""
    from config_helpers import get_param
    if regime_mode == "SPOT_BULL":
        return get_param(settings, "max_positions_spot_bull")
    if regime_mode == "RANGE_SCALP":
        return get_param(settings, "max_positions_range_scalp")
    if regime_mode == "FUTURES_SHORT":
        return get_param(settings, "max_positions_futures_short")
    if regime_mode == "PRESERVATION":
        return 0
    return get_param(settings, "max_positions")


def select_for_regime(
    candidates: List[Candidate],
    correlation_matrix: Dict[Tuple[str, str], float],
    settings,
    regime_mode: str,
    open_symbols: Optional[List[str]] = None,
) -> SelectionResult:
    """
    Regime-aware selector wrapper. Combines open positions with new candidates
    when the user wants full portfolio rebalancing (optional).

    Args:
        candidates: new candidates only (already filtered by quality gates)
        correlation_matrix: correlation matrix for all candidate symbols
        settings: application settings
        regime_mode: active regime
        open_symbols: optional existing open symbols to treat as locked-in;
                      they are prepended as fixed selections.

    Returns:
        SelectionResult
    """
    if regime_mode == "PRESERVATION":
        return SelectionResult(
            selected=[], excluded=candidates,
            expected_pnl_total=0.0, avg_correlation=0.0,
            objective_score=0.0, kelly_multiplier=1.0,
            weights={}, solver="none",
        )

    k = get_max_k_for_regime(settings, regime_mode)

    if open_symbols:
        # Mark open symbols as locked candidates with conservative expected PnL
        locked_map = {c.symbol: c for c in candidates if c.symbol in open_symbols}
        locked = []
        extra = []
        for sym in open_symbols:
            if sym in locked_map:
                locked.append(locked_map[sym])
            else:
                extra.append(Candidate(symbol=sym, expected_pnl=0.0, success_probability=0.5, risk_score=50.0))
        locked += extra
        # Reduce K by locked count so we don't exceed the regime limit
        k = max(0, k - len(locked))
        new_result = select_portfolio(
            [c for c in candidates if c.symbol not in open_symbols],
            correlation_matrix, k,
            lambda_corr=getattr(settings, "qubo_lambda_corr", 1.0),
            lambda_size=getattr(settings, "qubo_lambda_size", 5.0),
            lambda_risk=getattr(settings, "qubo_lambda_risk", 0.5),
            solver=getattr(settings, "qubo_solver", "sqa"),
            population_size=getattr(settings, "qubo_population_size", 30),
            iterations=getattr(settings, "qubo_iterations", 80),
            seed=getattr(settings, "qubo_seed", 42),
        )
        all_selected = locked + new_result.selected
        weights = _compute_weights(all_selected)
        return SelectionResult(
            selected=all_selected,
            excluded=new_result.excluded,
            expected_pnl_total=sum(c.expected_pnl for c in all_selected),
            avg_correlation=_avg_pairwise_correlation(all_selected, correlation_matrix),
            objective_score=new_result.objective_score,
            kelly_multiplier=_kelly_reduction(_avg_pairwise_correlation(all_selected, correlation_matrix)),
            weights=weights,
            solver=f"sqa_with_{len(locked)}_locked",
        )

    return select_portfolio(
        candidates, correlation_matrix, k,
        lambda_corr=getattr(settings, "qubo_lambda_corr", 1.0),
        lambda_size=getattr(settings, "qubo_lambda_size", 5.0),
        lambda_risk=getattr(settings, "qubo_lambda_risk", 0.5),
        solver=getattr(settings, "qubo_solver", "sqa"),
        population_size=getattr(settings, "qubo_population_size", 30),
        iterations=getattr(settings, "qubo_iterations", 80),
        seed=getattr(settings, "qubo_seed", 42),
    )
