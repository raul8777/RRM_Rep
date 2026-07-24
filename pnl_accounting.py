from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from binance_commission import exit_commission


def normalize_spot_fills(
    fills: Iterable[Dict[str, Any]],
    symbol: str,
    price_lookup: Optional[Callable[[str], float]] = None,
) -> Dict[str, Any]:
    total_qty = 0.0
    total_quote = 0.0
    commission_usdt = 0.0
    commissions_by_asset: Dict[str, float] = {}
    commission_conversions = []
    for fill in fills:
        qty = float(fill.get("qty", fill.get("quantity", 0.0)) or 0.0)
        price = float(fill.get("price", 0.0) or 0.0)
        raw_commission = float(fill.get("commission", 0.0) or 0.0)
        asset = str(fill.get("commissionAsset", "USDT") or "USDT").upper()
        if asset == "USDT":
            converted = raw_commission
            source = "fill_quote"
            conversion_price = 1.0
        elif asset == symbol.replace("USDT", "").upper():
            converted = raw_commission * price
            source = "fill_price"
            conversion_price = price
        elif asset == "BNB":
            conversion_price = float(price_lookup("BNBUSDT")) if price_lookup else 0.0
            converted = raw_commission * conversion_price
            source = "bnbusdt_mark" if conversion_price > 0 else "unavailable"
        else:
            pair = f"{asset}USDT"
            conversion_price = float(price_lookup(pair)) if price_lookup else 0.0
            converted = raw_commission * conversion_price
            source = f"{pair}_mark" if conversion_price > 0 else "unavailable"
        total_qty += qty
        total_quote += qty * price
        commission_usdt += converted
        commissions_by_asset[asset] = commissions_by_asset.get(asset, 0.0) + raw_commission
        commission_conversions.append({
            "asset": asset,
            "amount": raw_commission,
            "usdt": converted,
            "price": conversion_price,
            "source": source,
        })
    avg_price = total_quote / total_qty if total_qty > 0 else 0.0
    return {
        "symbol": symbol,
        "qty": total_qty,
        "quote_qty": total_quote,
        "avg_price": avg_price,
        "commission_usdt": commission_usdt,
        "commissions_by_asset": commissions_by_asset,
        "commission_conversions": commission_conversions,
        "accounting_timestamp": datetime.now(timezone.utc).isoformat(),
    }


def combine_spot_fills(*fills: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    confirmed = [fill for fill in fills if fill and fill.get("binance_confirmed")]
    total_qty = sum(float(fill.get("qty", 0.0) or 0.0) for fill in confirmed)
    total_quote = sum(float(fill.get("quote_qty", 0.0) or 0.0) for fill in confirmed)
    commission_usdt = sum(float(fill.get("commission_usdt", fill.get("commission", 0.0)) or 0.0) for fill in confirmed)
    commissions_by_asset: Dict[str, float] = {}
    conversions = []
    order_ids = []
    for fill in confirmed:
        order_id = fill.get("order_id")
        if order_id is not None:
            order_ids.append(str(order_id))
        for asset, amount in (fill.get("commissions_by_asset") or {}).items():
            commissions_by_asset[asset] = commissions_by_asset.get(asset, 0.0) + float(amount or 0.0)
        conversions.extend(fill.get("commission_conversions") or [])
    return {
        "symbol": confirmed[0].get("symbol", "") if confirmed else "",
        "order_id": order_ids[0] if len(order_ids) == 1 else None,
        "order_ids": order_ids,
        "qty": total_qty,
        "quote_qty": total_quote,
        "avg_price": total_quote / total_qty if total_qty > 0 else 0.0,
        "commission_usdt": commission_usdt,
        "commission": commission_usdt,
        "commissions_by_asset": commissions_by_asset,
        "commission_conversions": conversions,
        "accounting_timestamp": datetime.now(timezone.utc).isoformat(),
        "binance_confirmed": bool(confirmed),
    }


def mark_to_market_spot(position: Any, current_price: float, settings: Any) -> Dict[str, float | str]:
    entry_price = float(getattr(position, "entry_price", 0.0) or 0.0)
    quantity = float(getattr(position, "quantity", 0.0) or 0.0)
    if entry_price <= 0 or quantity <= 0:
        return {
            "gross_pnl_usdt": 0.0,
            "entry_commission_usdt": 0.0,
            "exit_commission_estimated_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "net_pnl_pct": 0.0,
            "accounting_mode": "unavailable",
        }
    gross = (float(current_price) - entry_price) * quantity
    entry_commission = float(getattr(position, "entry_commission", 0.0) or 0.0)
    if entry_commission <= 0:
        from binance_commission import entry_commission as estimate_entry_commission
        entry_commission = estimate_entry_commission(quantity, entry_price, "spot", settings)
    estimated_exit_commission = exit_commission(quantity, float(current_price), "spot", settings)
    net = gross - entry_commission - estimated_exit_commission
    return {
        "gross_pnl_usdt": gross,
        "entry_commission_usdt": entry_commission,
        "exit_commission_estimated_usdt": estimated_exit_commission,
        "net_pnl_usdt": net,
        "net_pnl_pct": net / (entry_price * quantity),
        "accounting_mode": "mark_to_market_estimated_exit_fee",
    }
