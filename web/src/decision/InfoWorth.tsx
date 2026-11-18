import "./details.css";
import { fmtMoney, settingName } from "./detailsFormat";
import type { RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "views" | "policies">;

/** Profit for each rule under each view. A view hides some facts from the rule, so the drop
 * from the first column is what that information is worth. */
export function InfoWorth({ model }: { model: Source }) {
  const { observations, dollars } = model.views;
  const rows = model.policies.filter((p) => dollars[p.policy]);
  const empty = observations.length === 0 || rows.length === 0;
  const first = observations[0];
  const caption =
    observations.length > 1
      ? "Each column hides some facts from the rule. The press still runs for real. The drop from the first column is what that information is worth."
      : `Every rule saw the ${settingName(first ?? "full")} view. Add more views to experiment.observation to see what information is worth.`;
  return (
    <div className="tf-card" data-testid="info-worth">
      <h3>What is information worth?</h3>
      {empty ? (
        <div className="tf-empty">
          <b>No view results were kept.</b>
          <span>Run the race again with at least one view.</span>
        </div>
      ) : (
        <>
          <p className="tf-sub">{caption}</p>
          <div
            className="tf-det-scroll"
            tabIndex={0}
            role="region"
            aria-label="Profit per run by view"
          >
            <table
              className={`tf-det-table${observations.length < 3 ? " tf-det-narrow" : ""}`}
            >
              <thead>
                <tr>
                  <th scope="col">Rule</th>
                  {observations.map((o) => (
                    <th key={o} scope="col" className="n">
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
                        <td key={o} className="n">
                          {value === undefined ? "–" : fmtMoney(value)}
                          {drop !== null && Number.isFinite(drop) && (
                            <span className="tf-det-muted">
                              {fmtMoney(drop, { signed: true })}
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
        </>
      )}
    </div>
  );
}
