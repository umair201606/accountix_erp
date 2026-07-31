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
                   flash, abort)
from flask_login import login_required, current_user

from shared.extensions import db
from shared.models.base import User, UserPermission
from shared.models.company_settings import (CompanyInfo, AccountingPeriod,
                                            FiscalYearRule, ReportSettings,
                                            PL_SECTIONS)
from shared.models.inventory_settings import InventorySettings
from shared.models.invoice_settings import InvoiceSettings
from shared.models.invoice_template import (
    InvoiceTemplate, DESIGNS, DESIGN_KEYS, ACCENT_PRESETS, PLACEHOLDER_HELP,
    option_groups, default_options, build_body, render_invoice_template,
    sample_context, _STRING_OPTIONS, DISPLAY_CHOICES)
from shared.models.ledger import ChartOfAccount
from shared.permissions import MODULES, ACTIONS

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
    ("inventory", "Inventory",         "&#128230;", lambda u: u.module_access("inventory")),
    ("purchase",  "Procurement",       "&#128228;", lambda u: u.module_access("invoicing")),
    ("sales",     "Sales",             "&#128229;", lambda u: u.module_access("invoicing")),
    ("invoicing", "Invoice Defaults",  "&#128207;", lambda u: u.module_access("invoicing")),
    ("templates", "Invoice Templates", "&#128196;", lambda u: u.module_access("invoicing")),
    ("rights",    "Rights & Access",   "&#128273;", lambda u: u.is_admin()),
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

    ctx = {
        "tab": tab,
        "sections": sections,
        "module_key": "settings",
    }
    if tab == "company":
        ctx["company"] = CompanyInfo.get()
    elif tab == "periods":
        ctx["fiscal_rule"] = FiscalYearRule.get()
        ctx["periods"] = AccountingPeriod.query.order_by(
            AccountingPeriod.start_date.desc()).all()
    elif tab == "reports":
        rs = ReportSettings.get()
        ctx["report_settings"] = rs
        ctx["pl_structure"] = rs.pl_structure()
        ctx["pl_sections"] = PL_SECTIONS
    elif tab == "cash_flow":
        ctx["report_settings"] = ReportSettings.get()
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
        ctx["users"] = User.query.order_by(User.full_name).all()
        ctx["modules"] = MODULES
        ctx["configured"] = {uid for (uid,) in
                             db.session.query(UserPermission.user_id).distinct()}
    return render_template("settings/index.html", **ctx)


# ── My Account (self-service, every user) ───────────────────────────────────

@settings_bp.route("/account", methods=["POST"])
@login_required
def save_account():
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    login_id = request.form.get("login_id", "").strip()
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    back = redirect(url_for("settings.index", tab="account"))

    if full_name:
        current_user.full_name = full_name
    if email and email != current_user.email:
        if User.query.filter(User.email == email, User.id != current_user.id).first():
            flash("That email is already in use.", "error")
            return back
        current_user.email = email
    if login_id and login_id != current_user.login_id:
        if User.query.filter(db.func.lower(User.login_id) == login_id.lower(),
                             User.id != current_user.id).first():
            flash("That User ID is already taken.", "error")
            return back
        current_user.login_id = login_id
    if new_pw or confirm:
        if len(new_pw) < 4:
            flash("Password must be at least 4 characters.", "error")
            return back
        if new_pw != confirm:
            flash("Passwords do not match — enter the new password twice.", "error")
            return back
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
    p = AccountingPeriod.query.get_or_404(id)
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
    p = AccountingPeriod.query.get_or_404(id)
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
    db.session.commit()
    flash("Cash flow method updated.", "success")
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
              f"{'FIFO' if method == 'fifo' else 'weighted average'}. Stock on "
              f"hand was revalued at book value; previously posted costs are "
              f"unchanged.", "success")
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
        t = InvoiceTemplate.query.get(template_id)
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
            InvoiceTemplate.query.filter_by(type=doc_type, is_default=True).update(
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
    t = InvoiceTemplate.query.get_or_404(id)
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
            InvoiceTemplate.query.filter_by(type=t.type, is_default=True).update(
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


@settings_bp.route("/templates/delete/<int:id>")
@login_required
def delete_template(id):
    denied = _require("templates")
    if denied:
        return denied
    t = InvoiceTemplate.query.get_or_404(id)
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

@settings_bp.route("/users/<int:uid>/rights", methods=["GET", "POST"])
@login_required
def user_rights(uid):
    denied = _require("rights")
    if denied:
        return denied
    u = User.query.get_or_404(uid)

    if request.method == "POST":
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if new_pw or confirm:
            if len(new_pw) < 4:
                flash("Password must be at least 4 characters.", "error")
                return redirect(url_for("settings.user_rights", uid=uid))
            if new_pw != confirm:
                flash("Passwords do not match — enter the new password twice.", "error")
                return redirect(url_for("settings.user_rights", uid=uid))
            u.set_password(new_pw)

        for module_key, _label, flag_attr, _sections in MODULES:
            setattr(u, flag_attr, request.form.get(f"module_{module_key}") == "on")

        # Rewrite this user's rows from the submitted grid so stored state
        # always matches exactly what the admin sees on screen.
        UserPermission.query.filter_by(user_id=u.id).delete()
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
    return render_template("settings/user_rights.html", u=u, modules=MODULES,
                           actions=ACTIONS, perms=perms,
                           is_configured=bool(perms), module_key="settings")


@settings_bp.route("/users/<int:uid>/reset-rights", methods=["POST"])
@login_required
def reset_rights(uid):
    denied = _require("rights")
    if denied:
        return denied
    u = User.query.get_or_404(uid)
    UserPermission.query.filter_by(user_id=u.id).delete()
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
    u = User.query.get_or_404(uid)
    if u.id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("settings.index", tab="rights"))
    u.is_active = not u.is_active
    db.session.commit()
    flash(f"{u.full_name} {'activated' if u.is_active else 'deactivated'}.", "success")
    return redirect(url_for("settings.index", tab="rights"))
