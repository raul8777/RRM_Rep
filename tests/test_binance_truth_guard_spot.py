from binance_truth_guard import auto_reconcile_spot, evaluate_spot_parity


class _EmptyPositionManager:
    def get_all_positions(self):
        return {}


class _StablecoinClient:
    def get_wallet_balance(self):
        return {"USDC": 5000.0, "USDT": 1000.0}


class _GhostPositionManager:
    def __init__(self):
        self.close_calls = []
        self.audit_events = []

    def get_all_positions(self):
        return {"NEARUSDT": [type("Position", (), {"quantity": 13.9})()]}

    def close_position(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))

    def _audit(self, symbol, event, detail, binance_confirmed=False):
        self.audit_events.append((symbol, event, detail, binance_confirmed))


class _EmptyWalletClient:
    def get_wallet_balance(self):
        return {}


def test_spot_parity_ignores_quote_stablecoin_balances():
    result = evaluate_spot_parity(_EmptyPositionManager(), _StablecoinClient())

    assert result["ok"] is True
    assert result["mismatches"] == []


def test_single_parity_snapshot_never_closes_db_position():
    manager = _GhostPositionManager()

    result = auto_reconcile_spot(
        manager,
        _EmptyWalletClient(),
        settings=None,
        dry_run=False,
    )

    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "deferred_to_confirmed_position_reconciliation"
    assert manager.close_calls == []
    assert manager.audit_events[0][1] == "spot_truth_ghost_close_deferred"