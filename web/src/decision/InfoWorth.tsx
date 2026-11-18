import "./details.css";
import { signedUsd, usd } from "./format";
import { Card, EmptyState } from "./primitives";
import { settingName } from "./settings";
import type { RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "views" | "policies">;

/** Profit for each rule under each view. A view hides some facts from the rule, so the drop
 * from the first column is what that information is worth. */
export function InfoWorth({ model }: { model: Source }) {
  const { observations, dollars } = model.views;
  const rows = model.policies.filter((p) => dollars[p.policy]);
  const first = observations[0];
  const sub =
    observations.length > 1
      ? "Each column hides some facts from the rule. The press still runs for real. The drop from the first column is what that information is worth."
      : `Every rule saw the ${settingName(first ?? "full")} view. Add more views to experiment.observation to see what information is worth.`;
  return (
    <div data-testid="info-worth">
      <Card
        question="What is information worth?"
        sub={observations.length && rows.length ? sub : undefined}
      >
        <div>
          {observations.length === 0 || rows.length === 0 ? (
            <EmptyState
              title="No view results were kept."
              body="Run the race again with at least one view."
            />
          ) : (
            <div
              className="tf-scroll"
              tabIndex={0}
              role="region"
              aria-label="Profit per run by view"
            >
              <table
                className={`tf-table tf-det-table${observations.length < 3 ? " tf-det-narrow" : ""}`}
              >
                <thead>
                  <tr>
                    <th scope="col">Rule</th>
                    {observations.map((o) => (
                      <th key={o} scope="col">
                        {settingName(o)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr key={p.policy}>
                      <th scope="row">{p.label}</th>
                      {observations.map((o, i) => {
                        const value = dollars[p.policy][o];
                        const drop =
                          i > 0 && value !== undefined
                            ? value - dollars[p.policy][first]
                            : null;
                        return (
                          <td key={o}>
                            {value === undefined ? "–" : usd(value)}
                            {drop !== null && Number.isFinite(drop) && (
                              <span className="tf-det-muted">
                                {signedUsd(drop)}
                              </span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
