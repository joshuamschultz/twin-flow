"""Due and promised days of a production order (TF-005 review ruling C, DEV-12; REQ-155).

The one place that reads a sales line's `requested_date`, so the validator and the builder
can never disagree on an order's due day:

* a linked line is due on its `promised_date`, else its `requested_date`;
* the order is due on the EARLIEST due across all its linked lines, else its own `due_date`;
* the order is promised on the earliest `promised_date` across its linked lines.

`lines_by_key` maps `_checks.line_key(...)` to the sales line (the validator's
`Index.sales_lines` shape); a link whose line is not in it is ignored. Link order never
matters.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping

from twinflow.erp._checks import LineKey, line_key
from twinflow.erp.schema import ProductionOrder, SalesOrderLine

__all__ = ["order_due_day", "order_promised_day"]


def order_due_day(
    order: ProductionOrder, lines_by_key: Mapping[LineKey, SalesOrderLine]
) -> dt.date | None:
    """Earliest linked-line due (promised, else requested), else the order's `due_date`."""
    days = [day for day in map(_line_due, _linked(order, lines_by_key)) if day is not None]
    return min(days) if days else order.due_date


def order_promised_day(
    order: ProductionOrder, lines_by_key: Mapping[LineKey, SalesOrderLine]
) -> dt.date | None:
    """Earliest `promised_date` across the order's linked lines (None: nothing promised)."""
    days = [line.promised_date for line in _linked(order, lines_by_key) if line.promised_date]
    return min(days) if days else None


def _line_due(line: SalesOrderLine) -> dt.date | None:
    if line.promised_date is not None:
        return line.promised_date
    return line.requested_date


def _linked(
    order: ProductionOrder, lines_by_key: Mapping[LineKey, SalesOrderLine]
) -> Iterator[SalesOrderLine]:
    for link in order.sales_links:
        line = lines_by_key.get(line_key(link.order_type, link.order_nbr, link.line_nbr))
        if line is not None:
            yield line
