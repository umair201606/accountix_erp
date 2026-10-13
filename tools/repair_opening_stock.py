"""Post the missing journals for opening stock that only the stock ledger holds.

    python tools/repair_opening_stock.py              # dry run, every company
    python tools/repair_opening_stock.py --apply      # post them
    python tools/repair_opening_stock.py --apply 3    # company id 3 only

Uses DATABASE_URL like the app. For each OPENING stock-ledger row whose
product has no posted OPENING journal it posts, on the row's own date:

    Dr  the product's stock account      (value of the row)
        Cr  Opening Balance Equity

Only needed when Books Integrity reports "Inventory account = stock
valuation" failing with "opening-stock rows ... have no journal" — stock that
was brought in by the old product form / import before opening stock posted
to the ledger. A closed period refuses (reopen it, repair, close again).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    apply = "--apply" in sys.argv
    only = [int(a) for a in sys.argv[1:] if a.isdigit()]
    from app import app
    with app.app_context():
        from shared.extensions import db
        from shared.models.company import Company
        from shared.tenancy import set_current_company, unscoped
        from shared.stock_entry import unjournaled_openings, journal_opening_row
        with unscoped():
            companies = [(c.id, c.name) for c in Company.query.order_by(Company.id).all()]
        for cid, name in companies:
            if only and cid not in only:
                continue
            set_current_company(cid)
            rows = unjournaled_openings()
            total = sum(float(r.total_cost or 0) for r in rows)
            print(f"{name}: {len(rows)} opening rows without a journal, {total:,.2f}")
            if not apply or not rows:
                continue
            for r in rows:
                journal_opening_row(r)
            db.session.commit()
            print(f"  posted {len(rows)} journals")
        if not apply:
            print("Dry run — nothing posted. Re-run with --apply.")


if __name__ == "__main__":
    main()
