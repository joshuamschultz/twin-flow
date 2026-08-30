import type { FulfillmentKpis } from "../api";
import { formatDuration } from "./format";

/** Orders & deliveries summary for a run — fill rate, on-time %, and lateness. */
export function FulfillmentCards({ fulfillment }: { fulfillment: FulfillmentKpis }) {
  const earlyOrLate = fulfillment.avg_delivery_lateness <= 0 ? "early" : "late";

  return (
    <div className="card">
      <div className="card-title">Orders &amp; Deliveries</div>
      <div className="card-grid">
        <div>
          <div className="stat-value" style={{ fontSize: 22 }}>
            {(fulfillment.fill_rate * 100).toFixed(1)}
            <span className="unit">% fill rate</span>
          </div>
          <div className="footnote">
            {fulfillment.delivered} delivered · {fulfillment.backordered} backordered · {fulfillment.orders}{" "}
            total
          </div>
        </div>
        <div>
          <div className="stat-value" style={{ fontSize: 22 }}>
            {fulfillment.on_time_delivery_pct.toFixed(1)}
            <span className="unit">% on time</span>
          </div>
          <div className="footnote">{fulfillment.on_time_deliveries} of delivered orders</div>
        </div>
        <div>
          <div className="stat-value" style={{ fontSize: 22 }}>
            {formatDuration(fulfillment.avg_delivery_lateness)}
          </div>
          <div className="footnote">avg delivery lateness, {earlyOrLate} on average</div>
        </div>
      </div>
    </div>
  );
}
