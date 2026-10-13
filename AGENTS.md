# Development Policies

## 0. Edit Fallback
If the edit tool fails or reports an error on a change (root cause on this machine: the folder was renamed from `Open Code\HR` to `accountix_erp` but opencode's sqlite state still held the old path, so permission/form endpoints 500'd on realPath ENOENT and every edit that needed a permission ask died instantly; fixed by repointing `project`/`project_directory`/`worktree`/`session` rows to the new path — keep that consistent if renaming again), retry the change with python tools/apply_edit.py <file> --old <exact text> --new <replacement> (add --replace-all only when every occurrence must change; use --old-file/--new-file for long blocks). It verifies uniqueness before writing and exits non-zero with a message on failure. Never silently skip a change; verify the file on disk afterwards.

## 1. Change Impact Assessment
When any new update is made, ensure all other actions/features connected to it are updated accordingly. Check for side effects before closing a change.

## 2. E2E Test Coverage
Every change must be covered by updated E2E tests. Run the full E2E suite before committing. Add new tests for any new functionality or UI flow.

## 3. Responsive UI
All UI must be responsive and optimized for mobile use. Every module must render correctly on small screens (320px+). Test on mobile viewport before marking complete.

## 4. E2E for All App Modules
E2E tests must cover all app modules:
- **Inventory**: Login, Dashboard, Products, Suppliers, Customers, Purchase Invoice (form, add/clear items, pill toggles, calculations, global inputs), Purchase Return, Logout
- **HR**: Login, Hub, Dashboard, Attendance, Leave, ESS, Profile, Logout

## Session Summary (13 Oct 2026) — Fixed Assets & Payroll to Corporate Standard

### Fixed assets
- **Depreciation engine** (`FixedAsset.monthly_charges`, used by `due_depreciation`, the run, disposal/transfer catch-up and the schedule): straight line spreads what is left over the life left (changes of life / salvage / cost are **prospective**, IAS 8); reducing balance is double-declining **switching to straight line**; the life's last month takes the remainder. Before: SL stopped a few rupees short, DDB never reached salvage (stuck forever), an edited life kept the old rate, and an asset past its life with value left was never charged again
- **Planned depreciation** table on the asset page (`depreciation_schedule`); run page has an "as of" date, method, charged-to and total; bulk post uses a savepoint per asset (a refused one used to leave its half-built row to be committed with the others)
- **Asset rules**: cost > 0, 1–100 years, 0 ≤ salvage < cost, method checked; edit refuses cost below posted depreciation, purchase date after the first charge, moving accumulated depreciation to another account, and cost/date edits on capitalised assets; out-of-service assets edit descriptive fields only (restating a disposed asset's cost re-posted FA-ACQ while FA-DISP credited the old figure). "Disposed" removed from the edit form (it skipped the disposal journal); blank account selects keep the current account. Fixed-asset account / label changes now restate the acquisition too
- **Disposal / transfer** refused when depreciation is posted after their date; manual charges only for assets in service and not before the last charge; inactive assets take no charge
- **Capitalisation drafts**: an unapproved capitalisation created an *active* asset (from Inventory even at a typed cost with no journal) that the run depreciated with nothing in the GL. Drafts are now `status="draft"`, quantity stored (`asset_transfers.quantity`), approvable later (`POST /fixed-assets/transfers/<id>/approve-capitalisation`), deleting a draft deletes its asset, unapprove returns it to draft. Both modules use `new_capitalisation` / `approve_capitalisation` / `discard_draft_asset`. Asset → stock now requires a stock item (it debited Inventory with no layer). Unapprove/delete are POST-only. Inventory's list filtered on a direction it never wrote (always empty); neither list ever showed the asset name (no relationship). Asset codes: `FixedAsset.next_code()` (count()+1 collided after deletes). Alembic **0008_capitalisation_drafts**
- **Movement schedule** (`fixed_assets_app/movement.py`, `GET /fixed-assets/reports/movement`, Excel/PDF/CSV): cost and accumulated depreciation b/f + additions/charge − disposals = c/f by category (the IAS 16 note); nav "Asset Register" + "Movement Schedule"

### Payroll (`hr_app/payroll_engine.py`, used by preview, AJAX recalculation and the run)
- Components fixed or **% of basic** (the stored method was ignored); edit validates name/type/basis/amount
- **Proration by calendar days** for joiners and approved **unpaid leave** (`LeaveType.is_paid=False`) — was a 30-day month, unpaid leave ignored
- **Taxable pay** = basic + components marked taxable + overtime + bonus (the taxable flag was never read); stored on the slip (`taxable_pay`, Alembic **0009_payslip_taxable_pay**)
- **Withholding = cumulative method** over the tax year (July–June): tax on YTD + this month + full months left, less tax already withheld, over the months left; a bonus/overtime is taxed once in its month. Was gross × 12, so a bonus month was taxed as if the bonus came every month. No slabs → no tax (was an invented flat 5%), with a warning on the run page. "Load tax year 2026 slabs" (Finance Act 2025: 0 / 1% / 11% / 23% / 30% / 35%, fixed 6,000 / 116,000 / 346,000 / 616,000) replaces the active set; the old seed was TY2025 and merged brackets. Slab entry validated
- **Loans** recovered only from what is left after other deductions (net could go negative); the run reuses the preview's loans instead of recomputing; a run where deductions still exceed pay is refused
- Company clock for the run page defaults

### Tests
- New: `tests/e2e/test_fixed_asset_rules.py` 9, `tests/e2e/test_payroll_engine.py` 8; `tests/unit/test_fixed_assets.py` +9 (DDB to salvage, SL to the paisa, prospective life, overdue residual, schedule, codes, movement roll-forward) — the file only ran inside the full suite (missing `project_label` import), fixed
- `test_responsive_pages.py` + movement, transfers, depreciation, inventory transfers, run payroll, tax settings; Alembic heads → 0009
- Full run: unit **461 passed**, E2E **931 passed, 2 skipped** (Postgres-only); not re-run on Postgres this session
- `tests/e2e/conftest.py` warms the server (first request seeds the fresh DB: 15–35 s, longer than Playwright's 30 s, so whichever browser test ran first failed at random — HEAD too)

### Known, not changed
- PF is a % of gross (common practice is basic); overtime is 1.5 × basic/160 h (Factories Act s.47 says double for factories); no leaving date, so a leaver is excluded only once deactivated; no EOBI/super-tax (income > 10m) logic

## Session Summary (12 Oct 2026) — App-wide Walkthrough: HR Logic, Payroll GL, Company Clock

### How the walkthrough was done
- Crawler (session scratchpad, not the repo): super admin on a copy of `instance/erp_dev.db`, every parameter-free GET plus every link found, ≤3 URLs per endpoint, both companies (Default + Samba) — 283 pages each, **no 5xx / template errors**. Same crawl with a per-page query counter found the N+1 below. The bugs were logic bugs, found by reading the routes module by module

### Fixed
- **Payroll could not run with a deduction component** (EOBI etc.): the slip counted it, the journal never credited it → "Unbalanced journal … 55000 != 54000", 500. Component + custom deductions now credit **Other Payroll Deductions Payable** (`2-01-02-02-0004`, added to the fixed chart); custom deductions used to be credited to Salary Payable (overstated what staff are owed — the bank sheet pays net). Salary Payable = net exactly
- **Loan instalments** credit each loan's own account (Employee Loans & Advances, the per-loan entity account) instead of the "Loan Deductions Clearing" liability, so the receivable comes down; clearing keeps only an unexpected difference
- **New payroll profiles paid basic twice**: create-profile added a "Basic Salary" *allowance* at 100% of basic (gross = basic + allowances). No longer created; Alembic **0007_drop_duplicate_basic** deletes exact duplicates (allowance, that name, value = profile basic); issued slips untouched
- **Payroll preview JSON** (run screen recalculation) 500'd for anyone with an active loan (returned model objects) → serialised
- **Payroll run detail page** `GET /compensation/run/<id>` (`hr_app/templates/compensation/run_detail.html`): KPIs, earnings / deductions by component (overtime & bonus row so tables add up), slips with per-company employee codes, bank sheet, **Accounting entry** button (PRL; only shown with Accounting access — the API is behind the module guard). Payroll history links to it. Salary slip printed "Solarkon (Private) Limited" for every tenant → company name
- **Leave** (`hr_app/leave_flow.py`, used by `leave.review` and `mss.approve_leave`): balances are derived from requests (used = approved, pending = pending, remaining = total − both). Before: approving twice counted twice, rejecting after approval subtracted, `remaining` was never updated, quota columns were INTEGER (half days rounded on Postgres), staff who joined after the seed had no quota so the balance check was skipped, an employee whose manager is not in this company (or none) got no approver — pending forever; now manager-in-company → company admin → auto-approve only for the sole admin. Overlap check, half day = one date, missing dates no longer 500. Alembic **0005_leave_quota_days** (INTEGER → NUMERIC on Postgres, recompute balances)
- **MSS / attendance / PF plain forms landed on raw JSON**: routes answer JSON only when asked (`Accept: application/json` / JSON body), else flash + redirect
- **Timesheets**: `TimesheetApproval.week` relationship was missing → every approve/reject 500'd; `merge_report` used an unimported `User` (NameError); entries editable after submit/approval, day outside the week, hours unbounded; double submit queued two approvals; same approver routing as leave
- **Loans (MSS)**: re-deciding a paid/rejected loan reopened it; ESS loan request with 0 instalments divided by zero, negative amounts accepted, `step=1000` input
- **PF**: approving a PF loan *credited* the member's fund (borrowing raised the balance) → debit, Alembic **0006_pf_loan_debit** flips existing rows; withdrawals/loans re-approvable and over the balance; settlement ignored withdrawals and profit (ledger never closed) and could run twice; profit shares over a base including negative balances; employee page showed the whole fund's profit; reject buttons added; `add-ledger-entry` / `settle` refuse non-members
- **Company clock** (`shared/clock.py`: `local_now()` / `local_today()` from `Company.timezone`, default Asia/Karachi, via python-dateutil — no tzdata needed): Vercel runs UTC, so a 09:30 Lahore clock-in was stored as 04:30 and never late, and "today" flipped at 05:00. Used by attendance, timesheets, PF, posting-date fallbacks (`posting_helpers.parse_doc_date` / `as_date`) and the template `now` (both injectors — inventory's overrode app.py's with UTC)
- **Attendance**: `/correct` assigned form text to DateTime columns (500) and never re-derived late / half-day / overtime; clock-out used fixed 8 h / 4 h instead of the time policy (`_book_hours`, `_late_and_half`)
- **Party deletes**: supplier delete only checked purchase orders → a supplier with direct purchase invoices / returns / voucher postings could be deleted; customer delete now also checks returns and ledger postings (`ledger_utils.entity_has_postings`)
- **Chart of accounts page**: `can_delete()` ran a COUNT per row (132 queries → 13)
- E2E fixes: `test_invoice_form_ux` unapprove predicate null-safe across the reload; Alembic head assertions → 0007

### Verification
- New E2E: `test_payroll_run.py` 12, `test_leave_flow.py` 16 (leave + timesheets), `test_attendance_flow.py` 6, `test_pf_flow.py` 9; `test_books_integrity.py::test_22` party deletes; `test_responsive_pages.py` + `/compensation/` and the payroll run page at 360px
- Postgres (pgserver in the session scratchpad): the four new files + books integrity 22 + storage/money 13 all pass; 0005 converts a reverted INTEGER table and rebuilds balances
- Unit **452 passed**; full E2E **912 passed, 2 skipped** (Postgres-only); baseline before the changes: 863 passed, 2 failed (the two test fixes above)

### Known, not changed
- `/accounting/gl-entry` is login + module guard only; any member with Accounting access can read any journal of the company by id
- PF withdrawals / settlements and loan disbursements still post nothing to the GL (accountant books the payment voucher)

## Session Summary (5 Oct 2026) — Serverless Hardening, Report Exports, Invoice Form UX, App-wide UI

### Constraint
Hosting is fixed: **Vercel (serverless) + Neon Postgres**. Design for it: no local disk, no workers, small pools, migrations at boot.

### Completed
- **Serverless hardening**
  - Document amounts: 187 `db.Float` columns → `shared/models/money.py::Money` (NUMERIC(20,6) in Postgres, float in Python). Hours/scores/GPS stay Float. GL/stock layers were already Numeric(16,4)
  - **Alembic** (`migrations/`, `shared/db_migrate.py`, `tools/db_migrate.py`): runs at boot after `_migrate_schema` (now frozen — new schema changes go in revisions, written idempotently), one transaction under `pg_advisory_xact_lock` (works through Neon's pooler). `0002_money_numeric` converts live double-precision columns
  - Uploads in the DB (`shared/models/stored_file.py`, `shared/file_store.py`, tenant-scoped) for employee documents and avatars; legacy disk files still read. Payroll bulk sheet parsed in memory
  - Pool 2+2 on Vercel; optional Sentry (`SENTRY_DSN`, no PII/bodies); boot warning when `SECRET_KEY` unset on Vercel; stdout/stderr.txt untracked
- **Report exports** — one pipeline, `shared/report_export.py` (finance's builders moved here; `finance_app/routes/reports.py` keeps thin `_build_excel_wb`/`_build_pdf`/`_finish_sheet` wrappers). Every file: company/title/period/filters/generated-by heading, frozen + print-repeated header, fit-to-width print setup, "Page X of Y", section/total/grand styling in Excel AND PDF, quantity formats, `Company_Report_Period.ext` names, `export_table(fmt, …)` one-call Excel/PDF/CSV
  - New exports: P&L by Label, Stock Valuation (+GL reconciliation rows), Stock Ledger, Product Ledger (+re-costing sheet), Product Sub-Ledgers, Low Stock, **Aged Receivables/Payables** (per-party FIFO buckets via `executive_reports.party_aging`), Party Ledger (all postings, oldest first, running balance), Books Integrity, Fixed Asset Register (+by-category sheet), Attendance/Leave Excel+PDF, Audit Log Excel+PDF, HR report builder through the shared workbook
  - General Ledger workbook leads with a linked Summary sheet; cash-flow Excel got its missing column headings; SOCIE/Cash Flow export without dates used to 500/return HTML (now default period)
  - `templates/partials/export.html::export_buttons()` keeps every on-screen filter in the link; inline-SVG icons (Font Awesome loads only on finance pages)
  - HR reports page: removed hard-coded July / 2026 / 2024–2027 defaults
- **Invoice create/edit UX** (sales + purchase) — `static/js/doc_form.js` (configured per form by inline `window.DOC`) + `static/css/doc_form.css`
  - Header: document number as title, one status chip, payment chip only when approved, back link; topbar no longer says "Unapproved …"
  - Action bar by state — new: Cancel · Save draft · **Save & approve**; draft: More(New, Delete) · Back · Print · Save (lit only when dirty) · **Approve**; approved: More(New, Validate FBR, Unapprove) · Back · Send to FBR · **Print**. Pinned to the bottom on phones
  - Bugs fixed: a saved draft froze every field until reload; reopened drafts had no Print/Delete; after Unapprove `const editable` stayed false (Ctrl+S refused); descriptions clipped; row ×/+ hover classes swapped; row buttons shown on locked docs
  - Fields: Payment terms (Due on receipt / Net 7–90) fill the due date; "Party label" → "Project label"; locked fields read "—"; developer badges ("computed", "input · Combined · by %") removed; "Total due" / "Total payable"
  - Purchase form gained Ctrl+S / Ctrl+Shift+S / Ctrl+P / Alt+N / Alt+C, the unsaved-changes guard and Esc layering
  - Phone: line items render as labelled cards
- **Invoice editor layout (round 2)**: totals in a sticky Summary rail beside the items (≥1340px; below, under them) with "+ Add charge" / "+ Further tax or withholding"; notes under the items; toolbar "Import from order · Charges · Invoice options"; mode chips replaced by one note shown only for per-line settings; options panel speaks plainly ("Whole invoice / Each line", section help) and is a drawer at every width (it used to pin open at 1500px+); charge editor asks "Who pays" with three plain-language cards (sales: Bill the customer / Add to item prices / Company expense; purchase: Supplier bills it / Add to stock cost / We bear it) driving the existing hidden select. All in `static/js/doc_form.js` + `static/css/doc_form.css` (text/structure layer only; calculations untouched)
- **Accounting entry button** (`templates/partials/gl_entry.html` macro `gl_entry_button(vtype, vid, id_from, also)`, `static/js/gl_entry.js`, `static/css/gl_entry.css`, `shared/gl_entry.py::entries_for`, `GET /accounting/gl-entry?type=&id=&also=`): floating pill on SI, PI (+PMT), SR, PR, accounting vouchers, CONS/SCRAP/ADJ vouchers and the asset page (FA-ACQ + FA-DISP + every FA-DEP); slide-in panel lists posted journals (debits then credits, codes, notes, labels, totals, balance check, ledger links), CADJ re-costings with a net-effect table, and reversed versions as history. Payroll runs have no detail page yet, so no button there
- **Reporting table + report speed**: `gl_daily_balances` (`shared/models/gl_balance.py`) = posted activity per company/account/label/day, maintained in the same transaction by `shared/gl_summary.py` (before/after-flush key recompute + rebuild after bulk update/delete on journal tables; hooks register from `shared/models/ledger.py`). Finance helpers (`_get_account_balance`, `_all_account_balances`, `_period_movements`, new `_sum_balances`), executive balances + FIFO aging, TWCF cash, inventory GL check and the voucher balance API read it. Alembic `0003_gl_reporting` adds journal indexes and backfills; Books Integrity gains "Reporting table matches the ledger"; `tools/rebuild_gl_summary.py`. Ledger sections set-based (3 queries); `CompanyInfo.get()` cached per request (every `amount_format` was a query: 13,811 on one ledger page). Benchmark, Postgres, 100k lines: TB 4.3 s → 125 ms, BS 1.4 s → 139 ms, ledger (all accounts, 1 month) 158 s → 0.66 s, executive dashboard 2.0 s → 0.55 s
  - Reports calculate only when asked (View / Run / export): TB, P&L, BS, SOCIE, cash flow, ledger already did; P&L by Label, Stock Valuation and Books Integrity now show an empty state until then
- **Sidebar**: accordion in every module (`static/js/app.js`): one group open, only the active page's group opens on load, old per-group localStorage state cleared (it re-opened every group ever opened)
- **Accounting voucher form v3** (`finance_app/templates/accounting/voucher_form.html`): header with number/status/actions (pinned on phones), drafts open editable (no Edit step), type picker on new vouchers only, summary rail with live **entry preview** (Dr lines, Cr cash/bank) and cash/bank **balance now → after** with negative-cash warning (`GET /accounting/api/account-balance`), More menu (print, delete / unapprove), in-page confirm and errors instead of alert/confirm
- **Settings clean-up**: one readable column, light section headers, sentence-case labels, filled primary Save everywhere; Invoice Defaults rendered "Form field visibility" twice (a switch turned off in one copy stayed on via the other — removed); Company Profile tab passed `company` and blanked the shell's company name (now `company_info`); "§x.y" spec references removed; Report Structure moves rows with ↑/↓ instead of typed order numbers
- **Settings, round 2**: Financial Periods printed "1 January → 0 December (… 00 December 2021)" (start day − 1) — now the real year end and the current year (`fy_example` in `shared/routes/settings.py`); the company Audit Log listed every super-admin console sign-in (path `/superadmin…`) — those stay on the console's own audit page (`shared/audit.py::search`); Labels / Cash Flow boxed cards now use the same in-card title as every other tab; Rights action buttons on one row; label toggles spaced
- **Demo data — Samba Private Limited** (`tools/seed_samba.py`): Lahore solar trader, FY 1 Jul 2025 – 30 Jun 2026 (July fiscal-year rule), ~7,500 documents posted through the real routes in date order via the Flask test client — SO/PO billed later (some in parts, linked with `source_order_item_id`), PI/SI with combined and per-line discount and tax, further tax on unregistered buyers, WHT by project clients and on some suppliers, charges in all three treatments (bill / absorb / expense) plus per-line purchase freight, sales/purchase returns, scrap/consumption, daily counter receipts, cheque/IBFT receipts by credit cycle, supplier payments, rent/utilities/marketing/bank charges, month-end salary/commission accruals, sales-tax set-off and FBR/WHT deposits, cash deposits. Stops if slug `samba` exists; `--scale 0.1` for a quick run; ends by running Books Integrity
- **Production bug found by seeding against a Neon clone**: on databases older than the DateTime model, `inv_invoices.invoice_date`/`due_date` are still DATE (create_all never alters a column), so every sales return 500'd on `invoice_date.date()`. Alembic `0004_invoice_dates_timestamp` converts them (Postgres, only if DATE); return routes compare through `posting_helpers.as_date`. A model-vs-live type scan of the Neon clone found no other drift
- **Costing rounding drift found by the full-year seed**: a purchase receipt stored qty × the 4dp landed unit cost (288 × 15,349.6528 = 4,420,700.01) while the journal debited the exact landed total (4,420,700.00) — a paisa per uneven purchase; after 7,488 documents Books Integrity showed inventory GL 0.07 above stock valuation. `costing.record_in(total_cost=…)` now carries the line's 2dp landed value and the purchase invoice gives the last line the rounding remainder, so the receipts sum to the Inventory debit. `test_books_integrity.py::test_21` (red before, green after; also on Postgres)
- **Companies without periods**: the super admin console's Manage Companies form created the company and admin but never called `provision_company` (only boot's `ensure_fixed_coa` filled the chart later), so such companies had no financial periods and every report's period picker was empty. The form now provisions; boot provisions any company still without periods (`app.py::_seed_all_data`). Tests in `test_superadmin_console.py` (46 pass)
- **Boot without Alembic**: `from shared import db_migrate` sat outside the try, so a server started with an interpreter lacking alembic (the user's system Python 3.12 running `run_local.py`) aborted the whole DB init. Import moved inside the try; boot also rebuilds `gl_daily_balances` whenever it is empty while posted journals exist, independent of Alembic — reports can no longer go blank because a revision did not run
- **Samba on Neon**: seeded on a local clone of Neon (Neon is 18.6, pg_dump 16 cannot dump it, so the clone is SQLAlchemy reflect + copy; ~460 ms round trip makes seeding over the wire take days) into the existing empty company 2, then pushed company 2's 131,869 rows in one transaction (refuses if Neon gained rows since the clone; dry run first). Scripts kept in the session scratchpad, not the repo
- **App-wide UI**
  - Theme switch on every page: `static/js/theme.js` + `partials/_theme_toggle.html` on hub, portal, super admin console, landing, sign-in/sign-up (floating). Fixed: on an OS-dark page the first click did nothing and showed the wrong glyph (`data-theme-effective`)
  - Tables never clipped on phones: `.table-responsive` had no CSS; `.table-wrap` was `overflow:hidden`; bare `.table` scrolls inside its box ≤768px; header import forms wrap; inventory dashboard grids responsive
  - Stock list "Value" printed `qty * cost|amount_format` (Jinja filter precedence → string repeated qty times); fixed
  - Dark mode: statement grand-total, employee-card and settings-card headers no longer glare white; flash banners use theme pairs (warning was ~2:1); avatar initial readable

### Verification
- Full E2E **831 passed, 1 skipped** (Postgres-only check) after updating the two register tests that used `fmt=csv` as their "unsupported" example; unit **452 passed**; Postgres (local pgserver + `TEST_DATABASE_URL`): books integrity 20, audit 7, storage/money 11, exports 59
- Later: full E2E **836 passed, 1 skipped** after the editor layout round; `test_gl_entry.py` 12 (API also on Postgres); `test_invoice_form_ux.py` now 14
- Round 3 tests: `test_gl_reporting.py` 3 (SQLite + Postgres), `test_voucher_v3_and_sidebar.py` 3, `test_settings_cleanup.py` 3; books integrity / storage / GL entry also green on Postgres
- Round 4: full E2E **859 passed, 1 skipped** before the seed/settings round; `test_settings_cleanup.py` now 5; audit + settings access 24 passed; seed `--scale 0.05` = 845 documents, Books Integrity all pass
- New E2E: `test_storage_and_money.py` (11), `test_report_exports_all.py` (59), `test_invoice_form_ux.py` (9), `test_theme_toggle.py` (8), `test_responsive_pages.py` (17); unit `test_report_export_module.py` (5)

## Session Summary (Oct 2026) — Document-Date Posting, Date-Ordered Costing, Books Integrity

### Objective
Make every module post accurately in real-world sequences: back-dated documents, edits (unapprove → change → re-approve), deletions/reversals and returns — with inventory costing, the general ledger, sub-ledgers and reports all staying tied. Add label-wise tracking to the remaining cost-bearing documents and distinctive reports.

### Completed
- **Documents post on their own date** (journal AND stock): sales/purchase invoices (the forms showed a date but never sent it; the server never saved it), sales/purchase returns, consumption/scrap/adjustment/stock-take vouchers (new date field), FA capitalisation / asset→inventory transfers (`asset_transfers.transfer_date`), FA disposal (`disposal_date`, depreciates to that date first), payroll (last day of the payroll month). The period lock now refuses back-dated documents in closed periods. `shared/posting_helpers.parse_doc_date`
- **Date-ordered costing** (`shared/costing.py`): `stock_ledger.txn_date`; layers consumed in (txn_date, id) order. A back-dated receipt/issue, or the reversal of an earlier document, replays the product's history (`_replay`): earlier rows frozen, later issues re-costed. Back-dated issues are refused if stock wasn't on hand AT THEIR DATE or if they'd strip a later issue. Sales returns follow their re-costed sale; purchase returns draw from their own invoice's layers (`prefer=`).
- **Re-costing hits the GL** (`shared/cost_adjustment.py`): every moved cost gets a `StockCostAdjustment` + journal `CADJ-<type>`/`<voucher id>` charging the difference to the account the issue originally hit (COGS on the line's label, consumption/scrap charge account, adjustment account, asset), dated in the issue's period or today if closed. Reversing a voucher un-posts its own CADJ journals. The old VAR/variance write-off path is gone (it produced wrong P&L on edit flows).
- **Bugs fixed**: approved inventory vouchers could be re-submitted and posted twice; stock-take journal sat under the take so unapproving its adjustment left the GL posted; purchase return credited inventory with a tax-inclusive value and never reversed input tax / WHT (now cost + tax + WHT + price-difference); returns trusted the browser's max-returnable qty (server cap now); invoices with approved returns could be unapproved; inventory→FA transfer credited inventory at a typed price without moving stock (now through costing); product opening stock / bulk import / quick adjust changed only the display column (now cost layer + journal: `shared/stock_entry.py`); products with stock history could be deleted; payroll "Replace All" left the old journal posted (double expense) and didn't restore loan balances; payroll preview hard-coded to Jul 2026; JSON saves refused by the period lock got a redirect instead of a JSON error; debug banner on the consumption form; ST reference via count()+1 collided after deletes; dashboard stock value used qty × rounded avg.
- **Labels**: per-line label splits for SI revenue/discount/charges and exact COGS per line label; PI inventory split by landed cost per label; label on consumption/scrap/adjustment vouchers and fixed assets (acquisition + depreciation lines).
- **New reports**: Executive → **Books Integrity** (`shared/integrity.py`: TB, every journal, inventory GL = stock valuation, layers, quantities, asset register = GL, approved docs = posted journals, recent re-costings); Finance → **P&L by Label** (labels as columns + Unlabelled = company total); Inventory → **Stock Valuation** as of any date with opening/in/out/closing and GL reconciliation; product ledger/stock ledger in document-date order with re-costing history.
- **Audit log** (`shared/audit.py`, `shared/models/audit_log.py`): a session hook records every create / edit (field-level old→new) / delete / approve / unapprove of tracked business records and every journal post / un-post, with user, time (UTC), IP and path — across all modules, no per-route code. `record_now()` adds sign-in, failed sign-in, sign-out and refused postings (e.g. into a closed period) on its own connection so they survive the rollback. Secrets (password hashes, tokens) are redacted. `audit_logs` is in `tenancy.GLOBAL_TABLES` (platform events have no company) and every reader filters explicitly. Viewers: Settings → Administration → **Audit Log** (company admins: filters, field diffs, CSV export at `/settings/audit-log.csv`) and Super Admin → **Audit** (all companies + platform events). No route edits or deletes audit rows. Tests: `tests/e2e/test_audit_log.py` (7). After the audit hook: full E2E **726 passed**, plus 2 Playwright timeouts that occurred while the unit suite ran in parallel and passed on re-run (8/8 in their classes); unit **447 passed**
- **UI**: voucher forms reworked (date + label, read-only once approved, POST unapprove/delete, responsive tables); verified no horizontal scroll at 360px on the new/changed pages.

### Verification
- Unit: 439 + 8 new (`tests/unit/test_costing_backdated.py` 17, `test_posting_helpers.py` 8; variance tests in `test_costing.py` rewritten for re-cost semantics)
- New E2E `tests/e2e/test_books_integrity.py` (20 steps, one fresh company, real routes, integrity checks after each): back-dated purchase/sale, refused back-dated sale, purchase price correction, sales/purchase return, over-return refusal, middle-sale reversal + delete, approved-voucher re-submit, closed period, capitalisation, opening stock, quick adjustment, product delete guard, payroll replace, multi-label invoice, reports render
- Full E2E run: **715 passed, 0 failed**; the late-touched files (books integrity, inventory, routes smoke, input hardening, HR, fixed assets) re-run after the final edits: **346 passed**. Unit: **447 passed**

### Key Files
`shared/costing.py`, `shared/cost_adjustment.py`, `shared/integrity.py`, `shared/posting_helpers.py`, `shared/stock_entry.py`, `invoicing_app/routes/{invoices,purchase_invoice,sales_return,purchase_return}.py`, `inventory_app/routes/{vouchers,transfers,products,reports}.py`, `fixed_assets_app/routes/{transfers,assets,depreciation}.py`, `hr_app/routes/compensation.py`, `executive_app/templates/executive/integrity.html`, `finance_app/templates/finance/label_pl.html`, `inventory_app/templates/reports/valuation.html`, `app.py` (migrations: `stock_ledger.txn_date` + backfill, label/date columns)

## Session Summary (Aug 2026) — Silent-Approve Bug Fix + E2E Flake Family Root-Caused

### Objective
Fix the silent-approve loophole found during browser verification of the executive dashboard (a `save_approve` action string posted a JV without actually approving it — so Unapprove → Approve was needed to post), and root-cause the recurring Playwright "timing flake" family that kept failing a handful of E2E tests only on full-suite runs.

### Completed
- **Silent-approve fix** (`finance_app/routes/accounting.py::voucher_form`):
  - The form posted `action=save_approve` but the handler branched on `action == "approve"` — an unknown action silently became a bare save, marking the voucher Approved without posting lines. Now `save_approve` **approves and posts** (same path as `approve`), `save` stays a bare save, and any other action string falls back to a bare save
  - `approved_at`/`approving_user_id` set in the same branch as the posting so approval and posting can never diverge again
- **Regression tests** (`tests/e2e/test_accounting_voucher_form.py` *(new)*, 3 tests): bare `save` keeps the voucher unapproved, `approve` is the only path to the ledger (lines posted exactly once, status Approved), and the action string round-trips through the form without leaking. Verified red → green: the tests fail against the pre-fix handler
- **E2E flake family — root cause** (`tests/e2e/conftest.py::flask_server`):
  - The readiness check just connected to port 5000. **Any leftover dev/test server already listening** made it "ready" instantly while the spawned `run_local.py` died with "Address already in use" — so the whole Playwright suite silently ran against the wrong database (a stale `erp_dev.db`) → state-dependent tests (`test_voucher_ui` mobile layout, `test_invoice_designs` freight) failed on full runs but passed in isolation. Root-cause confirmed live: two reloader parents (`app.py` + `run_local.py`) were fighting over port 5000 for hours, respawning their children
  - Fix 1 — **port ownership pre-check**: `_assert_port_free()` binds port 5000 before spawning; if it is taken, the fixture fails fast with a message naming the holder instead of silently testing the wrong DB
  - Fix 2 — **subprocess-alive check**: after the readiness loop, `proc.poll() is None` must hold; a server that died during startup raises with the log tail
  - Fix 3 — **fresh DB per run**: the fixed `sqlite:///e2e_test.db` accumulated rows across sessions, making "empty state" assertions flaky; each session now gets a `NamedTemporaryFile` db, deleted in `finally`
- **Environment cleanup**: killed 5 stale `python.exe` server processes (three `run_local.py` reloader leftovers from previous sessions + the `app.py` pair started during this session's verification); port 5000 verified free afterwards

### Verification (this session)
- New voucher-form tests: 3 passed in isolation; 56 passed for the targeted group (voucher UI + invoice designs + executive reports + voucher form)
- **Full E2E suite: 580 passed, 0 failed** (previous: 568 passed + 2 flake failures) — the flake family is gone; ran with leftover servers killed and the hardened fixture
- Unit suite: **281 passed**
- Port 5000 free after the suite (fixture terminates its server in `finally`)

### Key Files
- `finance_app/routes/accounting.py` — `save_approve` now approves and posts; approval fields set with the posting
- `tests/e2e/test_accounting_voucher_form.py` *(new)* — 3 regression tests
- `tests/e2e/conftest.py` — `_assert_port_free()`, subprocess-alive check, per-run temp DB

## Session Summary (Aug 2026) — Executive Dashboard: Net Assets, Ratios, Aging & Exposures

### Objective
Turn the Executive Reports dashboard into a real-time business snapshot: net assets derived straight off the chart, current/quick ratios, and FIFO-aged receivables and payables — weighted-average collection/payment days, oldest debt, aging-profile buckets, and the largest exposures — all computed from posted journal lines with nothing stored.

### Completed
- **FIFO aging engine** (`shared/executive_reports.py::aging`, `_age_party`, `_aging_side`):
  - Ages every open in-scope balance from its posting layers: on receivables, debits build the stack and credits consume the **oldest layer first**; payables mirror it. A payment pulls the oldest debt down first, so a party that received 1,000 forty-five days ago and 400 five days ago is aged as 600 @ 45 days
  - Returns per side: total, party count, **weighted-average days** (weighted by surviving balance), **oldest days + party**, the four buckets (Current / 31–60 / 61–90 / 90+), and the **top 5 parties with % share**; `as_of` filters postings and re-ages to that day (future-dated layers clamp to 0 days)
- **Liquidity snapshot** (`shared/executive_reports.py::liquidity`):
  - Company-wide (whole chart, available even with no selection): net assets (assets − liabilities), total/current assets, current liabilities, inventory, **current ratio** and **quick ratio**. Current Assets / Current Liabilities are located by their standard names at level 2, inventory by the Inventories subtree; contra accounts (accumulated depreciation) net themselves out as plain balances
  - Ratios render only when current liabilities > 0 — a negative "liability" balance (overpaid, drifted payroll) is really an asset, so the cards show "—" instead of a meaningless negative number
- **Dashboard** (`executive_app/templates/executive/dashboard.html`, `_style.html`, route in `executive_app/routes/reports.py`):
  - New **KPI row** (always visible): Net assets, Current ratio, Quick ratio (green ≥1.5 / ≥1, red <1), Avg collection days, Avg payment days, Oldest receivable/payable (days + party name)
  - **Aging profile** chart: CSS-only horizontal bars (no chart library) with receivable (green) vs payable (amber) per bucket, amounts and a legend
  - **Largest exposures** card: top 5 per side with share bars, each row linking to the party ledger
  - Responsive: KPI grid auto-fits (2 cols @360px, 140px min), charts stack to one column, no horizontal scroll at 320px+

### Verification (this session)
- 8 new E2E tests in `tests/e2e/test_executive_reports.py` (**32 passed** in the file): FIFO aging math (45d layer + 5d receipt → 600@45, oldest-first consumption), bucket shares, top-party order, empty-selection and as-of behaviors, liquidity deltas on a real cash posting (current assets / net assets / total assets +500, inventory untouched), ratio formulas, and dashboard rendering with all KPI labels + party names
- Full suite: **281 unit + 568 E2E passed**; 2 unrelated flake failures (`test_voucher_ui` mobile layout, `test_invoice_designs` freight) pass in isolation — known Playwright timing-flake family
- Manually verified in the browser against the live dev server: dashboard with real posted vouchers (5,000 receivable @ 45 days, 2,000 payable @ 5 days → correct buckets/avg/oldest), mobile 360px/342px emulation with zero horizontal overflow; settings picker and JV voucher flow exercised end-to-end (a `save_approve` action string silently sets `status=approved` without posting — used Unapprove→Approve to post correctly)

### Key Files
- `shared/executive_reports.py` — `aging()`, `_age_party()`, `_aging_side()`, `liquidity()`, `BUCKET_DEFS`
- `executive_app/routes/reports.py` — dashboard route passes `recv_age`, `pay_age`, `liq`
- `executive_app/templates/executive/dashboard.html` — KPI cards + aging/exposure charts
- `executive_app/templates/executive/_style.html` — `.exr-kpis`, `.exr-charts`, `.exr-bar-*`, `.exr-top-*` (+ ≤480px tweaks)
- `tests/e2e/test_executive_reports.py` — 8 new aging/liquidity/dashboard tests

## Session Summary (Aug 2026) — Super Admin Console: Users, Quotas, Module Entitlement, Blocking

### Objective
Finish the super admin console so it actually governs the platform: it is the only door an account comes through, it sets per-user company quotas, it decides which modules a company has bought, and it can block one person from one company. Also close the hole in HR, which used to mint logins that belonged to no company.

### Completed
- **Manage Users** (`superadmin_app/routes.py::users`, `user_detail`, templates `users.html`, `user_detail.html` *(new)*):
  - `POST /superadmin/users/` creates an account — the only creation path outside the console's own company form. Placeholder `employee_code` (`SA####`) because `users.employee_code` is globally unique and NOT NULL; the real per-company code comes from HR
  - `/superadmin/users/<id>/` — password reset (min 4 chars), activate/deactivate with a self-deactivation guard, name, quota overrides, membership list
- **Per-user quotas** (`shared/models/base.py`, `shared/models/company.py::GlobalLimits`):
  - `User.max_companies_owned` / `max_companies_joined`, both nullable. `company_limit_for(user)` falls back to `GlobalLimits.max_companies_per_user`; `join_limit_for(user)` has no global default, so unset = unlimited
  - Enforced in `shared/routes/portal.py::create` (owned) and `shared/routes/settings.py::accept_invitation` (joined). Blank input writes NULL, never 0
- **Module entitlement** (`Company.MODULE_COLUMNS`, `module_enabled()`, `enabled_modules()`):
  - Seven `mod_*_enabled` columns, DEFAULT 1 so existing companies keep everything. NULL reads as enabled — a column added by migration must not switch a module off
  - `User.module_access` is now **entitlement AND user flag**. A company admin bypasses the user flag (that is what admin means) but never the entitlement. With no active company (portal/console) the user flag alone decides
  - Company edit form is the whole truth: an absent checkbox is a disabled module
- **Member blocking** (`CompanyMembership.BLOCKED`, `superadmin.member_block`): status flips ACTIVE↔BLOCKED, keeping role, employee code and history. Every gate already filters on ACTIVE, so a blocked member simply cannot enter. The route checks the membership belongs to the company in the URL (404 otherwise)
- **My Companies** (`superadmin.my_companies`, `my_companies.html` *(new)*): the super admin's own books — same slug rule as the portal, same `provision_company()`, Open Books goes through the shared `company_switch`
- **HR no longer creates logins** (`hr_app/routes/auth.py`): `/users/add` is **gone**, replaced by `/members/assign`. It lists active members of this company with no membership `employee_code` yet and gives them one plus designation/department/manager. Codes are unique **per company**, so EMP100 can exist in two companies. Nav and back-link map updated
- **`templates/access_denied.html`** *(new)*: the ~20 fixed-assets route guards render `"access_denied.html"`, which had no file at the root of the template loader — every refusal raised TemplateNotFound and became a 500. Latent before (only non-admins hit it); reachable for everyone once a company can lose a module
- **Console nav**: Manage Users / Manage Companies / My Companies / Platform (the global-limits page, otherwise only reachable via the brand link)
- **Schema migration** (`app.py::_migrate_schema`): seven `companies.mod_*_enabled BOOLEAN DEFAULT 1`, two `users.max_companies_*` INTEGER

### Verification (this session)
- New E2E `tests/e2e/test_superadmin_console.py` (**25 tests**): user creation + duplicate refusal + super-admin-only access, quota overrides both ways (set, then cleared back to the global default), join cap blocking and then admitting the same invitation, password/deactivation/self-guard, My Companies provisioning (chart > 50 accounts, ≥14 voucher series) + slug/duplicate refusal, module defaults + toggling + admin-is-not-exempt + hub tile and route guard, block/unblock round-trip preserving role and code + pending-membership refusal + cross-company 404, HR assign (per-company codes, same code in two companies, clash refused, non-member refused, `/auth/users/add` now 404)
- **Full suite: 716 passed, 0 failed** (281 unit + 435 E2E). No flakes this run

### Key Files
- `superadmin_app/routes.py` — users/user_detail/my_companies/member_block, module entitlement save
- `superadmin_app/templates/superadmin/{users,user_detail,my_companies,companies,company_edit,base}.html`
- `shared/models/base.py` — quota columns, `module_access` entitlement gate
- `shared/models/company.py` — `MODULE_COLUMNS`, `module_enabled`, `BLOCKED`, `company_limit_for`/`join_limit_for`
- `hr_app/routes/auth.py`, `hr_app/templates/auth/member_assign.html` *(new)* — Assign Member
- `templates/access_denied.html` *(new)* — module refusal page
- `tests/e2e/test_superadmin_console.py` *(new)*

## Session Summary (Aug 2026) — Company Portal + Tenancy Count-Scoping Fix

### Objective
Implement the multi-company workflow the user described: a global user logs in and lands on a portal showing (a) companies he created and (b) companies where other global users assigned him a role; clicking a company opens its books; a separate super admin portal controls all users and companies. Also fix a latent tenancy bug discovered while testing: aggregate/count queries were never tenant-filtered.

### Completed
- **User portal** (`shared/routes/portal.py`, `templates/portal/index.html`):
  - `GET /portal/` — lists owned companies, companies where the user has a role, pending invitations; create form with quota text; Super Admin Console link for super admins only; responsive, blue theme `#1d4ed8`
  - `POST /portal/create` — slug regex `^[a-z0-9][a-z0-9-]{1,60}$`, quota `owns_company_count() >= GlobalLimits.get().max_companies_per_user`, creator auto-membership `Role.ADMIN`, then `provision_company()`, `session["company_id"]` set, redirect to hub
  - `should_redirect_to_portal()` — login goes to portal when `active_companies() != 1` OR pending invites > 0; single-company users still go straight to hub
  - `hr_app/routes/auth.py` login now routes via `should_redirect_to_portal()`; `app.py` registers `portal_bp`
- **Company provisioning** (`shared/company_setup.py`): `provision_company(company_id)` seeds COA (`ensure_fixed_coa`), 14 voucher-number prefixes (`VOUCHER_PREFIXES` constant, shared with app seed), asset categories, CompanyInfo/FiscalYearRule/AccountingPeriod/ReportSettings, InventorySettings, InvoiceTemplate defaults; restores previous company in `finally`
- **Invitation next-param**: `shared/routes/settings.py` accept/decline honor `request.form.get("next")` so portal flows return to `/portal/`
- **App shell**: "My Companies" link added to the company-switcher dropdown
- **New E2E** `tests/e2e/test_portal.py` (13 tests): login landing (multi/zero/single-company, pending invite), portal content, creation + provisioning (COA > 50, 14 voucher numbers, ≥1 period, settings, ≥4 invoice templates, session switch), quota/slug/duplicate enforcement, invite accept/decline return-to-portal, super-admin console link (asserted on `href="/superadmin/"`), landing page + superadmin login door tests
- **Tenancy bug fix** (`shared/tenancy.py`): `_collect_table_names` never recursed into `Subquery`/`Alias`/CTE nodes (they wrap a selectable via `.element`; `Subquery` is NOT a subclass of `Alias` in SQLAlchemy 2.0.51 — both derive from `AliasedReturnsRows`). Result: `Query.count()` and other aggregate/subquery queries silently bypassed tenant scoping (returned all tenants' rows). Fixed by recursing into `AliasedReturnsRows.element`.

### Verification (this session)
- Unit suite: **281 passed**
- New portal E2E in isolation: **9 passed** (at that point the file had 9 tests)
- Full E2E suite (after user's parallel edits — landing page, superadmin login door, FBR tweaks, etc.): **400 passed, 3 failed**:
  1. `test_discount_modes::TestPurchaseInvoiceParity::test_combined_by_percentage[chromium]` — Playwright navigation TimeoutError (timing flake family; passes in isolation)
  2. `test_portal.py::test_super_admin_portal_link_only_for_super_admins` — asserted `b"Super Admin Portal"` text that the template no longer contains (label is now "Super Admin Console"; test has since been updated to assert `href="/superadmin/"` instead)
  3. `test_settings_access.py::test_employee_cannot_change_fiscal_year_rule` — **real regression from the tenancy fix**: `AccountingPeriod.query.count()` in a bare `app_context()` (no active company) now correctly raises `NoActiveCompanyError`; the test needs `set_current_company(default_id)` around its count assertions (the old behavior — unfiltered count — was the bug)

### Next Move
1. Fix `test_employee_cannot_change_fiscal_year_rule`: wrap its `app_context()` blocks with `set_current_company(default_company_id)`
2. Re-run `tests/e2e/test_settings_access.py` + `tests/e2e/test_portal.py`, then full E2E
3. Commit on local main (never push): `git add -A && git commit`

### Key Files
- `shared/routes/portal.py`, `templates/portal/index.html` — user portal (new)
- `shared/company_setup.py` — `provision_company()`, `VOUCHER_PREFIXES` (new)
- `tests/e2e/test_portal.py` — portal E2E (new, 13 tests)
- `shared/tenancy.py` — `_collect_table_names` now recurses into `AliasedReturnsRows` (count-scoping fix)
- `hr_app/routes/auth.py`, `app.py`, `shared/routes/settings.py`, `templates/layouts/app_shell.html` — portal wiring
- `superadmin_app/` + `superadmin_app/templates/superadmin/login.html`, `templates/landing.html` — super admin console door + landing page (user's parallel edits, uncommitted)

## Session Summary (Jul 2026)

### Objective
Add comparative (multi-period) reporting to all finance reports (Single Period / Custom Range / Comparative toggle + dropdown+checkboxes) with side-by-side period data.

### Completed
- Period filter rewrite (`_period_filter.html`): three-mode toggle, dropdown+checkboxes panel, OK button, blue theme (`#1d4ed8`)
- Backend `_resolve_period()` returns 10-tuple: `(from_date, to_date, periods, selected_period_id, filter_mode, from_str, to_str, comp_mode, comp_periods, comp_period_ids_str)`
- **Balance Sheet**: full comparative with `_bs_data()`, `_merge_multi_period()`, merged columns, per-period totals, grand total
- **P&L**: comparative with `_pl_account_contribs()`, per-row `comp_amounts`, extra Excel/PDF columns
- **SOCIE**: comparative with per-component `comp_movements` array, comparative columns in HTML/Excel/PDF, `comp_movement_totals` in tfoot (no longer `—`)
- **Trial Balance**: comparative with `comp_dr_closing`/`comp_cr_closing` per row, comparative columns + period totals in tfoot, Excel/PDF updated
- **Cash Flow**: comparative with `comp_cf_summaries` (key totals per period), rendered as comparative summary sub-table below main statement
- **Ledger**: filter/links pass comparative params; data remains single-period (transaction-level detail)
- Removed `tr:hover td` from all CSS files; scoped hover to `tbody` only in base.html
- Fixed SOCIE `re_detail` closing column bug (each row now has proper closing value)
- Theme changed to blue `#1d4ed8`

### Key Files
- `finance_app/routes/reports.py` — all route updates (TB, BS, P&L, SOCIE, Cash Flow + `_resolve_period`)
- `finance_app/templates/accounting/_period_filter.html` — filter rewrite
- `finance_app/templates/finance/{trial_balance,balance_sheet,profit_loss,socie,cash_flow}.html` — comparative columns
- `finance_app/templates/finance/layouts/base.html` — `tbody tr:hover td` scope fix
