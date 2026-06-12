"""
Parameter Optimizer Module

Grid-searches indicator parameters (RSI period, MACD periods, BB std, signal
threshold) using walk-forward backtesting on real Binance klines.

Selection criterion: best Sharpe ratio (avoids overfitting to raw PnL).
Results are persisted to the `optimized_params` table.
"""

import itertools
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Parameter grid ────────────────────────────────────────────────────────────
DEFAULT_GRID = {
    "rsi_period":          [9, 14, 21],
    "macd_fast":           [9, 12],
    "macd_slow":           [21, 26],
    "macd_signal":         [7, 9],
    "bb_period":           [15, 20],
    "bb_std":              [1.5, 2.0, 2.5],
    "buy_score_threshold": [2, 3],
    "ema_fast_period":     [9, 12],
    "ema_slow_period":     [21, 26],
}


class ParameterOptimizer:
    """Grid-searches indicator parameters and persists the best config."""

    def __init__(self, db_client=None, grid: Dict = None):
        self.db = db_client
        self.grid = grid or DEFAULT_GRID

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        symbol: str,
        klines: List[Dict],
        initial_capital: float = 10_000.0,
        top_n: int = 3,
    ) -> Dict[str, Any]:
        """
        Run full grid search for *symbol* using pre-fetched *klines*.

        Returns:
            Dict with best_params, ranking, and whether they were persisted.
        """
        from backtester import Backtester

        combos = self._build_combos()
        logger.info(f"ParameterOptimizer: testing {len(combos)} combinations for {symbol}")

        results = []
        bt = Backtester(initial_capital=initial_capital)

        for params in combos:
            try:
                result = bt.run_backtest_with_params(klines, params, symbol=symbol)
                results.append((params, result))
            except Exception as e:
                logger.debug(f"Combo failed {params}: {e}")
                continue

        if not results:
            return {"status": "no_results", "symbol": symbol}

        # Rank by Sharpe (primary) then win_rate (secondary)
        results.sort(key=lambda x: (x[1].sharpe_ratio, x[1].win_rate), reverse=True)
        best_params, best_result = results[0]

        ranking = [
            {
                "rank": i + 1,
                "params": p,
                "sharpe": r.sharpe_ratio,
                "win_rate": r.win_rate,
                "total_pnl": r.total_pnl,
            }
            for i, (p, r) in enumerate(results[:top_n])
        ]

        run_id = self._persist(symbol, best_params, best_result, klines)

        return {
            "status": "completed",
            "symbol": symbol,
            "best_params": best_params,
            "best_sharpe": best_result.sharpe_ratio,
            "best_win_rate": best_result.win_rate,
            "best_pnl": best_result.total_pnl,
            "ranking": ranking,
            "combos_tested": len(results),
            "run_id": run_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_active_params(self, symbol: str) -> Optional[Dict]:
        """Return the currently active optimized params for *symbol* (or None)."""
        if self.db is None:
            return None
        try:
            sql = """
            SELECT rsi_period, macd_fast, macd_slow, macd_signal,
                   bb_period, bb_std, buy_score_threshold,
                   ema_fast_period, ema_slow_period,
                   sharpe_ratio, win_rate, created_at, run_id
            FROM optimized_params
            WHERE symbol = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
            """
            rows = self.db.execute_query(sql, (symbol,))
            if not rows:
                return None
            r = rows[0]
            return {
                "rsi_period": r["rsi_period"], "macd_fast": r["macd_fast"],
                "macd_slow": r["macd_slow"], "macd_signal": r["macd_signal"],
                "bb_period": r["bb_period"], "bb_std": r["bb_std"],
                "buy_score_threshold": r["buy_score_threshold"],
                "ema_fast_period": r["ema_fast_period"],
                "ema_slow_period": r["ema_slow_period"],
                "sharpe_ratio": r["sharpe_ratio"], "win_rate": r["win_rate"],
                "created_at": str(r["created_at"]), "run_id": str(r["run_id"]),
            }
        except Exception as e:
            logger.warning(f"Could not load active params for {symbol}: {e}")
            return None

    def get_optimization_history(self, symbol: str, limit: int = 5) -> List[Dict]:
        """Return last N optimizer runs for *symbol*."""
        if self.db is None:
            return []
        try:
            sql = """
            SELECT run_id, rsi_period, macd_fast, macd_slow, bb_std,
                   buy_score_threshold, sharpe_ratio, win_rate, total_pnl,
                   is_active, created_at
            FROM optimized_params
            WHERE symbol = %s
            ORDER BY created_at DESC LIMIT %s
            """
            rows = self.db.execute_query(sql, (symbol, limit))
            return [
                {
                    "run_id": str(r["run_id"]), "rsi_period": r["rsi_period"],
                    "macd_fast": r["macd_fast"], "macd_slow": r["macd_slow"],
                    "bb_std": r["bb_std"], "buy_score_threshold": r["buy_score_threshold"],
                    "sharpe_ratio": r["sharpe_ratio"], "win_rate": r["win_rate"],
                    "total_pnl": r["total_pnl"], "is_active": r["is_active"],
                    "created_at": str(r["created_at"]),
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Could not load optimization history: {e}")
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_combos(self) -> List[Dict]:
        keys = list(self.grid.keys())
        values = list(self.grid.values())
        combos = []
        for combo in itertools.product(*values):
            d = dict(zip(keys, combo))
            # Sanity: slow EMA must be > fast EMA
            if d["ema_slow_period"] <= d["ema_fast_period"]:
                continue
            if d["macd_slow"] <= d["macd_fast"]:
                continue
            combos.append(d)
        return combos

    def _persist(
        self, symbol: str, params: Dict, result, klines: List[Dict]
    ) -> Optional[str]:
        if self.db is None:
            return None
        try:
            import json as _json
            run_id = str(uuid.uuid4())
            # Deactivate previous active for this symbol
            self.db.execute_update(
                "UPDATE optimized_params SET is_active = FALSE WHERE symbol = %s",
                (symbol,),
            )
            sql = """
            INSERT INTO optimized_params
                (run_id, symbol, rsi_period, macd_fast, macd_slow, macd_signal,
                 bb_period, bb_std, buy_score_threshold,
                 ema_fast_period, ema_slow_period,
                 sharpe_ratio, win_rate, total_pnl, is_active, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
            """
            self.db.execute_update(sql, (
                run_id, symbol,
                params["rsi_period"], params["macd_fast"], params["macd_slow"],
                params["macd_signal"], params["bb_period"], params["bb_std"],
                params["buy_score_threshold"], params["ema_fast_period"],
                params["ema_slow_period"],
                result.sharpe_ratio, result.win_rate, result.total_pnl,
                _json.dumps({"total_trades": result.total_trades,
                             "max_drawdown": result.max_drawdown}),
            ))
            return run_id
        except Exception as e:
            logger.warning(f"Could not persist optimized params for {symbol}: {e}")
            return None


# Module-level singleton
_optimizer: Optional[ParameterOptimizer] = None


def get_optimizer() -> ParameterOptimizer:
    global _optimizer
    if _optimizer is None:
        try:
            from init_components import get_components
            _optimizer = ParameterOptimizer(db_client=get_components().db_client)
        except Exception:
            _optimizer = ParameterOptimizer()
    return _optimizer
