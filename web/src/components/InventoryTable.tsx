import type { InventoryKpis } from "../api";
import { formatDuration } from "./format";

/**
 * Per-stock inventory summary for a run. Hidden entirely by the caller when
 * `average_level` is empty (a floor with no stocks) — this component assumes
 * it has something to show.
 */
export function InventoryTable({ inventory }: { inventory: InventoryKpis }) {
  const stocks = Object.keys(inventory.average_level).sort();

  return (
    <div className="card">
      <div className="card-title">Inventory</div>
      <table>
        <thead>
          <tr>
            <th>stock</th>
            <th>avg level</th>
            <th>ending level</th>
            <th>orders placed</th>
            <th>stockout time</th>
          </tr>
        </thead>
        <tbody>
          {stocks.map((stock) => {
            const stockoutSeconds = inventory.stockout_seconds[stock] ?? 0;
            const hadStockout = stockoutSeconds > 0;
            return (
              <tr key={stock}>
                <td>{stock}</td>
                <td className="mono">{(inventory.average_level[stock] ?? 0).toFixed(1)}</td>
                <td className="mono">{(inventory.ending_level[stock] ?? 0).toFixed(1)}</td>
                <td className="mono">{inventory.orders_placed[stock] ?? 0}</td>
                <td className="mono" style={hadStockout ? { color: "var(--bad)", fontWeight: 600 } : undefined}>
                  {formatDuration(stockoutSeconds)}
                  {hadStockout ? " ⚠" : ""}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
