from opportunity_labels import build_forward_labels, round_trip_cost_pct


def _bar(index, close, high=None, low=None):
    return {
        "open_time": index,
        "close": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
    }


def test_round_trip_cost_includes_entry_and_exit_friction():
    assert round_trip_cost_pct(0.0005, 0.0005) == 0.2


def test_forward_labels_keep_net_return_mfe_and_mae_for_long():
    bars = [
        _bar(0, 100),
        _bar(1, 102, high=103, low=99),
        _bar(2, 105, high=106, low=101),
        _bar(3, 103, high=105, low=102),
        _bar(4, 104, high=107, low=100),
    ]
    labels = build_forward_labels(bars, horizons_bars=(2, 4), round_trip_cost_pct_value=0.2)
    first = labels[0]
    assert first["forward_gross_2h"] == 5.0
    assert first["forward_net_2h"] == 4.8
    assert first["forward_mfe_2h"] == 6.0
    assert first["forward_mae_2h"] == -1.0
    assert first["forward_net_4h"] == 3.8


def test_forward_labels_flip_returns_for_short():
    bars = [
        _bar(0, 100),
        _bar(1, 98, high=101, low=96),
        _bar(2, 95, high=99, low=94),
    ]
    labels = build_forward_labels(bars, horizons_bars=(2,), side="SHORT", round_trip_cost_pct_value=0.1)
    first = labels[0]
    assert first["forward_gross_2h"] == round((100 / 95 - 1) * 100, 8)
    assert first["forward_mfe_2h"] == round((100 / 94 - 1) * 100, 8)
    assert first["forward_mae_2h"] == round((100 / 101 - 1) * 100, 8)
