import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

BUY_WORDS = {"buy", "long", "compra", "buying", "bull"}
SELL_WORDS = {"sell", "short", "venta", "bear"}
SYMBOL_RE = re.compile(r"\b([A-Z]{3,6}USDT|USDT|BTCUSDT|ETHUSDT|BNBUSDT)\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\b")


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper().replace("/", "")
    return normalized


def parse_text_signal(text: str) -> Dict[str, Optional[str]]:
    normalized_text = text.strip().upper()
    symbol_match = SYMBOL_RE.search(normalized_text)
    symbol = normalize_symbol(symbol_match.group(1)) if symbol_match else ""

    action = ""
    if any(word in normalized_text for word in BUY_WORDS):
        action = "BUY"
    elif any(word in normalized_text for word in SELL_WORDS):
        action = "SELL"

    quantity = None
    amount_matches = AMOUNT_RE.findall(normalized_text)
    if amount_matches:
        if len(amount_matches) == 1 and symbol:
            quantity = float(amount_matches[0])
        elif len(amount_matches) >= 2:
            quantity = float(amount_matches[1]) if action else float(amount_matches[0])

    return {
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
    }


def parse_signal(payload: Dict) -> Dict:
    symbol = normalize_symbol(payload.get("symbol", "") or "")
    action = (payload.get("action") or "").strip().lower()
    quantity = payload.get("quantity")
    price = payload.get("price")
    comment = payload.get("comment")
    message = payload.get("message") or ""

    if not symbol or not action:
        if message:
            parsed = parse_text_signal(message)
            symbol = symbol or parsed.get("symbol", "")
            action = action or parsed.get("action", "").lower()
            quantity = quantity or parsed.get("quantity")

    if not symbol:
        raise ValueError("TradingView signal must include a symbol")

    if not action:
        raise ValueError("TradingView signal must include an action (buy/sell)")

    action_normalized = (
        "BUY" if action in BUY_WORDS else "SELL" if action in SELL_WORDS else action.upper()
    )

    signal = {
        "symbol": symbol,
        "action": action_normalized,
        "quantity": float(quantity) if quantity is not None else None,
        "price": float(price) if price is not None else None,
        "comment": comment,
        "message": message,
    }
    return signal


def build_order_params(signal: Dict, default_quantity: float) -> Dict[str, Optional[float]]:
    quantity = signal.get("quantity") or default_quantity
    return {
        "symbol": signal["symbol"],
        "side": signal["action"],
        "quantity": quantity,
    }


def _parse_timestamp(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def score_signal(signal: Dict, news_score: float = 0.0) -> float:
    action = (signal.get("action") or "").upper()
    score = 0.0
    if action == "BUY":
        score += 10.0
    elif action == "SELL":
        score -= 5.0

    quantity = signal.get("quantity") or 0.0
    score += min(quantity, 100.0) * 0.05
    score += news_score * 0.5

    received_at = _parse_timestamp(signal.get("received_at"))
    if received_at:
        age_hours = (datetime.now(timezone.utc) - received_at).total_seconds() / 3600.0
        score += max(0.0, 2.0 - age_hours * 0.25)

    return score


def rank_buy_signals(signals: List[Dict], news_scores: Dict[str, float], top_n: int) -> List[Dict]:
    candidates: Dict[str, Dict] = {}
    for signal in signals:
        symbol = normalize_symbol(signal.get("symbol", ""))
        if not symbol or signal.get("action", "").upper() != "BUY":
            continue

        score = score_signal(signal, news_scores.get(symbol, 0.0))
        existing = candidates.get(symbol)
        if existing is None or score > existing["score"]:
            candidates[symbol] = {**signal, "symbol": symbol, "score": score}

    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    return ranked[:top_n]


_REGIME_WEIGHTS = {
    # mode          tv     news   tech
    "SPOT_BULL":   (0.40,  0.30,  0.30),
    "RANGE_SCALP": (0.30,  0.20,  0.50),   # technical dominates in ranging
    "PRESERVATION":(0.35,  0.25,  0.40),   # more conservative
    "FUTURES_SHORT":(0.30, 0.10,  0.60),   # tech drives shorts, news irrelevant
}


def rank_with_technical(
    signals: List[Dict],
    news_scores: Dict[str, float],
    technical_scores: Dict[str, float],
    top_n: int,
    regime: str = "SPOT_BULL",
) -> List[Dict]:
    """Rank signals combining TradingView, news, and technical scores.
    Weights are regime-specific (B2): FUTURES_SHORT heavily favours technical.
    """
    w_tv, w_news, w_tech = _REGIME_WEIGHTS.get(regime, _REGIME_WEIGHTS["SPOT_BULL"])
    candidates: Dict[str, Dict] = {}

    for signal in signals:
        symbol = normalize_symbol(signal.get("symbol", ""))
        if not symbol or signal.get("action", "").upper() != "BUY":
            continue

        tv_score = score_signal(signal, 0.0)
        news_score = news_scores.get(symbol, 50.0)
        tech_score = technical_scores.get(symbol, 50.0)

        combined_score = (tv_score * w_tv) + (news_score * w_news) + (tech_score * w_tech)

        existing = candidates.get(symbol)
        if existing is None or combined_score > existing["combined_score"]:
            candidates[symbol] = {
                **signal,
                "symbol": symbol,
                "tv_score": tv_score,
                "news_score": news_score,
                "technical_score": tech_score,
                "combined_score": combined_score,
                "regime_weights": f"tv={w_tv}/news={w_news}/tech={w_tech}",
            }

    ranked = sorted(candidates.values(), key=lambda item: item["combined_score"], reverse=True)
    return ranked[:top_n]


def rank_for_portfolio_manager(
    signals: List[Dict],
    news_scores: Dict[str, float],
    technical_scores: Dict[str, float],
    risk_metrics: Dict[str, Dict[str, float]],
    min_confidence: float = 50.0,
    regime: str = "SPOT_BULL",
) -> Dict[str, float]:
    """
    Calculate combined ranking scores for Portfolio Manager.
    Weights are regime-specific (B2) — same table as rank_with_technical.
    Risk is included as a 4th component, capped at 20% of total score.
    """
    w_tv, w_news, w_tech = _REGIME_WEIGHTS.get(regime, _REGIME_WEIGHTS["SPOT_BULL"])
    # Reserve 20% for risk; rescale the other three proportionally
    scale = 0.80
    w_tv_r   = w_tv   * scale
    w_news_r = w_news * scale
    w_tech_r = w_tech * scale

    signal_scores: Dict[str, float] = {}

    for signal in signals:
        symbol = normalize_symbol(signal.get("symbol", ""))
        if not symbol or signal.get("action", "").upper() != "BUY":
            continue

        tv_score   = score_signal(signal, 0.0)
        news_score = news_scores.get(symbol, 50.0)
        tech_score = technical_scores.get(symbol, 50.0)

        risk_data  = risk_metrics.get(symbol, {})
        risk_score = risk_data.get("risk_score", 50.0)
        risk_contribution = (100 - risk_score) / 100.0  # lower risk → higher score

        combined_score = (
            tv_score   * w_tv_r +
            news_score * w_news_r +
            tech_score * w_tech_r +
            risk_contribution * 20.0
        )
        combined_score = max(0.0, min(100.0, combined_score))

        if combined_score >= min_confidence:
            signal_scores[symbol] = combined_score

    return signal_scores
