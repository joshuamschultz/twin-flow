// Measures its width and hands it to the chart, so text keeps its size at every width
// (DESIGN.md 8.2). Gives the chart its label and a "Show as table" toggle (8.4).
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

type Props = {
  /** The takeaway, read by screen readers. Not the chart type. */
  label: string;
  /** The same numbers as a real table. */
  table: () => ReactNode;
  /** The chart, drawn for the measured width in px. */
  children: (width: number) => ReactNode;
  /** Charts with focusable parts are a group, static ones an image. */
  interactive?: boolean;
  className?: string;
};

export function useWidth<T extends HTMLElement>(): [
  React.RefObject<T>,
  number,
] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(Math.floor(el.getBoundingClientRect().width));
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      setWidth((old) => (old === w ? old : w));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

export function ChartFrame({
  label,
  table,
  children,
  interactive,
  className,
}: Props) {
  const [asTable, setAsTable] = useState(false);
  const [ref, width] = useWidth<HTMLDivElement>();
  return (
    <div className={"tf-chart " + (className ?? "")}>
      <div ref={ref}>
        {asTable ? (
          <div className="tf-scroll">{table()}</div>
        ) : (
          <div
            role={interactive ? "group" : "img"}
            aria-roledescription={interactive ? "chart" : undefined}
            aria-label={label}
            data-chart
          >
            {width > 0 && children(width)}
          </div>
        )}
      </div>
      <div className="tf-chart-tools">
        <button
          type="button"
          className="tf-linkbtn"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? "Show as chart" : "Show as table"}
        </button>
      </div>
    </div>
  );
}
