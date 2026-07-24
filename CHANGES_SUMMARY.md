# v3 ERP Standard — correction & completion pass

Reference: `Sales_Purchase_Orders_Invoices_ERP_UI_Standard_v3.docx`.

`CHANGELOG.md` records the first implementation pass. This file records the
review-and-correct pass over it: what was wrong, what was finished, and how each
claim was verified. **31/31 automated checks pass**, including both worked
examples from the document reproduced end-to-end through the real save routes.

---

## 1. The calculation contract

The document's §8 chain, now implemented identically in all four forms and on
the server:

```
effectiveSubtotal = subtotal + Σ(absorbed charges)
salesTaxBase      = effectiveSubtotal − discount + Σ(billed charge in ST base)
salesTax          = rate × salesTaxBase            (combined) | Σ per-line
furtherTax        = rate × salesTaxBase            ← never on salesTax; sales only
whtBase           = effectiveSubtotal − discount + Σ(billed charge in WHT base)
withholding       = rate × whtBase                 ← never compounded
netPayable        = effectiveSubtotal − discount + Σ(billed) + salesTax
                    + furtherTax − withholding
```

### Bugs found and fixed

| Where | Was | Now |
|---|---|---|
| Purchase invoice `recalc()` | `furtherTax = (taxBase + salesTax) × pct` — compounded on sales tax, **and** further tax does not exist on purchases at all (§8) | Further tax removed from the purchase side entirely; pinned to 0 server-side too |
| Purchase invoice `recalc()` | Referenced `furtherTaxRow`, `summaryExpensesTotal`, `summaryCommission`… — elements the rewritten panel no longer contains, so the function threw on every keystroke | Rewritten against the actual panel |
| Purchase invoice template | `{% if invoice_charges %}try{charges=…}{% endif %}` — a `try` with no `catch`. Any invoice **with charges** was a JS syntax error, killing the entire script block | `try/catch` closed |
| Sales order `recalc()` | `fTax = netTotal × pct` then `wht = netTotal × pct` — both compounded on charges *and* on each other | Rewritten to the contract above |
| Sales order charges | Only legacy `taxable` / `tax_base`; no treatment, so absorb/expense charges were billed to the customer | Full treatment + independent ST/WHT switches |
| Sales invoice `collectData()` | Read `summaryChargesTotal`, an element that does not exist → `TypeError` on **every save** | Reads the recalc's charge pools |
| Sales invoice `collectData()` | Sent withholding as a negative (scraped from a `-1,353.00` label) | `Math.abs` |
| Sales invoice edit-form | Charge loader emitted only legacy fields → treatment silently reset to "bill" on reopen | Emits treatment/ST/WHT/`_display` |

## 2. General ledger (§12)

`invoicing_app/routes/invoices.py` posted a plug — `revenue = net − tax −
further + wht` — which balanced arithmetically but buried the discount and every
charge inside revenue, and credited further tax to Output Sales Tax.

Now posts per §12:

```
Dr Receivable          net receivable
Dr WHT Receivable      1-01-05-02-0001   (asset — §12: never an expense)
Dr Discounts Allowed   4-02-02-01-0001   (contra-revenue, not netted)
   Cr Revenue                            subtotal + absorbed
   Cr <each billed charge's own ledger>
   Cr Output Sales Tax  2-01-03-01-0001
   Cr Further Tax Payable 2-01-03-04-0001 (its own code, per §12)
Dr <expense charge ledger> / Cr Accrued   for each expense-only charge
```

Purchases mirror it: input tax is a **receivable**, withholding a **payable**,
absorbed charges capitalise into inventory (landed cost).

**Client totals are no longer trusted.** Both save routes re-derive the money
from the persisted rows before posting, and store the derived figure. The
browser still computes the same numbers for display, but a posted journal never
depends on a client-supplied value.

## 3. Verified against the document's own worked examples

| Check | Result |
|---|---|
| §12.1 Example A (sales, further + withholding) | Every figure matches: base 65,100 · tax 11,718 · further 1,953 · WHT 1,353 · net 79,418 · journal 91,671 = 91,671 |
| §12.3 Example C (purchase from PO, absorbed freight) | Inventory 51,000 · input tax 9,180 · AP 58,140 · WHT payable 2,040 |
| Orders never post (§12) | 0 SO and 0 PO journal entries |
| Further tax on purchases (§8) | Pinned off |

## 4. §13 checklist items completed

- Every charge itemised in the totals panel below the sales-tax line, tagged
  with its scope and which tax bases it entered.
- ST/WHT switches disabled and forced off for absorb/expense charges — in the
  UI, on save, and when reading old rows.
- "Per line → Combined seeds from the rollup, never resets to zero" — was
  implemented only on the sales invoice; ported to the sales order, purchase
  order and purchase invoice (including the purchase carriage columns).
- Per-line Add. charges column with pro-rata split and residual on the last line.

## 5. FBR integration

`fbr_app/services/fbr_mapper.py` was blind to the new model — it derived totals
from `global_delivery` / `global_installation`, so every additional charge, the
further tax and the withholding were missing from what would be filed.

Now reads the same shared totals module the journal posts from, and adds
`furtherTax`, `withholdingTax`, `totalOtherCharges` and an itemised
`otherCharges[]`. Product `hSCode` continues to flow from `inv_products.hs_code`.

## 6. New / changed files

**New**
- `shared/invoice_totals.py` — the single §8 implementation. The save route, the
  print context and the FBR mapper all call it, so the journal, the printed
  invoice and the FBR filing cannot disagree.

**Schema**
- `inv_purchase_invoices.apply_withholding_tax` — added to the model and to
  `app.py`'s idempotent migration list.

**Corrected**
- `invoicing_app/routes/` — `invoices.py`, `purchase_invoice.py`
- `invoicing_app/templates/` — `invoices/`, `sales/`, `purchases/`,
  `purchase_invoice/` `form_inv.html`
- `fbr_app/services/fbr_mapper.py`

## 7. §4 — order-to-invoice flow

This was missing entirely and is now built. Previously the picker displayed
"Invoiced 0.00" as a hardcoded literal and treated the whole ordered quantity
as the balance every time, and nothing was written back on posting — so the
same order could be invoiced over and over with nothing to stop it.

**New:** `shared/order_linkage.py` — balances, picker payload, over-invoice
control and write-back, shared by both sides.

**Schema:** `invoiced_qty` on both order-item tables, `fulfilment_status` on
both order tables, and `source_order_id` / `source_order_item_id` /
`source_order_number` on both invoice-item tables. The link lives on the
**line**, not the header, because one invoice can draw on several orders.

| §4 requirement | Status |
|---|---|
| Picker shows order value, uninvoiced value, status badge | Done |
| Fully-invoiced orders greyed and unselectable | Done |
| Per-line Ordered / Invoiced / Balance, editable qty capped at balance | Done |
| Multi-select across orders into one invoice | Done (purchase side too — it was single-select) |
| Per-line discounts and tax codes copy with the line | Done |
| Order-level combined pools copy pro-rata to the loaded share | Done |
| Order ref column, shown only on order-sourced invoices | Done |
| Posting writes back: qty rises, Open → Partially → Fully | Done |
| Over-invoicing blocked at save, per admin tolerance (§11.2) | Done |
| Un-posting restores balances and reopens the order | Done |

Verified by a 23-check lifecycle test: order of 4 → invoice 1 (partial) →
attempt 5 (refused, nothing written) → invoice 3 (fully, unselectable) →
unapprove (balance restored, reopened to partial). Purchase side checked
separately with the same result.

**Credit notes** (§4.4's "posting a credit note restores the balances") are not
yet wired to the release path — the release currently runs on unapprove.
Deleting a draft correctly restores nothing, because a draft never consumed
balance: write-back happens at posting, not at save.

## 8. Not done — needs a decision

**Per-line revenue and tax sub-accounts (§12.2, Example B).** The document says
Per-line mode "can post multiple lines against the same natural account type" —
a different revenue account per item category, and a different Sales Tax Payable
sub-account per rate code. The *totals* for Example B are correct (5,646.32),
but the posting still credits one revenue account and one output-tax account.

Delivering it needs a mapping that does not exist yet: product/category →
revenue account, and tax rate → tax sub-account. That is a data-model decision
(where the mapping lives, who maintains it), so it is flagged rather than
guessed at.
