import pytest

from Dev.scripts.score_universal_entry_philosophies_fixed_contract import _summarize, fixed_atr_contract


def test_fixed_atr_contract_uses_identical_risk_and_reward_for_both_sides():
    long_contract = fixed_atr_contract("LONG", 100.0, 2.0)
    short_contract = fixed_atr_contract("SHORT", 100.0, 2.0)

    assert long_contract["stop_price"] == 97.0
    assert long_contract["target_price"] == 104.0
    assert short_contract["stop_price"] == 103.0
    assert short_contract["target_price"] == 96.0
    assert long_contract["stop_atr_multiple"] == short_contract["stop_atr_multiple"] == 1.5
    assert long_contract["target_atr_multiple"] == short_contract["target_atr_multiple"] == 2.0


@pytest.mark.parametrize(
    ("side", "entry", "atr"),
    [("FLAT", 100.0, 2.0), ("LONG", 0.0, 2.0), ("SHORT", 100.0, 0.0)],
)
def test_fixed_atr_contract_rejects_invalid_geometry(side, entry, atr):
    with pytest.raises(ValueError):
        fixed_atr_contract(side, entry, atr)


def test_fixed_contract_summary_counts_only_valid_labels_and_keeps_abstentions_zero():
    result = _summarize(
        [
            {"dedupe_cluster_id": "cluster-1", "label_valid": True, "outcome": "TP", "net_per_100": 0.8},
            {"dedupe_cluster_id": "cluster-2", "label_valid": True, "outcome": "SL", "net_per_100": -0.6},
        ],
        total_proposals=3,
        seed=4071,
        replicates=200,
    )

    assert result["valid_attempts"] == 2
    assert result["TP"] == 1
    assert result["SL"] == 1
    assert result["mean_net_per_100"] == pytest.approx(0.1)
    assert result["mean_net_per_proposal_including_abstentions"] == pytest.approx(2.0 / 30.0)