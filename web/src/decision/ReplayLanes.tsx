// Floor replay lanes (DESIGN.md 3.7): one scrubber and Play drive both lanes.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  dayFacts,
  keyDay,
  nextPlayDay,
  parseDay,
  PLAY_MS,
  replayTakeaway,
  type ReplayRun,
} from "./replay";
import { usd } from "./format";
import { EmptyState, Tag, Takeaway } from "./primitives";
import { familyColour, StripStack } from "./ReplayStrips";
import { useViewState } from "./viewState";
import type { Economics, ExampleRun, Product } from "./types";
import "./replay.css";

export function DayFacts(props: {
  run: ExampleRun;
  day: number;
  products: Product[];
  economics?: Economics;
  live?: boolean;
}) {
  const f = dayFacts(props.run, props.day, props.products, props.economics);
  if (!f) {
    return <p className="tf-rp-dayline">This run has no day {props.day}.</p>;
  }
  return (
    <>
      <div className="tf-rp-facts">
        <div data-fact="makes">
          <div className="tf-rp-k">Press makes</div>
          <div className="tf-rp-v">{f.makes}</div>
        </div>
        <div data-fact="changeovers">
          <div className="tf-rp-k">Changeovers so far</div>
          <div className="tf-rp-v">{f.changeoversSoFar}</div>
        </div>
        <div data-fact="out">
          <div className="tf-rp-k">Products out today</div>
          <div className={f.out.length ? "tf-rp-v tf-rp-bad" : "tf-rp-v"}>
            {f.outLabel}
          </div>
        </div>
        <div data-fact="profit">
          <div className="tf-rp-k">Profit so far</div>
          <div className="tf-rp-v">{usd(f.profitSoFar)}</div>
        </div>
      </div>
      <p
        className="tf-rp-dayline"
        data-fact="sentence"
        aria-live={props.live ? "polite" : "off"}
      >
        {f.sentence}
      </p>
    </>
  );
}

export function Scrubber(props: {
  day: number;
  max: number;
  playing: boolean;
  onDay: (day: number) => void;
  onPlay: () => void;
}) {
  const { day, max, playing, onDay, onPlay } = props;
  return (
    <div className="tf-rp-scrub">
      <button type="button" className="tf-btn tf-rp-play" onClick={onPlay}>
        <span aria-hidden="true">{playing ? "❚❚" : "▶"}</span>{" "}
        {playing ? "Pause" : "Play"}
      </button>
      <span className="tf-rp-day" aria-hidden="true">
        Day {day}
      </span>
      <input
        type="range"
        className="tf-rp-range"
        min={1}
        max={max}
        step={1}
        value={day}
        aria-label="Day"
        aria-valuetext={`Day ${day} of ${max}`}
        onChange={(e) => onDay(Number(e.target.value))}
        onKeyDown={(e) => {
          const next = keyDay(e.key, day, max);
          if (next === null) return;
          e.preventDefault();
          onDay(next);
        }}
      />
    </div>
  );
}

function Legend({ families }: { families: string[] }) {
  return (
    <div className="tf-legend">
      {families.map((f, i) => (
        <span key={f}>
          <i style={{ background: familyColour(f, families) }} />
          {i === 0 ? `Press makes ${f}` : f}
        </span>
      ))}
      <span>
        <i className="tf-rp-key-rest" />
        rests
      </span>
      <span className="tf-rp-gap">
        <i style={{ background: "var(--tf-out)" }} />
        Out of stock or colour change
      </span>
      <span>
        <i style={{ background: "var(--tf-seq-1)" }} />
        under 4 days of stock
      </span>
      <span>
        <i style={{ background: "var(--tf-seq-2)" }} />4 to 10
      </span>
      <span>
        <i style={{ background: "var(--tf-seq-3)" }} />
        over 10
      </span>
    </div>
  );
}

const TAG_WORDS = { today: "Today", pick: "Pick", plain: "Rule" } as const;

export function ReplayLanes(props: {
  runs: ReplayRun[];
  products: Product[];
  families?: string[] | null;
  economics?: Economics;
  /** Controlled day. Leave out to keep the day in the URL hash. */
  day?: number;
  onDay?: (day: number) => void;
}) {
  const { runs, products, economics } = props;
  const families = props.families ?? [
    ...new Set(products.map((p) => p.family ?? "")),
  ];
  const max = Math.max(1, ...runs.map((r) => r.run.days.length));
  const [urlDay, setUrlDay] = useViewState("day", "1");
  const day = Math.min(max, props.day ?? parseDay(urlDay, max));
  const setUrl = useCallback((d: number) => setUrlDay(String(d)), [setUrlDay]);
  const setDay = props.onDay ?? setUrl;

  const [playing, setPlaying] = useState(false);
  const dayRef = useRef(day);
  dayRef.current = day;

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      const next = nextPlayDay(dayRef.current, max);
      if (next === null) {
        setPlaying(false);
        return;
      }
      setDay(next);
    }, PLAY_MS);
    return () => window.clearInterval(timer);
  }, [playing, max, setDay]);

  const onPlay = () => {
    if (playing) return setPlaying(false);
    if (day >= max) setDay(1);
    setPlaying(true);
  };
  const jump = (d: number) => {
    setPlaying(false);
    setDay(d);
  };

  if (!runs.length) {
    return (
      <EmptyState
        title="No run to replay."
        body="The race kept no day-by-day moves for these rules."
      />
    );
  }

  return (
    <div className="tf-rp-lanes">
      <Scrubber
        day={day}
        max={max}
        playing={playing}
        onDay={jump}
        onPlay={onPlay}
      />
      <Legend families={families} />
      <div className="tf-rp-grid">
        {runs.map((r) => (
          <div key={r.id} className="tf-rp-lane" data-lane={r.id}>
            <h3 className="tf-rp-h3">
              {TAG_WORDS[r.tag]} <Tag kind={r.tag}>{r.label}</Tag>
            </h3>
            <StripStack
              label={`${TAG_WORDS[r.tag]}, ${r.label}`}
              run={r.run}
              products={products}
              families={families}
              economics={economics}
              day={day}
              onDay={jump}
            />
            <DayFacts
              run={r.run}
              day={day}
              products={products}
              economics={economics}
              live={!playing}
            />
          </div>
        ))}
      </div>
      <Takeaway>{replayTakeaway(runs, products)}</Takeaway>
    </div>
  );
}
