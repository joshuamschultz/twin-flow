// Money leaks: the states that cost the pick the most per run (DESIGN.md 3.9).
// A click on a row selects that state in the map.
import { pct, usd } from "./stateFormat";
import {
  partsFromLabel,
  stateWords,
  wordsSentence,
  type LevelDef,
} from "./stateMap";
import type { Leak } from "./types";
import "./stateMap.css";

const TRY: Record<string, string> = {
  setup: "Try a longer campaign so the press switches less here.",
  holding: "Try a lower stock target so less sits on the shelf.",
  lost_sales: "Try starting this colour sooner so it does not run out.",
};

const STACK: [key: string, cls: string, words: string][] = [
  ["setup", "c-setup", "Changeovers"],
  ["holding", "c-hold", "Keeping stock"],
  ["lost_sales", "c-lost", "Lost sales"],
];

export type LeakRow = Pick<
  Leak,
  | "state"
  | "state_id"
  | "share"
  | "per_run"
  | "parts"
  | "cause"
  | "cause_words"
  | "try"
>;

/** "Try ..." line: from the data when it has one, else a template keyed on cause. */
export function leakTry(leak: LeakRow): string {
  return leak.try || TRY[leak.cause] || "";
}

export function leakWords(
  leak: LeakRow,
  levels?: readonly (LevelDef | string)[] | null,
): { name: string; detail: string } {
  const w = stateWords(partsFromLabel(leak.state), levels);
  return {
    name: w.title ? wordsSentence(w) : leak.state,
    detail: `${pct(leak.share)} of days. Biggest cost: ${leak.cause_words}.`,
  };
}

export type LeakListProps = {
  leaks: readonly LeakRow[];
  onSelectState?: (stateId: string) => void;
  levels?: readonly (LevelDef | string)[] | null;
  limit?: number;
};

export function LeakList({
  leaks,
  onSelectState,
  levels,
  limit = 5,
}: LeakListProps) {
  const rows = leaks.slice(0, limit);
  if (!rows.length)
    return (
      <div className="tf-sm-empty" data-leaks-empty="">
        No money leaks to show. No day-by-day record was kept for this rule.
        Rerun the race.
      </div>
    );
  const max = Math.max(1, ...rows.map((l) => l.per_run));
  return (
    <div data-leaks="">
      <div className="tf-leak-legend" aria-hidden="true">
        {STACK.map(([k, cls, words]) => (
          <span key={k}>
            <i className={cls} />
            {words}
          </span>
        ))}
        <span className="end">Cost per run</span>
      </div>
      <ol className="tf-leaks">
        {rows.map((leak, i) => {
          const { name, detail } = leakWords(leak, levels);
          const tryLine = leakTry(leak);
          const parts = STACK.filter(([k]) => (leak.parts[k] ?? 0) > 0);
          const id = leak.state_id ?? null;
          const barWords = parts
            .map(([k, , words]) => `${words} ${usd(leak.parts[k])}`)
            .join(", ");
          return (
            <li key={leak.state}>
              <button
                type="button"
                className="tf-leak"
                data-leak-state={id ?? ""}
                disabled={!id || !onSelectState}
                aria-label={`${i + 1}. ${name} ${detail} ${tryLine} Costs ${usd(leak.per_run)} per run: ${barWords}. Show it on the map.`}
                onClick={() => id && onSelectState?.(id)}
              >
                <span className="tf-leak-rank" aria-hidden="true">
                  {i + 1}
                </span>
                <span>
                  <span className="tf-leak-name">{name}</span>
                  <span className="tf-leak-try">
                    {detail} {tryLine}
                  </span>
                </span>
                <span className="tf-leak-stackcell" aria-hidden="true">
                  <span
                    className="tf-leak-stack"
                    style={{ width: `${(leak.per_run / max) * 100}%` }}
                  >
                    {parts.map(([k, cls, words]) => (
                      <i
                        key={k}
                        className={cls}
                        data-part={k}
                        title={`${words}: ${usd(leak.parts[k])}`}
                        style={{ flex: leak.parts[k] }}
                      />
                    ))}
                  </span>
                </span>
                <span className="tf-leak-val" aria-hidden="true">
                  {usd(leak.per_run)}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
