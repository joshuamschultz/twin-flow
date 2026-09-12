# RM-02 Portable scenario capsule

## Requirements

The capsule is a versioned, single YAML document containing `schema_version`,
`model`, `snapshot`, `experiment`, `assumptions`, `provenance`, and
`required_capabilities`. `snapshot.production_plan` embeds the existing plan
rows. The existing model and plan loaders remain the semantic execution
boundary; the capsule does not create another simulation engine.

Capsules are parsed with bounded, safe YAML input. Unknown envelope keys,
unsupported versions, malformed rows, and missing required sections produce
path-addressed issues. A canonical JSON representation gives a stable SHA-256
digest independent of YAML formatting or key order. `edit` and `branch` return
new values and preserve parent provenance.

## Public contract

`load`, `ScenarioCapsule.from_dict`, `ScenarioCapsule.to_dict`, `digest`,
`validate`, `compile`, `run`, `edit`, `branch`, `dump`, `import_legacy`, and
`schema` are stable Python entry points. `CompiledScenario` contains the
existing `CompiledModel` and `WorkOrder` list. A future application layer can
adapt `run` to bounded driver limits without changing capsule serialization.

## Acceptance

* Existing model-plus-CSV examples import and round-trip through one capsule.
* Formatting-only changes preserve digest; semantic edits change it.
* Unsafe/oversized/non-mapping YAML fails before model compilation.
* Model validator errors include capsule paths and unknown capabilities fail
  clearly.
* A valid capsule compiles through `load_model` and `load_plan` and can run a
  baseline locally without credentials.
