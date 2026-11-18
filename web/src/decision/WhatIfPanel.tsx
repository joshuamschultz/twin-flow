// File name differs from whatif.ts only so case-insensitive disks keep both.
// What if (DESIGN.md 3.6): cost sliders, flip hints, the live ranking and the honest label.
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { usd } from "./format";
import { Takeaway } from "./primitives";
import type { PolicyRow, WhatIf as WhatIfConfig } from "./types";
import { flipPoints, rerank, type Flip, type Multipliers } from "./whatif";
import "./explore.css";

type Props = {
  policies: PolicyRow[];
  config: WhatIfConfig;
  /** Test runs per rule, for the honest label. */
  runs: number;
  /** Rows shown in the live ranking. */
  limit?: number;
};

const SLIDER_LABEL: Record<string, string> = {
  setup: "A changeover costs",
  holding: "Keeping stock costs",
  lost_sales: "A lost sale costs",
};

const sliderLabel = (part: string, words: Record<string, string>) =>
  SLIDER_LABEL[part] ??
  capital(words[part] ?? part.replace(/_/g, " ")) + " cost";

function capital(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const times = (v: number, digits: number) => v.toFixed(digits) + "x";

export function flipHint(
  flip: { below: Flip; above: Flip },
  label: (id: string) => string,
  pickLabel: string,
): string {
  const bits: string[] = [];
  if (flip.below)
    bits.push(
      `Below ${times(flip.below.at, 2)} ${label(flip.below.leader)} wins.`,
    );
  if (flip.above)
    bits.push(
      `Above ${times(flip.above.at, 2)} ${label(flip.above.leader)} wins.`,
    );
  return bits.length
    ? bits.join(" ")
    : `${pickLabel} wins anywhere on this slider.`;
}

const ones = (parts: string[]): Multipliers =>
  Object.fromEntries(parts.map((p) => [p, 1]));

export function WhatIf({ policies, config, runs, limit = 8 }: Props) {
  const parts = config.parts as string[];
  const pick =
    policies.find((p) => p.is_recommended) ??
    [...policies].sort((a, b) => a.rank - b.rank)[0];
  const [m, setM] = useState<Multipliers>(() => ones(parts));
  const labelOf = (id: string) =>
    policies.find((p) => p.policy === id)?.label ?? id;
  const flips = useMemo(
    () => flipPoints(policies, pick?.policy ?? "", config),
    [policies, pick, config],
  );
  const rows = rerank(policies, m);
  const shown = rows.slice(0, limit);
  const top = Math.max(1, ...shown.map((r) => r.score));
  const listRef = useFlip(shown.map((r) => r.policy).join("|"));
  if (!pick || rows.length === 0) return null;

  const atToday = parts.every((p) => m[p] === 1);
  const leader = rows[0];
  const lead = rows.length > 1 ? leader.score - rows[1].score : 0;
  const flipped = leader.policy !== pick.policy;
  const digits = config.step < 0.1 ? 2 : 1;

  return (
    <div className="tf-grid-a tf-wi">
      <div className="tf-card">
        <div className="tf-wi-head">
          <h3>Set the costs</h3>
          <button
            type="button"
            className="tf-btn"
            onClick={() => setM(ones(parts))}
          >
            Reset to my costs
          </button>
        </div>
        <div className="tf-wi-sliders">
          {parts.map((part) => {
            const id = `wi-${part}`;
            return (
              <div className="tf-wi-slider" key={part}>
                <label htmlFor={id}>
                  {sliderLabel(part, config.words)}
                  <span className="tf-num">{times(m[part], digits)}</span>
                </label>
                <input
                  id={id}
                  type="range"
                  min={config.min}
                  max={config.max}
                  step={config.step}
                  value={m[part]}
                  aria-describedby={`wi-hint-${part}`}
                  aria-valuetext={`${times(m[part], digits)} the real cost`}
                  onChange={(e) =>
                    setM((old) => ({ ...old, [part]: Number(e.target.value) }))
                  }
                />
                <small id={`wi-hint-${part}`}>
                  {flipHint(flips[part], labelOf, pick.label)}
                </small>
              </div>
            );
          })}
        </div>
        <div className="tf-wi-honest" role="note" aria-label="Quick estimate">
          <b>This is a quick estimate.</b> It re-prices the same {runs} runs.
          The rules act the same as before. To let each rule adjust to new
          costs, change the costs in your project and rerun the race.
        </div>
      </div>
      <div className="tf-card">
        <h3>Who wins at these costs?</h3>
        <p className="tf-sub">
          Profit per run. Arrows show moves from the real ranking.
        </p>
        <ol className="tf-wi-list" ref={listRef}>
          {shown.map((r, n) => (
            <li
              key={r.policy}
              data-wi-row={r.policy}
              data-flip-key={r.policy}
              className={r.policy === pick.policy ? "tf-wi-pick" : undefined}
            >
              <span className="tf-wi-rk tf-num">{n + 1}</span>
              <Move value={r.move} />
              <span className="tf-wi-nm">{r.label}</span>
              <span className="tf-track" aria-hidden="true">
                <span
                  className={
                    "tf-bar " +
                    (r.policy === pick.policy ? "tf-gain" : "tf-wi-other")
                  }
                  style={{
                    left: 0,
                    width: `${(Math.max(0, r.score) / top) * 100}%`,
                  }}
                />
              </span>
              <span className="tf-val">{usd(r.score)}</span>
            </li>
          ))}
        </ol>
        <div role="status" aria-live="polite">
          <Takeaway tone={flipped ? "warn" : "info"}>
            {flipped ? (
              <>
                <b>{leader.label} would win</b> by {usd(lead)} per run at these
                costs. Rerun the race with these costs before you act.
              </>
            ) : (
              <>
                <b>
                  {pick.label} {atToday ? "wins" : "still wins"}.
                </b>{" "}
                {rows.length > 1 &&
                  `It leads ${rows[1].label} by ${usd(lead)} per run ${
                    atToday ? "at your costs today" : "at these costs"
                  }.`}
              </>
            )}
          </Takeaway>
        </div>
      </div>
    </div>
  );
}

function Move({ value }: { value: number }) {
  const words =
    value > 0 ? `up ${value}` : value < 0 ? `down ${-value}` : "no change";
  const mark = value > 0 ? `▲${value}` : value < 0 ? `▼${-value}` : "–";
  const tone = value > 0 ? "tf-up" : value < 0 ? "tf-dn" : "tf-eq";
  return (
    <span className={`tf-wi-move ${tone}`}>
      <span aria-hidden="true">{mark}</span>
      <span className="tf-sr">{words}</span>
    </span>
  );
}

/** FLIP move: rows slide from their old place to the new one in 170 ms (DESIGN.md 8.3). */
function useFlip(order: string) {
  const ref = useRef<HTMLOListElement>(null);
  const last = useRef(new Map<string, number>());
  useLayoutEffect(() => {
    const list = ref.current;
    if (!list) return;
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const next = new Map<string, number>();
    for (const el of Array.from(list.children) as HTMLElement[]) {
      const key = el.dataset.flipKey ?? "";
      const top = el.getBoundingClientRect().top;
      next.set(key, top);
      const before = last.current.get(key);
      if (reduce || before === undefined || before === top) continue;
      el.style.transition = "none";
      el.style.transform = `translateY(${before - top}px)`;
      requestAnimationFrame(() => {
        el.style.transition = "transform var(--ba-motion-micro)";
        el.style.transform = "";
      });
    }
    last.current = next;
  }, [order]);
  return ref;
}
