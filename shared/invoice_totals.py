"""The v3 §8 money for a document, derived from what was persisted.

Every consumer — the save route that posts the journal, the print context, and
the FBR payload mapper — asks this module rather than re-deriving the chain or
trusting the browser's copy of it. One implementation means the posted journal,
the printed invoice and what is filed with FBR can never disagree.

Sales_Purchase_Orders_Invoices_ERP_UI_Standard_v3, §6.3 (charge treatments),
§6.4 (per-charge tax-base switches) and §8 (further and withholding tax).
"""

from inventory_app.models.additional_charge import AdditionalCharge


def charge_pools(doc_type, doc_id):
    """Split a document's additional charges into the three v3 treatments.

    ``bill`` is charged to the counterparty and raises the receivable/payable;
    ``absorb`` is already inside the item values (revenue on sales, inventory
    cost on purchases); ``expense`` is a cost we bear that is never invoiced.
    Only billed charges can enter a tax base — the switches are forced off for
    the other two on save, but rows written before that rule are re-filtered
    here too.
    """
    rows = AdditionalCharge.query.filter_by(doc_type=doc_type, doc_id=doc_id).all()
    pools = {"billed": 0.0, "absorb": 0.0, "expense": 0.0,
             "st_base": 0.0, "wht_base": 0.0,
             "billed_rows": [], "expense_rows": []}
    for r in rows:
        amt = float(r.amount or 0)
        if amt <= 0:
            continue
        treatment = r.treatment if r.treatment in ("bill", "absorb", "expense") else "bill"
        if treatment == "absorb":
            pools["absorb"] += amt
        elif treatment == "expense":
            pools["expense"] += amt
            pools["expense_rows"].append(r)
        else:
            pools["billed"] += amt
            pools["billed_rows"].append(r)
            if r.st_taxable:
                pools["st_base"] += amt
            if r.wht_taxable:
                pools["wht_base"] += amt
    for k in ("billed", "absorb", "expense", "st_base", "wht_base"):
        pools[k] = round(pools[k], 2)
    return pools


def sales_totals(inv):
    """The full §8 chain for a sales invoice.

    Absorbed charges are inside the goods value, so they enter every base.
    Further tax is a percentage of the sales-tax base only — never of the sales
    tax itself, and never of a charge left out of that base. Withholding is
    deducted at source and is never compounded onto either tax.
    """
    pools = charge_pools("SI", inv.id)
    subtotal = round(float(inv.subtotal or 0), 2)
    discount = round(float(inv.total_discount or 0), 2)
    effective_subtotal = round(subtotal + pools["absorb"], 2)
    sales_tax_base = round(effective_subtotal - discount + pools["st_base"], 2)
    wht_base = round(effective_subtotal - discount + pools["wht_base"], 2)
    # Combined-scope tax is fully determined by the base, so re-derive it.
    # Per-line tax depends on each row's own rate; the summed line figure the
    # form sent is the only source for that, so it is taken as given.
    if (inv.tax_mode or "general") == "general":
        sales_tax = round(sales_tax_base * float(inv.global_sales_tax_pct or 0) / 100, 2)
    else:
        sales_tax = round(float(inv.total_tax or 0), 2)
    further_tax = round(sales_tax_base * float(inv.further_tax_pct or 0) / 100, 2) \
        if inv.apply_further_tax else 0.0
    wht = round(wht_base * float(inv.withholding_tax_pct or 0) / 100, 2) \
        if inv.apply_withholding_tax else 0.0
    net_receivable = round(effective_subtotal - discount + pools["billed"]
                           + sales_tax + further_tax - wht, 2)
    return {
        "pools": pools,
        "subtotal": subtotal,
        "discount": discount,
        "effective_subtotal": effective_subtotal,
        "sales_tax_base": sales_tax_base,
        "sales_tax": sales_tax,
        "further_tax": further_tax,
        "wht_base": wht_base,
        "wht": wht,
        "billed": pools["billed"],
        "net_receivable": net_receivable,
    }
