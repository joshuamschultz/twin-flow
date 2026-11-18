// Floor replay strips (DESIGN.md 3.7): Press, Setup $ and Stock on one shared day axis.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  cellIndexOfDay,
  cellLevel,
  changeKind,
  colourChanges,
  dayBuckets,
  dayFacts,
  money,
  outProductDays,
  pressWords,
  stockRows,
  stockWords,
  type DayCell,
  type StockLevel,
  type StockRow,
} from "./replay";
import type { Economics, ExampleRun, Product } from "./types";
import "./replay.css";

// ---------- shared scale ----------

export interface DayScale {
  x0: number;
  width: number;
  cw: number;
  n: number;
  x: (i: number) => number;
  indexAt: (px: number) => number;
}

export function makeScale(n: number, width: number, x0: number): DayScale {
  const cw = Math.max(1, (width - x0 - 2) / Math.max(1, n));
  return {
    x0,
    width,
    cw,
    n,
    x: (i) => x0 + i * cw,
    indexAt: (px) => Math.floor((px - x0) / cw),
  };
}

const NAMED = new Set(["grey", "black", "natural"]);

export function familyColour(
  family: string | null,
  families: string[],
): string {
  if (!family) return "none";
  if (NAMED.has(family)) return `var(--tf-fam-${family})`;
  const i = Math.max(0, families.indexOf(family));
  return `var(--tf-fam-${(i % 5) + 1})`;
}

const LEVEL_FILL: Record<StockLevel, string> = {
  out: "var(--tf-out)",
  low: "var(--tf-seq-1)",
  mid: "var(--tf-seq-2)",
  high: "var(--tf-seq-3)",
};

type Tip = (text: string | null, x?: number, y?: number) => void;

function tipHandlers(text: string, onTip: Tip) {
  return {
    onPointerEnter: (e: React.PointerEvent<SVGElement>) =>
      onTip(text, e.clientX, e.clientY),
    onPointerDown: (e: React.PointerEvent<SVGElement>) =>
      onTip(text, e.clientX, e.clientY),
    onPointerLeave: () => onTip(null),
  };
}

// ---------- strips ----------

export const PRESS_H = 22;
export const SETUP_H = 22;

export function PressStrip(props: {
  cells: DayCell[];
  scale: DayScale;
  y: number;
  families: string[];
  economics?: Economics;
  onTip: Tip;
}) {
  const { cells, scale, y, families, economics, onTip } = props;
  return (
    <g className="tf-rp-press">
      <text x={0} y={y + 15} className="tf-rp-lab">
        Press
      </text>
      {cells.map((c, i) => {
        const rest = c.family === null;
        return (
          <rect
            key={c.start}
            data-press=""
            data-days={
              c.start === c.end ? String(c.start) : `${c.start}-${c.end}`
            }
            data-family={c.family ?? "rest"}
            x={scale.x(i) + 0.5}
            y={y}
            width={Math.max(0.5, scale.cw - 1)}
            height={PRESS_H}
            rx={1.5}
            fill={familyColour(c.family, families)}
            className={rest ? "tf-rp-rest" : undefined}
            {...tipHandlers(pressWords(c, economics), onTip)}
          />
        );
      })}
    </g>
  );
}

export function SetupStrip(props: {
  cells: DayCell[];
  scale: DayScale;
  y: number;
  economics?: Economics;
  onTip: Tip;
}) {
  const { cells, scale, y, economics, onTip } = props;
  const base = y + SETUP_H;
  const merged = cells.some((c) => c.start !== c.end);
  const most = Math.max(1, ...cells.map((c) => c.changes));
  return (
    <g className="tf-rp-setup">
      <text x={0} y={y + 15} className="tf-rp-lab">
        Setup $
      </text>
      {cells.map((c, i) => {
        if (!merged) {
          const kind = changeKind(c.days[0]);
          if (kind === "none") return null;
          const h = kind === "colour" ? SETUP_H - 2 : 5;
          return (
            <rect
              key={c.start}
              data-change={kind}
              x={scale.x(i) + scale.cw * 0.2}
              y={base - h}
              width={Math.max(0.5, scale.cw * 0.6)}
              height={h}
              rx={1}
              className={kind === "colour" ? "tf-rp-colour" : "tf-rp-size"}
              {...tipHandlers(pressWords(c, economics), onTip)}
            />
          );
        }
        if (!c.changes && !c.sizes) return null;
        const h = c.changes ? 4 + ((SETUP_H - 6) * c.changes) / most : 4;
        return (
          <rect
            key={c.start}
            data-change={c.changes ? "colour" : "size"}
            data-count={c.changes}
            x={scale.x(i) + scale.cw * 0.2}
            y={base - h}
            width={Math.max(0.5, scale.cw * 0.6)}
            height={h}
            rx={1}
            className={c.changes ? "tf-rp-colour" : "tf-rp-size"}
            {...tipHandlers(pressWords(c, economics), onTip)}
          />
        );
      })}
      <line
        x1={scale.x0}
        x2={scale.width}
        y1={base + 0.5}
        y2={base + 0.5}
        className="tf-rp-base"
      />
    </g>
  );
}

export const ROW_H = 9;
export const GROUP_H = 12;
export const FAMILY_GAP = 4;

export interface RowLayout {
  row: StockRow;
  y: number;
  h: number;
}

export function layoutRows(
  rows: StockRow[],
  top: number,
): { rows: RowLayout[]; bottom: number } {
  let y = top;
  let last: string | null | undefined;
  const out: RowLayout[] = [];
  rows.forEach((row, i) => {
    if (i > 0 && row.family !== last) y += FAMILY_GAP;
    last = row.family;
    const h = row.group ? GROUP_H : ROW_H;
    out.push({ row, y, h });
    y += h;
  });
  return { rows: out, bottom: y };
}

export function StockStrip(props: {
  cells: DayCell[];
  scale: DayScale;
  rows: RowLayout[];
  products: Product[];
  onTip: Tip;
  onToggle: (family: string) => void;
}) {
  const { cells, scale, rows, products, onTip, onToggle } = props;
  return (
    <g className="tf-rp-stock">
      {rows.map(({ row, y, h }) => (
        <g key={row.key} data-row={row.label}>
          <text
            x={row.group ? 0 : 0}
            y={y + h - 1.5}
            className={
              row.group ? "tf-rp-lab tf-rp-group" : "tf-rp-lab tf-rp-sku"
            }
            onClick={row.group ? () => onToggle(row.family ?? "") : undefined}
          >
            {row.group ? `${row.open ? "▾" : "▸"} ${row.label}` : row.label}
          </text>
          {cells.map((c, i) => {
            const level = cellLevel(c, row, products);
            return (
              <rect
                key={c.start}
                data-level={level}
                x={scale.x(i) + 0.5}
                y={y}
                width={Math.max(0.5, scale.cw - 1)}
                height={h - 1}
                fill={LEVEL_FILL[level]}
                {...tipHandlers(stockWords(c, row, products), onTip)}
              />
            );
          })}
        </g>
      ))}
    </g>
  );
}

// ---------- width ----------

export function useWidth<T extends HTMLElement>(): [
  React.RefObject<T>,
  number,
] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

// ---------- one lane's strips with frame, ticks, tooltip and table ----------

function ticks(cells: DayCell[]): number[] {
  const last = cells[cells.length - 1]?.end ?? 0;
  const step = last > 180 ? 30 : last > 90 ? 20 : 10;
  const out = [1];
  for (let t = step; t <= last; t += step) out.push(t);
  if (out[out.length - 1] !== last && last - out[out.length - 1] >= step / 2)
    out.push(last);
  return out;
}

export function laneLabel(
  label: string,
  run: ExampleRun,
  products: Product[],
): string {
  return (
    `${label}: changes colour ${colourChanges(run)} times. ` +
    `${outProductDays(run, products)} product-days out of stock.`
  );
}

export function ReplayTable({
  run,
  products,
  economics,
}: {
  run: ExampleRun;
  products: Product[];
  economics?: Economics;
}) {
  return (
    <div className="tf-rp-tablewrap">
      <table className="tf-rp-table">
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col">Press makes</th>
            <th scope="col">Changeover</th>
            <th scope="col">Products out</th>
            <th scope="col">Profit today</th>
            <th scope="col">Profit so far</th>
          </tr>
        </thead>
        <tbody>
          {run.days.map((d) => {
            const f = dayFacts(run, d.day, products, economics);
            if (!f) return null;
            return (
              <tr key={d.day}>
                <td>{d.day}</td>
                <td>{f.makes}</td>
                <td>{f.change}</td>
                <td>{f.out.length ? f.out.join(", ") : "None"}</td>
                <td>{money(f.profitToday)}</td>
                <td>{money(f.profitSoFar)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function StripStack(props: {
  label: string;
  run: ExampleRun;
  products: Product[];
  families: string[] | null | undefined;
  economics?: Economics;
  day: number;
  onDay?: (day: number) => void;
  footer?: ReactNode;
}) {
  const { label, run, products, economics, day, onDay } = props;
  const families = useMemo(
    () => props.families ?? [...new Set(products.map((p) => p.family ?? ""))],
    [props.families, products],
  );
  const [box, width] = useWidth<HTMLDivElement>();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [table, setTable] = useState(false);
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(
    null,
  );

  const cells = useMemo(() => dayBuckets(run.days), [run.days]);
  const rows = useMemo(
    () => stockRows(products, families, open),
    [products, families, open],
  );
  const grouped = rows.some((r) => r.group);
  const longest = Math.max(
    5,
    ...rows.map((r) => r.label.length + (r.group ? 2 : 0)),
  );
  const x0 = Math.min(120, Math.max(50, Math.round(longest * 6.4 + 10)));
  const scale = makeScale(cells.length, width, x0);
  const pressY = 4;
  const setupY = pressY + PRESS_H + 6;
  const stockTop = setupY + SETUP_H + 12;
  const layout = layoutRows(rows, stockTop);
  const height = layout.bottom + 22;
  const sel = cellIndexOfDay(cells, day);

  const toggle = (f: string) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(f)) next.delete(f);
      else next.add(f);
      return next;
    });

  const onTip: Tip = (text, x = 0, y = 0) => {
    if (!text || !box.current) return setTip(null);
    const r = box.current.getBoundingClientRect();
    setTip({ text, x: x - r.left, y: y - r.top });
  };

  const click = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!onDay) return;
    const r = e.currentTarget.getBoundingClientRect();
    const i = scale.indexAt(e.clientX - r.left);
    if (i >= 0 && i < cells.length) onDay(cells[i].start);
  };

  return (
    <div className="tf-rp-stack">
      <div className="tf-rp-tools">
        <button
          type="button"
          className="tf-rp-btn tf-rp-small"
          aria-pressed={table}
          onClick={() => setTable((t) => !t)}
        >
          {table ? "Show as chart" : "Show as table"}
        </button>
      </div>
      {table && (
        <ReplayTable run={run} products={products} economics={economics} />
      )}
      <div
        ref={box}
        className="tf-rp-chart"
        hidden={table}
        onPointerLeave={() => setTip(null)}
      >
        {width > 0 && (
          <svg
            role="img"
            aria-label={laneLabel(label, run, products)}
            width={width}
            height={height}
            onClick={click}
            className={onDay ? "tf-rp-svg tf-rp-click" : "tf-rp-svg"}
          >
            <PressStrip
              cells={cells}
              scale={scale}
              y={pressY}
              families={families}
              economics={economics}
              onTip={onTip}
            />
            <SetupStrip
              cells={cells}
              scale={scale}
              y={setupY}
              economics={economics}
              onTip={onTip}
            />
            <StockStrip
              cells={cells}
              scale={scale}
              rows={layout.rows}
              products={products}
              onTip={onTip}
              onToggle={toggle}
            />
            <rect
              className="tf-rp-frame"
              data-frame={cells[sel]?.start}
              x={scale.x(sel) - 0.75}
              y={0.75}
              width={scale.cw + 1.5}
              height={layout.bottom + 2}
              rx={2}
            />
            {ticks(cells).map((t) => {
              const i = cellIndexOfDay(cells, t);
              const c = cells[i];
              const frac =
                c.end === c.start
                  ? 0.5
                  : (t - c.start + 0.5) / (c.end - c.start + 1);
              return (
                <text
                  key={t}
                  x={scale.x(i) + frac * scale.cw}
                  y={height - 6}
                  textAnchor="middle"
                  className="tf-rp-tick"
                >
                  {t}
                </text>
              );
            })}
          </svg>
        )}
        {tip && (
          <div
            role="tooltip"
            className="tf-rp-tip"
            style={{
              left: Math.min(Math.max(0, tip.x + 12), Math.max(0, width - 240)),
              top: tip.y + 14,
            }}
          >
            {tip.text}
          </div>
        )}
      </div>
      {grouped && !table && (
        <div className="tf-rp-families">
          {rows
            .filter((r) => r.group)
            .map((r) => (
              <button
                key={r.key}
                type="button"
                className="tf-rp-btn tf-rp-small"
                aria-expanded={r.open}
                onClick={() => toggle(r.family ?? "")}
              >
                {r.open
                  ? `Hide ${r.label} products`
                  : `Show ${r.label} products`}
              </button>
            ))}
        </div>
      )}
      {props.footer}
    </div>
  );
}
