"""The application's single settings module.

Replaces three scattered surfaces (``admin_settings`` at /settings,
``inv_settings`` at /invoicing/settings — which despite its URL edited
InventorySettings — and ``finance_settings`` at /finance/settings).

Access model: the page is open to every signed-in user, and each section
appears only if that user has the matching module access. Because hiding a tab
is not access control, every write handler re-checks the same predicate via
``_require(section)``. This also closes holes in the old code, where any
logged-in user could rewrite company info, the P&L layout, or close an
accounting period.

HR's own settings intentionally stay in the HR module
(``compensation.tax_settings`` for income-tax slabs, ``auth.account_settings``
for self-service profile).
"""
import re
from datetime import date, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, session, g)
from flask_login import login_required, current_user

from shared.extensions import db
from shared.models.base import User, UserPermission, Role
from shared.models.company import (Company, CompanyMembership,
                                   CompanyInvitation, GlobalLimits)
from shared.models.company_settings import (CompanyInfo, AccountingPeriod,
                                            FiscalYearRule, ReportSettings,
                                            PL_SECTIONS)
from shared.models.inventory_settings import InventorySettings
from shared.models.invoice_settings import InvoiceSettings
from shared.models.project_label import ProjectLabel
from shared.models.invoice_template import (
    InvoiceTemplate, DESIGNS, DESIGN_KEYS, ACCENT_PRESETS, PLACEHOLDER_HELP,
    option_groups, default_options, build_body, render_invoice_template,
    sample_context, _STRING_OPTIONS, DISPLAY_CHOICES)
from shared.models.ledger import ChartOfAccount
from shared.permissions import MODULES, ACTIONS
from shared.tenancy import (current_company_id, login_change_refusal,
                            scoped_get, scoped_get_404)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


# ── Section registry ────────────────────────────────────────────────────────
# key, label, icon, predicate(user). module_access() already returns True for
# admins, so admins see every section.
SECTIONS = [
    ("account",   "My Account",        "&#128100;", lambda u: True),
    ("company",   "Company Profile",   "&#127970;", lambda u: u.module_access("finance")),
    ("periods",   "Financial Periods", "&#128197;", lambda u: u.module_access("finance")),
    ("reports",   "Report Structure",  "&#128200;", lambda u: u.module_access("finance")),
    ("cash_flow", "Cash Flow Method",  "&#128181;", lambda u: u.module_access("finance")),
    ("labels",    "Project Labels",      "&#127991;", lambda u: u.module_access("finance")),
    ("inventory", "Inventory",         "&#128230;", lambda u: u.module_access("inventory")),
    ("purchase",  "Procurement",       "&#128228;", lambda u: u.module_access("invoicing")),
    ("sales",     "Sales",             "&#128229;", lambda u: u.module_access("invoicing")),
    ("invoicing", "Invoice Defaults",  "&#128207;", lambda u: u.module_access("invoicing")),
    ("templates", "Invoice Templates", "&#128196;", lambda u: u.module_access("invoicing")),
    ("rights",    "Rights & Access",   "&#128273;", lambda u: u.is_admin()),
    ("members",   "Members & Roles",   "&#128101;", lambda u: u.is_admin()),
    ("audit",     "Audit Log",         "&#128220;", lambda u: u.is_admin()),
    ("invites",   "Invitations",       "&#9993;",   lambda u: True),
]
_PREDICATE = {key: pred for key, _l, _i, pred in SECTIONS}


def visible_sections(user):
    return [{"key": k, "label": l, "icon": i}
            for k, l, i, pred in SECTIONS if pred(user)]


def _allowed(section):
    pred = _PREDICATE.get(section)
    return bool(pred and pred(current_user))


def _require(section):
    """Server-side gate for a write handler. Returns a response to return
    early, or None when the user may proceed."""
    if not _allowed(section):
        flash("You don't have access to that setting.", "error")
        return redirect(url_for("settings.index"))
    return None


def _postable_accounts():
    return ChartOfAccount.query.filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        ChartOfAccount.is_active == True,
    ).order_by(ChartOfAccount.code).all()


@settings_bp.route("/")
@login_required
def index():
    sections = visible_sections(current_user)
    tab = request.args.get("tab") or (sections[0]["key"] if sections else "account")
    if not _allowed(tab):
        # Deep link to a section this user can't see: fall back rather than 403.
        tab = sections[0]["key"] if sections else "account"
    # The sidebar's settings nav is tab-scoped (one route, many sections): the
    # shell needs the *resolved* tab — including the fallback above — or no
    # item highlights when the URL omits or misuses the param.
    g.settings_tab = tab

    ctx = {
        "tab": tab,
        "sections": sections,
        "module_key": "settings",
    }
    if tab == "audit":
        ctx.update(_audit_ctx())
    elif tab == "company":
        # Not "company": that name is the active company the app shell shows
        # in its header and switcher, and this tab used to blank it out.
        ctx["company_info"] = CompanyInfo.get()
    elif tab == "periods":
        rule = FiscalYearRule.get()
        ctx["fiscal_rule"] = rule
        # A worked example of the rule: the year that contains today.
        from datetime import timedelta
        import calendar
        m, d = rule.start_month or 1, rule.start_day or 1
        today = date.today()

        def _start(y):
            return date(y, m, min(d, calendar.monthrange(y, m)[1]))
        y = today.year if today >= _start(today.year) else today.year - 1
        ex_start = _start(y)
        ctx["fy_example"] = (ex_start, _start(y + 1) - timedelta(days=1))
        ctx["periods"] = AccountingPeriod.query.order_by(
            AccountingPeriod.start_date.desc()).all()
    elif tab == "reports":
        rs = ReportSettings.get()
        ctx["report_settings"] = rs
        ctx["pl_structure"] = rs.pl_structure()
        ctx["pl_sections"] = PL_SECTIONS
    elif tab == "cash_flow":
        ctx["report_settings"] = ReportSettings.get()
    elif tab == "labels":
        from shared.models.ledger import JournalLine
        from shared.models.accounting_voucher import (AccountingVoucher,
                                                      AccountingVoucherLine)
        from inventory_app.models.invoice import (InvInvoice,
                                                  InvInvoiceItem)
        from inventory_app.models.purchase_invoice import (
            InvPurchaseInvoice, InvPurchaseInvoiceItem)
        s = InventorySettings.get()
        ctx["labels"] = ProjectLabel.query.order_by(ProjectLabel.name).all()
        ctx["per_line_labeling_voucher"] = s.per_line_labeling_voucher
        ctx["per_line_labeling_invoice"] = s.per_line_labeling_invoice
        ctx["default_voucher_label_id"] = s.default_voucher_label_id
        ctx["default_invoice_label_id"] = s.default_invoice_label_id
        ctx["in_use_ids"] = _label_in_use_ids(
            (JournalLine, ("label_id",)),
            (AccountingVoucherLine, ("label_id",)),
            (AccountingVoucher, ("label_id",)),
            (InvInvoice, ("label_id",)),
            (InvPurchaseInvoice, ("label_id",)),
            (InvInvoiceItem, ("label_id",)),
            (InvPurchaseInvoiceItem, ("label_id",)),
        )
    elif tab == "inventory":
        ctx["inv"] = InventorySettings.get()
        ctx["accounts"] = _postable_accounts()
    elif tab in ("purchase", "sales"):
        ctx["report_settings"] = ReportSettings.get()
        ctx["templates"] = InvoiceTemplate.query.filter_by(type=tab).order_by(InvoiceTemplate.name).all()
    elif tab == "invoicing":
        ctx["invoice_settings"] = InvoiceSettings.get()
        ctx["accounts"] = _postable_accounts()
        # §11.1 — per-ledger charge defaults, and the ledgers still free to add.
        from shared.models.invoice_settings import ChargeLedgerDefault
        defaults = ChargeLedgerDefault.query.join(
            ChargeLedgerDefault.account).order_by(ChartOfAccount.code).all()
        ctx["charge_defaults"] = defaults
        taken = {d.account_id for d in defaults}
        ctx["charge_ledger_choices"] = [
            a for a in _postable_accounts()
            if a.id not in taken and a.type in ("expense", "revenue")]
        # §12.2 — the two posting maps: category -> revenue account, and
        # sales-tax rate -> Output Sales Tax sub-account.
        from shared.models.invoice_settings import (CategoryRevenueAccount,
                                                    TaxRateAccount)
        from inventory_app.models.category import InvCategory
        cat_maps = CategoryRevenueAccount.query.all()
        ctx["category_revenue_maps"] = sorted(
            cat_maps, key=lambda m: (m.account.code if m.account else ""))
        mapped_cats = {m.category_id for m in cat_maps}
        ctx["categories"] = InvCategory.query.order_by(InvCategory.name).all()
        ctx["unmapped_categories"] = [
            c for c in ctx["categories"] if c.id not in mapped_cats]
        ctx["revenue_accounts"] = [
            a for a in _postable_accounts() if a.type == "revenue"]
        ctx["tax_rate_maps"] = TaxRateAccount.query.order_by(
            TaxRateAccount.rate_pct).all()
        ctx["tax_accounts"] = [
            a for a in _postable_accounts() if a.type == "liability"]
    elif tab == "templates":
        ctx["sales_templates"] = InvoiceTemplate.query.filter_by(type="sales").order_by(InvoiceTemplate.name).all()
        ctx["purchase_templates"] = InvoiceTemplate.query.filter_by(type="purchase").order_by(InvoiceTemplate.name).all()
    elif tab == "rights":
        ctx["users"] = _company_users()
        ctx["modules"] = MODULES
        ctx["configured"] = {uid for (uid,) in
                             db.session.query(UserPermission.user_id).distinct()}
    elif tab == "members":
        ctx.update(_members_ctx())
    elif tab == "invites":
        ctx.update(_invites_ctx())
    return render_template("settings/index.html", **ctx)


# ── My Account (self-service, every user) ───────────────────────────────────

@settings_bp.route("/account", methods=["POST"])
@login_required
def save_account():
    from shared import account_security as acs
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower() or current_user.email
    login_id = request.form.get("login_id", "").strip() or (current_user.login_id or "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    back = redirect(url_for("settings.index", tab="account"))

    identity_changes = (email != (current_user.email or "").lower()
                        or login_id != (current_user.login_id or ""))
    if identity_changes or new_pw or confirm:
        # How this login is signed in to: a stolen session must not be
        # able to move it (or lock the owner out) without the password.
        if not current_user.check_password(request.form.get("current_password", "")):
            flash("Enter your current password to change your User ID, email "
                  "or password.", "error")
            return back
    if identity_changes:
        problem = acs.identity_problem(current_user, login_id or email, email)
        if problem:
            flash(problem, "error")
            return back
    if new_pw or confirm:
        problem = acs.password_problem(new_pw)
        if problem:
            flash(problem, "error")
            return back
        if new_pw != confirm:
            flash("Passwords do not match — enter the new password twice.", "error")
            return back
    if full_name:
        current_user.full_name = full_name
    if email != (current_user.email or "").lower():
        acs.move_invitations(current_user.email, email)
        current_user.email = email
    if login_id and login_id != (current_user.login_id or ""):
        current_user.login_id = login_id
    if new_pw:
        current_user.set_password(new_pw)
    db.session.commit()
    flash("Account updated.", "success")
    return back


# ── Company profile ─────────────────────────────────────────────────────────

@settings_bp.route("/company", methods=["POST"])
@login_required
def save_company():
    denied = _require("company")
    if denied:
        return denied
    c = CompanyInfo.get()
    c.company_name = request.form.get("company_name", c.company_name)
    c.address = request.form.get("address", "")
    c.city = request.form.get("city", "")
    c.state = request.form.get("state", "")
    c.country = request.form.get("country", "Pakistan")
    c.phone = request.form.get("phone", "")
    c.email = request.form.get("email", "")
    c.website = request.form.get("website", "")
    c.tax_id = request.form.get("tax_id", "")
    c.registration_number = request.form.get("registration_number", "")
    c.bank_name = request.form.get("bank_name", "")
    c.bank_account_title = request.form.get("bank_account_title", "")
    c.bank_account_number = request.form.get("bank_account_number", "")
    c.logo_url = request.form.get("logo_url", "")
    c.fiscal_year_start_month = request.form.get("fiscal_year_start_month", 1, type=int)
    c.currency = request.form.get("currency", "PKR")
    c.currency_symbol = request.form.get("currency_symbol", "Rs.")
    c.date_format = request.form.get("date_format", "Y-m-d")
    c.number_format = request.form.get("number_format", "en")
    c.timezone = request.form.get("timezone", "Asia/Karachi")
    db.session.commit()
    flash("Company profile updated.", "success")
    return redirect(url_for("settings.index", tab="company"))


# ── Financial periods ───────────────────────────────────────────────────────

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


@settings_bp.route("/periods/rule", methods=["POST"])
@login_required
def save_fiscal_year_rule():
    denied = _require("periods")
    if denied:
        return denied
    back = redirect(url_for("settings.index", tab="periods"))
    try:
        start_month = int(request.form["start_month"])
        start_day = int(request.form["start_day"])
    except (KeyError, ValueError):
        flash("Start month and day are required.", "error")
        return back
    if start_month < 1 or start_month > 12:
        flash("Start month must be between 1 and 12.", "error")
        return back
    if start_day < 1 or start_day > 28:
        flash("Start day must be between 1 and 28.", "error")
        return back

    rule = FiscalYearRule.get()
    rule.start_month = start_month
    rule.start_day = start_day
    rule.generate_periods()
    flash(f"Fiscal year rule updated to {start_day} {MONTHS[start_month - 1]}. Periods regenerated.", "success")
    return back


@settings_bp.route("/periods/generate", methods=["POST"])
@login_required
def generate_periods():
    denied = _require("periods")
    if denied:
        return denied
    back = redirect(url_for("settings.index", tab="periods"))
    try:
        from_year = int(request.form["from_year"])
        to_year = int(request.form["to_year"])
    except (KeyError, ValueError):
        flash("From year and to year are required.", "error")
        return back
    if from_year > to_year:
        flash("From year must not be after to year.", "error")
        return back

    rule = FiscalYearRule.get()
    rule.generate_periods(from_year, to_year)
    flash(f"Periods generated from {from_year} to {to_year}.", "success")
    return back


@settings_bp.route("/periods/<int:id>/close", methods=["POST"])
@login_required
def close_period(id):
    denied = _require("periods")
    if denied:
        return denied
    # Exclusive until commit: waits for postings into the period that are in
    # flight (shared/periods.py: require_open_period), then holds new ones off.
    p = scoped_get_404(AccountingPeriod, id, for_update=True)
    # Anything still open in the period can never be posted there once it is
    # closed: list it and ask again rather than closing over it silently.
    from shared.periods import close_checklist
    issues = close_checklist(p)
    if issues and request.form.get("force") != "1":
        session["period_close_review"] = {"id": p.id, "issues": issues}
        flash(f"{p.period_name} still has open items — review them below, then close anyway "
              "or finish them first.", "warning")
        return redirect(url_for("settings.index", tab="periods"))
    session.pop("period_close_review", None)
    p.is_open, p.is_closed, p.closed_at = False, True, datetime.utcnow()
    db.session.commit()
    flash(f"Period {p.period_name} closed.", "success")
    return redirect(url_for("settings.index", tab="periods"))


@settings_bp.route("/periods/<int:id>/reopen", methods=["POST"])
@login_required
def reopen_period(id):
    denied = _require("periods")
    if denied:
        return denied
    p = scoped_get_404(AccountingPeriod, id, for_update=True)
    p.is_open, p.is_closed, p.closed_at = True, False, None
    db.session.commit()
    flash(f"Period {p.period_name} reopened.", "success")
    return redirect(url_for("settings.index", tab="periods"))


# ── Report structure (P&L layout) ───────────────────────────────────────────

@settings_bp.route("/reports", methods=["POST"])
@login_required
def save_reports():
    denied = _require("reports")
    if denied:
        return denied
    s = ReportSettings.get()
    s.pl_detail_rows = request.form.get("pl_detail_rows", 10, type=int) or 10

    if request.form.get("reset_pl") == "1":
        s.set_pl_structure(None)
    else:
        # Rebuild from the posted rows. Each row carries its fixed identity
        # (section:<key> / subtotal:<key>) so users can reorder and rename but
        # never change which accounts feed a section.
        rows, idx = [], 0
        while True:
            ident = request.form.get(f"row_ident_{idx}")
            if ident is None:
                break
            label = (request.form.get(f"row_label_{idx}") or "").strip()
            order = request.form.get(f"row_order_{idx}", idx, type=int)
            kind, key = ident.split(":", 1)
            entry = {"label": label or key.replace("_", " ").title()}
            if kind == "section":
                entry["section"] = key
                if request.form.get(f"row_negate_{idx}") == "1":
                    entry["negate"] = True
            else:
                entry["subtotal"] = key
            rows.append((order, idx, entry))
            idx += 1
        if rows:
            rows.sort(key=lambda t: (t[0], t[1]))
            s.set_pl_structure([e for _o, _i, e in rows])
    db.session.commit()
    flash("Report structure updated.", "success")
    return redirect(url_for("settings.index", tab="reports"))


# ── Cash Flow Method ─────────────────────────────────────────────────────────

@settings_bp.route("/cash-flow", methods=["POST"])
@login_required
def save_cash_flow():
    denied = _require("cash_flow")
    if denied:
        return denied
    s = ReportSettings.get()
    s.cash_flow_method = request.form.get("cash_flow_method", "indirect")
    floor_raw = (request.form.get("twcf_cash_floor") or "").strip()
    if floor_raw:
        try:
            from decimal import Decimal
            s.twcf_cash_floor = Decimal(floor_raw)
        except (ValueError, TypeError):
            flash("Cash floor must be a number.", "danger")
            return redirect(url_for("settings.index", tab="cash_flow"))
    else:
        s.twcf_cash_floor = None
    db.session.commit()
    flash("Cash flow settings updated.", "success")
    return redirect(url_for("settings.index", tab="cash_flow"))


# ── Inventory ───────────────────────────────────────────────────────────────

@settings_bp.route("/inventory", methods=["POST"])
@login_required
def save_inventory():
    denied = _require("inventory")
    if denied:
        return denied
    s = InventorySettings.get()
    method = request.form.get("valuation_method", "weighted_average")
    method = method if method in ("weighted_average", "fifo") else "weighted_average"
    # A method change is a REVALUATION, not a re-reading of history: stock on
    # hand carries forward at its current book value so nothing already
    # computed and posted can shift. Must run while the OLD method is still in
    # force, then the new method governs receipts from here on.
    if method != s.valuation_method:
        from shared.costing import revalue_for_method_change
        revalue_for_method_change(method)
        flash(f"Valuation method changed to "
              f"{'FIFO' if method == 'fifo' else 'weighted average'}. "
              f"Old costs unchanged.", "success")
    s.valuation_method = method
    s.allow_negative_stock = request.form.get("allow_negative_stock") == "on"
    s.auto_generate_vouchers = request.form.get("auto_generate_vouchers") == "on"
    s.decimal_places = min(max(request.form.get("decimal_places", 4, type=int), 0), 6)
    pf = request.form.get("purchase_flow", "with_po")
    s.purchase_flow = pf if pf in ("with_po", "direct_invoice") else "with_po"
    sf = request.form.get("sales_flow", "with_so")
    s.sales_flow = sf if sf in ("with_so", "direct_invoice") else "with_so"
    for field in ("default_cogs_account_id", "default_inventory_account_id",
                  "default_return_account_id"):
        setattr(s, field, request.form.get(field, type=int) or None)
    db.session.commit()
    flash("Inventory settings updated.", "success")
    return redirect(url_for("settings.index", tab="inventory"))


# ── Purchase / sales invoice document settings ───────────────────────────────

@settings_bp.route("/documents/<doc>", methods=["POST"])
@login_required
def save_document_settings(doc):
    if doc not in ("purchase", "sales"):
        abort(404)
    denied = _require(doc)
    if denied:
        return denied
    s = ReportSettings.get()
    mode = request.form.get("party_mode", "relevant")
    mode = mode if mode in ("relevant", "all") else "relevant"
    if doc == "purchase":
        s.purchase_party_mode = mode
    else:
        s.sales_party_mode = mode
    db.session.commit()
    label = "Procurement" if doc == "purchase" else "Sales"
    flash(f"{label} settings updated.", "success")
    return redirect(url_for("settings.index", tab=doc))


@settings_bp.route("/invoicing", methods=["POST"])
@login_required
def save_invoicing():
    denied = _require("invoicing")
    if denied:
        return denied
    s = InvoiceSettings.get()
    s.default_sales_tax_pct = request.form.get("default_sales_tax_pct", 0, type=float) or 0
    s.default_further_tax_pct = request.form.get("default_further_tax_pct", 0, type=float) or 0
    s.default_withholding_tax_pct = request.form.get("default_withholding_tax_pct", 0, type=float) or 0
    s.default_discount_pct = request.form.get("default_discount_pct", 0, type=float) or 0
    s.default_charges_mode = request.form.get("default_charges_mode", "general")
    s.default_discount_mode = request.form.get("default_discount_mode", "general")
    s.default_tax_mode = request.form.get("default_tax_mode", "general")
    s.show_discount_column = request.form.get("show_discount_column") == "on"
    s.show_charges_column = request.form.get("show_charges_column") == "on"
    s.show_tax_column = request.form.get("show_tax_column") == "on"
    s.auto_add_line = request.form.get("auto_add_line") == "on"
    s.require_approval = request.form.get("require_approval") == "on"
    s.allow_partial_payment = request.form.get("allow_partial_payment") == "on"
    s.default_party_mode = request.form.get("default_party_mode", "relevant")

    # §11.2 — tax application defaults, over-invoicing tolerance, w/h base
    s.over_invoice_tolerance_pct = request.form.get(
        "over_invoice_tolerance_pct", 0, type=float) or 0
    wh_base = request.form.get("withholding_base", "taxable")
    s.withholding_base = wh_base if wh_base in ("taxable", "gross") else "taxable"

    # §11.3 — form-field visibility (hiding a control forces its behaviour)
    s.create_from_orders_enabled = request.form.get("create_from_orders_enabled") == "on"
    s.per_line_discount_enabled = request.form.get("per_line_discount_enabled") == "on"
    s.per_line_tax_enabled = request.form.get("per_line_tax_enabled") == "on"
    s.show_withholding_tax = request.form.get("show_withholding_tax") == "on"
    s.show_further_tax = request.form.get("show_further_tax") == "on"
    s.show_transport_block = request.form.get("show_transport_block") == "on"

    # §11.1 — per-ledger charge defaults. Tax-base switches only mean anything
    # on a billed charge, so they are forced off for absorb/expense ledgers.
    from shared.models.invoice_settings import ChargeLedgerDefault
    for d in ChargeLedgerDefault.query.all():
        if request.form.get(f"cld_delete_{d.id}") == "on":
            db.session.delete(d)
            continue
        treatment = request.form.get(f"cld_treatment_{d.id}", d.treatment)
        d.treatment = treatment if treatment in ("bill", "absorb", "expense") else "bill"
        billed = d.treatment == "bill"
        d.st_taxable = billed and request.form.get(f"cld_st_{d.id}") == "on"
        d.wht_taxable = billed and request.form.get(f"cld_wht_{d.id}") == "on"
        d.extra_taxable = billed and request.form.get(f"cld_extra_{d.id}") == "on"
        applies = request.form.get(f"cld_applies_{d.id}", d.applies_to)
        d.applies_to = applies if applies in ("both", "sales", "purchase") else "both"

    new_acct = request.form.get("cld_new_account", type=int)
    if new_acct and not ChargeLedgerDefault.query.filter_by(account_id=new_acct).first():
        treatment = request.form.get("cld_new_treatment", "bill")
        billed = treatment == "bill"
        db.session.add(ChargeLedgerDefault(
            account_id=new_acct,
            treatment=treatment if treatment in ("bill", "absorb", "expense") else "bill",
            st_taxable=billed and request.form.get("cld_new_st") == "on",
            wht_taxable=billed and request.form.get("cld_new_wht") == "on",
            extra_taxable=billed and request.form.get("cld_new_extra") == "on",
        ))

    # §12.2 — category -> revenue account. Deleting a row is not a loss of
    # data: an unmapped category simply posts to the global revenue account.
    from shared.models.invoice_settings import (CategoryRevenueAccount,
                                                TaxRateAccount)
    for m in CategoryRevenueAccount.query.all():
        if request.form.get(f"cra_delete_{m.id}") == "on":
            db.session.delete(m)
            continue
        acct = request.form.get(f"cra_account_{m.id}", type=int)
        if acct:
            m.account_id = acct

    new_cat = request.form.get("cra_new_category", type=int)
    new_cat_acct = request.form.get("cra_new_account", type=int)
    if new_cat and new_cat_acct and not CategoryRevenueAccount.query.filter_by(
            category_id=new_cat).first():
        db.session.add(CategoryRevenueAccount(category_id=new_cat,
                                              account_id=new_cat_acct))

    # §12.2 — sales-tax rate -> Output Sales Tax sub-account.
    for m in TaxRateAccount.query.all():
        if request.form.get(f"tra_delete_{m.id}") == "on":
            db.session.delete(m)
            continue
        acct = request.form.get(f"tra_account_{m.id}", type=int)
        if acct:
            m.account_id = acct

    new_rate = request.form.get("tra_new_rate", type=float)
    new_rate_acct = request.form.get("tra_new_account", type=int)
    if new_rate is not None and new_rate_acct and not TaxRateAccount.query.filter_by(
            rate_pct=new_rate).first():
        db.session.add(TaxRateAccount(rate_pct=new_rate,
                                      account_id=new_rate_acct))

    db.session.commit()
    flash("Invoice settings updated.", "success")
    return redirect(url_for("settings.index", tab="invoicing"))


@settings_bp.route("/documents/<doc>/template", methods=["POST"])
@login_required
def save_document_template(doc):
    if doc not in ("purchase", "sales"):
        abort(404)
    denied = _require(doc)
    if denied:
        return denied
    s = ReportSettings.get()
    text = request.form.get("template_text", "")
    if doc == "purchase":
        s.purchase_template_text = text
    else:
        s.sales_template_text = text
    db.session.commit()
    label = "Procurement" if doc == "purchase" else "Sales"
    flash(f"{label} template saved.", "success")
    return redirect(url_for("settings.index", tab=doc))


@settings_bp.route("/documents/<doc>/select-template", methods=["POST"])
@login_required
def select_document_template(doc):
    if doc not in ("purchase", "sales"):
        abort(404)
    denied = _require(doc)
    if denied:
        return denied
    s = ReportSettings.get()
    template_id = request.form.get("template_id", type=int) or None
    if template_id:
        t = scoped_get(InvoiceTemplate, template_id)
        if not t or t.type != doc:
            flash("Invalid template.", "error")
            return redirect(url_for("settings.index", tab=doc))
    if doc == "purchase":
        s.purchase_template_id = template_id
    else:
        s.sales_template_id = template_id
    db.session.commit()
    label = "Procurement" if doc == "purchase" else "Sales"
    flash(f"{label} print template updated.", "success")
    return redirect(url_for("settings.index", tab=doc))


# ── Invoice template management ─────────────────────────────────────────────
# Templates are authored by picking a design and ticking what should appear;
# the HTML is compiled from that on save. Hand-written HTML stays available as
# "custom" for anyone who needs it. See shared/models/invoice_template.py.


def _template_ctx(template, doc_type):
    """Everything template_form.html needs to render, for new or existing."""
    return {
        "template": template,
        "doc_type": doc_type,
        "designs": DESIGNS,
        "accents": ACCENT_PRESETS,
        "option_groups": option_groups(doc_type),
        "options": (template.options if template else default_options(doc_type)),
        "string_options": _STRING_OPTIONS,
        "display_choices": DISPLAY_CHOICES,
        "placeholders": PLACEHOLDER_HELP,
        "default_body": InvoiceTemplate.default_body(doc_type),
    }


def _read_template_form(doc_type):
    """(name, design, accent, options, custom_body, is_default) from the form."""
    design = request.form.get("design", "classic")
    if design not in DESIGN_KEYS and design != "custom":
        design = "classic"
    accent = request.form.get("accent_color", "#0f766e")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent or ""):
        accent = "#0f766e"
    opts = {}
    for _group, fields in option_groups(doc_type):
        for key, _label, _help in fields:
            if key in _STRING_OPTIONS:
                opts[key] = request.form.get(key, "combined")
            else:
                opts[key] = request.form.get(key) == "on"
    return (request.form.get("name", "").strip(), design, accent, opts,
            request.form.get("body_html", "").strip(),
            request.form.get("is_default") == "on")


@settings_bp.route("/templates/preview", methods=["POST"])
@login_required
def preview_template():
    """Render the current form state against sample data.

    Server-side on purpose: the design HTML is generated in Python, and a
    JavaScript copy of it would be a second source of truth that drifts. The
    form posts what it has and swaps in whatever comes back.
    """
    denied = _require("templates")
    if denied:
        return denied
    doc_type = "sales" if request.form.get("type") == "sales" else "purchase"
    _name, design, accent, opts, custom_body, _default = _read_template_form(doc_type)
    body = custom_body if design == "custom" else build_body(design, doc_type, accent, opts)
    return render_invoice_template(body, sample_context(doc_type, CompanyInfo.get(), opts))


@settings_bp.route("/templates/create", methods=["GET", "POST"])
@login_required
def create_template():
    denied = _require("templates")
    if denied:
        return denied
    doc_type = ("sales" if request.values.get("type") == "sales" else "purchase")
    if request.method == "POST":
        name, design, accent, opts, custom_body, is_default = _read_template_form(doc_type)
        if not name:
            flash("Give the template a name.", "error")
            return render_template("settings/template_form.html",
                                   **_template_ctx(None, doc_type))
        if design == "custom" and not custom_body:
            flash("A custom template needs some HTML.", "error")
            return render_template("settings/template_form.html",
                                   **_template_ctx(None, doc_type))

        t = InvoiceTemplate(name=name, type=doc_type, design=design,
                            accent_color=accent, is_default=is_default,
                            body_html=custom_body or "")
        t.set_options(opts)
        t.recompile()
        # The first template of its type becomes the default: otherwise it is
        # created and nothing prints with it, which reads as the save failing.
        if is_default or not InvoiceTemplate.query.filter_by(type=doc_type).first():
            InvoiceTemplate.query.filter_by(
                type=doc_type, is_default=True,
                company_id=current_company_id()).update(
                {"is_default": False})
            t.is_default = True
        db.session.add(t)
        db.session.commit()
        flash(f"Template '{name}' created.", "success")
        return redirect(url_for("settings.index", tab="templates"))

    return render_template("settings/template_form.html",
                           **_template_ctx(None, doc_type))


@settings_bp.route("/templates/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_template(id):
    denied = _require("templates")
    if denied:
        return denied
    t = scoped_get_404(InvoiceTemplate, id)
    if request.method == "POST":
        name, design, accent, opts, custom_body, is_default = _read_template_form(t.type)
        if not name:
            flash("Give the template a name.", "error")
            return render_template("settings/template_form.html",
                                   **_template_ctx(t, t.type))
        if design == "custom" and not custom_body:
            flash("A custom template needs some HTML.", "error")
            return render_template("settings/template_form.html",
                                   **_template_ctx(t, t.type))

        if is_default and not t.is_default:
            InvoiceTemplate.query.filter_by(
                type=t.type, is_default=True,
                company_id=current_company_id()).update(
                {"is_default": False})
        t.name = name
        t.design = design
        t.accent_color = accent
        t.set_options(opts)
        if design == "custom":
            t.body_html = custom_body
        else:
            t.recompile()
        t.is_default = is_default or t.is_default
        db.session.commit()
        flash(f"Template '{name}' updated.", "success")
        return redirect(url_for("settings.index", tab="templates"))

    return render_template("settings/template_form.html", **_template_ctx(t, t.type))


@settings_bp.route("/templates/delete/<int:id>", methods=["POST"])
@login_required
def delete_template(id):
    denied = _require("templates")
    if denied:
        return denied
    t = scoped_get_404(InvoiceTemplate, id)
    was_default, doc_type = t.is_default, t.type
    db.session.delete(t)
    db.session.flush()
    # Never leave a type with templates but no default — the print routes fall
    # back to the first by id, but the settings screen would show none marked.
    if was_default:
        nxt = InvoiceTemplate.query.filter_by(type=doc_type).order_by(
            InvoiceTemplate.id).first()
        if nxt:
            nxt.is_default = True
    db.session.commit()
    flash(f"Template '{t.name}' deleted.", "success")
    return redirect(url_for("settings.index", tab="templates"))


# ── Rights & access (admin only) ────────────────────────────────────────────
# Users are global rows, so tenancy cannot scope them for us: every user id
# that arrives from the URL or fills the list must be narrowed to the active
# company by hand, or an admin of one company could manage another's staff.


def _company_users():
    """Users this company's admin may manage: its active members, by name."""
    cid = current_company_id()
    if cid is None:
        return []
    return (User.query.join(CompanyMembership, db.and_(
        CompanyMembership.user_id == User.id,
        CompanyMembership.company_id == cid,
        CompanyMembership.status == CompanyMembership.ACTIVE))
        .order_by(User.full_name).all())


def _managed_user_or_404(uid):
    """A user the current admin may act on: an active member of the active
    company. Unlike ``get_member`` this has no super-admin bypass — a platform
    account must not become editable (password included) from a tenant's
    settings screen just by putting its id in the URL.
    """
    cid = current_company_id()
    if cid is None:
        abort(404)
    u = User.query.get(uid)
    if u is None or u.membership_in(cid) is None:
        abort(404)
    if getattr(u, "is_super_admin", False) and not getattr(
            current_user, "is_super_admin", False):
        abort(403)
    return u


@settings_bp.route("/users/<int:uid>/rights", methods=["GET", "POST"])
@login_required
def user_rights(uid):
    denied = _require("rights")
    if denied:
        return denied
    u = _managed_user_or_404(uid)

    if request.method == "POST":
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if new_pw or confirm:
            refusal = login_change_refusal(u)
            if refusal:
                flash(refusal, "error")
                return redirect(url_for("settings.user_rights", uid=uid))
            from shared.account_security import password_problem
            problem = password_problem(new_pw)
            if problem:
                flash(problem, "error")
                return redirect(url_for("settings.user_rights", uid=uid))
            if new_pw != confirm:
                flash("Passwords do not match — enter the new password twice.", "error")
                return redirect(url_for("settings.user_rights", uid=uid))
            u.set_password(new_pw)

        # Module access is set for THIS company: the flags on the user row
        # are shared by every company the person works in, so switching a
        # module off here used to switch it off there too.
        membership = u.membership_in(current_company_id())
        membership.set_module_flags({
            module_key: request.form.get(f"module_{module_key}") == "on"
            for module_key, _label, _flag, _sections in MODULES})

        # Rewrite this user's rows from the submitted grid so stored state
        # always matches exactly what the admin sees on screen. Company-scoped
        # (and hook-scoped): another company's rows for this user survive.
        UserPermission.query.filter_by(
            user_id=u.id, company_id=current_company_id()).delete()
        for _module_key, _label, _flag, sections in MODULES:
            for resource, _res_label in sections:
                db.session.add(UserPermission(
                    user_id=u.id, resource=resource,
                    **{f"can_{a}": request.form.get(f"perm_{resource}_{a}") == "on"
                       for a in ACTIONS}
                ))
        db.session.commit()
        flash(f"Access rights saved for {u.full_name}.", "success")
        return redirect(url_for("settings.index", tab="rights"))

    perms = {p.resource: p for p in UserPermission.query.filter_by(user_id=u.id).all()}
    cid = current_company_id()
    return render_template("settings/user_rights.html", u=u, modules=MODULES,
                           actions=ACTIONS, perms=perms,
                           flags={key: u.module_flag(key) for key, *_ in MODULES},
                           role=u.role_in(cid),
                           login_refusal=login_change_refusal(u),
                           is_configured=bool(perms), module_key="settings")


@settings_bp.route("/users/<int:uid>/reset-rights", methods=["POST"])
@login_required
def reset_rights(uid):
    denied = _require("rights")
    if denied:
        return denied
    u = _managed_user_or_404(uid)
    UserPermission.query.filter_by(
        user_id=u.id, company_id=current_company_id()).delete()
    db.session.commit()
    flash(f"Rights reset for {u.full_name} — unrestricted section access again.",
          "success")
    return redirect(url_for("settings.index", tab="rights"))


@settings_bp.route("/users/<int:uid>/toggle-active", methods=["POST"])
@login_required
def toggle_active(uid):
    denied = _require("rights")
    if denied:
        return denied
    u = _managed_user_or_404(uid)
    if u.id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("settings.index", tab="rights"))
    refusal = login_change_refusal(u)
    if refusal:
        # Deactivating the login would lock them out of every company.
        flash(refusal + " To stop them working here, remove them from this "
              "company under Members.", "error")
        return redirect(url_for("settings.index", tab="rights"))
    u.is_active = not u.is_active
    db.session.commit()
    flash(f"{u.full_name} {'activated' if u.is_active else 'deactivated'}.", "success")
    return redirect(url_for("settings.index", tab="rights"))


# ── Members & roles (company admin only) ────────────────────────────────────


def _active_company():
    cid = current_company_id()
    return Company.query.get(cid) if cid else None


def _member_roles():
    return Role.query.filter(
        Role.name.in_([Role.ADMIN, Role.MANAGER, Role.EMPLOYEE])
    ).order_by(Role.id).all()


def _role_names_by_id(role_ids):
    names = {}
    if role_ids:
        for r in Role.query.filter(Role.id.in_(role_ids)).all():
            names[r.id] = r.name
    return names


def _members_ctx():
    cid = current_company_id()
    company = _active_company()
    if company is None:
        return {"company": None, "members": [], "active_count": 0,
                "member_limit": 0, "active_admins": 0, "admin_role_id": None,
                "created_by_user_id": None, "member_roles": [],
                "invitations": [], "inviter_names": {},
                "inv_role_names": {}}
    members = [(m, User.query.get(m.user_id))
               for m in CompanyMembership.query
               .filter_by(company_id=cid)
               .order_by(CompanyMembership.status,
                         CompanyMembership.joined_at).all()]
    members.sort(key=lambda t: (t[0].status,
                                (t[1].full_name if t[1] else "")))
    admin_role = Role.query.filter_by(name=Role.ADMIN).first()
    active_count = CompanyMembership.query.filter_by(
        company_id=cid, status=CompanyMembership.ACTIVE).count()
    active_admins = (CompanyMembership.query.filter_by(
        company_id=cid, status=CompanyMembership.ACTIVE,
        role_id=admin_role.id).count() if admin_role else 0)
    invitations = CompanyInvitation.query.filter_by(
        company_id=cid).order_by(CompanyInvitation.created_at.desc()).all()
    inviter_names = {}
    inviter_ids = {i.invited_by for i in invitations if i.invited_by}
    if inviter_ids:
        for u in User.query.filter(User.id.in_(inviter_ids)).all():
            inviter_names[u.id] = u.full_name
    return {"company": company, "members": members,
            "active_count": active_count,
            "member_limit": GlobalLimits.get().member_limit_for(company),
            "active_admins": active_admins,
            "admin_role_id": admin_role.id if admin_role else None,
            "created_by_user_id": company.created_by,
            "member_roles": _member_roles(),
            "invitations": invitations,
            "inviter_names": inviter_names,
            "inv_role_names": _role_names_by_id(
                {i.role_id for i in invitations})}


def _audit_members():
    """{user_id: name} of this company's members (filter list + scoping)."""
    from shared.models.company import CompanyMembership
    from shared.tenancy import current_company_id
    cid = current_company_id()
    ids = [m.user_id for m in CompanyMembership.query.filter_by(company_id=cid).all()]
    users = User.query.filter(User.id.in_(ids)).all() if ids else []
    return {u.id: (u.full_name or u.email) for u in users}


def _audit_ctx():
    from shared import audit
    from shared.tenancy import current_company_id
    members = _audit_members()
    q = audit.search(current_company_id(), request.args, members.keys())
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per = 50
    total = q.count()
    rows = q.offset((page - 1) * per).limit(per).all()
    return {"audit_rows": rows, "audit_total": total, "audit_page": page,
            "audit_pages": max((total + per - 1) // per, 1),
            "audit_members": members, "audit_actions": audit.ACTIONS,
            "audit_modules": audit.MODULES, "audit_decode": audit.decode_changes,
            "audit_args": request.args}


@settings_bp.route("/audit-log.csv")
@login_required
def audit_csv():
    """The filtered audit trail as CSV (same filters as the screen)."""
    if not current_user.is_admin():
        abort(403)
    import csv
    import io
    from flask import Response
    from shared import audit
    from shared.tenancy import current_company_id
    members = _audit_members()
    rows = audit.search(current_company_id(), request.args, members.keys()).limit(20000).all()
    fmt = request.args.get("format", "csv")
    if fmt in ("excel", "pdf"):
        from shared import report_export as rx
        data = []
        for r in rows:
            changes = "; ".join(
                f"{c['field']}: {c['old']} -> {c['new']}" if c["old"] is not None
                else f"{c['field']}: {c['new']}" for c in audit.decode_changes(r))
            data.append([r.created_at, r.user_name or "system", r.action,
                         r.module or "", r.entity_type or "", r.reference or "",
                         r.summary or "", changes, r.ip_address or ""])
        flt = [f"{k}: {v}" for k, v in request.args.items()
               if v and k not in ("format", "page")]
        return rx.export_table(fmt, "Audit Log",
                               ["When (UTC)", "User", "Action", "Module", "Record",
                                "Reference", "Summary", "Changes", "IP"], data,
                               period=f"{len(data)} entries", filters=flt,
                               file_period=datetime.utcnow().date())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["When (UTC)", "User", "Action", "Module", "Record", "Reference",
                "Summary", "Changes", "IP"])
    for r in rows:
        changes = "; ".join(
            f"{c['field']}: {c['old']} -> {c['new']}" if c["old"] is not None
            else f"{c['field']}: {c['new']}" for c in audit.decode_changes(r))
        w.writerow([r.created_at.strftime("%Y-%m-%d %H:%M:%S"), r.user_name or "system",
                    r.action, r.module or "", r.entity_type or "", r.reference or "",
                    r.summary or "", changes, r.ip_address or ""])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=audit-log.csv"})


@settings_bp.route("/members/")
@login_required
def members():
    if not current_user.is_admin():
        abort(403)
    ctx = {"tab": "members", "sections": visible_sections(current_user),
           "module_key": "settings"}
    ctx.update(_members_ctx())
    return render_template("settings/index.html", **ctx)


@settings_bp.route("/members/<int:membership_id>/role", methods=["POST"])
@login_required
def member_role(membership_id):
    if not current_user.is_admin():
        abort(403)
    back = redirect(url_for("settings.members"))
    m = CompanyMembership.query.get_or_404(membership_id)
    if m.company_id != current_company_id():
        abort(404)
    company = _active_company()
    if m.status != CompanyMembership.ACTIVE:
        flash("Only active members can have their role changed.", "error")
        return back
    if company is not None and m.user_id == company.created_by:
        flash("The company owner's role is fixed and cannot be changed.", "error")
        return back
    if m.user_id == current_user.id:
        flash("You cannot change your own role.", "error")
        return back
    role = Role.query.filter_by(name=request.form.get("role", "")).first()
    if role is None or role.name not in (Role.ADMIN, Role.MANAGER,
                                         Role.EMPLOYEE):
        flash("Pick a valid role.", "error")
        return back
    u = User.query.get(m.user_id)
    m.role_id = role.id
    db.session.commit()
    flash(f"Role of {u.full_name if u else 'member'} changed to "
          f"{role.name.title()}.", "success")
    return back


@settings_bp.route("/members/<int:membership_id>/remove", methods=["POST"])
@login_required
def member_remove(membership_id):
    if not current_user.is_admin():
        abort(403)
    back = redirect(url_for("settings.members"))
    m = CompanyMembership.query.get_or_404(membership_id)
    if m.company_id != current_company_id():
        abort(404)
    company = _active_company()
    if company is not None and m.user_id == company.created_by:
        flash("The company owner cannot be removed.", "error")
        return back
    admin_role = Role.query.filter_by(name=Role.ADMIN).first()
    if m.status == CompanyMembership.ACTIVE and admin_role is not None:
        active_admins = CompanyMembership.query.filter_by(
            company_id=m.company_id, status=CompanyMembership.ACTIVE,
            role_id=admin_role.id).count()
        if m.role_id == admin_role.id and active_admins <= 1:
            flash("Cannot remove the last active admin of the company.", "error")
            return back
    u = User.query.get(m.user_id)
    m.status = CompanyMembership.REMOVED
    db.session.commit()
    flash(f"{u.full_name if u else 'Member'} removed from the company.",
          "success")
    return back


@settings_bp.route("/members/<int:membership_id>/restore", methods=["POST"])
@login_required
def member_restore(membership_id):
    if not current_user.is_admin():
        abort(403)
    back = redirect(url_for("settings.members"))
    m = CompanyMembership.query.get_or_404(membership_id)
    if m.company_id != current_company_id():
        abort(404)
    # Only a REMOVED membership is discarded. The page offers the button for
    # those alone, but the route deleted whatever id it was sent — an active
    # member, the company owner or the last admin — past every guard that
    # Remove applies.
    if m.status != CompanyMembership.REMOVED:
        flash("Only a removed membership can be discarded. Remove the member first.", "error")
        return back
    u = User.query.get(m.user_id)
    db.session.delete(m)
    db.session.commit()
    flash(f"Removed membership of {u.full_name if u else 'member'} "
          f"discarded.", "success")
    return back


@settings_bp.route("/invite", methods=["POST"])
@login_required
def invite():
    if not current_user.is_admin():
        abort(403)
    back = redirect(url_for("settings.members"))
    company = _active_company()
    if company is None:
        flash("No active company to invite into.", "error")
        return back
    email = request.form.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first() if email else None
    if user is None:
        flash("No registered user has that email address.", "error")
        return back
    role = Role.query.filter_by(name=request.form.get("role", "")).first()
    if role is None or role.name not in (Role.ADMIN, Role.MANAGER,
                                         Role.EMPLOYEE):
        flash("Pick a valid role.", "error")
        return back
    if CompanyMembership.query.filter(
            CompanyMembership.company_id == company.id,
            CompanyMembership.user_id == user.id,
            CompanyMembership.status != CompanyMembership.REMOVED).first():
        flash(f"{user.full_name} is already a member of this company.",
              "error")
        return back
    active_count = CompanyMembership.query.filter_by(
        company_id=company.id, status=CompanyMembership.ACTIVE).count()
    limit = GlobalLimits.get().member_limit_for(company)
    if active_count >= limit:
        flash(f"Member limit reached ({limit} active members) — no more "
              f"invitations can be sent.", "error")
        return back
    if CompanyInvitation.query.filter_by(
            company_id=company.id, email=email,
            status=CompanyInvitation.SENT).first():
        flash("An invitation to that email is already pending.", "error")
        return back
    db.session.add(CompanyInvitation(
        company_id=company.id, email=email, role_id=role.id,
        invited_by=current_user.id, status=CompanyInvitation.SENT))
    db.session.commit()
    flash(f"Invitation sent to {email}.", "success")
    return back


@settings_bp.route("/invite/<int:invitation_id>/revoke", methods=["POST"])
@login_required
def revoke_invite(invitation_id):
    if not current_user.is_admin():
        abort(403)
    inv = CompanyInvitation.query.get_or_404(invitation_id)
    if inv.company_id != current_company_id():
        abort(404)
    inv.status = CompanyInvitation.REVOKED
    db.session.commit()
    flash(f"Invitation to {inv.email} revoked.", "success")
    return redirect(url_for("settings.members"))


# ── Invitations (accept flow — any logged-in user) ──────────────────────────


def _invites_ctx():
    invites = CompanyInvitation.query.filter_by(
        email=current_user.email, status=CompanyInvitation.SENT
    ).order_by(CompanyInvitation.created_at.desc()).all()
    return {"invites": invites,
            "inv_role_names": _role_names_by_id(
                {i.role_id for i in invites})}


@settings_bp.route("/invitations/")
@login_required
def invitations():
    ctx = {"tab": "invites", "sections": visible_sections(current_user),
           "module_key": "settings"}
    ctx.update(_invites_ctx())
    return render_template("settings/index.html", **ctx)


@settings_bp.route("/invitations/<int:invitation_id>/accept", methods=["POST"])
@login_required
def accept_invitation(invitation_id):
    from shared.security import safe_local_url
    back = redirect(safe_local_url(request.form.get("next"))
                    or url_for("settings.invitations"))
    inv = CompanyInvitation.query.get_or_404(invitation_id)
    if inv.email != current_user.email:
        abort(404)
    if inv.status != CompanyInvitation.SENT:
        flash("That invitation is no longer valid.", "error")
        return back
    company = inv.company
    if company is None or not company.is_active:
        flash("That company is no longer accepting members.", "error")
        return back
    existing = CompanyMembership.query.filter_by(
        company_id=company.id, user_id=current_user.id).first()
    if existing is not None and existing.status != CompanyMembership.REMOVED:
        flash("You are already a member of that company.", "error")
        return back
    limits = GlobalLimits.get()
    active_count = CompanyMembership.query.filter_by(
        company_id=company.id, status=CompanyMembership.ACTIVE).count()
    limit = limits.member_limit_for(company)
    if active_count >= limit:
        flash(f"Cannot join {company.name}: it has reached its member "
              f"limit ({limit}).", "error")
        return back
    # The invitee's own cap on how many companies they may belong to. Unset
    # means unlimited, so this only bites where a super admin set it.
    join_cap = limits.join_limit_for(current_user)
    if join_cap is not None:
        mine = CompanyMembership.query.filter_by(
            user_id=current_user.id,
            status=CompanyMembership.ACTIVE).count()
        if mine >= join_cap:
            flash(f"You already belong to the maximum number of companies "
                  f"allowed for your account ({join_cap}).", "error")
            return back
    inv.status = CompanyInvitation.ACCEPTED
    if existing is not None:
        # Re-hire: a REMOVED row survives departure (unique on
        # company+user), so reactivate it instead of inserting a duplicate.
        existing.status = CompanyMembership.ACTIVE
        existing.role_id = inv.role_id
    else:
        db.session.add(CompanyMembership(
            company_id=company.id, user_id=current_user.id,
            role_id=inv.role_id, status=CompanyMembership.ACTIVE))
    db.session.commit()
    session["company_id"] = company.id
    flash(f"Welcome to {company.name}.", "success")
    from shared.security import safe_local_url
    return redirect(safe_local_url(request.form.get("next"))
                    or url_for("dashboard.hub"))


@settings_bp.route("/invitations/<int:invitation_id>/decline", methods=["POST"])
@login_required
def decline_invitation(invitation_id):
    from shared.security import safe_local_url
    back = redirect(safe_local_url(request.form.get("next"))
                    or url_for("settings.invitations"))
    inv = CompanyInvitation.query.get_or_404(invitation_id)
    if inv.email != current_user.email:
        abort(404)
    if inv.status != CompanyInvitation.SENT:
        flash("That invitation is no longer valid.", "error")
        return back
    inv.status = CompanyInvitation.REVOKED
    db.session.commit()
    flash("Invitation declined.", "success")
    return back


# ── Project Labels (the company-wide project dimension) ──────────────────────
# Lives in Settings rather than Accounting because vouchers, invoices and every
# finance report share the label dimension. One shared pool serves both
# document classes, with independent per-line toggles and per-class defaults
# (voucher default / invoice default) auto-assigned to slots saved without an
# explicit pick.

def _label_in_use_ids(*models):
    """Label ids referenced by any posting surface, as a set."""
    ids = set()
    for model, cols in models:
        for col in cols:
            ids |= {r[0] for r in db.session.query(getattr(model, col))
                    .filter(getattr(model, col).isnot(None)).distinct().all()}
    return ids


@settings_bp.route("/labels", methods=["GET", "POST"])
@login_required
def labels():
    resp = _require("labels")
    if resp:
        return resp
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Label name is required.", "error")
        elif ProjectLabel.query.filter(
                db.func.lower(ProjectLabel.name) == name.lower()).first():
            flash(f"A label named \"{name}\" already exists.", "error")
        else:
            db.session.add(ProjectLabel(name=name))
            db.session.commit()
            flash(f"Label \"{name}\" created.", "success")
        return redirect(url_for("settings.index", tab="labels"))
    sections = visible_sections(current_user)
    from shared.models.ledger import JournalLine
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from inventory_app.models.invoice import (InvInvoice,
                                                  InvInvoiceItem)
    from inventory_app.models.purchase_invoice import (
        InvPurchaseInvoice, InvPurchaseInvoiceItem)
    ctx = {
        "tab": "labels",
        "sections": sections,
        "module_key": "settings",
        "labels": ProjectLabel.query.order_by(ProjectLabel.name).all(),
        "per_line_labeling_voucher": InventorySettings.get().per_line_labeling_voucher,
        "per_line_labeling_invoice": InventorySettings.get().per_line_labeling_invoice,
        "default_voucher_label_id": InventorySettings.get().default_voucher_label_id,
        "default_invoice_label_id": InventorySettings.get().default_invoice_label_id,
        "in_use_ids": _label_in_use_ids(
            (JournalLine, ("label_id",)),
            (AccountingVoucherLine, ("label_id",)),
            (AccountingVoucher, ("label_id",)),
            (InvInvoice, ("label_id",)),
            (InvPurchaseInvoice, ("label_id",)),
            (InvInvoiceItem, ("label_id",)),
            (InvPurchaseInvoiceItem, ("label_id",)),
        ),
    }
    return render_template("settings/index.html", **ctx)


@settings_bp.route("/labels/per-line", methods=["POST"])
@login_required
def labels_per_line():
    resp = _require("labels")
    if resp:
        return resp
    s = InventorySettings.get()
    if request.form.get("scope") == "invoice":
        s.per_line_labeling_invoice = request.form.get("enabled") == "on"
        scope_label = "invoice"
        on = s.per_line_labeling_invoice
    else:
        s.per_line_labeling_voucher = request.form.get("enabled") == "on"
        scope_label = "voucher"
        on = s.per_line_labeling_voucher
    db.session.commit()
    flash(f"Per-line labeling for {scope_label}s "
          + ("enabled" if on else "disabled") + ".", "success")
    return redirect(url_for("settings.index", tab="labels"))


@settings_bp.route("/labels/<int:id>/rename", methods=["POST"])
@login_required
def label_rename(id):
    resp = _require("labels")
    if resp:
        return resp
    label = scoped_get_404(ProjectLabel, id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Label name is required.", "error")
    elif ProjectLabel.query.filter(ProjectLabel.id != id,
                                   db.func.lower(ProjectLabel.name) == name.lower()).first():
        flash(f"A label named \"{name}\" already exists.", "error")
    else:
        label.name = name
        db.session.commit()
        flash("Label renamed.", "success")
    return redirect(url_for("settings.index", tab="labels"))


@settings_bp.route("/labels/<int:id>/toggle", methods=["POST"])
@login_required
def label_toggle(id):
    resp = _require("labels")
    if resp:
        return resp
    label = scoped_get_404(ProjectLabel, id)
    label.is_active = not label.is_active
    # An archived label must never stay the default: the resolution skips
    # inactive labels, but clear the id too so the UI can't show it selected.
    if not label.is_active:
        s = InventorySettings.get()
        if s.default_voucher_label_id == label.id:
            s.default_voucher_label_id = None
        if s.default_invoice_label_id == label.id:
            s.default_invoice_label_id = None
    db.session.commit()
    flash(f"Label \"{label.name}\" {'deactivated' if not label.is_active else 'activated'}.",
          "success")
    return redirect(url_for("settings.index", tab="labels"))


@settings_bp.route("/labels/defaults", methods=["POST"])
@login_required
def labels_defaults():
    resp = _require("labels")
    if resp:
        return resp
    s = InventorySettings.get()
    dv = request.form.get("default_voucher_label_id", "").strip()
    di = request.form.get("default_invoice_label_id", "").strip()
    s.default_voucher_label_id = int(dv) if dv.isdigit() else None
    s.default_invoice_label_id = int(di) if di.isdigit() else None
    db.session.commit()
    flash("Default labels saved.", "success")
    return redirect(url_for("settings.index", tab="labels"))
