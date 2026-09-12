# Office profile

```python
from twinflow.domain import registry

model = {
    "domain": "office",
    "resources": [{"id": "analyst", "roles": ["analyst"], "capacity": 1}],
    "tasks": [
        {"id": "review", "duration": 2, "role": "analyst"},
        {"id": "approve", "duration": 1, "role": "analyst", "prerequisites": ["review"]},
    ],
}
snapshot = {"cases": [{"id": "case-1", "data": {}}]}
result = registry.evaluate("office", model, snapshot, limits={"max_events": 100})
```

Use `registry.validate` before evaluation and `registry.describe` to obtain a
small graph for a UI or agent. Add `artifact_dir` to retain `office-result.json`.
Calendar windows are absolute simulated-time windows. Rework is represented by
case `rework` counts and bounded by each task's `max_rework`. The profile does
not consume materials, call external services, infer approvals, or turn an
unknown document revision into a valid one.
