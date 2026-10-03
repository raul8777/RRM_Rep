from datetime import datetime, timezone

import pytest

from Dev.scripts import mimic_download_external


def test_coingecko_global_cannot_backdate_btc_dominance(tmp_path, monkeypatch):
    def reject_network(_url):
        raise AssertionError("historical request must fail before network access")

    monkeypatch.setattr(mimic_download_external, "_fetch_json", reject_network)

    with pytest.raises(ValueError, match="current snapshot"):
        mimic_download_external.download_btc_dominance(
            datetime(2025, 2, 5, tzinfo=timezone.utc),
            datetime(2025, 2, 19, tzinfo=timezone.utc),
            str(tmp_path),
        )
    assert not list(tmp_path.iterdir())