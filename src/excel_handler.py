from pathlib import Path

import openpyxl


def load_orders(path: str) -> list[dict]:
    """Read all rows from the Excel file and return as a list of dicts.

    The original 1-based row index (including header row) is stored under '_row_index'.
    Rows where product_url is empty are skipped.
    """
    wb = openpyxl.load_workbook(path)
    ws = wb.active

    headers = [cell.value for cell in ws[1]]
    orders = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        record = {headers[i]: row[i] for i in range(len(headers)) if i < len(row)}
        record["_row_index"] = row_idx
        # Skip empty rows
        if not record.get("product_url"):
            continue
        orders.append(record)

    wb.close()
    return orders


def update_order_fields(path: str, row_index: int, fields: dict) -> None:
    """Write arbitrary column values back to the given row."""
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    headers = [cell.value for cell in ws[1]]
    col_map = {name: idx + 1 for idx, name in enumerate(headers)}
    for field_name, value in fields.items():
        if field_name in col_map:
            ws.cell(row=row_index, column=col_map[field_name]).value = value
    wb.save(path)
    wb.close()


def update_order_status(
    path: str,
    row_index: int,
    status: str,
    order_id: str = "",
    note: str = "",
) -> None:
    """Write status back to the given row. Adds status/order_id columns if missing."""
    wb = openpyxl.load_workbook(path)
    ws = wb.active

    headers = [cell.value for cell in ws[1]]
    col_map = {name: idx + 1 for idx, name in enumerate(headers)}

    # Add status column if it doesn't exist
    if "status" not in col_map:
        next_col = len(headers) + 1
        ws.cell(row=1, column=next_col).value = "status"
        col_map["status"] = next_col

    # Add order_id column if it doesn't exist
    if "order_id" not in col_map:
        next_col = max(col_map.values()) + 1
        ws.cell(row=1, column=next_col).value = "order_id"
        col_map["order_id"] = next_col

    ws.cell(row=row_index, column=col_map["status"]).value = status
    if order_id and "order_id" in col_map:
        ws.cell(row=row_index, column=col_map["order_id"]).value = order_id

    wb.save(path)
    wb.close()
