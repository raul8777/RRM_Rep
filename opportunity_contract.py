"""Executable opportunity contract shared by arbitration and execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ExecutableOpportunity:
    side: str
    thesis_type: str
    trade_type: str
    entry_price: float
    margin_usdt: float
    leverage: int
    qty: float
    notional_usdt: float
    effective_target_price: float
    effective_stop_price: float
    economic_risk_usdt: float
    economic_reward_usdt: float
    projected_entry_fee_usdt: float
    projected_exit_fee_usdt: float
    slippage_reserve_usdt: float
    funding_reserve_usdt: float
    expected_net_pnl_usdt: float
    expected_return_on_risk: float
    executable: bool
    rejection_evidence: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _n(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def resistance_rejection_evidence(metadata: Dict[str, Any]) -> Dict[str, Any]:
    explicit = bool(metadata.get("resistance_bounce_sell") or metadata.get("sr_rejection"))
    breakout_pressure = bool(metadata.get("resistance_breakout_buy") or metadata.get("sr_breakout"))
    volume = _n(
        metadata.get("resistance_volume_ratio")
        or metadata.get("volume_ratio")
        or metadata.get("volume_spike_ratio")
        or metadata.get("relative_volume")
    )
    momentum_raw = metadata.get("graph_momentum_5")
    if momentum_raw is None:
        momentum_raw = metadata.get("graph_trend_slope")
    if momentum_raw is None:
        momentum_raw = metadata.get("rsi_delta")
    momentum = _n(momentum_raw)
    momentum_available = momentum_raw is not None
    confirmed = explicit and not breakout_pressure and (not momentum_available or momentum <= 0.0)
    return {
        "confirmed": confirmed,
        "explicit_rejection": explicit,
        "breakout_pressure": breakout_pressure,
        "volume": volume,
        "momentum": momentum if momentum_available else None,
    }


def build_executable_opportunity(
    side: str,
    thesis_type: str,
    prediction: Optional[Dict[str, Any]],
    metadata: Dict[str, Any],
    *,
    executable: bool = True,
    leverage: int = 1,
    trade_type: str = "spot",
    max_loss_usd: float = 0.0,
    entry_fee_bps: float = 4.0,
    exit_fee_bps: float = 4.0,
    slippage_bps: float = 0.0,
    funding_bps: float = 0.0,
    minimum_probability: float = 0.0,
) -> ExecutableOpportunity:
    pred = prediction if isinstance(prediction, dict) else {}
    entry = _n(metadata.get("current_price") or metadata.get("price") or pred.get("entry_price") or pred.get("current_price"))
    margin = max(0.0, _n(pred.get("position_size_usdt") or pred.get("margin_usdt")))
    leverage = 2 if leverage > 1 else 1
    notional = margin * leverage
    qty = notional / entry if entry > 0 else 0.0
    is_short = side.upper() == "SHORT"
    target = _n(pred.get("target_price") or pred.get("take_profit_price"))
    stop = _n(pred.get("stop_price") or pred.get("stop_loss_price"))
    risk_metrics = pred.get("risk_metrics") if isinstance(pred.get("risk_metrics"), dict) else {}
    tp_pct = abs(_n(
        pred.get("take_profit_percentage")
        or pred.get("take_profit_pct")
        or risk_metrics.get("percentage_reward")
    ))
    sl_pct = abs(_n(
        pred.get("stop_loss_percentage")
        or pred.get("stop_loss_pct")
        or risk_metrics.get("percentage_risk")
    ))
    if not target and entry and tp_pct:
        target = entry * (1 - tp_pct / 100 if is_short else 1 + tp_pct / 100)
    if not stop and entry and sl_pct:
        stop = entry * (1 + sl_pct / 100 if is_short else 1 - sl_pct / 100)

    entry_fee = notional * max(0.0, entry_fee_bps) / 10000.0
    exit_fee = notional * max(0.0, exit_fee_bps) / 10000.0
    slippage = notional * max(0.0, slippage_bps) / 10000.0
    funding = notional * abs(funding_bps) / 10000.0 if leverage > 1 else 0.0
    exit_reserve = exit_fee + slippage + funding
    if max_loss_usd > 0 and entry > 0 and qty > 0:
        price_risk_budget = max(0.0, max_loss_usd - exit_reserve)
        max_distance = price_risk_budget / qty
        capped_stop = entry + max_distance if is_short else entry - max_distance
        stop = min(stop, capped_stop) if is_short and stop > 0 else max(stop, capped_stop) if stop > 0 else capped_stop

    geometry = entry > 0 and qty > 0 and target > 0 and stop > 0 and (
        (target < entry < stop) if is_short else (stop < entry < target)
    )
    gross_risk = abs(stop - entry) * qty if geometry else 0.0
    gross_reward = abs(target - entry) * qty if geometry else 0.0
    economic_risk = gross_risk + exit_reserve
    probability = _n(pred.get("success_probability"))
    total_costs = entry_fee + exit_reserve
    expected_net = probability * gross_reward - (1.0 - probability) * gross_risk - total_costs
    rejection = resistance_rejection_evidence(metadata) if thesis_type == "SHORT_RESISTANCE_REJECTION" else None
    valid = bool(
        executable and pred and geometry and economic_risk > 0
        and expected_net > 0 and probability >= minimum_probability
    )
    if rejection is not None:
        valid = valid and bool(rejection["confirmed"])
    return ExecutableOpportunity(
        side.upper(), thesis_type, trade_type, entry, margin, leverage, qty, notional,
        target, stop, economic_risk, gross_reward, entry_fee, exit_fee, slippage,
        funding, expected_net, expected_net / economic_risk if economic_risk else 0.0,
        valid, rejection,
    )


def scale_contract(contract: Dict[str, Any], margin_usdt: float) -> Dict[str, Any]:
    """Scale economic fields down to actual margin; raising above locked is forbidden."""
    locked = _n(contract.get("margin_usdt"))
    actual = max(0.0, _n(margin_usdt))
    if actual > locked + 1e-9:
        raise ValueError("actual margin exceeds locked_position_size_usdt")
    ratio = actual / locked if locked else 0.0
    result = dict(contract)
    for key in (
        "margin_usdt", "qty", "notional_usdt", "economic_risk_usdt",
        "economic_reward_usdt", "projected_entry_fee_usdt",
        "projected_exit_fee_usdt", "slippage_reserve_usdt",
        "funding_reserve_usdt", "expected_net_pnl_usdt",
    ):
        result[key] = actual if key == "margin_usdt" else _n(contract.get(key)) * ratio
    return result
