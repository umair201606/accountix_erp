# Development Policies

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
