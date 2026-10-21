"""The v3 §8 money for a document, derived from what was persisted.

Every consumer — the save route that posts the journal, the print context, and
the FBR payload mapper — asks this module rather than re-deriving the chain or
trusting the browser's copy of it. One implementation means the posted journal,
the printed invoice and what is filed with FBR can never disagree.

Sales_Purchase_Orders_Invoices_ERP_UI_Standard_v3, §6.3 (charge treatments),
§6.4 (per-charge tax-base switches) and §8 (further and withholding tax).
"""

import json

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


def _line_value(it):
    """A line's pre-discount goods value — the figure that sums to subtotal."""
    v = float(it.total_before_discount or 0)
    if v:
        return v
    return float(it.quantity or 0) * float(it.unit_price or 0)


def _line_net(it):
    """A line's post-discount goods value — the basis the form splits on."""
    value = _line_value(it)
    net = float(it.total_after_discount or 0)
    if net or not value:
        return round(net, 2)
    discount = float(it.discount_amount or 0)
    if not discount:
        # Rows written before the amount column was populated may carry only
        # the percentage; the form's own rule is amount first, then percent.
        discount = value * float(it.discount_pct or 0) / 100
    return round(max(0.0, value - discount), 2)


def _allocate(total, buckets):
    """Split ``total`` across ``[(key, weight)]`` pro-rata, footing exactly.

    The residual lands on the last bucket, as everywhere else in the v3 rounding
    rules, so the split always adds back to the figure being posted. Weightless
    buckets share equally rather than vanishing.
    """
    total = round(float(total or 0), 2)
    buckets = [(k, float(w or 0)) for k, w in buckets]
    if not buckets or total == 0:
        return []
    basis = sum(w for _, w in buckets)
    out, allocated, n = [], 0.0, len(buckets)
    for i, (key, weight) in enumerate(buckets):
        if i == n - 1:
            share = round(total - allocated, 2)
        else:
            share = round(total * weight / basis, 2) if basis > 0 else round(total / n, 2)
            allocated = round(allocated + share, 2)
        if share:
            out.append((key, share))
    return out


def revenue_splits(inv, amount):
    """``amount`` of revenue split across per-category accounts (§12.2).

    Returns ``[(account_id_or_None, amount)]``; ``None`` means the caller's
    global revenue account. With no category mapped this is a single unmapped
    bucket, which posts exactly the one credit it always did.

    Absorbed charges are inside ``amount`` but belong to no category, so they
    ride along pro-rata with the goods they were absorbed into.
    """
    from shared.models.invoice_settings import CategoryRevenueAccount
    mapping = {m.category_id: m.account_id for m in CategoryRevenueAccount.query.all()}
    weights = {}
    for it in inv.items.all():
        product = getattr(it, "product", None)
        account_id = mapping.get(getattr(product, "category_id", None))
        weights[account_id] = weights.get(account_id, 0.0) + _line_value(it)
    # Sorted so the residual always lands on the same bucket for the same
    # invoice; dict order would otherwise depend on how the rows came back.
    ordered = sorted(weights.items(), key=lambda kv: (kv[0] is not None, kv[0] or 0))
    return _allocate(amount, ordered)


def _manual_charge_shares(chg, count):
    """A "Manual per line" split, verbatim, padded/trimmed to the line count.

    §5.1 makes the typed amounts the allocation; no residual is forced onto the
    last line. An unreadable or short list leaves the missing lines at zero,
    exactly as the form reports it, rather than inventing a split.
    """
    try:
        values = [round(float(v or 0), 2)
                  for v in json.loads(chg.manual_allocations or "[]")]
    except (TypeError, ValueError):
        values = []
    return (values + [0.0] * count)[:count]


def _charge_line_shares(chg, items, nets):
    """One charge's amount split across the document's lines (§6.2).

    Derived methods put the rounding residual on the last line so the shares
    foot to the charge; manual leaves the operator's figures untouched.
    """
    count = len(items)
    amount = round(float(chg.amount or 0), 2)
    if not count or amount <= 0:
        return [0.0] * count
    method = chg.distribution or "pro_rata_value"
    if method == "manual":
        return _manual_charge_shares(chg, count)
    if method == "pro_rata_qty":
        weights = [float(it.quantity or 0) for it in items]
    elif method == "by_weight":
        weights = [float(it.quantity or 0) *
                   float(getattr(it.product, "weight", 0) or 0)
                   for it in items]
        if sum(weights) <= 0:
            weights = nets
    else:
        weights = nets
    shares = dict(_allocate(amount, list(enumerate(weights))))
    return [shares.get(i, 0.0) for i in range(count)]


def line_tax_bases(inv):
    """Each sales-invoice line's sales-tax base, in line order (§10).

    A line's base is its post-discount net plus every charge that belongs in
    the tax base: its own Per-item shares, its slice of Combined billed
    charges flagged into the base, and absorbed charges (which are already
    part of the goods value). Expense-only charges never enter it. For the
    derived splits the bases sum to the document's taxable value, so the line
    taxes sum to its tax; a manual split leaves any unallocated gap unassigned,
    exactly as the form reports it.
    """
    items = list(inv.items.all())
    if not items:
        return []
    nets = [_line_net(it) for it in items]
    bases = list(nets)
    rows = AdditionalCharge.query.filter_by(doc_type="SI", doc_id=inv.id).all()
    for chg in rows:
        amount = round(float(chg.amount or 0), 2)
        if amount <= 0:
            continue
        treatment = (chg.treatment
                     if chg.treatment in ("bill", "absorb", "expense")
                     else "bill")
        if treatment == "expense":
            continue
        if (chg.scope or "general") == "individual":
            shares = _charge_line_shares(chg, items, nets)
        else:
            # A Combined charge is one document-level figure; for per-line
            # tax it rides pro-rata over the post-discount line values.
            split = dict(_allocate(amount, list(enumerate(nets))))
            shares = [split.get(i, 0.0) for i in range(len(items))]
        enters_base = treatment == "absorb" or bool(chg.st_taxable)
        if not enters_base:
            continue
        bases = [round(base + share, 2) for base, share in zip(bases, shares)]
    return bases


def line_tax_amounts(inv):
    """Tax each line generated, in line order; [] in Combined mode.

    The form sums these into ``total_tax``. Recomputing them here is what lets
    a rate split follow the tax each rate actually produced even when discounts
    and charges moved the bases away from the goods values.
    """
    if (inv.tax_mode or "general") == "general":
        return []
    items = list(inv.items.all())
    bases = line_tax_bases(inv)
    amounts = []
    for it, base in zip(items, bases):
        rate = float(it.sales_tax_pct or 0)
        amounts.append(round(base * rate / 100, 2) if rate > 0 else 0.0)
    return amounts


def output_tax_splits(inv, amount):
    """``amount`` of output sales tax split by rate (§12.2).

    Returns ``[(account_id_or_None, amount)]``; ``None`` means the caller's
    global Output Sales Tax account.

    In Per-line mode each rate's weight is the tax its own lines generated
    (``line_tax_amounts``), not a value-times-rate approximation: a discount or
    a charge can shift the two rates apart, and the tax return must follow the
    tax actually charged. The weights are then apportioned to the amount being
    posted, so the journal still foots to the invoice total to the paisa.
    """
    from shared.models.invoice_settings import TaxRateAccount
    mapping = {round(float(m.rate_pct or 0), 4): m.account_id
               for m in TaxRateAccount.query.all()}
    per_line = (inv.tax_mode or "general") != "general"
    items = list(inv.items.all())
    weights = {}
    if per_line:
        taxes = line_tax_amounts(inv)
        for it, tax in zip(items, taxes):
            rate = round(float(it.sales_tax_pct or 0), 4)
            if rate <= 0 or tax <= 0:
                continue
            account_id = mapping.get(rate)
            weights[account_id] = weights.get(account_id, 0.0) + tax
        if not weights or sum(weights.values()) <= 0:
            # Older rows without line tax, or a zero base: fall back to the
            # value-times-rate weights so a mapped rate still gets its share.
            for it in items:
                rate = round(float(it.sales_tax_pct or 0), 4)
                if rate <= 0:
                    continue
                account_id = mapping.get(rate)
                weights[account_id] = (weights.get(account_id, 0.0)
                                       + _line_value(it) * rate)
    else:
        # Combined mode is one document-level rate and one aggregate credit.
        rate = round(float(inv.global_sales_tax_pct or 0), 4)
        weights = {mapping.get(rate): 1.0}
    if not weights:
        rate = round(float(inv.global_sales_tax_pct or 0), 4)
        weights = {mapping.get(rate): 1.0}
    ordered = sorted(weights.items(), key=lambda kv: (kv[0] is not None, kv[0] or 0))
    return _allocate(amount, ordered)


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
