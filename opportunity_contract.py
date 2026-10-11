"""Executable opportunity contract shared by arbitration and execution."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple


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
    validation_evidence: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _n(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
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


def stop_noise_gate(
    *,
    entry_price: float,
    stop_price: float,
    indicators: Optional[Dict[str, Any]],
    min_stop_atr_multiple: float,
) -> Tuple[bool, float, float]:
    """Fail-closed noise floor usable by any entry path (not only arbiter
    contracts). Returns (ok, stop_distance_frac, atr_frac). When the gate is
    enabled and ATR is missing, ok=False — absence of volatility data is not
    proof the stop is outside the noise band."""
    entry = _n(entry_price)
    stop = _n(stop_price)
    stop_distance_frac = abs(stop - entry) / entry if entry > 0 and stop > 0 else 0.0
    ind = indicators if isinstance(indicators, dict) else {}
    atr_pct = _n(ind.get("atr_pct") or ind.get("atr_percent"))
    atr_raw = _n(ind.get("atr") or ind.get("atr_1h") or ind.get("atr_14"))
    atr_frac = atr_pct / 100.0 if atr_pct and atr_pct > 0 else (atr_raw / entry if atr_raw and entry > 0 else 0.0)
    if min_stop_atr_multiple <= 0:
        return True, stop_distance_frac, atr_frac
    required_stop_distance = min_stop_atr_multiple * atr_frac
    ok = atr_frac > 0 and stop_distance_frac + 1e-12 >= required_stop_distance
    return ok, stop_distance_frac, atr_frac


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
    min_stop_atr_multiple: float = 0.0,
    breakout_penalty: float = 1.0,
    exchange_filters: Optional[Dict[str, Any]] = None,
    sl_multiplier: float = 1.0,
    tp_multiplier: float = 1.0,
) -> ExecutableOpportunity:
    pred = prediction if isinstance(prediction, dict) else {}
    is_short = side.upper() == "SHORT"
    entry = _n(metadata.get("current_price") or metadata.get("price") or pred.get("entry_price") or pred.get("current_price"))

    # Resolve target and stop from prediction percentages / risk_metrics or structural SR.
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
    if not stop and entry > 0:
        stop = _n(metadata.get("resistance_level")) if is_short else _n(metadata.get("support_level"))
    if not target and entry > 0:
        target = _n(metadata.get("support_level")) if is_short else _n(metadata.get("resistance_level"))

    sl_multiplier = max(0.0, _n(sl_multiplier, 1.0))
    tp_multiplier = max(0.0, _n(tp_multiplier, 1.0))
    if entry > 0:
        stop = entry + (stop - entry) * sl_multiplier
        target = entry + (target - entry) * tp_multiplier

    filters = exchange_filters if isinstance(exchange_filters, dict) else {}
    filters_validated = bool(filters.get("exchange_filters_validated"))
    tick_size = max(0.0, _n(filters.get("tick_size") or filters.get("tickSize")))
    if tick_size > 0:
        from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

        def _round_price(value: float, rounding) -> float:
            return float((Decimal(str(value)) / Decimal(str(tick_size))).to_integral_value(rounding=rounding) * Decimal(str(tick_size)))

        # Conservative rounding: do not improve a contract's reward or reduce
        # its risk merely because the prediction was between exchange ticks.
        target = _round_price(target, ROUND_CEILING if is_short else ROUND_FLOOR)
        stop = _round_price(stop, ROUND_CEILING if is_short else ROUND_FLOOR)

    indicators = metadata.get("indicators") if isinstance(metadata.get("indicators"), dict) else {}
    atr_raw = _n(metadata.get("atr") or indicators.get("atr"))
    atr_pct = _n(metadata.get("atr_pct") or indicators.get("atr_pct"))
    atr_frac = atr_pct / 100.0 if atr_pct and atr_pct > 0 else (atr_raw / entry if atr_raw and entry > 0 else 0.0)
    original_stop = stop
    original_stop_distance_frac = abs(stop - entry) / entry if entry > 0 and stop > 0 else 0.0
    noise_floor_required = max(0.0, min_stop_atr_multiple) * atr_frac
    stop_noise_adjusted = False
    if min_stop_atr_multiple > 0 and atr_frac > 0 and original_stop_distance_frac < noise_floor_required:
        stop = entry * (1.0 + noise_floor_required if is_short else 1.0 - noise_floor_required)
        stop_noise_adjusted = True

    # Size by max_loss: scale notional down to the model/structural stop,
    # including any ATR noise-floor widening above.
    margin = max(0.0, _n(pred.get("position_size_usdt") or pred.get("margin_usdt")))
    leverage = 2 if leverage > 1 else 1
    requested_notional = margin * leverage
    price_distance_pct = abs(stop - entry) / entry if entry > 0 and stop > 0 else 0.0
    entry_fee_rate = max(0.0, entry_fee_bps) / 10000.0
    exit_fee_rate = max(0.0, exit_fee_bps) / 10000.0
    slippage_rate = max(0.0, slippage_bps) / 10000.0
    funding_rate = abs(funding_bps) / 10000.0 if leverage > 1 else 0.0
    stop_notional_ratio = stop / entry if entry > 0 and stop > 0 else 1.0
    risk_cost_ratio = (
        entry_fee_rate
        + stop_notional_ratio * exit_fee_rate
        + (1.0 + stop_notional_ratio) * slippage_rate
        + funding_rate
    )
    notional = requested_notional
    if max_loss_usd > 0 and price_distance_pct > 0:
        max_notional = max_loss_usd / (price_distance_pct + risk_cost_ratio)
        notional = min(requested_notional, max_notional)
    notional = max(0.0, notional)
    min_qty = max(0.0, _n(filters.get("min_qty") or filters.get("minQty")))
    min_notional_raw = filters.get("min_notional", filters.get("minNotional"))
    market_step = max(0.0, _n(filters.get("market_step") or filters.get("step") or filters.get("stepSize")))
    market_min_qty = max(0.0, _n(filters.get("market_min_qty") or filters.get("min_qty") or filters.get("minQty")))
    market_max_qty = max(0.0, _n(filters.get("market_max_qty") or filters.get("max_qty") or filters.get("maxQty")))
    if exchange_filters is not None and not filters_validated:
        min_notional = float("inf")
    elif exchange_filters is None:
        min_notional = 10.0
    else:
        min_notional = max(0.0, _n(min_notional_raw))
    margin = notional / leverage
    raw_qty = notional / entry if entry > 0 else 0.0
    qty = raw_qty
    quantity_rounded_down = False
    if exchange_filters is not None and filters_validated and market_step > 0 and raw_qty > 0:
        from decimal import Decimal, ROUND_DOWN

        step = Decimal(str(market_step))
        qty = float((Decimal(str(raw_qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)
        quantity_rounded_down = qty < raw_qty
        notional = qty * entry
        margin = notional / leverage

    target_notional = qty * target
    stop_notional = qty * stop
    entry_fee = notional * entry_fee_rate
    target_exit_fee = target_notional * exit_fee_rate
    stop_exit_fee = stop_notional * exit_fee_rate
    target_slippage = (notional + target_notional) * slippage_rate
    stop_slippage = (notional + stop_notional) * slippage_rate
    funding = notional * funding_rate

    geometry = entry > 0 and qty > 0 and (market_min_qty <= 0 or qty >= market_min_qty) and (
        market_max_qty <= 0 or qty <= market_max_qty
    ) and notional >= min_notional and target > 0 and stop > 0 and (
        (target < entry < stop) if is_short else (stop < entry < target)
    )
    gross_risk = abs(stop - entry) * qty if geometry else 0.0
    gross_reward = abs(target - entry) * qty if geometry else 0.0
    rejection = resistance_rejection_evidence(metadata) if thesis_type == "SHORT_RESISTANCE_REJECTION" else None
    raw_probability = _n(pred.get("success_probability"), -1.0)
    probability_valid = 0.0 <= raw_probability <= 1.0
    probability = raw_probability if probability_valid else 0.0
    # The fade thesis is invalidated by breakout pressure: the observed signal
    # systematically precedes continuation, not reversion. Penalize the contract
    # probability so the competing breakout contract wins on merit.
    if breakout_penalty < 1.0 and rejection and rejection.get("breakout_pressure"):
        probability *= breakout_penalty
    exit_fee = probability * target_exit_fee + (1.0 - probability) * stop_exit_fee
    slippage = probability * target_slippage + (1.0 - probability) * stop_slippage
    economic_risk = gross_risk + entry_fee + stop_exit_fee + stop_slippage + funding
    expected_net = (
        probability * gross_reward
        - (1.0 - probability) * gross_risk
        - entry_fee - exit_fee - slippage - funding
    )
    # Noise floor: a stop inside ~1×ATR is dominated by bar noise, which the
    # binary EV model cannot express.
    stop_distance_frac = abs(stop - entry) / entry if entry > 0 and stop > 0 else 0.0
    if min_stop_atr_multiple > 0:
        # fail-closed: ATR missing means we cannot prove the stop is outside the
        # noise band — treat the contract as non-executable, not as a bypass.
        stop_above_noise = atr_frac > 0 and stop_distance_frac + 1e-12 >= noise_floor_required
    else:
        stop_above_noise = True
    valid = bool(
        executable and pred and probability_valid and geometry and economic_risk > 0
        and expected_net > 0 and probability >= minimum_probability and stop_above_noise
    )
    if max_loss_usd > 0:
        valid = valid and (economic_risk <= max_loss_usd + 1e-9)
    return ExecutableOpportunity(
        side.upper(), thesis_type, trade_type, entry, margin, leverage, qty, notional,
        target, stop, economic_risk, gross_reward, entry_fee, exit_fee, slippage,
        funding, expected_net, expected_net / economic_risk if economic_risk else 0.0,
        valid, rejection, {
            "input_executable": bool(executable),
            "prediction_present": bool(pred),
            "geometry_valid": bool(geometry),
            "economic_risk_positive": economic_risk > 0,
            "expected_net_positive": expected_net > 0,
            "success_probability": probability,
            "success_probability_input_valid": probability_valid,
            "minimum_probability": minimum_probability,
            "probability_passed": probability >= minimum_probability,
            "max_loss_passed": max_loss_usd <= 0 or economic_risk <= max_loss_usd + 1e-9,
            "stop_distance_pct": stop_distance_frac,
            "original_stop_distance_pct": original_stop_distance_frac,
            "original_stop_price": original_stop,
            "atr_frac": atr_frac,
            "noise_floor_required_pct": noise_floor_required,
            "stop_noise_adjusted": stop_noise_adjusted,
            "stop_above_noise_floor": stop_above_noise,
            "atr_available": atr_frac > 0,
            "exchange_filters_available": bool(exchange_filters),
            "exchange_filters_validated": filters_validated,
            "exchange_filter_snapshot": filters.get("filter_snapshot"),
            "exchange_filter_snapshot_sha256": filters.get("filter_snapshot_sha256"),
            "exchange_filter_snapshot_retrieved_at": filters.get("filter_snapshot_retrieved_at"),
            "exchange_filter_snapshot_venue": filters.get("filter_snapshot_venue"),
            "exchange_filter_snapshot_symbol": filters.get("filter_snapshot_symbol"),
            "exchange_filter_snapshot_source": filters.get("filter_snapshot_source"),
            "exchange_min_qty": min_qty,
            "exchange_min_notional": min_notional if math.isfinite(min_notional) else None,
            "exchange_tick_size": tick_size,
            "market_step_size": market_step,
            "market_min_qty": market_min_qty,
            "market_max_qty": market_max_qty,
            "raw_qty_before_market_step": raw_qty,
            "quantity_rounded_down_to_market_step": quantity_rounded_down,
        },
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
