# RESUME HANDOFF — v3 ERP Standard implementation

**Status: complete.** The earlier pause (spend limit killed six parallel agents
mid-edit) is resolved; everything on the old checklist is done and verified. See
`CHANGES_SUMMARY.md` for what was corrected and how it was checked, and
`CHANGELOG.md` for the first implementation pass.

## Verified

31/31 automated checks pass, run against a live app through the real routes:

- All four forms + admin Invoice Settings render (HTTP 200).
- §12.1 Example A (sales invoice, further + withholding) reproduces every figure
  in the document, and the journal balances at 91,671.00.
- §12.3 Example C (purchase invoice from a PO, absorbed freight) reproduces
  Inventory 51,000 / input tax 9,180 / AP 58,140 / WHT payable 2,040.
- Orders post no journal entries (§12).
- Purchase invoices carry no further tax (§8).
- The FBR payload agrees with the posted journal.

The verification script lives in the session scratchpad (`verify_v3.py`). It is
not in the repo because it seeds stock and posts real vouchers — worth
promoting to `tests/` if you want it in CI.

## §12.2 per-line sub-accounts (implemented)

Settings → Invoicing carries the two maps: product category → revenue account,
and sales-tax rate → Output Sales Tax sub-account. Approval posts a credit per
mapped category and per mapped rate; with no mappings it still posts exactly
one revenue credit and one output-tax credit.

The tax split follows the tax each rate actually generated, including discounts,
Combined billed charges flagged into the ST base and absorbed charges — Example
B posts 335.15 / 501.17, not the value-weighted 346.06 / 490.26. The amount
posted is still apportioned exactly, so the journal always foots to the invoice.

Mappings are validated server-side: an account from another company, a header or
inactive account, or the wrong type is refused. Covered by
`tests/unit/test_invoice_splits.py` (22), `tests/e2e/test_invoice_subaccounts.py`
(4, real routes) and `tests/e2e/test_invoice_line_tax.py` (2, browser).

## Housekeeping done

- `spec.txt` / `scratch_spec.txt` (agent doc extractions) moved out of the repo
  root; the `.docx` remains the source of truth.
- Server-side totals now override client-supplied ones on both save routes.
