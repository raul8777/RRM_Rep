from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from execution_modes import FUTURES_SHORT, range_scalp_short_mode
from opportunity_contract import build_executable_opportunity


@dataclass(frozen=True)
class DirectionalArbitration:
    symbol: str
    selected_mode: str
    reason: str
    capital_intent: str
    locked_position_size_usdt: float
    short_score: float
    long_score: float
    short_eligible: bool
    long_eligible: bool
    long_structure: bool
    short_structure: bool
    short_resistance_setup: bool
    futures_executable: bool
    short_executable: bool
    short_prediction: Dict[str, Any]
    long_prediction: Dict[str, Any]
    futures_long_prediction: Dict[str, Any]
    sr_analysis_side: str = "NONE"
    short_risk_usdt: float = 0.0
    long_risk_usdt: float = 0.0
    short_expected_net_return: float = 0.0
    long_expected_net_return: float = 0.0
    selected_thesis_type: str = "NONE"
    long_thesis_type: str = "NONE"
    short_thesis_type: str = "NONE"
    selected_contract: Optional[Dict[str, Any]] = None
    alternative_contract: Optional[Dict[str, Any]] = None
    long_contract: Optional[Dict[str, Any]] = None
    short_contract: Optional[Dict[str, Any]] = None
    futures_long_contract: Optional[Dict[str, Any]] = None
    short_venue_contracts: Optional[list] = None
    long_venue_contracts: Optional[list] = None
    policy: str = "executable_contract_economic_return_on_risk_v2"
    all_venue_contracts: Optional[list] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _n(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _contract_costs(settings: Any, trade_type: str, metadata: Dict[str, Any]) -> Dict[str, float]:
    is_futures = trade_type == "futures"
    if is_futures:
        entry_rate = _n(getattr(settings, "opportunity_contract_futures_entry_fee_rate", 0.0002), 0.0002)
        exit_rate = _n(getattr(settings, "opportunity_contract_futures_exit_fee_rate", 0.0008), 0.0008)
    else:
        entry_rate = _n(getattr(settings, "opportunity_contract_spot_entry_fee_rate", 0.0005), 0.0005)
        exit_rate = _n(getattr(settings, "opportunity_contract_spot_exit_fee_rate", 0.001), 0.001)
    slippage_rate = _n(getattr(settings, "slippage_pct", 0.00052), 0.00052)
    return {
        "max_loss_usd": _n(getattr(settings, "max_loss_usd_per_trade", 18.0), 18.0),
        "entry_fee_bps": entry_rate * 10000.0,
        "exit_fee_bps": exit_rate * 10000.0,
        "slippage_bps": slippage_rate * 10000.0,
        "funding_bps": abs(_n(metadata.get("funding_bps"))) if is_futures else 0.0,
    }


def _sr_side(metadata: Dict[str, Any], settings: Any) -> str:
    price, support, resistance = map(_n, (metadata.get("current_price"), metadata.get("support_level"), metadata.get("resistance_level")))
    if price > 0 and support > 0 and resistance > support:
        zone = (price - support) / (resistance - support)
    else:
        zone = _n(metadata.get("sr_zone_position"), .5)
    support_event = bool(metadata.get("support_bounce_buy") or metadata.get("sr_bounce") or metadata.get("support_breakdown_sell") or metadata.get("sr_breakdown"))
    resistance_event = bool(metadata.get("resistance_bounce_sell") or metadata.get("sr_rejection") or metadata.get("resistance_breakout_buy") or metadata.get("sr_breakout"))
    if zone <= _n(getattr(settings, "range_scalp_long_support_zone_max", .55), .55) or support_event:
        return "SUPPORT"
    if zone >= _n(getattr(settings, "range_scalp_short_resistance_threshold", .65), .65) or resistance_event:
        return "RESISTANCE"
    return "NONE"


def _with_position_size(pred: Optional[Dict[str, Any]], size: float) -> Optional[Dict[str, Any]]:
    if not isinstance(pred, dict):
        return pred
    if pred.get("position_size_usdt") is not None and pred.get("margin_usdt") is not None:
        return pred
    if pred.get("position_size_usdt") is None:
        return {**pred, "position_size_usdt": float(size)}
    return pred


def evaluate_range_scalp(symbol: str, metadata: Dict[str, Any], long_prediction: Optional[Dict[str, Any]],
                         futures_long_prediction: Optional[Dict[str, Any]], short_prediction: Optional[Dict[str, Any]],
                         futures_executable: bool, settings: Any,
                         short_executable: Optional[bool] = None,
                         futures_short_prediction: Optional[Dict[str, Any]] = None,
                         spot_margin_short_prediction: Optional[Dict[str, Any]] = None,
                         spot_margin_executable: Optional[bool] = None) -> DirectionalArbitration:
    """Compare Spot/Futures LONG and executable SHORT contracts without side preference."""
    _base_size = float(
        (short_prediction or {}).get("position_size_usdt")
        or (long_prediction or {}).get("position_size_usdt")
        or getattr(settings, "range_scalp_usdt_per_trade", 1000.0)
        or 1000.0
    )
    long_prediction = _with_position_size(long_prediction, _base_size)
    futures_long_prediction = _with_position_size(futures_long_prediction, _base_size)
    short_prediction = _with_position_size(short_prediction, _base_size)
    futures_short_prediction = _with_position_size(futures_short_prediction, _base_size)
    spot_margin_short_prediction = _with_position_size(spot_margin_short_prediction, _base_size)
    side = _sr_side(metadata, settings)
    long_thesis = {
        "SUPPORT": "LONG_SUPPORT_BOUNCE",
        "RESISTANCE": "LONG_RESISTANCE_BREAKOUT",
        "NONE": "LONG_DIRECTIONAL_EDGE",
    }[side]
    short_thesis = {
        "SUPPORT": "SHORT_SUPPORT_BREAKDOWN",
        "RESISTANCE": "SHORT_RESISTANCE_REJECTION",
        "NONE": "SHORT_DIRECTIONAL_EDGE",
    }[side]
    short_mode = range_scalp_short_mode(settings)
    ranking_mode = str(getattr(settings, "range_scalp_ranking_mode", "ror") or "ror").strip().lower()

    def _rank_key(c):
        if ranking_mode == "ev_net":
            return (c.expected_net_pnl_usdt, c.expected_return_on_risk)
        return (c.expected_return_on_risk, c.expected_net_pnl_usdt)

    short_exec = bool(futures_executable if short_executable is None else short_executable)
    minimum_probability = _n(getattr(settings, "range_scalp_direction_min_probability", 0.55), 0.55)
    min_stop_atr = _n(getattr(settings, "range_scalp_min_stop_atr_multiple", 0.0), 0.0)
    breakout_penalty = _n(getattr(settings, "range_scalp_rejection_breakout_penalty", 1.0), 1.0)
    sl_multiplier = _n(
        getattr(settings, "entry_contract_sl_multiplier", getattr(settings, "range_scalp_contract_sl_multiplier", 1.0)),
        1.0,
    )
    tp_multiplier = _n(
        getattr(settings, "entry_contract_tp_multiplier", getattr(settings, "range_scalp_contract_tp_multiplier", 1.0)),
        1.0,
    )
    spot = build_executable_opportunity(
        "LONG", long_thesis, long_prediction, metadata,
        executable=bool(metadata.get("spot_executable", True)), leverage=1,
        trade_type="spot", minimum_probability=minimum_probability,
        min_stop_atr_multiple=min_stop_atr,
        exchange_filters=metadata.get("spot_exchange_filters"),
        sl_multiplier=sl_multiplier,
        tp_multiplier=tp_multiplier,
        **_contract_costs(settings, "spot", metadata),
    )
    fut_long = build_executable_opportunity(
        "LONG", long_thesis, futures_long_prediction, metadata,
        executable=futures_executable, leverage=2, trade_type="futures",
        minimum_probability=minimum_probability,
        min_stop_atr_multiple=min_stop_atr,
        exchange_filters=metadata.get("futures_exchange_filters"),
        sl_multiplier=sl_multiplier,
        tp_multiplier=tp_multiplier,
        **_contract_costs(settings, "futures", metadata),
    )
    venue_short_candidates = []
    futures_short_route_executable = bool(futures_executable and short_exec)
    if futures_short_route_executable:
        _futures_short_pred = futures_short_prediction
        if _futures_short_pred is None and short_mode == FUTURES_SHORT:
            _futures_short_pred = short_prediction
        _futures_short = build_executable_opportunity(
            "SHORT", short_thesis, _futures_short_pred, metadata, executable=True,
            leverage=2, trade_type="futures", minimum_probability=minimum_probability,
            min_stop_atr_multiple=min_stop_atr, breakout_penalty=breakout_penalty,
            exchange_filters=metadata.get("futures_exchange_filters"),
            sl_multiplier=sl_multiplier,
            tp_multiplier=tp_multiplier,
            **_contract_costs(settings, "futures", metadata),
        )
        venue_short_candidates.append((FUTURES_SHORT, _futures_short))
    _margin_exec = bool(
        getattr(settings, "spot_margin_short_enabled", False)
        and metadata.get("spot_executable", True)
        and (spot_margin_executable is not False)
    )
    if _margin_exec:
        _margin_short_pred = spot_margin_short_prediction
        if _margin_short_pred is None and short_mode != FUTURES_SHORT:
            _margin_short_pred = short_prediction
        _margin_short = build_executable_opportunity(
            "SHORT", short_thesis, _margin_short_pred, metadata, executable=True,
            leverage=1, trade_type="spot_margin", minimum_probability=minimum_probability,
            min_stop_atr_multiple=min_stop_atr, breakout_penalty=breakout_penalty,
            exchange_filters=metadata.get("spot_margin_exchange_filters") or metadata.get("spot_exchange_filters"),
            sl_multiplier=sl_multiplier,
            tp_multiplier=tp_multiplier,
            **_contract_costs(settings, "spot_margin", metadata),
        )
        venue_short_candidates.append(("SPOT_MARGIN_SHORT", _margin_short))
    if not venue_short_candidates:
        short_trade_type = "futures" if short_mode == FUTURES_SHORT else "spot_margin"
        short = build_executable_opportunity(
            "SHORT", short_thesis, short_prediction, metadata, executable=False,
            leverage=2 if short_mode == FUTURES_SHORT else 1, trade_type=short_trade_type,
            minimum_probability=minimum_probability, min_stop_atr_multiple=min_stop_atr,
            breakout_penalty=breakout_penalty,
            **_contract_costs(settings, short_trade_type, metadata),
        )
        venue_short_candidates.append((short_mode, short))

    short_contract_by_mode = {
        mode: contract
        for mode, contract in venue_short_candidates
        if mode in (FUTURES_SHORT, "SPOT_MARGIN_SHORT")
    }
    if FUTURES_SHORT not in short_contract_by_mode:
        _futures_short_pred = futures_short_prediction
        if _futures_short_pred is None and short_mode == FUTURES_SHORT:
            _futures_short_pred = short_prediction
        short_contract_by_mode[FUTURES_SHORT] = build_executable_opportunity(
            "SHORT", short_thesis, _futures_short_pred, metadata,
            executable=False, leverage=2, trade_type="futures",
            minimum_probability=minimum_probability,
            min_stop_atr_multiple=min_stop_atr,
            breakout_penalty=breakout_penalty,
            exchange_filters=metadata.get("futures_exchange_filters"),
            sl_multiplier=sl_multiplier,
            tp_multiplier=tp_multiplier,
            **_contract_costs(settings, "futures", metadata),
        )
    if "SPOT_MARGIN_SHORT" not in short_contract_by_mode:
        _margin_short_pred = spot_margin_short_prediction
        if _margin_short_pred is None and short_mode != FUTURES_SHORT:
            _margin_short_pred = short_prediction
        short_contract_by_mode["SPOT_MARGIN_SHORT"] = build_executable_opportunity(
            "SHORT", short_thesis, _margin_short_pred, metadata,
            executable=False, leverage=1, trade_type="spot_margin",
            minimum_probability=minimum_probability,
            min_stop_atr_multiple=min_stop_atr,
            breakout_penalty=breakout_penalty,
            exchange_filters=metadata.get("spot_margin_exchange_filters") or metadata.get("spot_exchange_filters"),
            sl_multiplier=sl_multiplier,
            tp_multiplier=tp_multiplier,
            **_contract_costs(settings, "spot_margin", metadata),
        )
    short_mode, short = max(venue_short_candidates, key=lambda item: _rank_key(item[1]))
    selected_short_prediction = (
        futures_short_prediction or (short_prediction if short_mode == FUTURES_SHORT else None)
        if short_mode == FUTURES_SHORT
        else spot_margin_short_prediction or short_prediction
    )
    routing = str(metadata.get("routing_intent") or "").upper()

    longs = [("SPOT_LONG", spot)]
    if futures_executable:
        longs.append(("FUTURES_LONG", fut_long))
    long_mode, long_contract = max(longs, key=lambda item: _rank_key(item[1]))
    long_ok = bool(getattr(settings, "range_scalp_long_entries_enabled", True) and long_contract.executable)
    short_ok = any(contract.executable for _, contract in venue_short_candidates)
    selected_mode, thesis = "ABSTAIN", "NONE"
    reason = "BILATERAL_NEUTRAL_NO_POSITIVE_EXECUTABLE_RETURN" if side == "NONE" else "BILATERAL_NO_POSITIVE_EXECUTABLE_RETURN"
    selected = alternative = None
    candidates = []
    if long_ok:
        candidates.append((long_mode, long_thesis, long_contract))
    if short_ok:
        candidates.append((short_mode, short_thesis, short))
    if candidates:
        candidates.sort(key=lambda item: _rank_key(item[2]), reverse=True)
        selected_mode, thesis, selected_obj = candidates[0]
        selected = selected_obj.to_dict()
        alternative = candidates[1][2].to_dict() if len(candidates) > 1 else (short if selected_mode != short_mode else long_contract).to_dict()
        suffix = "LONG" if selected_mode in ("SPOT_LONG", "FUTURES_LONG") else "SHORT"
        side_label = side if side != "NONE" else "NEUTRAL"
        reason = f"BILATERAL_{side_label}_{suffix}_{'MAX_NET_RETURN' if len(candidates) > 1 else 'ONLY_POSITIVE'}"
    locked = _n((selected or {}).get("margin_usdt"))
    def _venue_contract_record(mode: str, contract, available: bool, unavailable_reason: Optional[str] = None) -> Dict[str, Any]:
        return {
            "mode": mode,
            **contract.to_dict(),
            "venue_executable_at_time": bool(available),
            "venue_unavailable_reason": None if available else unavailable_reason,
        }

    all_venue_contracts = [
        _venue_contract_record("SPOT_LONG", spot, bool(metadata.get("spot_executable", True)), "SPOT_UNAVAILABLE"),
        _venue_contract_record("FUTURES_LONG", fut_long, bool(futures_executable), "FUTURES_UNAVAILABLE"),
        _venue_contract_record(
            FUTURES_SHORT,
            short_contract_by_mode[FUTURES_SHORT],
            futures_short_route_executable,
            "FUTURES_UNAVAILABLE" if not futures_executable else "FUTURES_SHORT_UNAVAILABLE",
        ),
        _venue_contract_record(
            "SPOT_MARGIN_SHORT",
            short_contract_by_mode["SPOT_MARGIN_SHORT"],
            _margin_exec,
            "SPOT_MARGIN_DISABLED_OR_UNAVAILABLE",
        ),
    ]
    return DirectionalArbitration(symbol, selected_mode, reason,
        "SPOT" if selected_mode == "SPOT_LONG" else ("NONE" if selected_mode == "ABSTAIN" else "FUTURES" if selected_mode != "SPOT_MARGIN_SHORT" else "SPOT_MARGIN"),
        locked, round(short.expected_return_on_risk, 6), round(long_contract.expected_return_on_risk, 6),
        short_ok, long_ok, side != "NONE", side != "NONE", side == "RESISTANCE", bool(futures_executable), short_exec,
        dict(selected_short_prediction or {}), dict(long_prediction or {}), dict(futures_long_prediction or {}), side,
        round(short.economic_risk_usdt, 6), round(long_contract.economic_risk_usdt, 6),
        round(short.expected_return_on_risk, 6), round(long_contract.expected_return_on_risk, 6), thesis,
        long_thesis, short_thesis, selected, alternative,
        long_contract.to_dict(), short.to_dict(), fut_long.to_dict(),
        [dict(mode=mode, **contract.to_dict()) for mode, contract in venue_short_candidates],
        [dict(mode=mode, **contract.to_dict()) for mode, contract in longs],
        all_venue_contracts=all_venue_contracts)
