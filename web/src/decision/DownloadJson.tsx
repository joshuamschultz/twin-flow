import { useMemo } from "react";
import "./details.css";

/** "Download race.json": a data: link built from the model the page already holds, so it
 * works from a file with no network. */
export function DownloadJson({
  model,
  name = "race.json",
}: {
  model: unknown;
  name?: string;
}) {
  const href = useMemo(
    () =>
      `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(model, null, 1))}`,
    [model],
  );
  return (
    <a className="tf-btn" href={href} download={name}>
      Download {name}
    </a>
  );
}
