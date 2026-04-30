"""Apply CSV-driven changes to QBO transactions. Run as: python -m src.apply <csv>"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from typing import Optional

from .client import QBOClient, QBOError

FIELD_TO_REF_TYPE = {
    "account": "Account",
    "class": "Class",
    "location": "Department",
    "customer": "Customer",
    "vendor": "Vendor",
}

LINE_DETAIL_KEYS = (
    "AccountBasedExpenseLineDetail",
    "ItemBasedExpenseLineDetail",
    "JournalEntryLineDetail",
    "DepositLineDetail",
    "SalesItemLineDetail",
)


def find_line(txn: dict, line_id: str) -> dict:
    for line in txn.get("Line", []):
        if str(line.get("Id")) == str(line_id):
            return line
    raise QBOError(f"line Id {line_id} not found")


def line_detail_key(line: dict) -> str:
    for k in LINE_DETAIL_KEYS:
        if k in line:
            return k
    raise QBOError(f"unknown line detail shape: {list(line.keys())}")


def apply_change(
    client: QBOClient,
    txn_type: str,
    txn: dict,
    line_id: Optional[str],
    field: str,
    new_value: str,
    modified: set[str],
) -> None:
    """Mutate `txn` for one CSV row; record which top-level fields changed."""

    if field == "memo":
        if line_id:
            line = find_line(txn, line_id)
            line["Description"] = new_value
            modified.add("Line")
        else:
            txn["PrivateNote"] = new_value
            modified.add("PrivateNote")
        return

    if field == "location":
        ref = client.lookup_ref("Department", new_value)
        if txn_type == "JournalEntry":
            if not line_id:
                raise QBOError("JournalEntry location is per-line; provide line_id")
            line = find_line(txn, line_id)
            line["JournalEntryLineDetail"]["DepartmentRef"] = ref
            modified.add("Line")
        else:
            txn["DepartmentRef"] = ref
            modified.add("DepartmentRef")
        return

    ref_type = FIELD_TO_REF_TYPE.get(field)
    if not ref_type:
        raise QBOError(f"unknown field: {field}")
    ref = client.lookup_ref(ref_type, new_value)

    if field == "vendor" and not line_id:
        if txn_type == "Bill":
            txn["VendorRef"] = ref
            modified.add("VendorRef")
        elif txn_type == "Purchase":
            txn["EntityRef"] = {**ref, "type": "Vendor"}
            modified.add("EntityRef")
        else:
            raise QBOError(f"vendor at header not supported on {txn_type}")
        return

    if field == "customer" and not line_id:
        if txn_type == "Invoice":
            txn["CustomerRef"] = ref
            modified.add("CustomerRef")
        else:
            raise QBOError(f"customer at header not supported on {txn_type}")
        return

    if not line_id:
        raise QBOError(f"{field} requires a line_id on {txn_type}")

    line = find_line(txn, line_id)
    detail_key = line_detail_key(line)
    detail = line[detail_key]

    if field == "account":
        if detail_key in ("ItemBasedExpenseLineDetail", "SalesItemLineDetail"):
            raise QBOError(
                f"can't change account on {detail_key} (driven by Item) — change the Item instead"
            )
        detail["AccountRef"] = ref
    elif field == "class":
        detail["ClassRef"] = ref
    elif field == "customer":
        if detail_key == "JournalEntryLineDetail":
            detail["Entity"] = {"EntityRef": ref, "Type": "Customer"}
        else:
            detail["CustomerRef"] = ref
    elif field == "vendor":
        if detail_key == "JournalEntryLineDetail":
            detail["Entity"] = {"EntityRef": ref, "Type": "Vendor"}
        else:
            raise QBOError(f"vendor at line not supported for {detail_key}")
    modified.add("Line")


def build_update_body(txn: dict, modified: set[str]) -> dict:
    body = {"Id": txn["Id"], "SyncToken": txn["SyncToken"]}
    for key in modified:
        body[key] = txn[key]
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply CSV changes to QBO transactions")
    ap.add_argument("csv_path", help="Path to CSV with columns: txn_type,txn_id,line_id,field,new_value")
    ap.add_argument("--dry-run", action="store_true", help="Resolve refs and show plan, don't write")
    args = ap.parse_args()

    client = QBOClient()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with open(args.csv_path) as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row["_row_num"] = i
            grouped[(row["txn_type"].strip(), row["txn_id"].strip())].append(row)

    ok = fail = 0
    for (txn_type, txn_id), rows in grouped.items():
        try:
            txn = client.get_entity(txn_type, txn_id)
        except QBOError as e:
            print(f"FAIL fetch {txn_type}/{txn_id}: {e}", file=sys.stderr)
            fail += 1
            continue

        modified: set[str] = set()
        change_failed = False
        for row in rows:
            line_id = (row.get("line_id") or "").strip() or None
            field = row["field"].strip().lower()
            new_value = row["new_value"].strip()
            try:
                apply_change(client, txn_type, txn, line_id, field, new_value, modified)
            except QBOError as e:
                print(
                    f"FAIL row {row['_row_num']} {txn_type}/{txn_id} line={line_id} {field}: {e}",
                    file=sys.stderr,
                )
                change_failed = True
                break

        if change_failed:
            fail += 1
            continue

        if args.dry_run:
            print(f"DRY  {txn_type}/{txn_id} -> {len(rows)} change(s), fields touched: {sorted(modified)}")
            ok += 1
            continue

        body = build_update_body(txn, modified)
        try:
            updated = client.update_entity(txn_type, body)
            print(f"OK   {txn_type}/{txn_id} -> SyncToken {updated['SyncToken']}")
            ok += 1
        except QBOError as e:
            print(f"FAIL update {txn_type}/{txn_id}: {e}", file=sys.stderr)
            fail += 1

    print(f"\nDone: {ok} ok, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
