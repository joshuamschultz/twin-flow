# B-36: workspace result pages on the shared parts

Before and after screenshots: `workspace-before/` and `workspace-after/` in this folder.

| Page | Now uses | What moved |
|---|---|---|
| Experiments (`ExperimentView`) | `ResultPage` (`Page`), `AnswerHero`, `Section`, `Card`, `StatusPill`, `tf-table` | Answer hero shows the result sentence and metrics. "When does each order finish?" holds the outcomes table. "Is this better than another run?" holds Compare. Details holds every number and the run outcomes. Evidence button is in the top bar. |
| Case and delivery results (`DomainResults`) | `Card`, `StatusPill`, `tf-table` | Same table in a card. Status badge is a `StatusPill`. |
| Scenario overview (`ScenarioView`) | `AnswerHero` (h2), `Section`, `Card`, `Tag`, `StatusPill`, `tf-table` | The on-time metric moved from the fourth tile into the answer hero. Orders, settings, assumptions, process map and source are cards. |
| Operational data and Scheduling (`DecisionLab`) | `ResultPage`, `AnswerHero`, `Section`, `Card`, `StatusPill` | Hero says the answer ("A verified schedule is ready."). "What must the schedule respect?" holds the input. "Does the plan hold up?" holds the result and evidence. |
| Action review (`ActionReview`) | `ResultPage`, `AnswerHero`, `Section`, `Card`, `StatusPill` | "What do you want to change?" holds the draft. "What is waiting for review?" holds the proposals. |

Deleted from `workspace.css`: `.ws-table-wrap`, `.ws-shell table/th/td`, `.ws-note`, `.ws-empty*`.
Kept: `.ws-card` and `.ws-badge`, still used by the library, Build a twin and Agent access pages.
