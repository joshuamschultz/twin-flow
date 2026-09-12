# RM-03 coverage

| Capability | State | Evidence |
|---|---|---|
| Weekly local shifts | Implemented | Hand-calculated weekday/weekend test |
| Closed and replacement-date exceptions | Implemented | Closed-Monday completion test and parser validation |
| Timezone origin and DST elapsed time | Implemented | America/Chicago spring-forward test |
| Pause, finish unattended, overtime | Implemented foundation | Pause and unattended timing tests; default regression suite |
| Independent capacity-N machine setup | Implemented | Two cold setups plus warm follow-on test |
| Stable machine/resource attribution | Implemented | `resource_usage.parquet` integration test |
| Machine-specific breakdown/maintenance | Unsupported | Breakdown remains location-pool based |
| Machine speed/eligibility and fixtures | Unsupported | No config contract yet |
| Expiring labor qualifications | Unsupported | Skills remain pool-level static sets |
| Atomic multi-resource reservation | Unsupported | Existing machine-then-labor order remains |
| Multi-input assembly and substitutions | Unsupported | One queue basis plus one secondary material remains |
| Route-revision-aware WIP/genealogy | Unsupported | Existing initial-WIP fields remain unchanged |
| Rework bounds/outside processing/buffers | Unsupported | Deferred pending dedicated state contracts |

