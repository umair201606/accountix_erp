"""Remove the three faulty July-2026 demo records from Default Company (id 1).

    python tools/remove_faulty_demo_records.py            # dry run (rolls back)
    python tools/remove_faulty_demo_records.py --apply    # commits

Uses DATABASE_URL like the app. Found on the live database on 15 Oct 2026
(Books Integrity: Default Company 9/11; every other company 11/11); tested
on a copy of it, after which every company passes 11/11:

1. journal entry BRV-00001: unposted, both lines debits, its voucher is gone
2. debit note DN-202607-0001: "approved" with no stock movement and no journal
3. purchase invoice PINV-202607-0001: Inventory debited 105,600 while its
   stock layers hold 92,250 (the old discount-sign bug); nothing drew on it

Every record is checked to be exactly what was found before anything is
deleted; any surprise aborts the whole transaction. Raw SQL against columns
that exist at every schema revision, so it runs on the deployed (0007) schema
and on a migrated one alike. Report totals (gl_daily_balances) and on-hand
quantities for company 1 are rebuilt afterwards.
"""
import os
import sys
import sqlalchemy as sa

APPLY = "--apply" in sys.argv
CID = 1
url = os.environ.get("DATABASE_URL")
if not url:
    raise SystemExit("Set DATABASE_URL to the database to clean.")
if url.startswith("postgres://"):
    url = "postgresql://" + url[len("postgres://"):]
eng = sa.create_engine(url)


def one(conn, sql, **k):
    return conn.execute(sa.text(sql), k).fetchall()


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"ABORT: {msg}")


with eng.connect() as c:
    tx = c.begin()
    comp = one(c, "select name from companies where id = :c", c=CID)
    expect(comp and comp[0][0] == "Default Company", f"company {CID} is {comp}")

    # 1 ── orphan BRV journal
    je1 = one(c, "select id, voucher_type, voucher_number, is_posted, voucher_id from journal_entries "
                 "where company_id = :c and voucher_type = 'BRV' and voucher_number = 'BRV-00001'", c=CID)
    expect(len(je1) == 1 and je1[0].is_posted is False, f"BRV-00001 journal: {je1}")
    expect(not one(c, "select 1 from accounting_vouchers where id = :v", v=je1[0].voucher_id),
           "BRV-00001's voucher exists after all")
    je1_id = je1[0].id

    # 2 ── debit note
    pr = one(c, "select id, original_invoice_id, status from inv_purchase_returns "
                "where company_id = :c and return_number = 'DN-202607-0001'", c=CID)
    expect(len(pr) == 1 and pr[0].status == "approved", f"DN-202607-0001: {pr}")
    pr_id = pr[0].id
    expect(not one(c, "select 1 from journal_entries where voucher_type = 'PR' and voucher_id = :i", i=pr_id),
           "DN-202607-0001 has a journal")
    expect(not one(c, "select 1 from stock_ledger where voucher_type = 'PRV' and voucher_id = :i", i=pr_id),
           "DN-202607-0001 has stock rows")

    # 3 ── purchase invoice
    pi = one(c, "select id, status, purchase_order_id from inv_purchase_invoices "
                "where company_id = :c and invoice_number = 'PINV-202607-0001'", c=CID)
    expect(len(pi) == 1 and pi[0].status == "approved" and pi[0].purchase_order_id is None, f"PI: {pi}")
    pi_id = pi[0].id
    expect(pr[0].original_invoice_id == pi_id, "the debit note is not against this invoice")
    others = one(c, "select return_number from inv_purchase_returns where original_invoice_id = :i "
                    "and id <> :r", i=pi_id, r=pr_id)
    expect(not others, f"other returns against the PI: {others}")
    pi_je = one(c, "select id, is_posted from journal_entries where voucher_type = 'PI' and voucher_id = :i "
                   "and company_id = :c", i=pi_id, c=CID)
    expect(len(pi_je) == 1, f"PI journals: {pi_je}")
    rows = one(c, "select id, product_id, transaction_type, total_cost from stock_ledger "
                  "where voucher_type = 'PI' and voucher_id = :i and company_id = :c", i=pi_id, c=CID)
    expect(len(rows) == 2 and all(r.transaction_type == "IN" for r in rows), f"PI stock rows: {rows}")
    row_ids = [r.id for r in rows]
    prods = sorted({r.product_id for r in rows})
    layers = [r.id for r in one(c, "select id from stock_layers where source_ledger_id = any(:ids)",
                                ids=row_ids)] if eng.dialect.name == "postgresql" else \
        [r.id for r in one(c, f"select id from stock_layers where source_ledger_id in ({','.join(map(str, row_ids))})")]
    if layers:
        used = one(c, f"select count(*) from layer_consumptions where layer_id in ({','.join(map(str, layers))})")
        expect(used[0][0] == 0, "stock from the PI was issued")
    later = one(c, f"select count(*) from stock_ledger where product_id in ({','.join(map(str, prods))}) "
                   f"and not (voucher_type = 'PI' and voucher_id = :i)", i=pi_id)
    expect(later[0][0] == 0, "the PI's products have other movements")
    expect(not one(c, "select 1 from payment_allocations where doc_type = 'PI' and doc_id = :i", i=pi_id),
           "payments are assigned to the PI")

    print("found: BRV journal", je1_id, "| DN", pr_id, "| PI", pi_id, "journal", pi_je[0].id,
          "stock rows", row_ids, "layers", layers, "products", prods)

    # ── delete
    jids = [je1_id, pi_je[0].id]
    n = {}
    n["journal_lines"] = c.execute(sa.text(
        f"delete from journal_lines where journal_entry_id in ({jids[0]}, {jids[1]})")).rowcount
    n["journal_entries"] = c.execute(sa.text(
        f"delete from journal_entries where id in ({jids[0]}, {jids[1]}) and company_id = :c"), {"c": CID}).rowcount
    n["pr_items"] = c.execute(sa.text("delete from inv_purchase_return_items where return_id = :i"),
                              {"i": pr_id}).rowcount
    n["pr"] = c.execute(sa.text("delete from inv_purchase_returns where id = :i and company_id = :c"),
                        {"i": pr_id, "c": CID}).rowcount
    if layers:
        n["stock_layers"] = c.execute(sa.text(
            f"delete from stock_layers where id in ({','.join(map(str, layers))})")).rowcount
    n["stock_ledger"] = c.execute(sa.text(
        f"delete from stock_ledger where id in ({','.join(map(str, row_ids))})")).rowcount
    n["stock_movements"] = c.execute(sa.text(
        "delete from inv_stock_movements where reference_type = 'purchase_invoice' and reference_id = :i "
        "and company_id = :c"), {"i": pi_id, "c": CID}).rowcount
    n["charges"] = c.execute(sa.text(
        "delete from additional_charges where doc_type = 'PI' and doc_id = :i"), {"i": pi_id}).rowcount \
        if "additional_charges" in sa.inspect(c).get_table_names() else 0
    n["pi_items"] = c.execute(sa.text("delete from inv_purchase_invoice_items where invoice_id = :i"),
                              {"i": pi_id}).rowcount
    n["pi"] = c.execute(sa.text("delete from inv_purchase_invoices where id = :i and company_id = :c"),
                        {"i": pi_id, "c": CID}).rowcount
    for p in prods:
        c.execute(sa.text(
            "update inv_products set current_stock = (select coalesce(sum(case when s.transaction_type = 'IN' "
            "then s.quantity else -s.quantity end), 0) from stock_ledger s where s.product_id = :p) "
            "where id = :p"), {"p": p})

    # report totals for company 1, from its remaining posted lines
    day = "CAST(e.entry_date AS DATE)" if eng.dialect.name == "postgresql" else "date(e.entry_date)"
    comp_expr = "COALESCE(jl.company_id, e.company_id)"
    c.execute(sa.text("delete from gl_daily_balances where company_id = :c"), {"c": CID})
    c.execute(sa.text(
        "INSERT INTO gl_daily_balances (company_id, account_id, label_id, day, debit, credit, line_count) "
        f"SELECT {comp_expr}, jl.account_id, COALESCE(jl.label_id, 0), {day}, "
        "COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0), COUNT(*) "
        "FROM journal_lines jl JOIN journal_entries e ON jl.journal_entry_id = e.id "
        f"WHERE e.is_posted = :t AND {comp_expr} = :c "
        f"GROUP BY {comp_expr}, jl.account_id, COALESCE(jl.label_id, 0), {day}"), {"t": True, "c": CID})
    print("deleted:", n)
    expect(n["journal_entries"] == 2 and n["pr"] == 1 and n["pi"] == 1, "unexpected delete counts")

    left = one(c, "select count(*) from journal_entries where company_id = :c", c=CID)[0][0]
    inv_gl = one(c, "select coalesce(sum(debit) - sum(credit), 0) from gl_daily_balances g "
                    "join chart_of_accounts a on a.id = g.account_id where g.company_id = :c "
                    "and a.code like '1-01-04%'", c=CID)[0][0]
    stock = one(c, "select coalesce(sum(case when transaction_type = 'IN' then total_cost else -total_cost end), 0) "
                   "from stock_ledger where company_id = :c", c=CID)[0][0]
    print(f"company {CID} after: journals {left}, inventory GL {inv_gl}, stock value {stock}")
    if APPLY:
        tx.commit()
        print("COMMITTED")
    else:
        tx.rollback()
        print("dry run - rolled back (add --apply to commit)")
