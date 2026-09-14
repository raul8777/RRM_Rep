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
    policy: str = "executable_contract_economic_return_on_risk_v2"

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
                         short_executable: Optional[bool] = None) -> DirectionalArbitration:
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
    side = _sr_side(metadata, settings)
    long_thesis = {"SUPPORT": "LONG_SUPPORT_BOUNCE", "RESISTANCE": "LONG_RESISTANCE_BREAKOUT"}.get(side, "NONE")
    short_thesis = {"SUPPORT": "SHORT_SUPPORT_BREAKDOWN", "RESISTANCE": "SHORT_RESISTANCE_REJECTION"}.get(side, "NONE")
    short_mode = range_scalp_short_mode(settings)
    short_exec = bool(futures_executable if short_executable is None else short_executable)
    minimum_probability = _n(getattr(settings, "range_scalp_direction_min_probability", 0.55), 0.55)
    spot = build_executable_opportunity(
        "LONG", long_thesis, long_prediction, metadata,
        executable=bool(metadata.get("spot_executable", True)), leverage=1,
        trade_type="spot", minimum_probability=minimum_probability,
        **_contract_costs(settings, "spot", metadata),
    )
    fut_long = build_executable_opportunity(
        "LONG", long_thesis, futures_long_prediction, metadata,
        executable=futures_executable, leverage=2, trade_type="futures",
        minimum_probability=minimum_probability,
        **_contract_costs(settings, "futures", metadata),
    )
    short_trade_type = "futures" if short_mode == FUTURES_SHORT else "spot_margin"
    short = build_executable_opportunity(
        "SHORT", short_thesis, short_prediction, metadata, executable=short_exec,
        leverage=2 if short_mode == FUTURES_SHORT else 1, trade_type=short_trade_type,
        minimum_probability=minimum_probability,
        **_contract_costs(settings, short_trade_type, metadata),
    )
    routing = str(metadata.get("routing_intent") or "").upper()
    longs = [("SPOT_LONG", spot)]
    if futures_executable:
        longs.append(("FUTURES_LONG", fut_long))
    if routing == "FUTURES_LONG_ANALYSIS":
        long_mode, long_contract = "FUTURES_LONG", fut_long
    else:
        long_mode, long_contract = max(longs, key=lambda item: item[1].expected_return_on_risk)
    long_ok = bool(getattr(settings, "range_scalp_long_entries_enabled", True) and long_contract.executable)
    short_ok = short.executable
    selected_mode, thesis, reason = "ABSTAIN", "NONE", "NO_SR_BILATERAL_TRIGGER"
    selected = alternative = None
    if side != "NONE":
        candidates = []
        if long_ok:
            candidates.append((long_mode, long_thesis, long_contract))
        if short_ok:
            candidates.append((short_mode, short_thesis, short))
        if candidates:
            candidates.sort(key=lambda item: item[2].expected_return_on_risk, reverse=True)
            selected_mode, thesis, selected_obj = candidates[0]
            selected = selected_obj.to_dict()
            alternative = candidates[1][2].to_dict() if len(candidates) > 1 else (short if selected_mode != short_mode else long_contract).to_dict()
            suffix = "LONG" if selected_mode in ("SPOT_LONG", "FUTURES_LONG") else "SHORT"
            reason = f"BILATERAL_{side}_{suffix}_{'MAX_NET_RETURN' if len(candidates) > 1 else 'ONLY_POSITIVE'}"
        elif short_thesis == "SHORT_RESISTANCE_REJECTION" and short.rejection_evidence and not short.rejection_evidence["confirmed"]:
            reason = "SHORT_RESISTANCE_REJECTION_UNCONFIRMED"
        else:
            reason = f"BILATERAL_{side}_NO_POSITIVE_EXECUTABLE_RETURN"
    locked = _n((selected or {}).get("margin_usdt"))
    return DirectionalArbitration(symbol, selected_mode, reason,
        "SPOT" if selected_mode == "SPOT_LONG" else ("NONE" if selected_mode == "ABSTAIN" else "FUTURES" if selected_mode != "SPOT_MARGIN_SHORT" else "SPOT_MARGIN"),
        round(locked, 6), round(short.expected_return_on_risk, 6), round(long_contract.expected_return_on_risk, 6),
        short_ok, long_ok, side != "NONE", side != "NONE", side == "RESISTANCE", bool(futures_executable), short_exec,
        dict(short_prediction or {}), dict(long_prediction or {}), dict(futures_long_prediction or {}), side,
        round(short.economic_risk_usdt, 6), round(long_contract.economic_risk_usdt, 6),
        round(short.expected_return_on_risk, 6), round(long_contract.expected_return_on_risk, 6), thesis,
        long_thesis, short_thesis, selected, alternative)
