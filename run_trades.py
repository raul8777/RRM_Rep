import math
import time
from decimal import Decimal
from typing import Dict, List

from binance_client import BinanceClient
from config import get_settings
from news_client import NewsClient
from signal_store import get_signals
from strategy import rank_buy_signals, rank_with_technical, normalize_symbol
from analysis_cache import analysis_cache


def _align_quantity_to_step(quantity: float, step: float) -> float:
    if step <= 0 or quantity <= 0:
        return 0.0
    q = Decimal(str(quantity))
    step_dec = Decimal(str(step))
    aligned = (q // step_dec) * step_dec
    if aligned <= 0:
        return 0.0
    return float(aligned.normalize())


def _get_symbol_filters(client: BinanceClient, symbol: str) -> Dict[str, float]:
    info = client.client.get_symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Symbol info not available for {symbol}")

    filters = info.get("filters", [])
    lot = next((f for f in filters if f["filterType"] == "LOT_SIZE"), None)
    notional = next((f for f in filters if f["filterType"] == "NOTIONAL"), None)

    step = float(lot.get("stepSize", 1.0)) if lot else 1.0
    min_qty = float(lot.get("minQty", 0.0)) if lot else 0.0
    min_notional = float(notional.get("minNotional", 0.0)) if notional else 0.0
    return {"step": step, "min_qty": min_qty, "min_notional": min_notional}


def _get_news_scores(symbols: List[str], news_client: NewsClient, settings) -> Dict[str, float]:
    if not symbols:
        return {}

    recent = news_client.get_recent_news(symbols, settings.news_lookback_hours)
    scores: Dict[str, float] = {}
    for article in recent:
        text = f"{article.get('title', '')} {article.get('summary', '')}".upper()
        for symbol in symbols:
            if symbol.upper() in text:
                scores[symbol] = scores.get(symbol, 0.0) + 1.0
    return scores


def _get_top_buy_symbols(settings, news_client: NewsClient) -> List[Dict]:
    signals = get_signals()
    if not signals:
        return []

    symbols = sorted({normalize_symbol(signal.get("symbol", "")) for signal in signals if signal.get("symbol")})
    news_scores = _get_news_scores(symbols, news_client, settings)
    
    # Get technical scores from analysis cache
    technical_scores: Dict[str, float] = {}
    for symbol in symbols:
        cached = analysis_cache.get(symbol)
        if cached:
            technical_scores[symbol] = cached.get("technical_score", 50.0)
        else:
            technical_scores[symbol] = 50.0  # Default neutral score if no analysis yet
    
    # Use combined ranking if technical scores are available
    if any(v != 50.0 for v in technical_scores.values()):
        top_signals = rank_with_technical(signals, news_scores, technical_scores, settings.max_positions)
    else:
        top_signals = rank_buy_signals(signals, news_scores, settings.max_positions)
    
    return top_signals


def _sell_holdings_not_in_top(client: BinanceClient, settings, balances: Dict[str, float], top_symbols: List[str]) -> List[Dict]:
    orders = []
    for asset, amount in balances.items():
        if asset.upper() == "USDT" or amount <= 0:
            continue

        symbol = f"{asset.upper()}USDT"
        if symbol in top_symbols:
            print(f"Keeping asset {asset} because {symbol} is in top symbols")
            continue

        try:
            filters = _get_symbol_filters(client, symbol)
        except Exception as exc:
            print(f"Skip sell {symbol}: cannot load symbol filters: {exc}")
            continue

        price = client.get_symbol_price(symbol)
        qty = _align_quantity_to_step(amount, filters["step"])
        if qty <= 0 or qty < filters["min_qty"]:
            print(f"Skipping sell {symbol}: qty {qty} below min_qty {filters['min_qty']}")
            continue
        if qty * price < filters["min_notional"]:
            print(f"Skipping sell {symbol}: notional {qty * price} below min_notional {filters['min_notional']}")
            continue

        try:
            order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
            orders.append({"symbol": symbol, "side": "SELL", "quantity": qty, "order": order})
            print(f"Sold {qty} {symbol}")
        except Exception as exc:
            print(f"Sell order failed for {symbol}: {exc}")
    return orders


def _buy_top_symbols(client: BinanceClient, settings, top_symbols: List[str]) -> List[Dict]:
    orders = []
    if not top_symbols:
        return orders

    balances = client.get_account_balance()
    usdt = float(balances.get("USDT", 0.0))
    if usdt <= 0:
        print("No USDT available for buys")
        return orders

    allocation = min(settings.max_per_trade_usdt, usdt / len(top_symbols))
    print(f"USDT available: {usdt}, allocation per symbol: {allocation}")

    for symbol in top_symbols:
        try:
            price = client.get_symbol_price(symbol)
            filters = _get_symbol_filters(client, symbol)
        except Exception as exc:
            print(f"Skip buy {symbol}: {exc}")
            continue

        qty = _align_quantity_to_step(allocation / price, filters["step"])
        if qty <= 0 or qty < filters["min_qty"]:
            print(f"Skipping buy {symbol}: qty {qty} below min_qty {filters['min_qty']}")
            continue
        if qty * price < filters["min_notional"]:
            print(f"Skipping buy {symbol}: notional {qty * price} below min_notional {filters['min_notional']}")
            continue

        try:
            order = client.create_market_order(symbol=symbol, side="BUY", quantity=qty)
            orders.append({"symbol": symbol, "side": "BUY", "quantity": qty, "order": order})
            print(f"Bought {qty} {symbol}")
        except Exception as exc:
            print(f"Buy order failed for {symbol}: {exc}")
    return orders


def execute_once():
    settings = get_settings()
    client = BinanceClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        testnet=settings.binance_use_testnet,
        dry_run=settings.dry_run,
    )
    news_client = NewsClient()

    print("DRY_RUN=", settings.dry_run)
    print("ALLOW_REAL_TRADING=", settings.allow_real_trading)
    print("MAX_PER_TRADE_USDT=", settings.max_per_trade_usdt)

    if not settings.binance_use_testnet and not settings.allow_real_trading:
        print("Real trading is disabled. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    if settings.agent_mode != "auto":
        print("Agent mode is not auto. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    top_signals = _get_top_buy_symbols(settings, news_client)
    top_symbols = [signal["symbol"] for signal in top_signals]
    print("Top buy symbols:", top_symbols)

    balances = client.get_account_balance()
    print("Balances:", balances)

    if top_symbols:
        sell_orders = _sell_holdings_not_in_top(client, settings, balances, top_symbols)
        if sell_orders:
            balances = client.get_account_balance()
            print("Balances after sells:", balances)

        buy_orders = _buy_top_symbols(client, settings, top_symbols)
    else:
        print("No buy signals available; skipping buy allocation.")
        sell_orders = []
        buy_orders = []

    print("Completed cycle.")
    return {"top_symbols": top_symbols, "sell_orders": sell_orders, "buy_orders": buy_orders}


def run_loop(interval_minutes: int = 5):
    """Run execute_once periodically every interval_minutes (blocking)."""
    while True:
        try:
            execute_once()
        except Exception as e:
            print("Error during execute_once:", e)
        time.sleep(max(1, interval_minutes) * 60)


if __name__ == '__main__':
    settings = get_settings()
    run_loop(getattr(settings, 'trade_cycle_minutes', 5))
