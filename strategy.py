import re
from typing import Dict, Optional

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
