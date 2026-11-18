// Before you switch: up to three cautions, right under the hero (DESIGN.md 3.2).
import type { Caution } from "./types";

export function CautionCards({ cautions }: { cautions: Caution[] }) {
  const shown = cautions.slice(0, 3);
  return (
    <div className="tf-wrap" id="cautions" data-section="cautions">
      <h2 className="tf-sr">Before you switch</h2>
      <div className="tf-watch">
        {shown.length === 0 ? (
          <div className="tf-card tf-neutral">
            <div className="tf-eyebrow">Before you switch</div>
            <h3>No cautions</h3>
            <p>The new rule is better on money, orders filled and stock.</p>
          </div>
        ) : (
          shown.map((c, i) => (
            <div className="tf-card" key={c.key} data-caution={c.key}>
              <div className="tf-eyebrow">Before you switch · {i + 1}</div>
              <h3>{c.title}</h3>
              <p>{c.body}</p>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
