from __future__ import annotations

import uuid
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional

from binance_client import BinanceClient


@dataclass
class SpotMarginShortPosition:
    symbol: str
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    loan_transaction_id: str
    entry_order_id: str
    protection_order_list_id: str
    entry_quote_received: float
    entry_commission_usdt: float
    position_id: str
    thesis_id: str
    close_group_id: str
    metadata: Dict[str, Any]


class SpotMarginClient:
    """Isolated-margin execution adapter for real SHORT positions on Binance Demo."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = True,
        dry_run: bool = False,
        spot_client: Optional[BinanceClient] = None,
        db_client: Any = None,
    ) -> None:
        if dry_run:
            raise ValueError("Spot Margin execution requires Binance Demo; dry_run is not supported")
        if testnet and spot_client is None:
            raise RuntimeError(
                "Binance Spot Demo/Testnet does not expose /sapi Margin endpoints"
            )
        self.spot_client = spot_client or BinanceClient(
            api_key,
            api_secret,
            testnet=testnet,
            dry_run=False,
        )
        self.client = self.spot_client.client
        self.db_client = db_client

    @staticmethod
    def _base_asset(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if not normalized.endswith("USDT") or len(normalized) <= 4:
            raise ValueError(f"Unsupported isolated-margin symbol: {symbol}")
        return normalized[:-4]

    def preflight_short(self, symbol: str) -> Dict[str, Any]:
        symbol = str(symbol).strip().upper()
        base_asset = self._base_asset(symbol)
        account = self.client.get_isolated_margin_account(symbols=symbol)
        assets = account.get("assets") or []
        pair = next((row for row in assets if str(row.get("symbol") or "").upper() == symbol), None)
        if not pair:
            raise RuntimeError(f"Isolated Margin is not enabled for {symbol}")
        base = pair.get("baseAsset") or {}
        if base.get("borrowEnabled") is False:
            raise RuntimeError(f"Borrow is disabled for {base_asset} on {symbol}")
        loan = self.client.get_max_margin_loan(asset=base_asset, isolatedSymbol=symbol)
        max_borrowable = float(loan.get("amount", 0.0) or 0.0)
        if max_borrowable <= 0:
            raise RuntimeError(f"No borrow capacity for {base_asset} on {symbol}")
        return {
            "available": True,
            "symbol": symbol,
            "base_asset": base_asset,
            "max_borrowable": max_borrowable,
        }

    def _quantity_for_notional(self, symbol: str, notional_usdt: float, price: float) -> float:
        filters = self.spot_client.get_symbol_filters(symbol)
        step = Decimal(str(filters.get("step", 0.0) or 0.0))
        if step <= 0:
            raise RuntimeError(f"Missing quantity step for {symbol}")
        raw = Decimal(str(notional_usdt)) / Decimal(str(price))
        quantity = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        result = float(quantity)
        if result <= 0:
            raise RuntimeError(f"Calculated quantity is zero for {symbol}")
        return result

    def _quantity_for_debt_deficit(self, symbol: str, deficit: float, price: float) -> float:
        filters = self.spot_client.get_symbol_filters(symbol)
        step = Decimal(str(filters.get("step", 0.0) or 0.0))
        if step <= 0:
            raise RuntimeError(f"Missing quantity step for {symbol}")
        min_notional = Decimal(str(filters.get("min_notional", 0.0) or 0.0))
        required = max(
            Decimal(str(deficit)) * Decimal("1.001"),
            (min_notional * Decimal("1.001")) / Decimal(str(price)),
        )
        quantity = (required / step).to_integral_value(rounding=ROUND_UP) * step
        return float(quantity)

    @staticmethod
    def _average_fill_price(order: Dict[str, Any], fallback: float) -> float:
        executed = float(order.get("executedQty", 0.0) or 0.0)
        quote = float(order.get("cummulativeQuoteQty", 0.0) or 0.0)
        if executed > 0 and quote > 0:
            return quote / executed
        return float(order.get("price", 0.0) or fallback)

    @staticmethod
    def _require_filled(order: Dict[str, Any], action: str) -> None:
        if str(order.get("status") or "").upper() != "FILLED":
            raise RuntimeError(f"Margin {action} order was not filled")

    def _repay(self, symbol: str, asset: str, amount: float) -> Dict[str, Any]:
        return self.client.repay_margin_loan(
            asset=asset,
            amount=self.spot_client._format_amount(amount),
            isIsolated="TRUE",
            symbol=symbol,
        )

    @staticmethod
    def _commission_usdt(order: Dict[str, Any], symbol: str, fill_price: float) -> float:
        base_asset = symbol[:-4]
        total = 0.0
        for fill in order.get("fills") or []:
            commission = float(fill.get("commission", 0.0) or 0.0)
            asset = str(fill.get("commissionAsset") or "").upper()
            if asset == "USDT":
                total += commission
            elif asset == base_asset:
                total += commission * float(fill.get("price", 0.0) or fill_price)
        return total

    def reconcile_debt(self, symbol: str) -> Dict[str, float]:
        symbol = str(symbol).strip().upper()
        base_asset = self._base_asset(symbol)
        account = self.client.get_isolated_margin_account(symbols=symbol)
        assets = account.get("assets") or []
        pair = next((row for row in assets if str(row.get("symbol") or "").upper() == symbol), None)
        if not pair:
            raise RuntimeError(f"Isolated Margin is not enabled for {symbol}")
        base = pair.get("baseAsset") or {}
        borrowed = float(base.get("borrowed", 0.0) or 0.0)
        interest = float(base.get("interest", 0.0) or 0.0)
        return {
            "borrowed": borrowed,
            "interest": interest,
            "total_debt": borrowed + interest,
            "free": float(base.get("free", 0.0) or 0.0),
            "asset": base_asset,
        }

    def _buy_to_cover(self, symbol: str, quantity: float, client_order_id: str) -> Dict[str, Any]:
        order = self.client.create_margin_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quantity=self.spot_client._format_amount(quantity),
            isIsolated="TRUE",
            sideEffectType="NO_SIDE_EFFECT",
            newClientOrderId=client_order_id,
        )
        self._require_filled(order, "cover")
        return order

    def open_short(
        self,
        symbol: str,
        notional_usdt: float,
        *,
        stop_loss_pct: float,
        take_profit_pct: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SpotMarginShortPosition:
        symbol = str(symbol).strip().upper()
        capability = self.preflight_short(symbol)
        price = float(self.spot_client.get_symbol_price(symbol))
        if price <= 0 or notional_usdt <= 0:
            raise ValueError("Price and notional must be positive")
        quantity = self._quantity_for_notional(symbol, notional_usdt, price)
        if quantity > capability["max_borrowable"]:
            raise RuntimeError(f"Requested quantity exceeds borrow capacity for {symbol}")

        base_asset = capability["base_asset"]
        operation_id = uuid.uuid4().hex[:12]
        position_id = str(uuid.uuid4())
        thesis_id = str(uuid.uuid4())
        close_group_id = str(uuid.uuid4())
        loan = self.client.create_margin_loan(
            asset=base_asset,
            amount=self.spot_client._format_amount(quantity),
            isIsolated="TRUE",
            symbol=symbol,
        )
        loan_id = str(loan.get("tranId") or "")
        try:
            entry = self.client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=self.spot_client._format_amount(quantity),
                isIsolated="TRUE",
                sideEffectType="NO_SIDE_EFFECT",
                newClientOrderId=f"ms-e-{operation_id}",
            )
            self._require_filled(entry, "entry")
        except Exception:
            self._repay(symbol, base_asset, quantity)
            raise

        entry_price = self._average_fill_price(entry, price)
        entry_quote = float(entry.get("cummulativeQuoteQty", 0.0) or (entry_price * quantity))
        entry_commission = self._commission_usdt(entry, symbol, entry_price)
        stop_price = entry_price * (1.0 + float(stop_loss_pct) / 100.0)
        target_price = entry_price * (1.0 - float(take_profit_pct) / 100.0)
        try:
            protection = self.client.create_margin_oco_order(
                symbol=symbol,
                side="BUY",
                quantity=self.spot_client._format_amount(quantity),
                price=self.spot_client._format_amount(target_price),
                stopPrice=self.spot_client._format_amount(stop_price),
                stopLimitPrice=self.spot_client._format_amount(stop_price * 1.001),
                stopLimitTimeInForce="GTC",
                isIsolated="TRUE",
                sideEffectType="NO_SIDE_EFFECT",
                listClientOrderId=f"ms-p-{operation_id}",
            )
        except Exception:
            self._buy_to_cover(symbol, quantity, f"ms-r-{operation_id}")
            self._repay(symbol, base_asset, quantity)
            raise

        position = SpotMarginShortPosition(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            loan_transaction_id=loan_id,
            entry_order_id=str(entry.get("orderId") or ""),
            protection_order_list_id=str(protection.get("orderListId") or ""),
            entry_quote_received=entry_quote,
            entry_commission_usdt=entry_commission,
            position_id=position_id,
            thesis_id=thesis_id,
            close_group_id=close_group_id,
            metadata={
                **dict(metadata or {}),
                "trade_type": "spot_margin",
                "selected_mode": "SPOT_MARGIN_SHORT",
                "position_id": position_id,
                "thesis_id": thesis_id,
                "close_group_id": close_group_id,
                "entry_quote_received": entry_quote,
            },
        )
        try:
            self._persist_open(position)
        except Exception:
            self.client.cancel_margin_oco_order(
                symbol=symbol,
                orderListId=position.protection_order_list_id,
                isIsolated="TRUE",
            )
            self._buy_to_cover(symbol, quantity, f"ms-r-{operation_id}")
            debt = self.reconcile_debt(symbol)
            if debt["total_debt"] > 0:
                self._repay(symbol, base_asset, debt["total_debt"])
            raise
        return position

    def load_open_positions(self) -> List[SpotMarginShortPosition]:
        if not self.db_client:
            return []
        rows = self.db_client.execute_query(
            """
            SELECT symbol, quantity, price, stop_loss, take_profit, metadata,
                   entry_commission, position_id, thesis_id, close_group_id,
                   entry_order_id, protection_order_list_id,
                   margin_loan_transaction_id
            FROM trades_open
            WHERE metadata->>'trade_type' = 'spot_margin'
            ORDER BY entry_time ASC
            """
        )
        positions = []
        for row in rows or []:
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            quantity = float(row.get("quantity", 0.0) or 0.0)
            entry_price = float(row.get("price", 0.0) or 0.0)
            positions.append(SpotMarginShortPosition(
                symbol=str(row.get("symbol") or ""),
                quantity=quantity,
                entry_price=entry_price,
                stop_price=float(row.get("stop_loss", 0.0) or 0.0),
                target_price=float(row.get("take_profit", 0.0) or 0.0),
                loan_transaction_id=str(row.get("margin_loan_transaction_id") or ""),
                entry_order_id=str(row.get("entry_order_id") or ""),
                protection_order_list_id=str(row.get("protection_order_list_id") or ""),
                entry_quote_received=float(
                    metadata.get("entry_quote_received", entry_price * quantity) or 0.0
                ),
                entry_commission_usdt=float(row.get("entry_commission", 0.0) or 0.0),
                position_id=str(row.get("position_id") or ""),
                thesis_id=str(row.get("thesis_id") or ""),
                close_group_id=str(row.get("close_group_id") or ""),
                metadata=dict(metadata),
            ))
        return positions

    def _margin_order_with_trades(self, symbol: str, order_id: str) -> Dict[str, Any]:
        order = self.client.get_margin_order(
            symbol=symbol,
            orderId=order_id,
            isIsolated="TRUE",
        )
        try:
            trades = self.client.get_margin_trades(
                symbol=symbol,
                orderId=order_id,
                isIsolated="TRUE",
            )
            order = {**order, "fills": trades or []}
        except Exception:
            pass
        return order

    def _finalize_protection_fill(
        self,
        position: SpotMarginShortPosition,
        filled_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        symbol = position.symbol
        order_id = str(filled_report.get("orderId") or "")
        protection_fill = self._margin_order_with_trades(symbol, order_id)
        self._require_filled(protection_fill, "protection")
        orders = [protection_fill]

        debt = self.reconcile_debt(symbol)
        total_debt = float(debt["total_debt"])
        if total_debt > 0 and float(debt["free"]) + 1e-12 < total_debt:
            price = float(self.spot_client.get_symbol_price(symbol))
            deficit = total_debt - float(debt["free"])
            quantity = self._quantity_for_debt_deficit(symbol, deficit, price)
            operation_id = uuid.uuid4().hex[:12]
            orders.append(self._buy_to_cover(symbol, quantity, f"ms-i-{operation_id}"))
            debt = self.reconcile_debt(symbol)
            total_debt = float(debt["total_debt"])

        if total_debt > 0 and float(debt["free"]) + 1e-12 < total_debt:
            raise RuntimeError(f"Insufficient base asset to repay recovered debt for {symbol}")

        repayment = self._repay(symbol, debt["asset"], total_debt) if total_debt > 0 else {}
        total_executed = sum(float(order.get("executedQty", 0.0) or 0.0) for order in orders)
        total_quote = sum(float(order.get("cummulativeQuoteQty", 0.0) or 0.0) for order in orders)
        average_price = total_quote / total_executed if total_executed > 0 else position.entry_price
        used_quantity = min(total_debt, total_executed) if total_debt > 0 else position.quantity
        cover_quote = average_price * used_quantity
        total_commission = sum(self._commission_usdt(order, symbol, average_price) for order in orders)
        commission_used = total_commission * (used_quantity / total_executed) if total_executed > 0 else 0.0
        net_pnl = position.entry_quote_received - cover_quote - position.entry_commission_usdt - commission_used
        result = {
            "symbol": symbol,
            "entry_price": position.entry_price,
            "exit_price": average_price,
            "quantity": position.quantity,
            "repaid_quantity": total_debt,
            "interest_base": float(debt["interest"]),
            "interest_usdt": float(debt["interest"]) * average_price,
            "entry_commission_usdt": position.entry_commission_usdt,
            "exit_commission_usdt": commission_used,
            "net_pnl_usdt": net_pnl,
            "cover_order_id": order_id,
            "repay_transaction_id": str(repayment.get("tranId") or ""),
        }
        self._persist_closed(position, result, "margin_protection_fill")
        return result

    def recover_open_positions(self) -> List[Dict[str, Any]]:
        recovered = []
        for position in self.load_open_positions():
            try:
                order_list = self.client.get_margin_oco_order(
                    symbol=position.symbol,
                    orderListId=position.protection_order_list_id,
                    isIsolated="TRUE",
                )
                filled = next(
                    (
                        report for report in order_list.get("orderReports") or []
                        if str(report.get("side") or "").upper() == "BUY"
                        and str(report.get("status") or "").upper() == "FILLED"
                    ),
                    None,
                )
                if filled:
                    result = self._finalize_protection_fill(position, filled)
                    recovered.append({"position_id": position.position_id, "status": "closed", **result})
                else:
                    debt = self.reconcile_debt(position.symbol)
                    if debt["total_debt"] <= 0:
                        raise RuntimeError("Open DB position has neither active debt nor a filled protection order")
                    recovered.append({
                        "position_id": position.position_id,
                        "symbol": position.symbol,
                        "status": "active",
                        "total_debt": debt["total_debt"],
                    })
            except Exception as exc:
                recovered.append({
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "status": "error",
                    "error": str(exc),
                })
        return recovered

    def _persist_open(self, position: SpotMarginShortPosition) -> None:
        if not self.db_client:
            return
        self.db_client.execute_update(
            """
            INSERT INTO trades_open
                (symbol, side, quantity, price, entry_time, stop_loss, take_profit,
                 metadata, entry_commission, position_id, thesis_id, close_group_id,
                 entry_order_id, protection_order_list_id, margin_loan_transaction_id,
                 margin_borrowed_qty)
            VALUES (%s, 'SELL', %s, %s, NOW(), %s, %s, %s::jsonb, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            """,
            (
                position.symbol,
                position.quantity,
                position.entry_price,
                position.stop_price,
                position.target_price,
                json.dumps(position.metadata),
                position.entry_commission_usdt,
                position.position_id,
                position.thesis_id,
                position.close_group_id,
                position.entry_order_id,
                position.protection_order_list_id,
                position.loan_transaction_id,
                position.quantity,
            ),
        )

    def close_short(self, position: SpotMarginShortPosition, reason: str = "manual_close") -> Dict[str, Any]:
        symbol = position.symbol
        if position.protection_order_list_id:
            self.client.cancel_margin_oco_order(
                symbol=symbol,
                orderListId=position.protection_order_list_id,
                isIsolated="TRUE",
            )

        debt = self.reconcile_debt(symbol)
        total_debt = float(debt["total_debt"])
        if total_debt <= 0:
            raise RuntimeError(f"No isolated-margin debt found for {symbol}")
        operation_id = uuid.uuid4().hex[:12]
        execution = {
            "mode": "active",
            "urgency": "HARD_RISK" if any(token in reason.upper() for token in ("STOP", "EMERGENCY", "LIQUIDAT")) else "SOFT_PROFIT",
            "full_quantity": total_debt,
            "order_type": "MARKET",
        }
        try:
            book = self.spot_client.get_order_book(symbol, limit=20)
            plan = build_close_plan(
                symbol, "SHORT", total_debt, execution["urgency"],
                bids=book.get("bids") or [], asks=book.get("asks") or [],
                max_slippage_bps=10.0, notional_usdt=total_debt * position.entry_price,
            )
            execution = plan.to_dict()
        except Exception as exc:
            execution["reason"] = "depth_unavailable_fallback_market"
            execution["depth_error"] = str(exc)

        if execution.get("order_type") == "LIMIT_FOK":
            try:
                cover = self.client.create_margin_order(
                    symbol=symbol,
                    side="BUY",
                    type="LIMIT",
                    timeInForce="FOK",
                    quantity=self.spot_client._format_amount(total_debt),
                    price=self.spot_client._format_amount(float(execution["expected_vwap"])),
                    isIsolated="TRUE",
                    sideEffectType="NO_SIDE_EFFECT",
                    newClientOrderId=f"ms-x-{operation_id}",
                )
                self._require_filled(cover, "cover_fok")
            except Exception as exc:
                execution["order_type"] = "MARKET"
                execution["escalation_reason"] = str(exc) or "fok_not_fully_filled"
                cover = self._buy_to_cover(symbol, total_debt, f"ms-x-{operation_id}")
        else:
            cover = self._buy_to_cover(symbol, total_debt, f"ms-x-{operation_id}")
        exit_price = self._average_fill_price(cover, position.entry_price)
        cover_quote = float(cover.get("cummulativeQuoteQty", 0.0) or (exit_price * total_debt))
        cover_commission = self._commission_usdt(cover, symbol, exit_price)
        repayment = self._repay(symbol, debt["asset"], total_debt)
        interest_usdt = float(debt["interest"]) * exit_price
        net_pnl = (
            position.entry_quote_received
            - cover_quote
            - position.entry_commission_usdt
            - cover_commission
        )
        result = {
            "symbol": symbol,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "repaid_quantity": total_debt,
            "interest_base": float(debt["interest"]),
            "interest_usdt": interest_usdt,
            "entry_commission_usdt": position.entry_commission_usdt,
            "exit_commission_usdt": cover_commission,
            "net_pnl_usdt": net_pnl,
            "cover_order_id": str(cover.get("orderId") or ""),
            "repay_transaction_id": str(repayment.get("tranId") or ""),
            "close_execution": execution,
            "close_expected_price": execution.get("expected_vwap") or position.entry_price,
            "close_real_price": exit_price,
        }
        self._persist_closed(position, result, reason)
        return result

    def _persist_closed(
        self,
        position: SpotMarginShortPosition,
        result: Dict[str, Any],
        reason: str,
    ) -> None:
        if not self.db_client:
            return
        idempotency_key = f"{position.position_id}:close:0"
        metadata = {
            **position.metadata,
            "margin_interest_qty": result["interest_base"],
            "margin_interest_usdt": result["interest_usdt"],
            "repay_transaction_id": result["repay_transaction_id"],
            "pnl_accounting_mode": "margin_fills_fees_interest_reconciled",
            "close_execution": result.get("close_execution"),
            "close_expected_price": result.get("close_expected_price"),
            "close_real_price": result.get("close_real_price"),
        }
        self.db_client.execute_update(
            """
            WITH closed_source AS (
                SELECT * FROM trades_open WHERE position_id = %s
            ), inserted AS (
            INSERT INTO trades_closed
                (symbol, side, quantity, entry_price, exit_price, entry_time, exit_time,
                 pnl, pnl_percentage, close_reason, metadata, entry_commission,
                 exit_commission, position_id, thesis_id, close_group_id,
                 entry_order_id, exit_order_id, margin_loan_transaction_id,
                 margin_repay_transaction_id, margin_interest_qty, idempotency_key)
            SELECT symbol, side, quantity, price, %s, entry_time, NOW(), %s,
                   CASE WHEN price > 0 AND quantity > 0 THEN %s / (price * quantity) * 100 ELSE 0 END,
                   %s, %s::jsonb, entry_commission, %s, position_id, thesis_id,
                   close_group_id, entry_order_id, %s, margin_loan_transaction_id,
                   %s, %s, %s
            FROM closed_source
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
            RETURNING id
            )
            DELETE FROM trades_open WHERE position_id = %s
            """,
            (
                position.position_id,
                result["exit_price"],
                result["net_pnl_usdt"],
                result["net_pnl_usdt"],
                reason,
                json.dumps(metadata),
                result["exit_commission_usdt"],
                result["cover_order_id"],
                result["repay_transaction_id"],
                result["interest_base"],
                idempotency_key,
                position.position_id,
            ),
        )