import "./details.css";
import type { RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "about" | "delta">;

/** The method in plain words, and how to read the table. */
export function MethodNote({ model }: { model: Source }) {
  const { about, delta } = model;
  const range = `${Math.round(about.interval * 100)}%`;
  return (
    <div className="tf-det-method" data-testid="method-note">
      <p>{about.method}</p>
      <p>
        <b>How to read this.</b> Every rule faced the same customer orders, run
        by run. "vs today" is the average gap to today's rule across those runs.
        The {range} range is where that average is likely to fall (a {range}{" "}
        confidence interval). {delta?.assumption}
      </p>
    </div>
  );
}
