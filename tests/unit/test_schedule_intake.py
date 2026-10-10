import base64
import io

import openpyxl
import pytest

from twinflow.application.schedule_intake import decode_upload, preview_mapping


def _encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


def test_csv_preview_reports_every_row_issue() -> None:
    data = (
        b"id,part,qty,release,due\na,finished,2,0,100\na,missing,0,5,3\nb,finished,x,,2026-10-08\n"
    )
    headers, rows, _ = decode_upload("orders.csv", _encoded(data))
    mapped = preview_mapping(
        headers,
        rows,
        {
            "order_id": "id",
            "product": "part",
            "quantity": "qty",
            "release_second": "release",
            "due_second": "due",
        },
        {"finished"},
    )
    assert not mapped["committable"]
    assert mapped["orders"] == []
    assert {issue["row"] for issue in mapped["issues"]} == {3, 4}
    assert {issue["code"] for issue in mapped["issues"]} >= {
        "duplicate",
        "unknown_product",
        "invalid_quantity",
        "invalid_time",
        "invalid_number",
        "missing",
    }


def test_formula_and_oversize_are_rejected() -> None:
    with pytest.raises(ValueError, match="formula-like"):
        decode_upload("orders.csv", _encoded(b"id,part\n=1,finished\n"))
    with pytest.raises(ValueError, match="1048576"):
        decode_upload("orders.csv", _encoded(b"x" * 1_048_577))
    with pytest.raises(ValueError, match="Row 2"):
        decode_upload("orders.csv", _encoded(b"id,part,qty\na,finished\n"))


def test_missing_mapping_columns_and_calendar_dates_block_commit() -> None:
    headers, rows, _ = decode_upload(
        "orders.csv",
        _encoded(b"id,part,qty,release,due\na,finished,2,0,2026-10-08\n"),
    )
    wrong = preview_mapping(
        headers,
        rows,
        {
            "order_id": "id",
            "product": "part",
            "quantity": "qty",
            "release_second": "start",
            "due_second": "due",
        },
        {"finished"},
    )
    assert wrong["issues"][0]["code"] == "unknown_column"
    mapped = preview_mapping(
        headers,
        rows,
        {
            "order_id": "id",
            "product": "part",
            "quantity": "qty",
            "release_second": "release",
            "due_second": "due",
        },
        {"finished"},
    )
    assert not mapped["committable"]
    assert any(issue["field"] == "due_second" for issue in mapped["issues"])


def test_xlsx_values_are_read_and_formulas_rejected() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["id", "part", "qty", "release", "due"])
    sheet.append(["a", "finished", 2, 0, 100])
    buffer = io.BytesIO()
    workbook.save(buffer)
    headers, rows, _ = decode_upload("orders.xlsx", _encoded(buffer.getvalue()))
    assert headers == ["id", "part", "qty", "release", "due"]
    assert rows[0][2] == "2"
    sheet["C2"] = "=1+1"
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(ValueError, match="formulas"):
        decode_upload("orders.xlsx", _encoded(buffer.getvalue()))
