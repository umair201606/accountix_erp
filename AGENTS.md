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
