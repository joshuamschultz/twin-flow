// State map with its panel, legend, coverage line and the Pick / Today switch.
// Works controlled (chainId, selected) or on its own.
import { useEffect, useMemo, useRef, useState } from "react";
import { familyColor } from "./StateGlyph";
import { StateMap, type StateChain } from "./StateMapChart";
import { StatePanel } from "./StatePanel";
import { normalDay, pct } from "./stateFormat";
import type { LanesBy, LevelDef } from "./stateMap";
import type { RaceViewModel } from "./types";

export type ChainChoice = {
  id: string;
  /** Switch label: "Pick", "Today". */
  label: string;
  chain: StateChain;
  avgPerDay: number;
};

export type StateMapCardProps = {
  chains: ChainChoice[];
  horizon: number;
  runs?: number;
  levels?: readonly (LevelDef | string)[] | null;
  families?: readonly string[];
  lanesBy?: LanesBy;
  chainId?: string;
  onChainChange?: (id: string) => void;
  selected?: string | null;
  onSelect?: (id: string) => void;
};

function dollars(m: RaceViewModel, policy: string): number {
  return m.policies.find((p) => p.policy === policy)?.dollars.mean ?? 0;
}

/** The Pick and Today chains from a race view model. */
export function raceChains(m: RaceViewModel): ChainChoice[] {
  const horizon = m.about.horizon_days;
  const pick = m.recommendation.policy;
  const today = m.baseline?.policy;
  const out: ChainChoice[] = [];
  if (pick && m.markov[pick])
    out.push({
      id: pick,
      label: "Pick",
      chain: m.markov[pick],
      avgPerDay: normalDay(dollars(m, pick), horizon),
    });
  if (today && today !== pick && m.markov[today])
    out.push({
      id: today,
      label: "Today",
      chain: m.markov[today],
      avgPerDay: normalDay(dollars(m, today), horizon),
    });
  return out;
}

/** The most common state: what the map selects first. */
export function topState(chain: StateChain): string | null {
  let best: StateChain["nodes"][number] | null = null;
  for (const n of chain.nodes) if (!best || n.share > best.share) best = n;
  return best?.id ?? null;
}

export function coverageWords(chain: StateChain): string {
  const n = chain.nodes.length;
  const base = `The map shows the ${n} most common ${n === 1 ? "state" : "states"}.`;
  return chain.coverage == null
    ? base
    : `${base} They cover ${pct(chain.coverage, 0)} of all days.`;
}

export function StateMapCard({
  chains,
  horizon,
  runs,
  levels,
  families = [],
  lanesBy,
  chainId,
  onChainChange,
  selected,
  onSelect,
}: StateMapCardProps) {
  const [ownChain, setOwnChain] = useState(chains[0]?.id ?? "");
  const activeId = chainId ?? ownChain;
  const active = chains.find((c) => c.id === activeId) ?? chains[0];
  const [ownSel, setOwnSel] = useState<string | null>(null);
  const valid = (id: string | null | undefined) =>
    !!id && !!active?.chain.nodes.some((n) => n.id === id);
  const current = valid(selected)
    ? selected!
    : valid(ownSel)
      ? ownSel!
      : active
        ? topState(active.chain)
        : null;
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => setOwnSel(null), [activeId]);

  const choose = (id: string) => {
    setOwnSel(id);
    onSelect?.(id);
  };
  const switchTo = (id: string) => {
    setOwnChain(id);
    setOwnSel(null);
    onChainChange?.(id);
  };
  const node = useMemo(
    () => active?.chain.nodes.find((n) => n.id === current) ?? null,
    [active, current],
  );

  if (!active || !active.chain.nodes.length) {
    return (
      <div className="tf-sm-empty" data-state-empty="">
        No moves were captured for this run. Add{" "}
        <code>experiment.state_capture: {"{every_hours: 4}"}</code> and run
        again.
      </div>
    );
  }

  const setupLanes = active.chain.nodes.some((n) => n.parts_of_label?.setup);
  return (
    <div className="tf-sm-wrap">
      {chains.length > 1 && (
        <div className="tf-sm-seg" role="group" aria-label="Which rule">
          {chains.map((c) => (
            <button
              key={c.id}
              type="button"
              aria-pressed={c.id === active.id}
              className={c.id === active.id ? "on" : ""}
              onClick={() => switchTo(c.id)}
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
      <div className="tf-sm-grid">
        <div className="tf-sm-card">
          <div className="tf-sm-legend">
            {families.length > 0 && setupLanes && (
              <span>
                Bars in a box: stock of
                {families.map((f) => (
                  <span key={f} className="tf-sm-key">
                    <i style={{ background: familyColor(f, families) }} />
                    {f}
                  </span>
                ))}
              </span>
            )}
            <span className="tf-sm-key">
              <i style={{ background: "var(--tf-out)" }} />
              low
            </span>
            <span>
              Edge of box:
              <span className="tf-sm-key">
                <i style={{ background: "var(--tf-gain-fill)" }} />
                better than a normal day
              </span>
              <span className="tf-sm-key">
                <i style={{ background: "var(--tf-today)" }} />
                worse
              </span>
              <span className="tf-sm-key">
                <i style={{ background: "var(--tf-loss)" }} />
                loses money
              </span>
            </span>
          </div>
          <StateMap
            chain={active.chain}
            selected={current}
            onSelect={choose}
            avgPerDay={active.avgPerDay}
            levels={levels}
            families={families}
            lanesBy={lanesBy}
          />
          <p className="tf-sm-note" data-state-coverage="">
            {coverageWords(active.chain)}
          </p>
          {!setupLanes && !active.chain.nodes.some((n) => n.parts_of_label) && (
            <p className="tf-sm-note">
              Add `state_groups` to your model to get a clearer map.
            </p>
          )}
        </div>
        <div className="tf-sm-card">
          {node && (
            <StatePanel
              ref={heading}
              node={node}
              chain={active.chain}
              avgPerDay={active.avgPerDay}
              horizon={horizon}
              runs={runs}
              levels={levels}
              onSelect={(id) => {
                choose(id);
                heading.current?.focus({ preventScroll: true });
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
