from __future__ import annotations

import json

from twinflow.cli.data import main


def test_cli_import_snapshot_and_validate(tmp_path, capsys) -> None:
    db = tmp_path / "events.db"
    events = tmp_path / "events.json"
    events.write_text(
        json.dumps(
            [
                {
                    "source": "erp",
                    "event_id": "e1",
                    "source_revision": "1",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "ingested_at": "2026-01-01T00:01:00Z",
                    "entity_type": "order",
                    "entity_id": "o1",
                    "event_type": "created",
                    "payload": {"status": "open"},
                }
            ]
        )
    )
    assert main(["import", "--db", str(db), str(events)]) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 1
    assert main(["snapshot", "--db", str(db), "--known-at", "2026-01-02T00:00:00Z", "--save"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["entities"][0]["values"] == {"status": "open"}
    assert output["snapshot_id"].startswith("snap-")

    forecasts = tmp_path / "forecasts.json"
    actuals = tmp_path / "actuals.json"
    forecasts.write_text(
        json.dumps(
            [
                {
                    "item_id": "o1",
                    "decision_at": "2026-01-01T00:00:00Z",
                    "point_date": "2026-01-03T00:00:00Z",
                }
            ]
        )
    )
    actuals.write_text(json.dumps([{"item_id": "o1", "observed_date": "2026-01-04T00:00:00Z"}]))
    assert main(["validate-forecasts", str(forecasts), str(actuals)]) == 0
    assert json.loads(capsys.readouterr().out)["mean_absolute_error_days"] == 1
