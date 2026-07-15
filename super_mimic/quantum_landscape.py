"""Super MIMIC Quantum Landscape — física de configs.

Trata el espacio de parámetros como un paisaje energético:
- Picos = configs con alto Sharpe/PnL.
- Valles = configs perdedoras.
- Atractores = zonas planas y robustas (baja varianza entre vecinos, buen rendimiento).

El módulo consume la lista de resultados crudos de un engine (MIMIC sweep, backtest,
Monte Carlo) y produce:
  1. Un landscape 2D listo para heatmap.
  2. Una lista de atractores detectados.
  3. Un phase map con zonas operativas (peak, valley, plateau, unstable).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class LandscapePoint:
    """One evaluated point in parameter space."""
    x: float
    y: float
    metric: float
    params: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParameterLandscape:
    """2D grid of a metric over two parameter axes."""
    param_x: str
    param_y: str
    metric: str
    grid_size: int
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    grid: List[List[Optional[float]]]
    points: List[LandscapePoint]
    phase_map: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param_x": self.param_x,
            "param_y": self.param_y,
            "metric": self.metric,
            "grid_size": self.grid_size,
            "x_range": [self.x_min, self.x_max],
            "y_range": [self.y_min, self.y_max],
            "grid": self.grid,
            "phase_map": self.phase_map,
            "point_count": len(self.points),
        }


@dataclass
class Attractor:
    """A robust config: high mean metric, low variance among neighbors, safe DD."""
    params: Dict[str, Any]
    mean_metric: float
    std_metric: float
    max_drawdown: float
    neighbor_count: int
    badge: str = "atractor"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": self.params,
            "mean_metric": self.mean_metric,
            "std_metric": self.std_metric,
            "max_drawdown": self.max_drawdown,
            "neighbor_count": self.neighbor_count,
            "badge": self.badge,
        }


class QuantumLandscapeAnalyzer:
    """Build landscapes and detect attractors from experiment results."""

    def __init__(self, results: Optional[List[Dict[str, Any]]] = None):
        self.results = results or []

    # ── Public API ─────────────────────────────────────────────────────────────

    def build_landscape(
        self,
        param_x: str,
        param_y: str,
        metric: str = "sharpe",
        grid_size: int = 20,
        aggregation: str = "mean",
    ) -> ParameterLandscape:
        """Build a 2D landscape grid for two parameters and one metric."""
        points = self._to_points(param_x, param_y, metric)
        if len(points) < 4:
            return ParameterLandscape(
                param_x=param_x,
                param_y=param_y,
                metric=metric,
                grid_size=grid_size,
                x_min=0.0,
                x_max=0.0,
                y_min=0.0,
                y_max=0.0,
                grid=[],
                points=[],
                phase_map={},
            )

        xs = [p.x for p in points]
        ys = [p.y for p in points]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)

        # Avoid degenerate single-value axes
        if x_min == x_max:
            x_min -= 1.0
            x_max += 1.0
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0

        grid = [[None for _ in range(grid_size)] for _ in range(grid_size)]
        buckets: Dict[Tuple[int, int], List[float]] = {}
        for p in points:
            ix = int((p.x - x_min) / (x_max - x_min) * (grid_size - 1))
            iy = int((p.y - y_min) / (y_max - y_min) * (grid_size - 1))
            ix = max(0, min(grid_size - 1, ix))
            iy = max(0, min(grid_size - 1, iy))
            buckets.setdefault((ix, iy), []).append(p.metric)

        for (ix, iy), vals in buckets.items():
            grid[iy][ix] = float(np.mean(vals)) if aggregation == "mean" else float(np.max(vals))

        landscape = ParameterLandscape(
            param_x=param_x,
            param_y=param_y,
            metric=metric,
            grid_size=grid_size,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            grid=grid,
            points=points,
        )
        landscape.phase_map = self._compute_phase_map(landscape)
        return landscape

    def detect_attractors(
        self,
        metric: str = "sharpe",
        min_metric: float = 0.0,
        max_drawdown: float = 20.0,
        neighbor_std_threshold: float = 0.15,
        radius: float = 0.05,
    ) -> List[Attractor]:
        """Detect robust configs (attractors) in the result cloud."""
        if not self.results:
            return []
        metric_values = [self._extract_metric(r, metric, f"best_{metric}") for r in self.results]
        if all(v is None for v in metric_values):
            return []

        # Use all numeric params for distance; radius is relative in normalized space
        all_params = self._numeric_params()
        if not all_params:
            return []

        # Normalize each param dimension to [0, 1]
        mins = {k: min(r.get(k, 0.0) for r in self.results if r.get(k) is not None) for k in all_params}
        maxs = {k: max(r.get(k, 0.0) for r in self.results if r.get(k) is not None) for k in all_params}

        def normalized(row: Dict[str, Any]) -> np.ndarray:
            vec = []
            for k in all_params:
                v = row.get(k, 0.0)
                denom = maxs[k] - mins[k]
                nv = (v - mins[k]) / denom if denom > 0 else 0.0
                vec.append(nv)
            return np.array(vec)

        vectors = [normalized(r) for r in self.results]

        attractors = []
        for i, row in enumerate(self.results):
            vi = vectors[i]
            neighbors = []
            neighbor_rows = []
            for j, vj in enumerate(vectors):
                if i == j:
                    continue
                dist = float(np.linalg.norm(vi - vj))
                if dist <= radius:
                    neighbors.append(metric_values[j])
                    neighbor_rows.append(self.results[j])
            if len(neighbors) < 3:
                continue
            mean_metric = float(np.mean(neighbors))
            std_metric = float(np.std(neighbors))
            if mean_metric < min_metric:
                continue
            if std_metric > neighbor_std_threshold:
                continue

            # Check drawdown across neighbors
            dd_values = [
                self._extract_metric(r, "drawdown_pct", "best_drawdown_pct", "max_drawdown_pct")
                for r in neighbor_rows
            ]
            max_dd = max(dd_values) if dd_values else 100.0
            if max_dd > max_drawdown:
                continue

            attractors.append(
                Attractor(
                    params={k: row.get(k) for k in all_params},
                    mean_metric=mean_metric,
                    std_metric=std_metric,
                    max_drawdown=max_dd,
                    neighbor_count=len(neighbors),
                )
            )

        # Deduplicate by params (keep best mean_metric)
        seen: Dict[str, Attractor] = {}
        for a in sorted(attractors, key=lambda x: x.mean_metric, reverse=True):
            key = json.dumps({k: round(v, 6) for k, v in a.params.items()}, sort_keys=True)
            if key not in seen:
                seen[key] = a
        return list(seen.values())

    def phase_summary(self, landscape: ParameterLandscape) -> Dict[str, Any]:
        """Return summary of phase map zones."""
        return landscape.phase_map

    # ── Internals ─────────────────────────────────────────────────────────────

    def _to_points(
        self, param_x: str, param_y: str, metric: str
    ) -> List[LandscapePoint]:
        """Convert raw results to LandscapePoint objects."""
        points = []
        for r in self.results:
            x = r.get(param_x)
            y = r.get(param_y) if param_y != "_dummy_x" else 0.0
            if x is None or y is None:
                continue
            try:
                x = float(x)
                y = float(y)
            except (TypeError, ValueError):
                continue
            m = self._extract_metric(r, metric, f"best_{metric}")
            if m is None:
                continue
            points.append(
                LandscapePoint(
                    x=x,
                    y=y,
                    metric=float(m),
                    params={k: r.get(k) for k in self._numeric_params()},
                    raw=r,
                )
            )
        return points

    def _extract_metric(self, row: Dict[str, Any], *keys: str) -> Optional[float]:
        for k in keys:
            if k in row and row[k] is not None:
                try:
                    return float(row[k])
                except (TypeError, ValueError):
                    continue
        return None

    def _numeric_params(self) -> List[str]:
        """Return parameter keys that are numeric and vary across results."""
        if not self.results:
            return []
        candidates = set()
        for r in self.results:
            for k, v in r.items():
                if k in ("sharpe", "best_sharpe", "pnl_per_day_usdt", "best_pnl_per_day_usdt",
                         "drawdown_pct", "best_drawdown_pct", "max_drawdown_pct", "win_rate",
                         "best_win_rate", "trades", "best_trades", "status", "error"):
                    continue
                if isinstance(v, (int, float)):
                    candidates.add(k)
        # Keep only params that actually vary
        varied = []
        for k in sorted(candidates):
            vals = {r.get(k) for r in self.results if r.get(k) is not None}
            if len(vals) > 1:
                varied.append(k)
        return varied

    def _compute_phase_map(self, landscape: ParameterLandscape) -> Dict[str, Any]:
        """Classify grid cells into phase zones."""
        flat = [v for row in landscape.grid for v in row if v is not None]
        if not flat:
            return {}
        mean_val = float(np.mean(flat))
        std_val = float(np.std(flat))
        if std_val == 0:
            std_val = 1.0

        zones = {"peak": 0, "valley": 0, "plateau": 0, "unstable": 0}
        for row in landscape.grid:
            for v in row:
                if v is None:
                    continue
                z_score = (v - mean_val) / std_val
                if z_score >= 1.5:
                    zones["peak"] += 1
                elif z_score <= -1.5:
                    zones["valley"] += 1
                elif abs(z_score) <= 0.5:
                    zones["plateau"] += 1
                else:
                    zones["unstable"] += 1
        return {
            "zones": zones,
            "mean": mean_val,
            "std": std_val,
            "max": float(np.max(flat)),
            "min": float(np.min(flat)),
        }
