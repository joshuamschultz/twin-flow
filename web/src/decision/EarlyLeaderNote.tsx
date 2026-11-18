import "./details.css";
import type { RaceViewModel } from "./types";

/** Who led after day 1, and when the pick caught up. Nothing shows without an early leader. */
export function EarlyLeaderNote({
  model,
}: {
  model: Pick<RaceViewModel, "early_leader">;
}) {
  if (!model.early_leader) return null;
  return (
    <p className="tf-note" data-testid="early-leader">
      {model.early_leader.words}
    </p>
  );
}
