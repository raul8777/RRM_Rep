"""
Portfolio Manager Module

Manages intelligent capital allocation and pair selection for trading.
"""

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from risk_calculator import RiskCalculator
from config import Settings
from pnl_predictor import PnLPredictor
from pnl_service import get_predictor
import pnl_service


def _get_bnb_reserve_usdt(settings) -> dict:
    """Return per-wallet BNB reserve dict: spot_reserve_usdt, futures_reserve_usdt, total_reserve_usdt."""
    try:
        from binance_commission import get_bnb_reserve_usdt
        return get_bnb_reserve_usdt(settings)
    except Exception:
        return {'spot_reserve_usdt': 0.0, 'futures_reserve_usdt': 0.0, 'total_reserve_usdt': 0.0}

logger = logging.getLogger(__name__)


@dataclass
class PairRanking:
    """Represents a ranked trading pair."""
    symbol: str
    confidence_score: float
    signal_score: float
    technical_score: float
    news_score: float
    risk_score: float
    combined_score: float


@dataclass
class CapitalAllocation:
    """Represents capital allocation for a pair."""
    symbol: str
    capital_usdt: float
    percentage: float
    risk_adjusted: bool


class PortfolioManager:
    """Manages intelligent capital allocation and pair selection."""
    
    def __init__(self, binance_client, settings: Settings, position_manager=None):
        """
        Initialize Portfolio Manager.
        
        Args:
            binance_client: BinanceClient instance
            settings: Application settings
            position_manager: PositionManager instance (optional)
        """
        self.binance_client = binance_client
        self.settings = settings
        self.risk_calculator = RiskCalculator()
        self.position_manager = position_manager
        self.pnl_predictor = get_predictor()
        
        # Portfolio configuration
        self.allocation_pct = settings.portfolio_allocation_pct / 100.0
        self.min_confidence = settings.min_pair_confidence_score
        self.max_risk_per_pair = settings.max_risk_per_pair_pct / 100.0
        self.diversification_factor = settings.diversification_factor
        self.enable_closure = settings.enable_position_closure
        
        # Prediction-based closure configuration
        self.min_pnl_improvement_threshold = getattr(settings, 'min_pnl_improvement_threshold', 0.5)
        self.min_success_probability_improvement = getattr(settings, 'min_success_probability_improvement', 0.2)
        self.enable_prediction_based_closing = getattr(settings, 'enable_prediction_based_closing', True)

        # Balance cache: (result_dict, monotonic_timestamp)
        self._balance_cache: Optional[tuple] = None
        self._balance_cache_ttl = 30.0  # seconds
        self._liquidity_reconcile_lock = threading.Lock()
        
        logger.info(
            f"Portfolio Manager initialized: allocation_pct={self.allocation_pct:.2%}, "
            f"min_confidence={self.min_confidence}, max_risk_per_pair={self.max_risk_per_pair:.2%}"
        )

    def _get_symbol_price_cached(self, symbol: str, price_cache: Optional[Dict[str, float]] = None) -> float:
        """Return a symbol price using a caller-local cache, then BinanceClient cache/API."""
        if price_cache is not None and symbol in price_cache:
            return price_cache[symbol]
        price = self.binance_client.get_symbol_price(symbol)
        if price_cache is not None:
            price_cache[symbol] = price
        return price

    def _estimate_market_sell_slippage_bps(self, symbol: str, quantity: float) -> Optional[float]:
        """Estimate sell slippage from visible bid depth; None means unverified."""
        book = self.binance_client.get_order_book(symbol, limit=20)
        bids = book.get("bids") if isinstance(book, dict) else None
        asks = book.get("asks") if isinstance(book, dict) else None
        if not bids or not asks or quantity <= 0:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        reference = (best_bid + best_ask) / 2.0
        remaining = float(quantity)
        quote_received = 0.0
        for price_raw, qty_raw in bids:
            price = float(price_raw)
            level_qty = float(qty_raw)
            filled = min(remaining, level_qty)
            quote_received += filled * price
            remaining -= filled
            if remaining <= 1e-12:
                break
        if remaining > 1e-12 or reference <= 0:
            return None
        vwap = quote_received / quantity
        return max(0.0, (reference - vwap) / reference * 10000.0)

    def _managed_spot_assets(self) -> Tuple[set, bool]:
        """Return base assets protected by tracked Spot positions and lookup health."""
        if self.position_manager is None:
            return set(), False
        try:
            positions = self.position_manager.get_all_positions()
            protected = set()
            for symbol, position_list in (positions or {}).items():
                symbol = str(symbol or "").upper()
                for quote in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD"):
                    if symbol.endswith(quote):
                        protected.add(symbol[:-len(quote)])
                        break
            return protected, True
        except Exception as exc:
            logger.warning("Could not identify managed Spot assets; limiting auto-funding to USDC: %s", exc)
            return set(), False

    def ensure_usdt_liquidity(
        self,
        required_spendable_usdt: float,
        *,
        decision_id: Optional[str],
        db_client=None,
        liquidate_bnb_surplus: bool = False,
    ) -> bool:
        """Fund a Spot liquidity target from free stable, BNB-surplus, or crypto assets.

    Stable balances are preferred for JIT funding; proactive preparation may
    liquidate BNB only above its commission reserve. Other crypto may be sold
    only when PositionManager confirms it is not backing an open position.
    Conversion is testnet-only, slippage-bounded, fill-verified, and audited.
        """
        try:
            from trade_utils import align_quantity_to_step

            required = max(0.0, float(required_spendable_usdt or 0.0))
            balances = self.binance_client.get_account_balance()
            usdt_balance = float(balances.get("USDT", 0.0) or 0.0)
            reserves = _get_bnb_reserve_usdt(self.settings)
            bnb_reserve = max(0.0, float(reserves.get("spot_reserve_usdt", 0.0) or 0.0))
            bnb_max_reserve = max(0.0, float(getattr(self.settings, "bnb_max_reserve_usd", 100.0) or 0.0))
            bnb_coin_reserve = min(bnb_reserve, bnb_max_reserve) if bnb_max_reserve > 0 else bnb_reserve
            bnb_balance = float(balances.get("BNB", 0.0) or 0.0)
            try:
                bnb_price = float(self._get_symbol_price_cached("BNBUSDT"))
            except Exception:
                bnb_price = 0.0
            bnb_surplus_usdt = max(0.0, bnb_balance * bnb_price - bnb_coin_reserve) if bnb_price > 0 else 0.0
            bnb_min_notional = 10.0
            sell_bnb_surplus = bool(
                liquidate_bnb_surplus and bnb_surplus_usdt >= bnb_min_notional
            )
            if max(0.0, usdt_balance - bnb_reserve) >= required and not sell_bnb_surplus:
                return True

            if (
                not bool(getattr(self.settings, "portfolio_spot_asset_auto_convert_enabled", True))
                or not bool(getattr(self.settings, "binance_use_testnet", False))
                or not bool(getattr(self.binance_client, "testnet", False))
                or bool(getattr(self.binance_client, "dry_run", False))
            ):
                return False
            if not decision_id or db_client is None:
                logger.warning("Spot asset conversion blocked: decision id and audit database are required")
                return False

            protected_assets, managed_assets_known = self._managed_spot_assets()
            stable_assets = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "USD1"}
            candidates = []
            for asset, amount in balances.items():
                asset = str(asset or "").upper()
                amount = float(amount or 0.0)
                if asset == "USDT" or asset in protected_assets or amount <= 0:
                    continue
                if not managed_assets_known and asset != "USDC":
                    continue
                if asset not in {"BNB", "USDC"} and asset not in stable_assets and not managed_assets_known:
                    continue
                try:
                    price = float(self._get_symbol_price_cached(f"{asset}USDT"))
                except Exception:
                    continue
                if price <= 0:
                    continue
                available_amount = amount
                if asset == "BNB":
                    reserve_qty = bnb_coin_reserve / price
                    try:
                        from binance_commission import get_config
                        commission_rate = max(0.0, float(get_config(self.settings).rate_for("spot")))
                    except Exception:
                        commission_rate = 0.001
                    available_amount = max(0.0, (amount - reserve_qty) / (1.0 + commission_rate))
                if available_amount <= 0 or available_amount * price < bnb_min_notional and asset == "BNB":
                    continue
                if liquidate_bnb_surplus:
                    priority = 0 if asset == "BNB" else 1 if asset in stable_assets else 2
                else:
                    priority = 0 if asset in stable_assets else 1 if asset == "BNB" else 2
                candidates.append((priority, available_amount * price, asset, available_amount, price))
            candidates.sort(key=lambda item: (item[0], item[1]))

            for _, _, asset, available_amount, price in candidates:
                current_balances = self.binance_client.get_account_balance()
                current_usdt = float(current_balances.get("USDT", 0.0) or 0.0)
                shortfall = required + bnb_reserve - current_usdt
                if shortfall <= 0:
                    if asset != "BNB" or not liquidate_bnb_surplus:
                        return True
                available_amount = min(
                    available_amount,
                    float(current_balances.get(asset, 0.0) or 0.0),
                )
                try:
                    filters = self.binance_client.get_symbol_filters(f"{asset}USDT")
                except Exception:
                    continue
                step = float(filters.get("market_step") or filters.get("step") or 0.0)
                min_qty = max(
                    float(filters.get("market_min_qty") or 0.0),
                    float(filters.get("min_qty") or 0.0),
                )
                min_notional = float(filters.get("min_notional") or 0.0)
                if asset == "BNB" and shortfall <= 0:
                    requested_qty = available_amount
                else:
                    requested_qty = max(shortfall * 1.003 / price, min_qty, min_notional / price)
                qty = align_quantity_to_step(min(requested_qty, available_amount), step)
                if qty <= 0 or qty * price < min_notional or qty < min_qty:
                    continue

                order_symbol = f"{asset}USDT"
                try:
                    slippage_bps = self._estimate_market_sell_slippage_bps(order_symbol, qty)
                except Exception as exc:
                    logger.warning("Spot asset conversion blocked: order book unavailable for %s: %s", order_symbol, exc)
                    slippage_bps = None
                max_slippage_bps = max(
                    0.0,
                    float(getattr(self.settings, "portfolio_liquidity_max_conversion_slippage_bps", 25.0) or 0.0),
                )
                if slippage_bps is None or slippage_bps > max_slippage_bps:
                    result = {
                        "decision_id": str(decision_id),
                        "asset": asset,
                        "symbol": order_symbol,
                        "requested_asset_qty": qty,
                        "estimated_slippage_bps": slippage_bps,
                        "max_slippage_bps": max_slippage_bps,
                        "status": "blocked_order_book_unverified" if slippage_bps is None else "blocked_slippage_limit",
                    }
                    try:
                        db_client.execute_update(
                            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                            "VALUES (%s, %s, %s, %s, false)",
                            (order_symbol, "system", "spot_asset_conversion_blocked", json.dumps(result)),
                        )
                    except Exception as exc:
                        logger.error("Spot asset conversion block audit failed for %s: %s", order_symbol, exc)
                    continue
                authorization = {
                    "decision_id": str(decision_id),
                    "direction": f"{asset}_TO_USDT",
                    "asset": asset,
                    "symbol": order_symbol,
                    "requested_asset_qty": qty,
                    "required_spendable_usdt": required,
                    "usdt_before": current_usdt,
                    "spot_bnb_reserve_usdt": bnb_reserve,
                    "estimated_slippage_bps": slippage_bps,
                    "max_slippage_bps": max_slippage_bps,
                }
                try:
                    db_client.execute_update(
                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                        "VALUES (%s, %s, %s, %s, false)",
                        (order_symbol, "system", "spot_asset_conversion_authorized", json.dumps(authorization)),
                    )
                except Exception as exc:
                    logger.error("Spot asset conversion blocked: authorization audit failed: %s", exc)
                    return False

                try:
                    order = self.binance_client.create_market_order(
                        symbol=order_symbol,
                        side="SELL",
                        quantity=qty,
                    )
                    fill = self.binance_client.verify_and_fetch_fills(
                        order,
                        f"PORTFOLIO CONVERT {asset} TO USDT",
                    )
                except Exception as exc:
                    logger.error("Spot asset conversion failed for %s: %s", asset, exc)
                    fill = None
                    order = {}
                if not fill or not fill.get("binance_confirmed") or float(fill.get("quote_qty", 0.0) or 0.0) <= 0:
                    result = {**authorization, "status": "fill_unconfirmed", "order_id": (order or {}).get("orderId")}
                    try:
                        db_client.execute_update(
                            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                            "VALUES (%s, %s, %s, %s, false)",
                            (order_symbol, "system", "spot_asset_conversion_result", json.dumps(result)),
                        )
                    except Exception as exc:
                        logger.error("Spot asset conversion result audit failed: %s", exc)
                    return False

                refreshed = self.binance_client.get_account_balance()
                refreshed_usdt = float(refreshed.get("USDT", 0.0) or 0.0)
                spendable_after = max(0.0, refreshed_usdt - bnb_reserve)
                result = {
                    **authorization,
                    "status": "succeeded" if spendable_after >= required else "shortfall_remains",
                    "order_id": fill.get("order_id"),
                    "filled_asset_qty": float(fill.get("qty", 0.0) or 0.0),
                    "received_quote_qty": float(fill.get("quote_qty", 0.0) or 0.0),
                    "avg_price": float(fill.get("avg_price", 0.0) or 0.0),
                    "commission_usdt": float(fill.get("commission_usdt", fill.get("commission", 0.0)) or 0.0),
                    "usdt_after": refreshed_usdt,
                    "spendable_usdt_after_reserve": spendable_after,
                }
                try:
                    db_client.execute_update(
                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (order_symbol, "system", "spot_asset_conversion_result", json.dumps(result), True),
                    )
                except Exception as exc:
                    logger.error("Spot asset conversion result audit failed: %s", exc)
                    return False
                self._balance_cache = None
                if spendable_after >= required:
                    return True
                balances = refreshed
            return False

        except Exception as exc:
            logger.error("Spot asset conversion could not be verified: %s", exc)
            return False

    def proactive_liquidity_targets(self) -> Dict[str, float]:
        """Return spendable targets for one maximum configured entry per wallet."""
        from config_helpers import get_param

        buffer_usdt = max(0.0, float(get_param(self.settings, "portfolio_liquidity_buffer_usdt", 10.0) or 0.0))
        spot_max = min(
            max(0.0, float(get_param(self.settings, "max_per_trade_usdt", 0.0) or 0.0)),
            max(0.0, float(get_param(self.settings, "kelly_max_usdt", 0.0) or 0.0)),
        )
        futures_max = max(
            0.0,
            float(get_param(self.settings, "futures_kelly_max_usdt", 0.0) or 0.0),
            float(get_param(self.settings, "futures_usdt_per_trade", 0.0) or 0.0),
            float(get_param(self.settings, "range_scalp_short_usdt_per_trade", 0.0) or 0.0),
        )
        return {
            "spot_entry_max_usdt": spot_max,
            "spot_min_ready_usdt": spot_max,
            "spot_target_usdt": spot_max + buffer_usdt,
            "futures_entry_max_usdt": futures_max,
            "futures_min_ready_usdt": futures_max,
            "futures_target_usdt": futures_max + buffer_usdt,
            "buffer_usdt": buffer_usdt,
        }

    def reconcile_proactive_liquidity(
        self,
        *,
        futures_client=None,
        db_client=None,
        trigger: str = "cycle_complete",
    ) -> Dict[str, Any]:
        """Prepare one maximum entry plus buffer in each Staging wallet."""
        result: Dict[str, Any] = {"status": "skipped", "trigger": trigger, "actions": []}
        settings = self.settings
        if not bool(getattr(settings, "portfolio_proactive_liquidity_enabled", False)):
            result["reason"] = "feature_disabled"
            return result
        if (
            not bool(getattr(settings, "binance_use_testnet", False))
            or not bool(getattr(self.binance_client, "testnet", False))
            or bool(getattr(settings, "dry_run", False))
            or bool(getattr(self.binance_client, "dry_run", False))
        ):
            result.update({"status": "blocked", "reason": "testnet_live_orders_required"})
            return result
        if db_client is None or not hasattr(db_client, "execute_update") or not hasattr(db_client, "execute_query"):
            result.update({"status": "blocked", "reason": "audit_database_unavailable"})
            return result
        if not self._liquidity_reconcile_lock.acquire(blocking=False):
            result.update({"status": "skipped", "reason": "reconcile_in_progress"})
            return result

        decision_id = f"portfolio-liquidity-{uuid.uuid4().hex}"

        def _audit(event: str, detail: Dict[str, Any], confirmed: bool = False) -> bool:
            try:
                db_client.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("PORTFOLIO", "system", event, json.dumps(detail), confirmed),
                )
                return True
            except Exception as exc:
                logger.error("Portfolio liquidity audit failed (%s): %s", event, exc)
                return False

        def _read_balances() -> Dict[str, float]:
            spot = self.binance_client.get_account_balance()
            if not isinstance(spot, dict):
                raise RuntimeError("Spot balance response is invalid")
            reserves = _get_bnb_reserve_usdt(settings)
            spot_reserve = max(0.0, float(reserves["spot_reserve_usdt"]))
            futures_reserve = max(0.0, float(reserves["futures_reserve_usdt"]))
            futures_free = float(futures_client.get_available_usdt()) if futures_client else 0.0
            if futures_client is not None and futures_free <= 0:
                account = futures_client.get_account_info()
                if not isinstance(account, dict) or not isinstance(account.get("assets"), list):
                    raise RuntimeError("Futures balance response is invalid")
                usdt_rows = [row for row in account["assets"] if row.get("asset") == "USDT"]
                if not usdt_rows:
                    raise RuntimeError("Futures USDT balance is unavailable")
                futures_free = float(usdt_rows[0].get("availableBalance", 0.0) or 0.0)
            return {
                "spot_usdt": float(spot.get("USDT", 0.0) or 0.0),
                "spot_bnb": float(spot.get("BNB", 0.0) or 0.0),
                "spot_reserve_usdt": spot_reserve,
                "spot_spendable_usdt": max(0.0, float(spot.get("USDT", 0.0) or 0.0) - spot_reserve),
                "futures_available_usdt": futures_free,
                "futures_reserve_usdt": futures_reserve,
                "futures_spendable_usdt": max(0.0, futures_free - futures_reserve),
            }

        try:
            pause_rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key='pause_new_entries'"
            )
            pause_value = (pause_rows[0].get("value") if pause_rows else {}) or {}
            if isinstance(pause_value, str):
                pause_value = json.loads(pause_value)
            if isinstance(pause_value, dict) and bool(pause_value.get("paused")):
                result.update({"status": "skipped", "reason": "new_entries_paused"})
                return result

            targets = self.proactive_liquidity_targets()
            before = _read_balances()
            cross_wallet_transfer_supported = bool(
                not bool(getattr(settings, "binance_use_testnet", False))
                and bool(getattr(settings, "portfolio_cross_wallet_transfer_enabled", False))
            )
            result.update({
                "decision_id": decision_id,
                "targets": targets,
                "before": before,
                "cross_wallet_transfer_supported": cross_wallet_transfer_supported,
            })
            if not _audit("liquidity_plan_observed", {
                "decision_id": decision_id,
                "trigger": trigger,
                "targets": targets,
                "balances": before,
            }):
                result.update({"status": "blocked", "reason": "plan_audit_failed"})
                return result

            bnb_max_reserve = max(0.0, float(getattr(settings, "bnb_max_reserve_usd", 100.0) or 0.0))
            spot_bnb_target = min(before["spot_reserve_usdt"], bnb_max_reserve) if bnb_max_reserve else before["spot_reserve_usdt"]
            futures_bnb_target = min(before["futures_reserve_usdt"], bnb_max_reserve) if bnb_max_reserve else before["futures_reserve_usdt"]

            futures_transfer_cooling_down = False
            if futures_client is not None and not cross_wallet_transfer_supported:
                result["cross_wallet_transfer_reason"] = "unsupported_in_testnet_environment"
                _audit("liquidity_transfer_blocked", {
                    "decision_id": decision_id,
                    "reason": result["cross_wallet_transfer_reason"],
                    "directions": ["SPOT_TO_FUTURES", "FUTURES_TO_SPOT", "FUTURES_BNB_SURPLUS_TO_SPOT"],
                })
            if futures_client is not None and cross_wallet_transfer_supported:
                try:
                    last_transfer_rows = db_client.execute_query(
                        "SELECT ts, detail FROM trades_audit "
                        "WHERE event='liquidity_transfer_result' "
                        "AND detail->>'direction'='SPOT_TO_FUTURES' "
                        "ORDER BY id DESC LIMIT 1"
                    )
                    if last_transfer_rows:
                        last_detail = last_transfer_rows[0].get("detail") or {}
                        if isinstance(last_detail, str):
                            last_detail = json.loads(last_detail)
                        last_status = str(last_detail.get("status") or "").lower()
                        last_route = str(last_detail.get("route") or "")
                        last_at = last_transfer_rows[0].get("ts")
                        if isinstance(last_at, str):
                            last_at = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                        if isinstance(last_at, datetime) and last_at.tzinfo is None:
                            last_at = last_at.replace(tzinfo=timezone.utc)
                        cooldown_minutes = max(
                            1.0,
                            float(getattr(settings, "portfolio_liquidity_transfer_cooldown_minutes", 30.0) or 30.0),
                        )
                        if (
                            last_status in {"failed", "unverified"}
                            and last_route == "universal_asset_transfer"
                            and isinstance(last_at, datetime)
                            and datetime.now(timezone.utc) - last_at < timedelta(minutes=cooldown_minutes)
                        ):
                            futures_transfer_cooling_down = True
                except Exception as exc:
                    futures_transfer_cooling_down = True
                    logger.warning("Futures funding route status unavailable; suppressing proactive transfer: %s", exc)
            result["futures_transfer_cooling_down"] = futures_transfer_cooling_down
            if futures_transfer_cooling_down:
                _audit("liquidity_transfer_cooldown", {
                    "decision_id": decision_id,
                    "direction": "SPOT_TO_FUTURES",
                    "reason": "recent_transfer_failed_or_route_status_unknown",
                })

            # Move only available Futures BNB above its commission reserve; open-position
            # collateral remains unavailable and is never used for wallet preparation.
            if futures_client is not None:
                try:
                    bnb_price = float(self._get_symbol_price_cached("BNBUSDT"))
                    account = futures_client.get_account_info()
                    bnb_rows = [row for row in account.get("assets", []) if row.get("asset") == "BNB"]
                    if bnb_price > 0 and bnb_rows:
                        bnb_row = bnb_rows[0]
                        wallet_bnb = float(bnb_row.get("walletBalance", 0.0) or 0.0)
                        reserve_qty = futures_bnb_target / bnb_price
                        excess_qty = max(0.0, wallet_bnb - reserve_qty)
                        bnb_filters = self.binance_client.get_symbol_filters("BNBUSDT")
                        bnb_step = float(bnb_filters.get("market_step") or bnb_filters.get("step") or 0.001)
                        from trade_utils import align_quantity_to_step
                        transfer_qty = align_quantity_to_step(excess_qty, bnb_step)
                        if transfer_qty * bnb_price >= 10.0:
                            transfer_detail = {
                                "decision_id": decision_id,
                                "direction": "FUTURES_BNB_SURPLUS_TO_SPOT",
                                "quantity": transfer_qty,
                                "reserve_usdt": futures_bnb_target,
                                "wallet_bnb_before": wallet_bnb,
                            }
                            if not _audit("liquidity_transfer_authorized", transfer_detail):
                                result.update({"status": "blocked", "reason": "bnb_transfer_audit_failed"})
                                return result
                            try:
                                self.binance_client.client.universal_transfer(
                                    type="UMFUTURE_MAIN", asset="BNB", amount=transfer_qty,
                                )
                                result["actions"].append({"action": "futures_bnb_to_spot", "quantity": transfer_qty})
                                if not _audit("liquidity_transfer_result", {
                                    **transfer_detail, "status": "succeeded",
                                }, confirmed=True):
                                    result.update({"status": "blocked", "reason": "bnb_transfer_result_audit_failed"})
                                    return result
                            except Exception as exc:
                                _audit("liquidity_transfer_result", {
                                    **transfer_detail, "status": "failed", "error": str(exc),
                                })
                                result["actions"].append({"action": "futures_bnb_to_spot", "quantity": transfer_qty, "status": "failed"})
                except Exception as exc:
                    logger.warning("Futures BNB surplus transfer skipped: %s", exc)

            before = _read_balances()
            spot_target = float(targets["spot_target_usdt"])
            futures_target = float(targets["futures_target_usdt"])
            spot_min_ready = float(targets["spot_min_ready_usdt"])
            futures_min_ready = float(targets["futures_min_ready_usdt"])

            # Return only genuine Futures surplus, preserving its own entry target
            # and reserve, when Spot is below its minimum-ready amount.
            if (
                cross_wallet_transfer_supported
                and futures_client is not None
                and before["spot_spendable_usdt"] < spot_target
                and before["futures_available_usdt"] > before["futures_reserve_usdt"] + futures_target
            ):
                amount = min(
                    spot_target + before["spot_reserve_usdt"] - before["spot_usdt"],
                    before["futures_available_usdt"] - before["futures_reserve_usdt"] - futures_target,
                )
                if amount >= 1.0:
                    transfer_detail = {
                        "decision_id": decision_id,
                        "direction": "FUTURES_TO_SPOT",
                        "route": "universal_asset_transfer",
                        "amount_usdt": round(amount, 2),
                        "reason": "restore_spot_entry_target",
                        "spot_target_usdt": spot_target,
                        "futures_target_usdt": futures_target,
                        "balances_before": before,
                    }
                    if not _audit("liquidity_transfer_authorized", transfer_detail):
                        result.update({"status": "blocked", "reason": "transfer_audit_failed"})
                        return result
                    transferred = futures_client.transfer_to_spot(
                        amount,
                        spot_api_key=getattr(settings, "binance_api_key", None),
                        spot_api_secret=getattr(settings, "binance_api_secret", None),
                        safety_buffer_usdt=before["futures_reserve_usdt"] + futures_target,
                    )
                    time.sleep(1.0)
                    after = _read_balances()
                    transfer_detail.update({
                        "status": "succeeded" if transferred and after["spot_spendable_usdt"] >= before["spot_spendable_usdt"] + amount * 0.95 else "unverified",
                        "balances_after": after,
                    })
                    _audit("liquidity_transfer_result", transfer_detail, confirmed=transfer_detail["status"] == "succeeded")
                    result["actions"].append({"action": "futures_to_spot", "amount_usdt": amount, "status": transfer_detail["status"]})
                    before = after

            future_deficit = (
                max(0.0, futures_target + before["futures_reserve_usdt"] - before["futures_available_usdt"])
                if (
                    futures_client is not None
                    and cross_wallet_transfer_supported
                    and not futures_transfer_cooling_down
                    and before["futures_spendable_usdt"] < futures_target
                )
                else 0.0
            )
            required_spot = before["spot_spendable_usdt"]
            if before["spot_spendable_usdt"] < spot_target:
                required_spot = spot_target
            if future_deficit > 0:
                required_spot = max(spot_target, required_spot) + future_deficit

            should_prepare_spot = (
                before["spot_spendable_usdt"] < spot_target
                or future_deficit > 0
                or before["spot_bnb"] * max(0.0, float(self._get_symbol_price_cached("BNBUSDT"))) > spot_bnb_target + 10.0
            )
            if should_prepare_spot:
                converted = self.ensure_usdt_liquidity(
                    required_spot,
                    decision_id=decision_id,
                    db_client=db_client,
                    liquidate_bnb_surplus=True,
                )
                after_conversion = _read_balances()
                result["actions"].append({
                    "action": "convert_spot_assets",
                    "status": "target_ready" if converted else "shortfall_remains",
                    "required_spendable_usdt": required_spot,
                })
                before = after_conversion

            if (
                cross_wallet_transfer_supported
                and futures_client is not None
                and not futures_transfer_cooling_down
                and before["futures_spendable_usdt"] < futures_target
            ):
                amount = min(
                    futures_target + before["futures_reserve_usdt"] - before["futures_available_usdt"],
                    before["spot_usdt"] - before["spot_reserve_usdt"] - spot_target,
                )
                if amount >= 1.0:
                    transfer_detail = {
                        "decision_id": decision_id,
                        "direction": "SPOT_TO_FUTURES",
                        "route": "universal_asset_transfer",
                        "amount_usdt": round(amount, 2),
                        "reason": "restore_futures_entry_target",
                        "spot_target_usdt": spot_target,
                        "futures_target_usdt": futures_target,
                        "balances_before": before,
                    }
                    if not _audit("liquidity_transfer_authorized", transfer_detail):
                        result.update({"status": "blocked", "reason": "transfer_audit_failed"})
                        return result
                    transferred = futures_client.transfer_to_futures(
                        amount,
                        spot_api_key=getattr(settings, "binance_api_key", None),
                        spot_api_secret=getattr(settings, "binance_api_secret", None),
                    )
                    time.sleep(1.0)
                    after = _read_balances()
                    transfer_status = (
                        "failed" if not transferred
                        else "succeeded" if after["futures_spendable_usdt"] >= before["futures_spendable_usdt"] + amount * 0.95
                        else "unverified"
                    )
                    transfer_detail.update({
                        "status": transfer_status,
                        "balances_after": after,
                    })
                    _audit("liquidity_transfer_result", transfer_detail, confirmed=transfer_status == "succeeded")
                    result["actions"].append({"action": "spot_to_futures", "amount_usdt": amount, "status": transfer_status})
                    before = after

            result["after"] = before
            ready = (
                before["spot_spendable_usdt"] >= spot_min_ready
                and (futures_client is None or before["futures_spendable_usdt"] >= futures_min_ready)
            )
            at_target = (
                before["spot_spendable_usdt"] >= spot_target
                and (futures_client is None or before["futures_spendable_usdt"] >= futures_target)
            )
            result["status"] = "targets_met" if at_target else "minimum_ready" if ready else "shortfall"
            result["shortfall_usdt"] = {
                "spot": max(0.0, spot_min_ready - before["spot_spendable_usdt"]),
                "futures": max(0.0, futures_min_ready - before["futures_spendable_usdt"]) if futures_client else 0.0,
            }
            _audit("liquidity_reconcile_result", {
                "decision_id": decision_id,
                "trigger": trigger,
                "status": result["status"],
                "targets": targets,
                "balances_after": before,
                "actions": result["actions"],
                "shortfall_usdt": result["shortfall_usdt"],
            }, confirmed=ready)
            return result
        except Exception as exc:
            result.update({"status": "error", "reason": str(exc)})
            _audit("liquidity_reconcile_failed", {
                "decision_id": decision_id,
                "trigger": trigger,
                "error": str(exc),
            })
            logger.error("Proactive liquidity reconciliation failed: %s", exc)
            return result
        finally:
            self._liquidity_reconcile_lock.release()
    
    def get_total_balance(self, use_cache: bool = True) -> Dict[str, float]:
        """
        Get total balance from Binance account (cached 30 s by default).

        Args:
            use_cache: Set False to force a fresh Binance API call.

        Returns:
            Dictionary with balance information
        """
        now_mono = time.monotonic()
        if use_cache and self._balance_cache is not None:
            cached_result, cached_ts = self._balance_cache
            if now_mono - cached_ts < self._balance_cache_ttl:
                return cached_result

        try:
            balances = self.binance_client.get_account_balance()
            price_cache: Dict[str, float] = {}
            
            # Calculate total USDT value including all assets
            total_usdt = 0.0
            usdt_balance = float(balances.get("USDT", 0))
            total_usdt += usdt_balance
            spot_deployable_usdt = usdt_balance
            spot_bnb_usdt = 0.0
            protected_assets, managed_assets_known = self._managed_spot_assets()
            stable_assets = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "USD1"}
            
            # Convert other assets to USDT value (price cache in BinanceClient prevents extra calls)
            for asset, amount in balances.items():
                if asset.upper() == "USDT" or amount <= 0:
                    continue
                try:
                    symbol = f"{asset.upper()}USDT"
                    price = self._get_symbol_price_cached(symbol, price_cache)
                    asset_value = amount * price
                    total_usdt += asset_value
                    if asset.upper() == "BNB":
                        spot_bnb_usdt = asset_value
                    elif asset.upper() not in protected_assets and (
                        asset.upper() in stable_assets or managed_assets_known
                    ):
                        spot_deployable_usdt += asset_value
                except Exception:
                    pass
            
            _bnb_reserves = _get_bnb_reserve_usdt(self.settings)
            bnb_reserve = _bnb_reserves.get('spot_reserve_usdt', _bnb_reserves.get('total_reserve_usdt', 0.0))
            usdt_available_after_reserve = max(0.0, usdt_balance - bnb_reserve)
            result = {
                "usdt_available": usdt_balance,
                "usdt_available_after_reserve": usdt_available_after_reserve,
                "spot_deployable_usdt_after_reserve": max(0.0, spot_deployable_usdt - bnb_reserve),
                "bnb_reserve_usdt": bnb_reserve,
                "spot_bnb_usdt": spot_bnb_usdt,
                "total_usdt": total_usdt,
                "allocation_usdt": total_usdt * self.allocation_pct,
                "timestamp": datetime.utcnow().isoformat()
            }
            self._balance_cache = (result, now_mono)
            return result
        except Exception as e:
            logger.error(f"Error getting total balance: {e}")
            return {
                "usdt_available": 0.0,
                "usdt_available_after_reserve": 0.0,
                "spot_deployable_usdt_after_reserve": 0.0,
                "bnb_reserve_usdt": 0.0,
                "spot_bnb_usdt": 0.0,
                "total_usdt": 0.0,
                "allocation_usdt": 0.0,
                "timestamp": datetime.utcnow().isoformat()
            }
    
    def get_unified_balance(self, futures_client=None) -> dict:
        """
        Returns combined SPOT + Futures balance for Unified Portfolio Manager sizing.
        Provides separate 'usdt_available' (SPOT) and 'futures_available' (Futures free margin)
        so each trading mode can use its own wallet as Kelly reference.

        Falls back gracefully to get_total_balance() if futures_client is None or fails.
        Does NOT replace get_total_balance() — all existing SPOT callers are unaffected.
        """
        spot = self.get_total_balance()
        if futures_client is None:
            return spot
        try:
            futures_total = futures_client.get_total_wallet_usdt()
            futures_avail = futures_client.get_available_usdt()
            spot_usdt     = spot["usdt_available"]
            _bnb_reserves = _get_bnb_reserve_usdt(self.settings)
            spot_bnb_reserve = _bnb_reserves.get('spot_reserve_usdt', 0.0)
            futures_bnb_reserve = _bnb_reserves.get('futures_reserve_usdt', 0.0)
            bnb_reserve = spot_bnb_reserve  # expose spot reserve for usdt_available_after_reserve
            usdt_after_reserve = max(0.0, spot_usdt - spot_bnb_reserve)
            futures_after_reserve = max(0.0, futures_avail - futures_bnb_reserve)
            combined_total = spot["total_usdt"] + futures_total
            result = {
                "usdt_available":                  spot_usdt,
                "usdt_available_after_reserve":    usdt_after_reserve,
                "spot_deployable_usdt_after_reserve": spot.get("spot_deployable_usdt_after_reserve", usdt_after_reserve),
                "futures_available":               futures_avail,
                "futures_available_after_reserve": futures_after_reserve,
                "bnb_reserve_usdt":                bnb_reserve,
                "spot_bnb_usdt":                   spot.get("spot_bnb_usdt", 0.0),
                "futures_total":                   futures_total,
                "total_usdt":                      combined_total,
                "allocation_usdt":               combined_total * self.allocation_pct,
                "timestamp":                       spot.get("timestamp", datetime.utcnow().isoformat()),
            }
            logger.debug(
                f"[UnifiedPM] spot=${spot_usdt:.2f} futures_avail=${futures_avail:.2f} "
                f"futures_total=${futures_total:.2f} combined=${combined_total:.2f}"
            )
            return result
        except Exception as e:
            logger.warning(f"[UnifiedPM] futures balance error ({e}) — falling back to spot-only")
            return spot

    def rank_pairs(
        self,
        signals: Dict[str, float],
        technical_scores: Dict[str, float],
        news_scores: Dict[str, float],
        risk_metrics: Dict[str, Dict[str, float]]
    ) -> List[PairRanking]:
        """
        Rank trading pairs using combined scoring.
        
        Args:
            signals: Dictionary of signal scores per symbol
            technical_scores: Dictionary of technical analysis scores per symbol
            news_scores: Dictionary of news sentiment scores per symbol
            risk_metrics: Dictionary of risk metrics per symbol
            
        Returns:
            List of ranked pairs (highest score first)
        """
        rankings = []
        
        # Get all unique symbols from all sources
        all_symbols = set(signals.keys()) | set(technical_scores.keys()) | set(news_scores.keys())
        
        for symbol in all_symbols:
            # Get scores (default to 0 if not available)
            signal_score = signals.get(symbol, 0.0)
            technical_score = technical_scores.get(symbol, 0.0)
            news_score = news_scores.get(symbol, 0.0)
            
            # Get risk score
            risk_data = risk_metrics.get(symbol, {})
            risk_score = risk_data.get("risk_score", 50.0)  # Default medium risk
            
            # Calculate combined score (weighted average)
            # Higher is better for signal/technical/news, lower is better for risk
            # We invert risk score so lower risk = higher contribution
            risk_contribution = (100 - risk_score) / 100.0
            
            combined_score = (
                signal_score * 0.4 +
                technical_score * 0.3 +
                news_score * 0.2 +
                risk_contribution * 0.1
            )
            
            # Only include if above minimum confidence
            if combined_score >= self.min_confidence:
                rankings.append(PairRanking(
                    symbol=symbol,
                    confidence_score=combined_score,
                    signal_score=signal_score,
                    technical_score=technical_score,
                    news_score=news_score,
                    risk_score=risk_score,
                    combined_score=combined_score
                ))
        
        # Sort by combined score (descending)
        rankings.sort(key=lambda x: x.combined_score, reverse=True)
        
        logger.info(f"Ranked {len(rankings)} pairs above minimum confidence {self.min_confidence}")
        
        return rankings
    
    def calculate_optimal_pair_count(self, ranked_pairs: List[PairRanking]) -> int:
        """
        Calculate optimal number of pairs to trade.
        
        Args:
            ranked_pairs: List of ranked pairs
            
        Returns:
            Optimal number of pairs
        """
        if not ranked_pairs:
            return 0
        
        # Dynamic calculation based on diversification factor and available capital
        # More capital = can handle more pairs
        balance = self.get_total_balance()
        allocation_usdt = balance["allocation_usdt"]
        
        # Base calculation: diversification factor * log of capital
        # This means more capital allows more diversification
        import math
        base_count = max(1, int(self.diversification_factor * math.log(max(allocation_usdt, 1000))))
        
        # Limit by number of available pairs above threshold
        max_pairs = len(ranked_pairs)
        
        # Also limit by settings.max_positions if available
        from config_helpers import get_param
        max_positions = get_param(self.settings, "max_positions")
        
        optimal_count = min(base_count, max_pairs, max_positions)
        
        logger.info(f"Optimal pair count: {optimal_count} (base={base_count}, available={max_pairs}, max={max_positions})")
        
        return optimal_count
    
    def allocate_capital(
        self,
        ranked_pairs: List[PairRanking],
        total_capital: float
    ) -> List[CapitalAllocation]:
        """
        Allocate capital to selected pairs.
        
        Args:
            ranked_pairs: List of ranked pairs
            total_capital: Total capital to allocate
            
        Returns:
            List of capital allocations per pair
        """
        if not ranked_pairs or total_capital <= 0:
            return []
        
        # Calculate optimal number of pairs
        optimal_count = self.calculate_optimal_pair_count(ranked_pairs)
        selected_pairs = ranked_pairs[:optimal_count]
        
        allocations = []
        
        # Calculate weights based on combined scores
        total_score = sum(p.combined_score for p in selected_pairs)
        
        for pair in selected_pairs:
            # Weight based on score
            weight = pair.combined_score / total_score if total_score > 0 else 1.0 / len(selected_pairs)
            
            # Calculate capital allocation
            capital_usdt = total_capital * weight
            
            # Risk adjustment: reduce allocation if risk is high
            risk_adjustment = 1.0
            if pair.risk_score > 70:  # High risk
                risk_adjustment = 0.7
            elif pair.risk_score > 50:  # Medium risk
                risk_adjustment = 0.85
            
            capital_usdt *= risk_adjustment
            
            # Ensure allocation doesn't exceed max risk per pair
            max_capital_per_pair = total_capital * self.max_risk_per_pair
            capital_usdt = min(capital_usdt, max_capital_per_pair)
            
            percentage = (capital_usdt / total_capital) * 100 if total_capital > 0 else 0.0
            
            allocations.append(CapitalAllocation(
                symbol=pair.symbol,
                capital_usdt=capital_usdt,
                percentage=percentage,
                risk_adjusted=risk_adjustment < 1.0
            ))
        
        # Normalize to ensure total equals 100%
        total_allocated = sum(a.capital_usdt for a in allocations)
        if total_allocated > 0 and total_allocated != total_capital:
            scale_factor = total_capital / total_allocated
            for alloc in allocations:
                alloc.capital_usdt *= scale_factor
                alloc.percentage = (alloc.capital_usdt / total_capital) * 100
        
        logger.info(
            f"Allocated {total_capital:.2f} USDT to {len(allocations)} pairs: "
            f"{', '.join(f'{a.symbol}({a.percentage:.1f}%)' for a in allocations)}"
        )
        
        return allocations
    
    def get_position_predictions(self, current_positions: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Get PnL predictions for current open positions.
        
        Args:
            current_positions: Current open positions (symbol -> position data)
            
        Returns:
            Dictionary mapping symbol to prediction data
        """
        predictions = {}
        
        if not self.position_manager:
            logger.warning("Position Manager not available, cannot get predictions")
            return predictions
        
        try:
            # Import AutonomousSignalGenerator for technical indicators
            from autonomous_signals import AutonomousSignalGenerator
            signal_gen = AutonomousSignalGenerator(self.binance_client)
            price_cache: Dict[str, float] = {}
            
            for symbol, position_data in current_positions.items():
                try:
                    # Get current price
                    current_price = self._get_symbol_price_cached(symbol, price_cache)
                    
                    # Get technical indicators
                    signal_data = signal_gen.analyze_symbol(symbol, interval='1h')
                    position_size = position_data.get('quantity', 0) * current_price
                    prediction, _ = pnl_service.predict_for_symbol(
                        symbol=symbol,
                        client=self.binance_client,
                        settings=self.settings,
                        position_size_usdt=position_size,
                        precomputed_signal=signal_data,
                        trade_type='spot',
                        current_price=current_price,
                    )
                    if prediction is None:
                        continue
                    
                    predictions[symbol] = prediction
                    logger.debug(f"Prediction for {symbol}: {prediction['expected_pnl']:.2f} USDT")
                    
                except Exception as e:
                    logger.error(f"Error getting prediction for {symbol}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error in get_position_predictions: {e}")
        
        return predictions
    
    def compare_predictions(
        self,
        current_predictions: Dict[str, Dict],
        new_predictions: Dict[str, Dict]
    ) -> List[Tuple[str, str, float]]:
        """
        Compare current position predictions with new opportunity predictions.
        
        Args:
            current_predictions: Predictions for current positions
            new_predictions: Predictions for new opportunities
            
        Returns:
            List of tuples (current_symbol, new_symbol, improvement_score) sorted by improvement
        """
        comparisons = []
        
        for current_symbol, current_pred in current_predictions.items():
            for new_symbol, new_pred in new_predictions.items():
                # Skip if same symbol
                if current_symbol == new_symbol:
                    continue
                
                # Calculate improvement score
                current_pnl = current_pred.get('expected_pnl', 0)
                new_pnl = new_pred.get('expected_pnl', 0)
                
                current_prob = current_pred.get('success_probability', 0.5)
                new_prob = new_pred.get('success_probability', 0.5)
                
                current_risk = current_pred.get('risk_metrics', {}).get('percentage_risk', 10.0)
                new_risk = new_pred.get('risk_metrics', {}).get('percentage_risk', 10.0)
                
                # Calculate improvement factors
                pnl_improvement = (new_pnl - current_pnl) / abs(current_pnl) if current_pnl != 0 else 0
                prob_improvement = new_prob - current_prob
                risk_improvement = (current_risk - new_risk) / current_risk if current_risk > 0 else 0
                
                # Combined improvement score (weighted)
                improvement_score = (
                    pnl_improvement * 0.5 +
                    prob_improvement * 0.3 +
                    risk_improvement * 0.2
                )
                
                # Only include if improvement is positive
                if improvement_score > 0:
                    comparisons.append((current_symbol, new_symbol, improvement_score))
        
        # Sort by improvement score (descending)
        comparisons.sort(key=lambda x: x[2], reverse=True)
        
        return comparisons
    
    def decide_position_closures(
        self,
        current_positions: Dict[str, Dict],
        new_allocations: List[CapitalAllocation],
        new_predictions: Optional[Dict[str, Dict]] = None
    ) -> List[str]:
        """
        Decide which positions to close for capital reallocation.
        
        Args:
            current_positions: Current open positions
            new_allocations: New capital allocations
            new_predictions: Optional predictions for new opportunities
            
        Returns:
            List of symbols to close
        """
        if not self.enable_closure:
            return []
        
        symbols_to_close = []
        
        # Get symbols in new allocations
        new_symbols = set(a.symbol for a in new_allocations)
        
        # Get symbols in current positions
        current_symbols = set(current_positions.keys())
        
        # If prediction-based closing is enabled and we have new predictions
        if self.enable_prediction_based_closing and new_predictions:
            try:
                # Get predictions for current positions
                current_predictions = self.get_position_predictions(current_positions)
                
                if current_predictions:
                    # Compare predictions
                    comparisons = self.compare_predictions(current_predictions, new_predictions)
                    
                    # Decide which positions to close based on predictions
                    for current_symbol, new_symbol, improvement_score in comparisons:
                        # Check if improvement meets threshold
                        if improvement_score >= self.min_pnl_improvement_threshold:
                            # Check if new symbol is in new allocations
                            if new_symbol in new_symbols:
                                symbols_to_close.append(current_symbol)
                                logger.info(
                                    f"Closing {current_symbol} for {new_symbol} (improvement: {improvement_score:.2f})"
                                )
                                break  # Only close each position once
                    
                    if symbols_to_close:
                        logger.info(f"Prediction-based closures: {symbols_to_close}")
            except Exception as e:
                logger.error(f"Error in prediction-based closure: {e}")
        
        # NOTE: no fallback — if the predictor found no genuine improvement, return empty.
        # Closing positions just because they are absent from new_allocations destroys value.
        # Actual sell execution is handled by _close_prediction_based() in run_trades.py
        # which applies min_hold, protect_profit, Fix-D, and Opción B filters before selling.
        
        return symbols_to_close
    
    def risk_adjusted_sizing(
        self,
        symbol: str,
        capital: float,
        risk_score: float
    ) -> float:
        """
        Calculate risk-adjusted position size.
        
        Args:
            symbol: Trading pair symbol
            capital: Allocated capital
            risk_score: Risk score (0-100)
            
        Returns:
            Risk-adjusted position size
        """
        # Base size is the allocated capital
        size = capital
        
        # Adjust based on risk
        if risk_score > 70:  # High risk
            size *= 0.7
        elif risk_score > 50:  # Medium risk
            size *= 0.85
        
        # Ensure minimum size
        min_size = getattr(self.settings, "default_trade_size", 10.0)
        size = max(size, min_size)
        
        return size
    
    def get_portfolio_summary(self) -> Dict:
        """
        Get summary of current portfolio state.
        
        Returns:
            Dictionary with portfolio summary
        """
        balance = self.get_total_balance()
        
        return {
            "total_balance_usdt": balance["total_usdt"],
            "available_usdt": balance["usdt_available"],
            "spot_deployable_usdt_after_reserve": balance.get("spot_deployable_usdt_after_reserve", balance["usdt_available"]),
            "allocated_usdt": balance["allocation_usdt"],
            "allocation_percentage": self.allocation_pct * 100,
            "min_confidence_threshold": self.min_confidence,
            "max_risk_per_pair": self.max_risk_per_pair * 100,
            "diversification_factor": self.diversification_factor,
            "position_closure_enabled": self.enable_closure,
            "timestamp": datetime.utcnow().isoformat()
        }
