// Compare two rules side by side (DESIGN.md 3.5). A and B live in the URL.
import { Fragment, useId } from "react";
import { compareRows, compareSentence } from "./compare";
import { Takeaway } from "./primitives";
import type { PolicyRow } from "./types";
import { useViewState } from "./viewState";
import "./explore.css";

type Props = {
  policies: PolicyRow[];
  /** Test runs per rule. */
  runs: number;
  horizonDays: number;
};

export function defaultPair(policies: PolicyRow[]): [string, string] {
  const byRank = [...policies].sort((x, y) => x.rank - y.rank);
  const pick = (byRank.find((p) => p.is_recommended) ?? byRank[0])?.policy;
  const today =
    byRank.find((p) => p.is_baseline)?.policy ??
    byRank.find((p) => p.policy !== pick)?.policy ??
    pick;
  return [pick ?? "", today ?? ""];
}

export function CompareTable({ policies, runs, horizonDays }: Props) {
  const byRank = [...policies].sort((x, y) => x.rank - y.rank);
  const [pickId, todayId] = defaultPair(policies);
  const [aId, setA] = useViewState("a", pickId);
  const [bId, setB] = useViewState("b", todayId);
  const find = (id: string, fallback: string) =>
    byRank.find((p) => p.policy === id) ??
    byRank.find((p) => p.policy === fallback)!;
  const a = find(aId, pickId);
  const b = find(bId, todayId);
  const headId = useId();
  if (!a || !b) return null;
  const rows = compareRows(a, b, horizonDays);
  const groups = [...new Set(rows.map((r) => r.group))];
  const sentence = compareSentence(a, b, runs);
  const different = a.policy !== b.policy;

  const picker = (
    label: string,
    value: string,
    set: (v: string) => void,
    suffix: (p: PolicyRow) => string,
  ) => (
    <label>
      {label}
      <select value={value} onChange={(e) => set(e.target.value)}>
        {byRank.map((p) => (
          <option key={p.policy} value={p.policy}>
            {p.label}
            {suffix(p)}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div className="tf-card tf-cmp" role="region" aria-labelledby={headId}>
      <h3 id={headId} className="tf-sr">
        Compare two rules
      </h3>
      <div className="tf-cmp-pickers">
        {picker("Rule A", a.policy, setA, (p) =>
          p.is_recommended ? " (pick)" : p.is_baseline ? " (today)" : "",
        )}
        <span className="tf-cmp-vs" aria-hidden="true">
          VS
        </span>
        {picker("Rule B", b.policy, setB, (p) =>
          p.is_baseline ? " (today)" : p.is_recommended ? " (pick)" : "",
        )}
      </div>
      <div role="status" aria-live="polite">
        <Takeaway>
          {different ? <Lead name={a.label} sentence={sentence} /> : sentence}
        </Takeaway>
      </div>
      {different && (
        <>
          <div className="tf-scroll">
            <table className="tf-cmp-table">
              <thead>
                <tr>
                  <th scope="col">Measure</th>
                  <th scope="col" className="tf-n">
                    {a.label}
                  </th>
                  <th scope="col" className="tf-cmp-barcol">
                    <span className="tf-sr">Bars</span>
                  </th>
                  <th scope="col" className="tf-n">
                    {b.label}
                  </th>
                  <th scope="col" className="tf-n">
                    A minus B
                  </th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <Fragment key={g}>
                    <tr className="tf-cmp-grp">
                      <th scope="rowgroup" colSpan={5}>
                        {g}
                      </th>
                    </tr>
                    {rows
                      .filter((r) => r.group === g)
                      .map((r) => (
                        <tr key={r.id} data-measure={r.id}>
                          <th scope="row">{r.label}</th>
                          <td className="tf-n">{r.aText}</td>
                          <td aria-hidden="true">
                            <div className="tf-cmp-pair">
                              <i
                                className="tf-cmp-a"
                                style={{ width: `${r.aShare * 100}%` }}
                              />
                              <i
                                className="tf-cmp-b"
                                style={{ width: `${r.bShare * 100}%` }}
                              />
                            </div>
                          </td>
                          <td className="tf-n">{r.bText}</td>
                          <td className="tf-n">
                            {r.deltaText && (
                              <span className={`tf-pill tf-${r.tone}`}>
                                {r.deltaText}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          <p className="tf-note">
            Blue pill: A is better. Orange pill: A is worse. Grey: neither is
            better by itself.
          </p>
        </>
      )}
    </div>
  );
}

/** The sentence with the names and the money in bold. Same words as compareSentence. */
function Lead({ name, sentence }: { name: string; sentence: string }) {
  if (!sentence.startsWith(name + " makes ")) return <>{sentence}</>;
  const m = sentence
    .slice(name.length)
    .match(/^ makes (\S+ (?:more|less)) (.*)$/);
  if (!m) return <>{sentence}</>;
  return (
    <>
      <b>{name}</b> makes <b>{m[1]}</b> {m[2]}
    </>
  );
}
