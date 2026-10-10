from twinflow.application.linear_schedule import translate_schedule


def test_translation_blocks_binding_shared_operators() -> None:
    scenario = {
        "id": "s",
        "digest": "digest",
        "draft_link": {"compiler_version": "linear-piece-1"},
        "capsule": {
            "provenance": {
                "facts": {
                    "operators": {"value": 1},
                    "stages": [
                        {
                            "id": "cut",
                            "seconds_per_piece": {"value": 2.0},
                            "machine_capacity": {"value": 2},
                        }
                    ],
                    "unsupported_features": [],
                }
            }
        },
    }
    snapshot = {
        "scenario_id": "s",
        "scenario_digest": "digest",
        "orders": [{"order_id": "o", "quantity": 1, "release_second": 0.0}],
    }
    result = translate_schedule(scenario, snapshot)
    assert result["status"] == "blocked"
    assert result["issues"][0]["path"] == "scenario.operators"
    assert "problem" not in result
