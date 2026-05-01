"""Look up QBO transaction internal Ids by DocNumber, date, or entity name.

Run as:
  python -m src.find_id Bill --doc-number 12345
  python -m src.find_id JournalEntry --doc-number JE-1042
  python -m src.find_id Invoice --customer "Acme Corp" --date 2026-04-15
  python -m src.find_id Purchase --date-range 2026-04-01 2026-04-30
"""
from __future__ import annotations

import argparse
import sys

from .client import QBOClient, QBOError

SUPPORTED_TYPES = ("Bill", "Invoice", "JournalEntry", "Purchase", "Deposit", "CreditMemo", "VendorCredit")


def _esc(value: str) -> str:
    return value.replace("'", "\\'")


def build_query(txn_type: str, args: argparse.Namespace) -> str:
    where: list[str] = []

    if args.doc_number:
        where.append(f"DocNumber = '{_esc(args.doc_number)}'")

    if args.date:
        where.append(f"TxnDate = '{args.date}'")

    if args.date_range:
        start, end = args.date_range
        where.append(f"TxnDate >= '{start}' AND TxnDate <= '{end}'")

    if args.customer:
        if txn_type not in ("Invoice", "CreditMemo"):
            print(f"--customer only valid for Invoice/CreditMemo, not {txn_type}", file=sys.stderr)
            sys.exit(2)
        # need to resolve customer name -> Id first
        client_for_lookup = QBOClient()
        ref = client_for_lookup.lookup_ref("Customer", args.customer)
        where.append(f"CustomerRef = '{ref['value']}'")

    if args.vendor:
        if txn_type not in ("Bill", "Purchase", "VendorCredit"):
            print(f"--vendor only valid for Bill/Purchase/VendorCredit, not {txn_type}", file=sys.stderr)
            sys.exit(2)
        client_for_lookup = QBOClient()
        ref = client_for_lookup.lookup_ref("Vendor", args.vendor)
        if txn_type == "Purchase":
            # Purchase.EntityRef is not queryable via SQL; fall back to client-side filter.
            args._purchase_vendor_id = ref["value"]
        else:
            where.append(f"VendorRef = '{ref['value']}'")

    if args.amount is not None:
        where.append(f"TotalAmt = {args.amount}")

    if not where:
        print("Provide at least one filter (--doc-number, --date, --date-range, --customer, --vendor, --amount)", file=sys.stderr)
        sys.exit(2)

    sql = f"SELECT * FROM {txn_type} WHERE {' AND '.join(where)}"
    if args.limit:
        sql += f" MAXRESULTS {args.limit}"
    return sql


def summarize_row(txn_type: str, row: dict) -> dict:
    out = {
        "Id": row.get("Id", ""),
        "DocNumber": row.get("DocNumber", ""),
        "TxnDate": row.get("TxnDate", ""),
        "TotalAmt": row.get("TotalAmt", ""),
    }
    if txn_type in ("Invoice", "CreditMemo"):
        out["Customer"] = (row.get("CustomerRef") or {}).get("name", "")
    if txn_type in ("Bill", "VendorCredit"):
        out["Vendor"] = (row.get("VendorRef") or {}).get("name", "")
    if txn_type == "Purchase":
        out["Entity"] = (row.get("EntityRef") or {}).get("name", "")
    if txn_type == "JournalEntry":
        out["PrivateNote"] = (row.get("PrivateNote") or "")[:60]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Find QBO transaction internal Ids by metadata")
    ap.add_argument("txn_type", choices=SUPPORTED_TYPES, help="QBO entity type")
    ap.add_argument("--doc-number", help="Match the displayed DocNumber (e.g. 'JE-1042')")
    ap.add_argument("--date", help="Match TxnDate exactly (YYYY-MM-DD)")
    ap.add_argument("--date-range", nargs=2, metavar=("START", "END"), help="TxnDate within [START, END]")
    ap.add_argument("--customer", help="Filter Invoice/CreditMemo by customer name")
    ap.add_argument("--vendor", help="Filter Bill/Purchase/VendorCredit by vendor name")
    ap.add_argument("--amount", type=float, help="Match TotalAmt exactly")
    ap.add_argument("--limit", type=int, default=50, help="Max rows (default 50, QBO cap 1000)")
    args = ap.parse_args()

    client = QBOClient()
    sql = build_query(args.txn_type, args)
    try:
        rows = client.query(sql)
    except QBOError as e:
        print(f"Query failed: {e}", file=sys.stderr)
        return 1

    purchase_vendor_id = getattr(args, "_purchase_vendor_id", None)
    if purchase_vendor_id:
        rows = [r for r in rows if (r.get("EntityRef") or {}).get("value") == purchase_vendor_id]

    if not rows:
        print("No matches.")
        return 0

    summaries = [summarize_row(args.txn_type, r) for r in rows]
    headers = list(summaries[0].keys())
    widths = {h: max(len(h), max(len(str(s[h])) for s in summaries)) for h in headers}
    print("  ".join(h.ljust(widths[h]) for h in headers))
    print("  ".join("-" * widths[h] for h in headers))
    for s in summaries:
        print("  ".join(str(s[h]).ljust(widths[h]) for h in headers))
    print(f"\n{len(rows)} match(es).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
