import "./details.css";
import { Card, EmptyState, Section } from "./primitives";
import { TuningCurve } from "./TuningCurve";
import { TuningTable } from "./TuningTable";
import { tuningRows } from "./tuningModel";
import type { RaceViewModel } from "./types";
import { useViewState } from "./viewState";

type Source = Pick<
  RaceViewModel,
  "tuning" | "policies" | "recommendation" | "about"
>;

/** The Tuning section: "Did each rule get a fair try?" (DESIGN.md 3.10). */
export function TuningView({ model }: { model: Source }) {
  const rows = tuningRows(model);
  const [picked, setPicked] = useViewState("tune", rows[0]?.policy ?? "");
  const current = rows.find((r) => r.policy === picked) ?? rows[0];
  const tuning = model.tuning.find((t) => t.policy === current?.policy);
  return (
    <Section
      id="tuning"
      eyebrow="Tuning"
      question="Did each rule get a fair try?"
      hint={`Each rule was tested with several settings on ${model.about.tune_episodes} separate runs. Its best setting went into the race.`}
    >
      {!current || !tuning ? (
        <EmptyState
          title="No rule had settings to try, so nothing was tuned."
          body={
            <span data-testid="tuning-none">
              Add a list of settings to a rule, then run the race again.
            </span>
          }
        />
      ) : (
        <div className="tf-grid-a">
          <Card
            question={`Which setting works best for ${current.label}?`}
            sub="Profit per run in the tuning runs."
            takeaway={tuning.words || undefined}
            tone={current.touchy ? "warn" : undefined}
          >
            <label className="tf-tune-pick">
              <span>Rule</span>
              <select
                value={current.policy}
                onChange={(e) => setPicked(e.target.value)}
              >
                {rows.map((r) => (
                  <option key={r.policy} value={r.policy}>
                    {r.label}
                    {r.isPick ? " (pick)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <TuningCurve
              key={current.policy}
              tuning={tuning}
              label={current.label}
            />
          </Card>
          <Card
            question="How touchy is each rule?"
            sub="The gap between its best and worst setting."
          >
            <TuningTable
              rows={rows}
              selected={current.policy}
              onSelect={setPicked}
            />
            <p className="tf-note">
              A rule is touchy when that gap is more than a quarter of its best
              profit. Check a touchy rule before you rely on it.
            </p>
          </Card>
        </div>
      )}
    </Section>
  );
}
