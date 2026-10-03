import json

import pytest

from Dev.scripts.universal_entry_run_state import GIB, ProgressLedger, safe_worker_limit


def test_worker_limit_never_spends_the_memory_reserve():
    assert safe_worker_limit(
        8, available_bytes=5 * GIB, reserve_bytes=3 * GIB, estimated_worker_bytes=1 * GIB,
    ) == 2
    assert safe_worker_limit(
        8, available_bytes=3500 * 1024**2, reserve_bytes=3 * GIB, estimated_worker_bytes=1 * GIB,
    ) == 0
    assert safe_worker_limit(
        8, available_bytes=None, reserve_bytes=3 * GIB, estimated_worker_bytes=1 * GIB,
    ) == 0


def test_progress_ledger_persists_checkpoints_and_resumes_same_manifest(tmp_path):
    run_dir = tmp_path / "run-1"
    manifest = {"window_id": "dev", "seed": 7}
    ledger = ProgressLedger(run_dir, run_id="run-1", manifest=manifest)
    ledger.start("proposal_generation", total_jobs=2, requested_workers=8)
    ledger.complete_job("BTCUSDT", checkpoint="shards/BTCUSDT.jsonl", result_sha256="a" * 64)

    resumed = ProgressLedger(run_dir, run_id="run-1", manifest=manifest, resume=True)
    status = json.loads(resumed.status_path.read_text(encoding="utf-8"))
    events = resumed.events_path.read_text(encoding="utf-8").splitlines()

    assert status["completed_jobs"]["BTCUSDT"]["status"] == "completed"
    assert status["counts"]["completed"] == 1
    assert status["counts"]["pending"] == 1
    assert json.loads(events[-1])["event"] == "job_completed"


def test_progress_ledger_refuses_resume_with_different_experiment_manifest(tmp_path):
    run_dir = tmp_path / "run-1"
    ProgressLedger(run_dir, run_id="run-1", manifest={"window_id": "dev"})

    with pytest.raises(ValueError, match="manifest differs"):
        ProgressLedger(run_dir, run_id="run-1", manifest={"window_id": "validation"}, resume=True)