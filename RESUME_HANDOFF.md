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

## Known gap (deliberate, needs your decision)

Per-line revenue / tax **sub-account** posting (§12.2 Example B) is not
implemented: the totals are right, but the journal still credits one revenue
account and one output-tax account rather than splitting by item category and
tax-rate code. It needs a product/category → revenue account mapping and a tax
rate → sub-account mapping, neither of which exists yet. See §7 of
`CHANGES_SUMMARY.md`.

## Housekeeping done

- `spec.txt` / `scratch_spec.txt` (agent doc extractions) moved out of the repo
  root; the `.docx` remains the source of truth.
- Server-side totals now override client-supplied ones on both save routes.
