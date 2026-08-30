import { useEffect, useState } from "react";
import { api, type FloorGraph } from "../api";
import { FloorMap } from "./FloorMap";
import { Spinner } from "./Spinner";

export function FloorTab({ model }: { model: string }) {
  const [floor, setFloor] = useState<FloorGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setFloor(null);
    setError(null);
    api
      .floor(model)
      .then(setFloor)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [model]);

  return (
    <div>
      <div className="panel-header">
        <h2>Floor Map</h2>
        <span className="hint">locations, stocks, and the routes parts follow</span>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!error && !floor && <Spinner label="Loading floor…" />}
      {floor && <FloorMap floor={floor} />}
    </div>
  );
}
