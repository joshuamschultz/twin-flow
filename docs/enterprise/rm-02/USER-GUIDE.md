# Portable scenarios

Import an existing pair:

```python
from twinflow.scenario import import_legacy

capsule = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv")
capsule.dump("spring.twin.yaml")
```

Review and execute it:

```python
from twinflow.scenario import load

capsule = load("spring.twin.yaml")
issues = capsule.validate()
if issues:
    raise ValueError(issues)
result = capsule.run(seed=0)
print(capsule.digest, result.horizon)
```

Agents can inspect `schema()`, call `from_dict` to receive a precise missing
field or type issue, then call `edit("/snapshot/production_plan/0/qty", 2)`.
Edits are immutable. The current capsule intentionally keeps model YAML's
existing domain vocabulary; calendars, richer WIP, and archive packaging are
future capability additions. Credentials and external connector references are
not accepted as execution inputs.
