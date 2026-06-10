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


def rank_with_technical(
    signals: List[Dict],
    news_scores: Dict[str, float],
    technical_scores: Dict[str, float],
    top_n: int,
) -> List[Dict]:
    """Rank signals combining TradingView (40%), news (30%), and technical (30%)."""
    candidates: Dict[str, Dict] = {}

    for signal in signals:
        symbol = normalize_symbol(signal.get("symbol", ""))
        if not symbol or signal.get("action", "").upper() != "BUY":
            continue

        tv_score = score_signal(signal, 0.0)
        news_score = news_scores.get(symbol, 50.0)
        tech_score = technical_scores.get(symbol, 50.0)

        combined_score = (tv_score * 0.4) + (news_score * 0.3) + (tech_score * 0.3)

        existing = candidates.get(symbol)
        if existing is None or combined_score > existing["combined_score"]:
            candidates[symbol] = {
                **signal,
                "symbol": symbol,
                "tv_score": tv_score,
                "news_score": news_score,
                "technical_score": tech_score,
                "combined_score": combined_score,
            }

    ranked = sorted(candidates.values(), key=lambda item: item["combined_score"], reverse=True)
    return ranked[:top_n]


def rank_for_portfolio_manager(
    signals: List[Dict],
    news_scores: Dict[str, float],
    technical_scores: Dict[str, float],
    risk_metrics: Dict[str, Dict[str, float]],
    min_confidence: float = 50.0,
) -> Dict[str, float]:
    """
    Calculate combined ranking scores for Portfolio Manager.
    
    This function expands on rank_with_technical to include risk metrics
    and returns a dictionary of symbol -> combined_score for use by Portfolio Manager.
    
    Args:
        signals: List of trading signals
        news_scores: Dictionary of news sentiment scores per symbol
        technical_scores: Dictionary of technical analysis scores per symbol
        risk_metrics: Dictionary of risk metrics per symbol (with risk_score key)
        min_confidence: Minimum confidence threshold (0-100)
        
    Returns:
        Dictionary mapping symbol to combined score (0-100)
    """
    signal_scores: Dict[str, float] = {}
    
    for signal in signals:
        symbol = normalize_symbol(signal.get("symbol", ""))
        if not symbol or signal.get("action", "").upper() != "BUY":
            continue
        
        tv_score = score_signal(signal, 0.0)
        news_score = news_scores.get(symbol, 50.0)
        tech_score = technical_scores.get(symbol, 50.0)
        
        # Get risk score (default to 50 if not available)
        risk_data = risk_metrics.get(symbol, {})
        risk_score = risk_data.get("risk_score", 50.0)
        
        # Calculate combined score with risk consideration
        # TradingView: 40%, News: 20%, Technical: 20%, Risk (inverted): 20%
        # Lower risk = higher contribution
        risk_contribution = (100 - risk_score) / 100.0
        
        combined_score = (
            tv_score * 0.4 +
            news_score * 0.2 +
            tech_score * 0.2 +
            risk_contribution * 20.0  # Scale to 0-20 range
        )
        
        # Normalize to 0-100 range
        combined_score = max(0, min(100, combined_score))
        
        # Only include if above minimum confidence
        if combined_score >= min_confidence:
            signal_scores[symbol] = combined_score
    
    return signal_scores
