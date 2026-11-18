import { useState } from "react";
import "./details.css";
import { TuningCurve } from "./TuningCurve";
import { TuningTable } from "./TuningTable";
import { tuningRows } from "./tuningModel";
import type { RaceViewModel } from "./types";

type Source = Pick<
  RaceViewModel,
  "tuning" | "policies" | "recommendation" | "about"
>;

/** The Tuning section: "Did each rule get a fair try?" (DESIGN.md 3.10). */
export function TuningView({ model }: { model: Source }) {
  const rows = tuningRows(model);
  const [picked, setPicked] = useState<string | null>(null);
  const current = rows.find((r) => r.policy === picked) ?? rows[0];
  const tuning = model.tuning.find((t) => t.policy === current?.policy);
  return (
    <section className="tf-sec" id="tuning" aria-labelledby="tuning-h">
      <div className="tf-sec-head">
        <div className="tf-eyebrow">Tuning</div>
        <h2 id="tuning-h">Did each rule get a fair try?</h2>
        <p>
          Each rule was tested with several settings on{" "}
          {model.about.tune_episodes} separate runs. Its best setting went into
          the race.
        </p>
      </div>
      {!current || !tuning ? (
        <div className="tf-empty" data-testid="tuning-none">
          <b>No rule had settings to try, so nothing was tuned.</b>
          <span>
            Add a list of settings to a rule, then run the race again.
          </span>
        </div>
      ) : (
        <div className="tf-tune-layout">
          <div className="tf-card">
            <h3>Which setting works best for {current.label}?</h3>
            <p className="tf-sub">Profit per run in the tuning runs.</p>
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
            {tuning.words && (
              <p
                className={`tf-takeaway${current.touchy ? " is-warn" : ""}`}
                data-testid="tuning-takeaway"
              >
                {tuning.words}
              </p>
            )}
          </div>
          <div className="tf-card">
            <h3>How touchy is each rule?</h3>
            <p className="tf-sub">
              The gap between its best and worst setting.
            </p>
            <TuningTable
              rows={rows}
              selected={current.policy}
              onSelect={setPicked}
            />
            <p className="tf-note">
              A rule is touchy when that gap is more than a quarter of its best
              profit. Check a touchy rule before you rely on it.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
