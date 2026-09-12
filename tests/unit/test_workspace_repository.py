from pathlib import Path

import pytest

from twinflow.application.repository import WorkspaceRepository


def test_durable_immutable_records_and_scope(tmp_path: Path) -> None:
    repo = WorkspaceRepository(tmp_path)
    repo.create("scenarios", "abc", {"name": "Original"})
    with pytest.raises(ValueError, match="exists"):
        repo.create("scenarios", "abc", {"name": "Changed"})
    assert WorkspaceRepository(tmp_path).get("scenarios", "abc") == {"name": "Original"}
    assert repo.list("jobs") == []
    with pytest.raises(KeyError):
        repo.get("jobs", "abc")


def test_idempotency_conflict_and_lookup(tmp_path: Path) -> None:
    repo = WorkspaceRepository(tmp_path)
    assert repo.claim_job("key", "digest", {"id": "job-a", "status": "queued"}) == ("job-a", True)
    assert repo.claim_job("key", "digest", {"id": "job-b", "status": "queued"}) == ("job-a", False)
    with pytest.raises(ValueError, match="different"):
        repo.claim_job("key", "different", {"id": "job-c"})
    assert len(repo.list("jobs")) == 1


def test_update_requires_existing_record(tmp_path: Path) -> None:
    repo = WorkspaceRepository(tmp_path)
    with pytest.raises(KeyError):
        repo.update("jobs", "missing", {"status": "done"})
    repo.create("jobs", "job-a", {"status": "queued"})
    repo.update("jobs", "job-a", {"status": "failed", "error": "Interrupted"})
    assert repo.get("jobs", "job-a")["status"] == "failed"
