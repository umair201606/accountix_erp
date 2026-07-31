import os
import sys
import traceback as _tb

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, redirect, url_for, request
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException


def _create_app():
    from shared.config import Config
    from shared.extensions import db, login_manager
    from shared.models.base import User, Role, Permission, load_user
    import shared.models.company  # noqa: F401  (tenancy tables: register before create_all)

    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
        static_url_path="/static",
    )

    app.config.from_object(Config)

    import jinja2
    my_loader = jinja2.ChoiceLoader([
        app.jinja_loader,
        jinja2.FileSystemLoader([
            os.path.join(os.path.dirname(__file__), "hr_app", "templates"),
            os.path.join(os.path.dirname(__file__), "inventory_app", "templates"),
            os.path.join(os.path.dirname(__file__), "invoicing_app", "templates"),
            os.path.join(os.path.dirname(__file__), "finance_app", "templates"),
            os.path.join(os.path.dirname(__file__), "fbr_app", "templates"),
            os.path.join(os.path.dirname(__file__), "fixed_assets_app", "templates"),
            os.path.join(os.path.dirname(__file__), "superadmin_app", "templates"),
        ]),
    ])
    app.jinja_loader = my_loader

    db.init_app(app)
    login_manager.init_app(app)

    # Multi-company tenancy: registers the do_orm_execute scoping listener.
    # Must come after db.init_app — before_request (_set_company_context)
    # feeds it the active company per request.
    import shared.tenancy  # noqa: F401

    from shared.routes.dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp)

    from shared.routes.settings import settings_bp
    app.register_blueprint(settings_bp)

    from hr_app.app import register_hr_blueprints
    register_hr_blueprints(app)

    from inventory_app.app import register_inventory_blueprints
    register_inventory_blueprints(app)

    from invoicing_app.app import register_invoicing_blueprints
    register_invoicing_blueprints(app)

    from finance_app.app import register_finance_blueprints
    register_finance_blueprints(app)

    from fbr_app.app import register_fbr_blueprints
    register_fbr_blueprints(app)

    from fixed_assets_app.app import register_fixed_assets_blueprints
    register_fixed_assets_blueprints(app)

    from superadmin_app.routes import superadmin_bp
    app.register_blueprint(superadmin_bp)

    @app.context_processor
    def inject_now():
        return {"now": __import__("datetime").datetime.utcnow()}

    @app.context_processor
    def inject_company():
        # Company letterhead info for print headers on invoices/vouchers/forms.
        try:
            from shared.tenancy import current_company_id
            from shared.models.company import Company
            cid = current_company_id()
            if cid is not None:
                c = Company.query.get(cid)
                if c is not None:
                    return {"company": c}
            # Legacy single-company fallback.
            from shared.models.company_settings import CompanyInfo
            return {"company": CompanyInfo.get()}
        except Exception:
            return {"company": None}

    @app.context_processor
    def inject_navigation():
        """Sidebar helpers for templates/layouts/app_shell.html.

        Exposed as callables (not values) so the shell can pass the module_key
        it sets, which isn't known until the template renders.
        """
        from flask import request
        from shared.navigation import (MODULE_META, build_nav,
                                       accessible_modules)

        def nav_meta(module_key):
            return MODULE_META.get(module_key, MODULE_META["hr"])

        def nav_for(module_key):
            if not current_user.is_authenticated:
                return []
            ctx = {}
            try:
                from shared.models.inventory_settings import InventorySettings
                s = InventorySettings.get()
                ctx = {"purchase_flow": s.purchase_flow, "sales_flow": s.sales_flow}
            except Exception:
                pass
            return build_nav(module_key, current_user, request.endpoint, ctx)

        def nav_modules():
            if not current_user.is_authenticated:
                return []
            return accessible_modules(current_user)

        return {"nav_meta": nav_meta, "nav_for": nav_for, "nav_modules": nav_modules}

    # ── Number formatting helpers ──────────────────────────────────────────

    # The screen, the PDF and the spreadsheet all format money through
    # shared.formatting so a figure reads the same wherever it is seen.
    from shared.formatting import format_amount as _format_amount

    app.add_template_filter(_format_amount, name="amount_format")

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.hub"))
        return redirect(url_for("auth.login"))

    # Company switcher: sets the session key _set_company_context reads, so the
    # next request runs scoped to that company. Only valid for an ACTIVE
    # membership of the target company — otherwise _set_company_context would
    # silently fall back, which is a confusing "switch" for the user.
    @app.route("/company/switch/<int:company_id>")
    @login_required
    def company_switch(company_id):
        from flask import session as _session, flash
        from shared.models.company import CompanyMembership
        m = CompanyMembership.query.filter_by(
            company_id=company_id, user_id=current_user.id,
            status=CompanyMembership.ACTIVE).first()
        if m is None:
            flash("You don't have access to that company.", "error")
            return redirect(url_for("dashboard.hub"))
        _session["company_id"] = company_id
        nxt = request.args.get("next")
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("dashboard.hub"))

    # DB init (lazy — runs on first request, not at import)
    @app.before_request
    def _init_db_once():
        if not getattr(app, "_db_initialized", False):
            app._db_initialized = True
            try:
                db.create_all()
                _seed_all_data(app)
            except Exception as e:
                print("DB INIT ERROR:", e)
                _tb.print_exc()

    # Active company for the request (multi-company). Runs after _init_db_once
    # and after flask-login has loaded current_user. Super admin may also
    # browse any company via session["company_id"]; with none chosen, they
    # operate outside a company (portal), and tenancy fails closed for scoped
    # tables.
    @app.before_request
    def _set_company_context():
        from flask import session as _session
        from shared.models.company import CompanyMembership
        from shared.tenancy import set_current_company
        if not current_user.is_authenticated:
            set_current_company(None)
            return
        cid = _session.get("company_id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            m = CompanyMembership.query.filter_by(
                company_id=cid, user_id=current_user.id,
                status=CompanyMembership.ACTIVE).first()
            if m is not None:
                set_current_company(cid)
                return
        m = (CompanyMembership.query
             .filter_by(user_id=current_user.id,
                        status=CompanyMembership.ACTIVE)
             .order_by(CompanyMembership.joined_at).first())
        if m is not None:
            _session["company_id"] = m.company_id
            set_current_company(m.company_id)
        else:
            set_current_company(None)

    def _friendly_error_page(code, title, message):
        return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{code} · {title}</title>
<style>
body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#f1f5f9;color:#1e293b;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0}}
.box{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.06);
padding:40px 44px;max-width:440px;text-align:center}}
.code{{font-size:56px;font-weight:800;color:#2563eb;line-height:1;margin:0}}
h1{{font-size:20px;margin:12px 0 6px}}
p{{color:#64748b;font-size:14px;margin:0 0 22px}}
a{{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:9px 20px;
border-radius:6px;font-size:14px;font-weight:600}}
a:hover{{background:#1d4ed8}}
</style></head><body><div class='box'>
<p class='code'>{code}</p><h1>{title}</h1><p>{message}</p>
<a href='/'>&larr; Back to dashboard</a></div></body></html>"""

    @app.errorhandler(HTTPException)
    def handle_http(e):
        # Proper pages for expected HTTP errors (404 missing record, 403 denied,
        # 405, etc.) instead of dumping a traceback — this is what made links to
        # deleted/forbidden records look like a crash.
        messages = {
            403: "You don't have permission to view this page.",
            404: "The page or record you're looking for doesn't exist.",
            405: "That action isn't allowed here.",
        }
        msg = messages.get(e.code, e.description or "Something went wrong.")
        return _friendly_error_page(e.code, e.name, msg), e.code

    from shared.costing import NegativeStockError, ConsumedLayerError
    from shared.periods import ClosedPeriodError

    @app.errorhandler(NegativeStockError)
    @app.errorhandler(ConsumedLayerError)
    @app.errorhandler(ClosedPeriodError)
    def handle_costing_refusal(e):
        # Not a crash: the engine refused an operation that would have posted a
        # cost it cannot back, or touched a closed period. Every stock-moving
        # route can raise these, so they are handled once here rather than
        # wrapped at each of the ~20 call sites. Roll back first — the request
        # died mid-transaction and the partial voucher must not survive.
        from flask import flash, jsonify
        from shared.extensions import db as _db
        _db.session.rollback()
        # The unapprove endpoints are fetch()-driven and parse the body as
        # JSON; handing them a redirect to an HTML page fails silently in the
        # browser and looks like nothing happened.
        if request.accept_mimetypes.best_match(["application/json", "text/html"]) \
                == "application/json":
            return jsonify({"ok": False, "error": str(e)}), 409
        flash(str(e), "error")
        return redirect(request.referrer or url_for("dashboard.hub"))

    @app.errorhandler(Exception)
    def handle_all(e):
        # Genuine unexpected server error — keep the traceback (useful while the
        # app is being stabilised) but only for real 500s, not HTTP errors.
        return f"<pre style='background:#fef2f2;padding:20px;border:2px solid #ef4444;border-radius:8px;font-size:13px;overflow:auto;max-height:90vh;'>{_tb.format_exc()}</pre>", 500

    return app


def _migrate_schema(db):
    """Idempotent, cross-dialect schema migrations.

    Each ALTER runs in its OWN autocommit transaction (``engine.begin()``) so a
    single failure can never roll back the others — the previous design shared
    one session, so one bad statement aborted the whole batch. This MUST run
    before any ORM query touches the affected tables, otherwise SQLAlchemy emits
    SELECTs listing model columns that do not yet exist (the production
    "column chart_of_accounts.level does not exist" 500s).
    """
    from sqlalchemy import inspect

    engine = db.engine
    is_pg = engine.dialect.name == "postgresql"
    # Postgres rejects `BOOLEAN DEFAULT 0` — it needs FALSE/TRUE literals.
    bool_false = "BOOLEAN DEFAULT FALSE" if is_pg else "BOOLEAN DEFAULT 0"
    bool_true = "BOOLEAN DEFAULT TRUE" if is_pg else "BOOLEAN DEFAULT 1"
    ts_type = "TIMESTAMP" if is_pg else "DATETIME"

    # (table, column, column_type_ddl)
    migrations = [
        ("chart_of_accounts", "level", "INTEGER DEFAULT 4"),
        ("chart_of_accounts", "is_fixed", bool_false),
        ("accounting_periods", "is_active", bool_true),
        ("users", "has_hr_access", bool_false),
        ("users", "has_inventory_access", bool_false),
        ("users", "has_invoicing_access", bool_false),
        ("users", "has_finance_access", bool_false),
        ("users", "has_accounting_access", bool_false),
        ("users", "has_fbr_access", bool_false),
        ("users", "has_fixed_assets_access", bool_false),
        ("users", "login_id", "VARCHAR(120)"),
        ("users", "is_super_admin", bool_false),
        ("consumption_vouchers", "charge_account_id", "INTEGER"),
        ("scrap_vouchers", "charge_account_id", "INTEGER"),
        ("stock_ledger", "valuation_method", "VARCHAR(20)"),
        ("chart_of_accounts", "cash_flow_activity", "VARCHAR(20)"),
        ("chart_of_accounts", "pl_section", "VARCHAR(30)"),
        ("inv_invoices", "party_account_id", "INTEGER"),
        ("inv_purchase_invoices", "party_account_id", "INTEGER"),
        ("report_settings", "purchase_party_mode", "VARCHAR(10)"),
        ("report_settings", "sales_party_mode", "VARCHAR(10)"),
        ("report_settings", "purchase_template_text", "TEXT"),
        ("report_settings", "sales_template_text", "TEXT"),
        ("inv_suppliers", "mobile", "VARCHAR(200) DEFAULT ''"),
        ("inv_suppliers", "tax_id", "VARCHAR(200) DEFAULT ''"),
        ("inv_suppliers", "payment_terms", "VARCHAR(200) DEFAULT ''"),
        ("inv_suppliers", "website", "VARCHAR(200) DEFAULT ''"),
        ("inv_suppliers", "notes", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "contact_person", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "mobile", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "tax_id", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "payment_terms", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "website", "VARCHAR(200) DEFAULT ''"),
        ("inv_customers", "notes", "VARCHAR(200) DEFAULT ''"),
        ("inventory_settings", "purchase_flow", "VARCHAR(200) DEFAULT ''"),
        ("inventory_settings", "sales_flow", "VARCHAR(200) DEFAULT ''"),
        ("inv_invoices", "discount_mode", "VARCHAR(200) DEFAULT ''"),
        ("inv_invoices", "tax_mode", "VARCHAR(200) DEFAULT ''"),
        ("inv_invoices", "global_discount_pct", "FLOAT DEFAULT 0"),
        ("inv_invoices", "global_discount_value", "FLOAT DEFAULT 0"),
        ("inv_invoices", "global_sales_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_invoices", "subtotal", "FLOAT DEFAULT 0"),
        ("inv_invoices", "total_discount", "FLOAT DEFAULT 0"),
        ("inv_invoices", "total_tax", "FLOAT DEFAULT 0"),
        ("inv_invoices", "notes", "VARCHAR(200) DEFAULT ''"),
        ("inv_invoices", "created_by", "INTEGER"),
        ("inv_invoices", "voucher_number", "VARCHAR(50) DEFAULT ''"),
        ("inv_invoices", "voucher_status", "VARCHAR(20) DEFAULT 'unapproved'"),
        ("inv_invoices", "payment_status", "VARCHAR(20) DEFAULT 'unpaid'"),
        ("inv_invoices", "charges_mode", "VARCHAR(20) DEFAULT 'general'"),
        ("inv_invoices", "total_charges", "FLOAT DEFAULT 0"),
        ("inv_invoices", "global_delivery", "FLOAT DEFAULT 0"),
        ("inv_invoices", "global_installation", "FLOAT DEFAULT 0"),
        ("inv_invoices", "approved_by", "INTEGER"),
        ("inv_invoices", "approved_at", ts_type),
        ("inv_invoice_items", "delivery", "FLOAT DEFAULT 0"),
        ("inv_invoice_items", "installation", "FLOAT DEFAULT 0"),
        ("inv_invoice_items", "comments", "TEXT"),
        ("report_settings", "purchase_template_id", "INTEGER"),
        ("report_settings", "sales_template_id", "INTEGER"),
        ("report_settings", "cash_flow_method", "VARCHAR(20) DEFAULT 'indirect'"),
        ("invoice_templates", "design", "VARCHAR(20)"),
        ("invoice_templates", "accent_color", "VARCHAR(20)"),
        ("invoice_templates", "options_json", "TEXT"),
        ("inv_invoices", "further_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_invoices", "apply_further_tax", bool_false),
        ("inv_invoices", "withholding_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_invoices", "apply_withholding_tax", bool_false),
        ("inv_invoices", "total_further_tax", "FLOAT DEFAULT 0"),
        ("inv_invoices", "total_withholding_tax", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "further_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "apply_further_tax", bool_false),
        ("additional_charges", "distribution", "VARCHAR(20) DEFAULT 'pro_rata_value'"),
        # v3 §4 order->invoice linkage: how much of each order line has been
        # billed, the order's invoicing progress, and each invoice line's source.
        ("inv_sales_order_items", "invoiced_qty", "FLOAT DEFAULT 0"),
        ("inv_purchase_order_items", "invoiced_qty", "FLOAT DEFAULT 0"),
        ("inv_sales_orders", "fulfilment_status", "VARCHAR(20) DEFAULT 'open'"),
        ("inv_purchase_orders", "fulfilment_status", "VARCHAR(20) DEFAULT 'open'"),
        ("inv_invoice_items", "source_order_id", "INTEGER"),
        ("inv_invoice_items", "source_order_item_id", "INTEGER"),
        ("inv_invoice_items", "source_order_number", "VARCHAR(50) DEFAULT ''"),
        ("inv_purchase_invoice_items", "source_order_id", "INTEGER"),
        ("inv_purchase_invoice_items", "source_order_item_id", "INTEGER"),
        ("inv_purchase_invoice_items", "source_order_number", "VARCHAR(50) DEFAULT ''"),
        ("inv_purchase_invoices", "apply_withholding_tax", bool_false),
        ("inv_purchase_invoices", "total_further_tax", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "total_withholding_tax", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "total_amount", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "paid_amount", "FLOAT DEFAULT 0"),
        ("inv_purchase_invoices", "payment_status", "VARCHAR(20) DEFAULT 'unpaid'"),
        ("inv_purchase_invoices", "purchase_order_id", "INTEGER"),
        ("inv_products", "hs_code", "VARCHAR(50) DEFAULT ''"),
        ("inv_products", "weight", "FLOAT DEFAULT 0"),
        ("additional_charges", "manual_allocations", "TEXT DEFAULT ''"),
        ("inv_sales_orders", "party_account_id", "INTEGER"),
        ("inv_sales_orders", "expected_date", "DATE"),
        ("inv_sales_orders", "tax_mode", "VARCHAR(20) DEFAULT 'general'"),
        ("inv_sales_orders", "global_sales_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_sales_orders", "subtotal", "FLOAT DEFAULT 0"),
        ("inv_sales_orders", "total_tax", "FLOAT DEFAULT 0"),
        ("inv_sales_orders", "total_amount", "FLOAT DEFAULT 0"),
        ("inv_sales_orders", "approved_by", "INTEGER"),
        ("inv_sales_orders", "approved_at", ts_type),
        ("inv_sales_order_items", "description", "VARCHAR(200) DEFAULT ''"),
        ("inv_sales_order_items", "unit", "VARCHAR(20) DEFAULT 'pcs'"),
        ("inv_sales_order_items", "quantity", "FLOAT DEFAULT 1"),
        ("inv_sales_order_items", "sales_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_sales_order_items", "total_before_discount", "FLOAT DEFAULT 0"),
        ("inv_sales_order_items", "total_after_discount", "FLOAT DEFAULT 0"),
        ("inv_purchase_orders", "party_account_id", "INTEGER"),
        ("inv_purchase_orders", "tax_mode", "VARCHAR(20) DEFAULT 'general'"),
        ("inv_purchase_orders", "global_sales_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_purchase_orders", "subtotal", "FLOAT DEFAULT 0"),
        ("inv_purchase_orders", "total_tax", "FLOAT DEFAULT 0"),
        ("inv_purchase_orders", "total_amount", "FLOAT DEFAULT 0"),
        ("inv_purchase_orders", "driver_name", "VARCHAR(100) DEFAULT ''"),
        ("inv_purchase_orders", "driver_contact", "VARCHAR(50) DEFAULT ''"),
        ("inv_purchase_orders", "vehicle_number", "VARCHAR(50) DEFAULT ''"),
        ("inv_purchase_orders", "gate_pass", "VARCHAR(50) DEFAULT ''"),
        ("inv_purchase_orders", "approved_by", "INTEGER"),
        ("inv_purchase_orders", "approved_at", ts_type),
        ("inv_purchase_order_items", "description", "VARCHAR(200) DEFAULT ''"),
        ("inv_purchase_order_items", "unit", "VARCHAR(20) DEFAULT 'pcs'"),
        ("inv_purchase_order_items", "quantity", "FLOAT DEFAULT 1"),
        ("inv_purchase_order_items", "sales_tax_pct", "FLOAT DEFAULT 0"),
        ("inv_purchase_order_items", "total_before_discount", "FLOAT DEFAULT 0"),
        ("inv_purchase_order_items", "total_after_discount", "FLOAT DEFAULT 0"),
        # v3 ERP-standard: per-charge treatment + independent tax-base switches
        ("additional_charges", "treatment", "VARCHAR(10) DEFAULT 'bill'"),
        ("additional_charges", "st_taxable", "BOOLEAN DEFAULT 1"),
        ("additional_charges", "wht_taxable", "BOOLEAN DEFAULT 0"),
        ("additional_charges", "extra_taxable", "BOOLEAN DEFAULT 0"),
        # invoice_settings — §11 admin defaults, tolerance, field visibility
        ("invoice_settings", "over_invoice_tolerance_pct", "FLOAT DEFAULT 0"),
        ("invoice_settings", "withholding_base", "VARCHAR(10) DEFAULT 'taxable'"),
        ("invoice_settings", "show_further_tax", "BOOLEAN DEFAULT 1"),
        ("invoice_settings", "show_withholding_tax", "BOOLEAN DEFAULT 1"),
        ("invoice_settings", "show_transport_block", "BOOLEAN DEFAULT 1"),
        ("invoice_settings", "create_from_orders_enabled", "BOOLEAN DEFAULT 1"),
        ("invoice_settings", "per_line_discount_enabled", "BOOLEAN DEFAULT 1"),
        ("invoice_settings", "per_line_tax_enabled", "BOOLEAN DEFAULT 1"),
        ("company_info", "number_format", "VARCHAR(10) DEFAULT 'en'"),
        # Remittance details printed opposite the totals on a sales invoice
        ("company_info", "bank_name", "VARCHAR(200)"),
        ("company_info", "bank_account_title", "VARCHAR(200)"),
        ("company_info", "bank_account_number", "VARCHAR(100)"),
        # Fixed Assets Management module columns
        ("fixed_assets", "fixed_asset_account_id", "INTEGER"),
        ("fixed_assets", "accum_dep_account_id", "INTEGER"),
        ("fixed_assets", "dep_expense_account_id", "INTEGER"),
        ("fixed_assets", "acquisition_credit_account_id", "INTEGER"),
        ("asset_depreciation", "journal_entry_id", "INTEGER"),
    ]

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, col, ddl in migrations:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if col in cols:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        except Exception as e:
            print(f"MIGRATION SKIP {table}.{col}: {e}")

    # Columns added by an EARLIER version of the list above, with the wrong
    # type. The add loop skips any column that already exists, so a column that
    # arrived as VARCHAR stays VARCHAR forever no matter what the model says.
    #
    # On SQLite that is harmless — it is typeless in practice. On Postgres it is
    # fatal to every page that reads the table: psycopg2 hands SQLAlchemy's
    # numeric result processor a varchar OID and the query dies with
    # "Unknown PG numeric type: 1043", which is what took out the invoice list,
    # the invoice form and everything else touching inv_invoices.
    #
    # NULLIF(...,'') matters: these columns hold '' where they should hold NULL,
    # and a bare cast of '' to double precision raises.
    retyped_columns = [
        ("inv_invoices", "global_discount_pct", "DOUBLE PRECISION"),
        ("inv_invoices", "global_discount_value", "DOUBLE PRECISION"),
        ("inv_invoices", "global_sales_tax_pct", "DOUBLE PRECISION"),
        ("inv_invoices", "subtotal", "DOUBLE PRECISION"),
        ("inv_invoices", "total_discount", "DOUBLE PRECISION"),
        ("inv_invoices", "total_tax", "DOUBLE PRECISION"),
        ("inv_invoices", "total_charges", "DOUBLE PRECISION"),
        ("inv_invoices", "global_delivery", "DOUBLE PRECISION"),
        ("inv_invoices", "global_installation", "DOUBLE PRECISION"),
        ("inv_invoices", "further_tax_pct", "DOUBLE PRECISION"),
        ("inv_invoices", "withholding_tax_pct", "DOUBLE PRECISION"),
        ("inv_invoices", "total_further_tax", "DOUBLE PRECISION"),
        ("inv_invoices", "total_withholding_tax", "DOUBLE PRECISION"),
        ("inv_invoices", "created_by", "INTEGER"),
        ("inv_purchase_invoices", "further_tax_pct", "DOUBLE PRECISION"),
        ("inv_purchase_invoices", "total_further_tax", "DOUBLE PRECISION"),
        ("inv_purchase_invoices", "total_withholding_tax", "DOUBLE PRECISION"),
        ("inv_purchase_invoices", "total_amount", "DOUBLE PRECISION"),
        ("inv_purchase_invoices", "paid_amount", "DOUBLE PRECISION"),
        ("inv_purchase_invoices", "purchase_order_id", "INTEGER"),
        ("inv_invoice_items", "delivery", "DOUBLE PRECISION"),
        ("inv_invoice_items", "installation", "DOUBLE PRECISION"),
        ("inv_invoice_items", "source_order_id", "INTEGER"),
        ("inv_invoice_items", "source_order_item_id", "INTEGER"),
        ("inv_purchase_invoice_items", "source_order_id", "INTEGER"),
        ("inv_purchase_invoice_items", "source_order_item_id", "INTEGER"),
        ("inv_products", "weight", "DOUBLE PRECISION"),
    ]
    if is_pg:
        conn = engine.connect()
        try:
            for table, col, target in retyped_columns:
                if table not in existing_tables:
                    continue
                current = {c["name"]: c["type"].__class__.__name__.upper()
                           for c in inspector.get_columns(table)}
                if col not in current or current[col] != "VARCHAR":
                    continue
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {target} "
                        f"USING NULLIF(TRIM({col}), '')::{target}"))
                    conn.commit()
                    print(f"MIGRATION retyped {table}.{col} -> {target}")
                except Exception as e:
                    conn.rollback()
                    print(f"MIGRATION SKIP retype {table}.{col}: {e}")
        finally:
            conn.close()

    # Charges, discount, withholding and further tax are not eligible on an
    # order — they are decided at invoicing. The columns are dropped rather than
    # left dormant so nothing can quietly read a stale figure, and the entries
    # above were deleted at the same time: this loop and that one would
    # otherwise fight, re-adding on every boot what this had just removed.
    obsolete_order_columns = [
        ("inv_sales_orders", "discount_mode"),
        ("inv_sales_orders", "charges_mode"),
        ("inv_sales_orders", "global_discount_pct"),
        ("inv_sales_orders", "global_discount_value"),
        ("inv_sales_orders", "global_delivery"),
        ("inv_sales_orders", "global_installation"),
        ("inv_sales_orders", "further_tax_pct"),
        ("inv_sales_orders", "apply_further_tax"),
        ("inv_sales_orders", "withholding_tax_pct"),
        ("inv_sales_orders", "apply_withholding_tax"),
        ("inv_sales_orders", "total_discount"),
        ("inv_sales_orders", "total_charges"),
        ("inv_sales_orders", "total_further_tax"),
        ("inv_sales_orders", "total_withholding_tax"),
        ("inv_sales_order_items", "discount_pct"),
        ("inv_sales_order_items", "discount_amount"),
        ("inv_sales_order_items", "delivery"),
        ("inv_sales_order_items", "installation"),
        ("inv_purchase_orders", "discount_mode"),
        ("inv_purchase_orders", "charges_mode"),
        ("inv_purchase_orders", "global_discount_pct"),
        ("inv_purchase_orders", "global_discount_value"),
        ("inv_purchase_orders", "further_tax_pct"),
        ("inv_purchase_orders", "apply_further_tax"),
        ("inv_purchase_orders", "withholding_tax_pct"),
        ("inv_purchase_orders", "apply_withholding_tax"),
        ("inv_purchase_orders", "total_discount"),
        ("inv_purchase_orders", "total_charges"),
        ("inv_purchase_orders", "total_further_tax"),
        ("inv_purchase_orders", "total_withholding_tax"),
        ("inv_purchase_order_items", "discount_pct"),
        ("inv_purchase_order_items", "discount_amount"),
    ]
    for table, col in obsolete_order_columns:
        if table not in existing_tables:
            continue
        if col not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        except Exception as e:
            # SQLite gained DROP COLUMN in 3.35. An older build just keeps the
            # column; the models no longer map it, so it is inert either way.
            print(f"MIGRATION SKIP drop {table}.{col}: {e}")

    # Charge rows attached to orders have nothing left to belong to.
    try:
        with engine.begin() as conn:
            conn.execute(db.text(
                "DELETE FROM additional_charges WHERE doc_type IN ('SO', 'PO')"))
    except Exception as e:
        print("MIGRATION SKIP order charge cleanup:", e)

    # Legacy GLOBAL unique index on inv_invoices.voucher_number. It predates
    # the composite (company_id, voucher_number) constraint and must go — with
    # it in place company 2's first invoice collides with company 1's. On
    # SQLite the constraint-rebuild phase below drops it with the table; this
    # is the belt-and-braces drop for DBs already past the rebuild (best
    # effort — it may not exist).
    try:
        with engine.begin() as conn:
            conn.execute(db.text(
                "DROP INDEX IF EXISTS ix_inv_invoices_voucher_number"))
    except Exception as e:
        print("MIGRATION SKIP drop ix_inv_invoices_voucher_number:", e)

    # Create additional_charges table if not exists
    # Create invoice_settings table if not exists
    try:
        with engine.begin() as conn:
            conn.execute(db.text("""
                CREATE TABLE IF NOT EXISTS invoice_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    default_sales_tax_pct FLOAT DEFAULT 0,
                    default_further_tax_pct FLOAT DEFAULT 0,
                    default_withholding_tax_pct FLOAT DEFAULT 0,
                    default_discount_pct FLOAT DEFAULT 0,
                    default_charges_mode VARCHAR(20) DEFAULT 'general',
                    default_discount_mode VARCHAR(20) DEFAULT 'general',
                    default_tax_mode VARCHAR(20) DEFAULT 'general',
                    show_discount_column BOOLEAN DEFAULT 1,
                    show_charges_column BOOLEAN DEFAULT 1,
                    show_tax_column BOOLEAN DEFAULT 1,
                    auto_add_line BOOLEAN DEFAULT 1,
                    require_approval BOOLEAN DEFAULT 1,
                    allow_partial_payment BOOLEAN DEFAULT 1,
                    default_party_mode VARCHAR(10) DEFAULT 'relevant',
                    over_invoice_tolerance_pct FLOAT DEFAULT 0,
                    withholding_base VARCHAR(10) DEFAULT 'taxable',
                    show_further_tax BOOLEAN DEFAULT 1,
                    show_withholding_tax BOOLEAN DEFAULT 1,
                    show_transport_block BOOLEAN DEFAULT 1,
                    create_from_orders_enabled BOOLEAN DEFAULT 1,
                    per_line_discount_enabled BOOLEAN DEFAULT 1,
                    per_line_tax_enabled BOOLEAN DEFAULT 1
                )
            """))
    except Exception as e:
        print("MIGRATION SKIP invoice_settings table:", e)

    try:
        with engine.begin() as conn:
            conn.execute(db.text("""
                CREATE TABLE IF NOT EXISTS additional_charges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_type VARCHAR(10) NOT NULL,
                    doc_id INTEGER NOT NULL,
                    charge_account_id INTEGER NOT NULL REFERENCES chart_of_accounts(id),
                    description VARCHAR(200),
                    amount FLOAT DEFAULT 0,
                    scope VARCHAR(20) DEFAULT 'general',
                    treatment VARCHAR(10) DEFAULT 'bill',
                    st_taxable BOOLEAN DEFAULT 1,
                    wht_taxable BOOLEAN DEFAULT 0,
                    extra_taxable BOOLEAN DEFAULT 0,
                    taxable BOOLEAN DEFAULT 1,
                    tax_base VARCHAR(30) DEFAULT 'after_discount',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
    except Exception as e:
        print("MIGRATION SKIP additional_charges table:", e)

    # ── Multi-company: company_id column + index on every scoped table ─────
    # Generic over the model registry so a new scoped model needs no entry
    # here. Same best-effort per-statement pattern as the rest of this
    # function. Backfill happens in _seed_all_data AFTER this runs.
    from shared.tenancy import scoped_model_classes, _reset_registry
    _reset_registry()
    for table, _cls in scoped_model_classes().items():
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "company_id" not in cols:
            try:
                with engine.begin() as conn:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} ADD COLUMN company_id INTEGER"))
            except Exception as e:
                print(f"MIGRATION SKIP {table}.company_id: {e}")
        try:
            with engine.begin() as conn:
                conn.execute(db.text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_company_id "
                    f"ON {table}(company_id)"))
        except Exception as e:
            print(f"MIGRATION SKIP ix_{table}_company_id: {e}")

    # ── Multi-company: rebuild GLOBAL unique constraints as composite
    # (company_id, ...) on every tenant-scoped table ─────────────────────────
    # Every unique on a scoped table used to be global, so company 2's INSERT
    # collided with company 1's rows (voucher_numbers.prefix, chart_of_accounts
    # .code, inv_invoices.invoice_number, ...). The models now declare the
    # composite form; this phase makes EXISTING databases match.
    #
    # SQLite cannot ALTER a constraint, so the table is rebuilt from the
    # current model schema (CREATE <t>_new + INSERT SELECT + DROP + RENAME).
    # Postgres gets DROP CONSTRAINT + ADD CONSTRAINT. Idempotent: when the
    # composite constraint is already present the phase is a no-op, so a
    # second boot does nothing.
    #
    # (table, [(new_constraint_name, (cols, ...)), ...],
    #  (old constraint/index names to drop on Postgres, ...))
    constraint_rebuilds = [
        ("chart_of_accounts",
         [("uq_chart_of_accounts_code", ("company_id", "code"))],
         ("uq_chart_of_accounts_code", "chart_of_accounts_code_key")),
        ("voucher_numbers",
         [("uq_voucher_numbers_prefix", ("company_id", "prefix"))],
         ("uq_voucher_numbers_prefix", "voucher_numbers_prefix_key")),
        ("consumption_vouchers",
         [("uq_consumption_vouchers_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_consumption_vouchers_voucher_number",
          "consumption_vouchers_voucher_number_key")),
        ("scrap_vouchers",
         [("uq_scrap_vouchers_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_scrap_vouchers_voucher_number",
          "scrap_vouchers_voucher_number_key")),
        ("stock_adjustment_vouchers",
         [("uq_stock_adjustment_vouchers_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_stock_adjustment_vouchers_voucher_number",
          "stock_adjustment_vouchers_voucher_number_key")),
        ("stock_takes",
         [("uq_stock_takes_reference", ("company_id", "reference"))],
         ("uq_stock_takes_reference", "stock_takes_reference_key")),
        ("accounting_vouchers",
         [("uq_accounting_vouchers_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_accounting_vouchers_voucher_number",
          "accounting_vouchers_voucher_number_key")),
        ("asset_transfers",
         [("uq_asset_transfers_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_asset_transfers_voucher_number",
          "asset_transfers_voucher_number_key")),
        ("charge_ledger_defaults",
         [("uq_charge_ledger_defaults_account_id",
           ("company_id", "account_id"))],
         ("uq_charge_ledger_defaults_account_id",
          "charge_ledger_defaults_account_id_key")),
        ("category_revenue_accounts",
         [("uq_category_revenue_accounts_category_id",
           ("company_id", "category_id"))],
         ("uq_category_revenue_accounts_category_id",
          "category_revenue_accounts_category_id_key")),
        ("tax_rate_accounts",
         [("uq_tax_rate_accounts_rate_pct", ("company_id", "rate_pct"))],
         ("uq_tax_rate_accounts_rate_pct", "tax_rate_accounts_rate_pct_key")),
        ("inv_products",
         [("uq_inv_products_sku", ("company_id", "sku"))],
         ("uq_inv_products_sku", "inv_products_sku_key")),
        ("inv_units",
         [("uq_inv_units_name", ("company_id", "name")),
          ("uq_inv_units_abbreviation", ("company_id", "abbreviation"))],
         ("uq_inv_units_name", "inv_units_name_key",
          "uq_inv_units_abbreviation", "inv_units_abbreviation_key")),
        ("inv_invoices",
         [("uq_inv_invoices_invoice_number", ("company_id", "invoice_number")),
          ("uq_inv_invoices_voucher_number", ("company_id", "voucher_number"))],
         ("uq_inv_invoices_invoice_number", "inv_invoices_invoice_number_key",
          "uq_inv_invoices_voucher_number", "inv_invoices_voucher_number_key",
          "ix_inv_invoices_voucher_number")),
        ("inv_purchase_invoices",
         [("uq_inv_purchase_invoices_invoice_number",
           ("company_id", "invoice_number")),
          ("uq_inv_purchase_invoices_voucher_number",
           ("company_id", "voucher_number"))],
         ("uq_inv_purchase_invoices_invoice_number",
          "inv_purchase_invoices_invoice_number_key",
          "uq_inv_purchase_invoices_voucher_number",
          "inv_purchase_invoices_voucher_number_key")),
        ("inv_purchase_orders",
         [("uq_inv_purchase_orders_po_number", ("company_id", "po_number"))],
         ("uq_inv_purchase_orders_po_number",
          "inv_purchase_orders_po_number_key")),
        ("inv_sales_orders",
         [("uq_inv_sales_orders_so_number", ("company_id", "so_number"))],
         ("uq_inv_sales_orders_so_number", "inv_sales_orders_so_number_key")),
        ("inv_purchase_returns",
         [("uq_inv_purchase_returns_return_number",
           ("company_id", "return_number"))],
         ("uq_inv_purchase_returns_return_number",
          "inv_purchase_returns_return_number_key")),
        ("inv_sales_returns",
         [("uq_inv_sales_returns_return_number",
           ("company_id", "return_number"))],
         ("uq_inv_sales_returns_return_number",
          "inv_sales_returns_return_number_key")),
        ("leave_types",
         [("uq_leave_types_name", ("company_id", "name")),
          ("uq_leave_types_code", ("company_id", "code"))],
         ("uq_leave_types_name", "leave_types_name_key",
          "uq_leave_types_code", "leave_types_code_key")),
        ("projects",
         [("uq_projects_code", ("company_id", "code"))],
         ("uq_projects_code", "projects_code_key")),
        ("file_categories",
         [("uq_file_categories_name", ("company_id", "name"))],
         ("uq_file_categories_name", "file_categories_name_key")),
        ("payroll_profiles",
         [("uq_payroll_profiles_user_id", ("company_id", "user_id"))],
         ("uq_payroll_profiles_user_id", "payroll_profiles_user_id_key")),
        ("fa_categories",
         [("uq_fa_categories_name", ("company_id", "name"))],
         ("uq_fa_categories_name", "fa_categories_name_key")),
        ("fixed_assets",
         [("uq_fixed_assets_asset_code", ("company_id", "asset_code"))],
         ("uq_fixed_assets_asset_code", "fixed_assets_asset_code_key")),
        ("attendance",
         [("uq_attendance_company_date", ("company_id", "user_id", "date"))],
         ("uq_attendance_company_date", "uq_attendance_date")),
        ("timesheet_weeks",
         [("uq_ts_week_company", ("company_id", "user_id", "week_start"))],
         ("uq_ts_week_company", "uq_ts_week")),
        ("leave_quotas",
         [("uq_leave_quota_company",
           ("company_id", "user_id", "leave_type_id", "year"))],
         ("uq_leave_quota_company", "uq_leave_quota")),
        ("pf_contributions",
         [("uq_pf_contribution_company",
           ("company_id", "user_id", "month", "year"))],
         ("uq_pf_contribution_company", "uq_pf_contribution")),
        ("company_holidays",
         [("uq_holiday_date_dept_company",
           ("company_id", "holiday_date", "department"))],
         ("uq_holiday_date_dept_company", "uq_holiday_date_dept")),
        ("overtime_accounts",
         [("uq_overtime_date_company", ("company_id", "user_id", "date"))],
         ("uq_overtime_date_company", "uq_overtime_date")),
        ("button_permissions",
         [("uq_button_perm_company", ("company_id", "role_id", "button_code"))],
         ("uq_button_perm_company", "uq_button_perm")),
        ("payroll_runs",
         [("uq_payroll_run_company", ("company_id", "month", "year"))],
         ("uq_payroll_run_company", "uq_payroll_run")),
        ("payroll_slips",
         [("uq_payroll_slip_company",
           ("company_id", "payroll_run_id", "user_id"))],
         ("uq_payroll_slip_company", "uq_payroll_slip")),
    ]

    if is_pg:
        for table, new_constrs, old_names in constraint_rebuilds:
            if table not in existing_tables:
                continue
            try:
                with engine.connect() as conn:
                    have = {r[0] for r in conn.execute(db.text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = to_regclass(:t)"),
                        {"t": table})}
                if all(n in have for n, _c in new_constrs):
                    continue
                with engine.begin() as conn:
                    for old in old_names:
                        # Legacy entries are constraints OR the unique INDEX
                        # ix_inv_invoices_voucher_number — try both shapes.
                        try:
                            conn.execute(db.text(
                                f"ALTER TABLE {table} "
                                f"DROP CONSTRAINT IF EXISTS {old}"))
                        except Exception:
                            pass
                        try:
                            conn.execute(db.text(
                                f"DROP INDEX IF EXISTS {old}"))
                        except Exception:
                            pass
                    for name, cols in new_constrs:
                        if name in have:
                            continue
                        col_sql = ", ".join(cols)
                        conn.execute(db.text(
                            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                            f"UNIQUE ({col_sql})"))
                print(f"MIGRATION composite unique on {table}: "
                      f"{[n for n, _c in new_constrs]}")
            except Exception as e:
                print(f"MIGRATION SKIP composite {table}: {e}")
    else:
        from sqlalchemy.schema import CreateIndex, CreateTable
        for table, new_constrs, _old in constraint_rebuilds:
            if table not in existing_tables:
                continue
            try:
                with engine.connect() as conn:
                    # SQLite never names the backing index of a table-level
                    # UNIQUE constraint (it is always sqlite_autoindex_<t>_<n>),
                    # so presence is detected in the table's own DDL text,
                    # which keeps the CONSTRAINT name + columns verbatim.
                    row = conn.execute(db.text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = :t"),
                        {"t": table}).fetchone()
                ddl_now = (row[0] if row else "") or ""
                if all(
                        f"CONSTRAINT {n} UNIQUE ({', '.join(c)})" in ddl_now
                        for n, c in new_constrs):
                    continue
                model_table = db.Model.metadata.tables.get(table)
                if model_table is None:
                    print(f"MIGRATION SKIP rebuild {table}: "
                          "no model table in metadata")
                    continue
                legacy_cols = {c["name"]
                               for c in inspector.get_columns(table)}
                common = [c.name for c in model_table.columns
                          if c.name in legacy_cols]
                col_list = ", ".join(f'"{c}"' for c in common)
                # CreateTable compiles the model's own table name; the staging
                # table needs the _new name (FK targets resolve fine — they
                # live in the same metadata).
                ddl = str(CreateTable(model_table).compile(
                    dialect=engine.dialect)).replace(
                    f"CREATE TABLE {table}",
                    f"CREATE TABLE {table}_new", 1)
                with engine.begin() as conn:
                    conn.execute(db.text(ddl))
                    conn.execute(db.text(
                        f'INSERT INTO "{table}_new" ({col_list}) '
                        f'SELECT {col_list} FROM "{table}"'))
                    conn.execute(db.text(f'DROP TABLE "{table}"'))
                    conn.execute(db.text(
                        f'ALTER TABLE "{table}_new" RENAME TO "{table}"'))
                    for idx in model_table.indexes:
                        conn.execute(db.text(str(
                            CreateIndex(idx).compile(
                                dialect=engine.dialect))))
                print(f"MIGRATION rebuilt {table} with composite unique "
                      f"{[n for n, _c in new_constrs]}")
            except Exception as e:
                # A failed INSERT SELECT (duplicate codes across companies in
                # the data) leaves the staging table behind; clear it so the
                # next boot retries cleanly instead of colliding with it.
                try:
                    with engine.begin() as conn:
                        conn.execute(db.text(
                            f'DROP TABLE IF EXISTS "{table}_new"'))
                except Exception:
                    pass
                print(f"MIGRATION SKIP rebuild {table}: {e}")


def _bootstrap_default_company(db):
    """First boot / legacy-DB migration into multi-company mode.

    1. Create the Default Company (idempotent).
    2. Backfill company_id on every scoped table to it.
    3. Migrate users -> active memberships (role + employee_code carried
       over from the legacy global columns).
    4. Mark SYSADMIN as super admin.
    Then select the default company so the rest of seeding is scoped to it.
    """
    from shared.models.company import (Company, CompanyMembership,
                                       GlobalLimits)
    from shared.models.base import User, Role
    from shared.tenancy import set_current_company, scoped_model_classes

    company = Company.query.filter_by(slug="default").first()
    if company is None:
        company = Company(name="Default Company", slug="default",
                          is_active=True, plan_name="free")
        db.session.add(company)
        db.session.commit()

    # Backfill company_id = default company on every scoped table.
    # The backfill writes through engine.begin() (raw SQL) — a DIFFERENT
    # pooled connection than the session's. SQLite blocks that write for as
    # long as the session still holds an open transaction from earlier
    # seeding (its read lock never releases to the second connection), so
    # commit any pending session work first. No-op when the session is clean
    # (first bootstrap), and required at the second bootstrap, which runs
    # after the bulk of _seed_all_data's uncommitted work.
    db.session.commit()
    for table, _cls in scoped_model_classes().items():
        try:
            with db.engine.begin() as conn:
                conn.execute(db.text(
                    f"UPDATE {table} SET company_id = :cid "
                    f"WHERE company_id IS NULL OR company_id = 0"),
                    {"cid": company.id})
        except Exception as e:
            print(f"BACKFILL SKIP {table}: {e}")

    set_current_company(company.id)

    # Legacy users -> memberships (idempotent).
    for u in User.query.all():
        if CompanyMembership.query.filter_by(
                company_id=company.id, user_id=u.id).first():
            continue
        role_id = u.role_id if u.role_id else None
        if role_id is None:
            admin = Role.query.filter_by(name=Role.ADMIN).first()
            role_id = admin.id if admin else None
        if role_id is None:
            continue
        db.session.add(CompanyMembership(
            company_id=company.id, user_id=u.id, role_id=role_id,
            status=CompanyMembership.ACTIVE, employee_code=u.employee_code))

    # SYSADMIN becomes the super admin.
    sysadmin = User.query.filter_by(employee_code="SYSADMIN").first()
    if sysadmin is not None:
        sysadmin.is_super_admin = True

    GlobalLimits.get()
    db.session.commit()
    return company


def _seed_all_data(app):
    with app.app_context():
        from shared.extensions import db
        from shared.models.base import User, Role, Permission
        from shared.models.ledger import ChartOfAccount
        from shared.models.stock_ledger import VoucherNumber, StockLedger
        from shared.models.stock_layer import StockLayer, LayerConsumption
        from shared.models.inventory_settings import InventorySettings

        # Run schema migrations FIRST, before any ORM query, so model columns
        # added after the initial deploy are guaranteed to exist.
        _migrate_schema(db)

        # Multi-company: default company + company_id backfill + memberships.
        # Must run before the first scoped ORM query below, and it selects the
        # default company so all subsequent seeding is scoped to it.
        _bootstrap_default_company(db)

        Role.seed()
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        mgr_role = Role.query.filter_by(name=Role.MANAGER).first()
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()

        for role, data in [(admin_role, [
            ("attendance", 1, 1, 1), ("leaves", 1, 1, 1),
            ("ess", 1, 1, 1), ("reports", 1, 1, 1),
            ("mss", 1, 1, 1), ("workplace", 1, 1, 1),
            ("timesheets", 1, 1, 1), ("digital_files", 1, 1, 1),
            ("compensation", 1, 1, 1), ("communications", 1, 1, 1),
            ("pf", 1, 1, 1), ("users", 1, 1, 1),
            ("products", 1, 1, 1), ("suppliers", 1, 1, 1),
            ("purchase_invoice", 1, 1, 1), ("purchase_return", 1, 1, 1),
            ("sales", 1, 1, 1), ("inventory", 1, 1, 1),
        ]), (mgr_role, [
            ("attendance", 1, 1, 0), ("leaves", 1, 1, 0),
            ("ess", 1, 1, 0), ("reports", 1, 0, 0),
            ("mss", 1, 1, 0), ("workplace", 1, 1, 0),
            ("timesheets", 1, 1, 0), ("digital_files", 1, 0, 0),
            ("compensation", 1, 0, 0), ("communications", 1, 0, 0),
            ("pf", 1, 0, 0), ("users", 0, 0, 0),
            ("products", 1, 1, 0), ("suppliers", 1, 1, 0),
            ("purchase_invoice", 1, 1, 0), ("purchase_return", 1, 1, 0),
            ("sales", 1, 1, 0), ("inventory", 1, 0, 0),
        ]), (emp_role, [
            ("attendance", 1, 1, 0), ("leaves", 1, 1, 0),
            ("ess", 1, 1, 0), ("reports", 0, 0, 0),
            ("mss", 0, 0, 0), ("workplace", 1, 0, 0),
            ("timesheets", 1, 1, 0), ("digital_files", 1, 0, 0),
            ("compensation", 0, 0, 0), ("communications", 1, 0, 0),
            ("pf", 1, 0, 0), ("users", 0, 0, 0),
            ("products", 0, 0, 0), ("suppliers", 0, 0, 0),
            ("purchase_invoice", 0, 0, 0), ("purchase_return", 0, 0, 0),
            ("sales", 0, 0, 0), ("inventory", 0, 0, 0),
        ])]:
            for resource, cr, cw, cd in data:
                from shared.models.base import Permission
                if not Permission.query.filter_by(role_id=role.id, resource=resource).first():
                    db.session.add(Permission(role_id=role.id, resource=resource,
                                              can_read=bool(cr), can_write=bool(cw), can_delete=bool(cd)))

        from fixed_assets_app.models.asset import AssetCategory as FAssetCat
        FAssetCat.seed()

        for prefix in ["PI", "PR", "CONS", "SCRAP", "ADJ", "ST", "CPV", "CRV", "BPV", "BRV", "JV", "PRL", "FA-TRF", "INV-FA"]:
            if not VoucherNumber.query.filter_by(prefix=prefix).first():
                db.session.add(VoucherNumber(prefix=prefix, next_number=1))

        # Fixed five-level segmented chart of accounts. Also migrates any
        # legacy chart (flat 1000-series or old 111-series) onto the new tree
        # in place, preserving all journal history.
        from shared.coa import ensure_fixed_coa
        ensure_fixed_coa()

        db.session.commit()

        for u in User.query.all():
            if not u.has_hr_access and not u.has_inventory_access:
                u.has_hr_access = True
                u.has_inventory_access = (u.role_id == admin_role.id or u.role_id == mgr_role.id)

        # One-time backfill: invoicing was split out of inventory, so users who
        # had inventory access keep working in the new Invoicing module.
        if not User.query.filter_by(has_invoicing_access=True).first():
            for u in User.query.all():
                u.has_invoicing_access = bool(u.has_inventory_access)

        # NOTE: uses u.role_id (legacy global role) rather than
        # is_admin()/is_manager(): those route through the per-company
        # membership chain, which is not guaranteed to exist mid-seed (and
        # whose role_name() helper is broken while multi-company lands).
        if not User.query.filter_by(has_fbr_access=True).first():
            for u in User.query.all():
                if u.role_id == admin_role.id:
                    u.has_fbr_access = True

        if not User.query.filter_by(has_fixed_assets_access=True).first():
            for u in User.query.all():
                u.has_fixed_assets_access = bool(
                    u.role_id in (admin_role.id, mgr_role.id))

        seed_users = [
            # Built-in system administrator — always present, hidden from the HR
            # module, manageable only via ERP hub Settings. Created once; a
            # changed password is never reset by seeding.
            ("SYSADMIN", "admin@gmail.com", "Administrator", admin_role.id, "admin123", True, True, "System Administrator"),
            ("ADM001", "admin@solarkon.com", "System Admin", admin_role.id, "admin123", True, True, "Administrator"),
            ("MGR002", "manager@solarkon.com", "Manager User", mgr_role.id, "mgr123", True, True, "Manager"),
            ("EMP001", "emp@solarkon.com", "Employee User", emp_role.id, "emp123", True, False, "Employee"),
            ("EMP002", "john.doe@solarkon.com", "John Doe", emp_role.id, "emp123", True, False, "Employee"),
        ]
        for code, email, name, rid, pw, hr, inv, desig in seed_users:
            u = User.query.filter_by(email=email).first()
            if not u:
                u = User(employee_code=code, email=email, full_name=name, role_id=rid,
                         has_hr_access=hr, has_inventory_access=inv, is_active=True, designation=desig)
                u.set_password(pw)
                db.session.add(u)

        from inventory_app.models.product import InvProduct
        from inventory_app.models.category import InvCategory
        from inventory_app.models.supplier import InvSupplier
        from inventory_app.models.customer import InvCustomer
        from inventory_app.models.unit import InvUnit

        cat_names = ["Solar Panels", "Inverters", "Batteries", "Cables & Wiring", "Mounting Structures",
                     "Electrical Components", "Tools & Equipment", "Safety Gear"]
        cat_map = {}
        for cn in cat_names:
            c = InvCategory.query.filter_by(name=cn).first()
            if not c:
                c = InvCategory(name=cn, description=f"{cn} category")
                db.session.add(c)
                db.session.flush()
            cat_map[cn] = c.id

        unit_seed = [
            ("Piece", "pcs", "Individual unit count"),
            ("Kilogram", "kg", "Weight in kilograms"),
            ("Gram", "g", "Weight in grams"),
            ("Meter", "m", "Length in meters"),
            ("Liter", "l", "Volume in liters"),
            ("Box", "box", "Box or carton"),
            ("Set", "set", "Complete set"),
            ("Pair", "pair", "Two units"),
            ("Dozen", "doz", "12 units"),
            ("Square Meter", "sqm", "Area measurement"),
            ("Kilowatt", "kW", "Power rating"),
            ("Watt", "W", "Power rating"),
            ("Ampere", "A", "Current measurement"),
            ("Volt", "V", "Voltage measurement"),
        ]
        for name, abbr, expl in unit_seed:
            if not InvUnit.query.filter_by(name=name).first():
                db.session.add(InvUnit(name=name, abbreviation=abbr, explanation=expl))
        db.session.flush()

        for sku, name, cat, price, cost, stock, reorder in [
            ("SOL-MONO-450", "Monocrystalline Solar Panel 450W", cat_names[0], 32000, 28000, 50, 10),
            ("SOL-MONO-550", "Monocrystalline Solar Panel 550W", cat_names[0], 42000, 37000, 30, 5),
            ("SOL-POLY-330", "Polycrystalline Solar Panel 330W", cat_names[0], 22000, 18500, 40, 8),
            ("INV-5KW", "5kW Hybrid Inverter", cat_names[1], 85000, 72000, 15, 3),
            ("INV-10KW", "10kW Hybrid Inverter", cat_names[1], 145000, 125000, 10, 2),
            ("INV-3KW", "3kW String Inverter", cat_names[1], 55000, 46000, 20, 4),
            ("BAT-LFP-5KWH", "5kWh LiFePO4 Battery", cat_names[2], 180000, 155000, 25, 5),
            ("BAT-LFP-10KWH", "10kWh LiFePO4 Battery", cat_names[2], 320000, 280000, 15, 3),
            ("BAT-TUB-200", "200Ah Tubular Battery", cat_names[2], 45000, 38000, 30, 5),
            ("CBL-SOL-4MM", "Solar DC Cable 4mm (per meter)", cat_names[3], 180, 120, 500, 100),
            ("CBL-AC-2.5MM", "AC Cable 2.5mm (per meter)", cat_names[3], 120, 80, 1000, 200),
            ("CBL-MC4", "MC4 Connector Pair", cat_names[3], 350, 250, 200, 50),
            ("MNT-ALU-RACK", "Aluminum Mounting Rack (set)", cat_names[4], 8500, 6500, 20, 5),
            ("MNT-RAIL-2M", "Mounting Rail 2m", cat_names[4], 2200, 1600, 50, 10),
            ("ELEC-DB", "Distribution Board 16-way", cat_names[5], 4500, 3200, 30, 5),
            ("ELEC-SPD", "Surge Protection Device", cat_names[5], 2800, 2000, 40, 8),
            ("ELEC-MCB-16A", "MCB 16A Single Pole", cat_names[5], 350, 220, 100, 20),
            ("TOOL-CRIMP", "Solar Crimping Tool", cat_names[6], 5500, 4200, 10, 2),
            ("TOOL-MULTI", "Digital Multimeter", cat_names[6], 3500, 2500, 15, 3),
            ("SAFE-HLMT", "Safety Helmet", cat_names[7], 800, 500, 50, 10),
            ("SAFE-GLOVES", "Insulated Gloves (pair)", cat_names[7], 1200, 800, 40, 10),
            ("SAFE-HARNESS", "Safety Harness", cat_names[7], 4500, 3200, 15, 3),
        ]:
            if not InvProduct.query.filter_by(sku=sku).first():
                p = InvProduct(sku=sku, name=name, category_id=cat_map.get(cat),
                              unit_price=float(price), cost_price=float(cost),
                              current_stock=stock, reorder_level=reorder,
                              unit="pcs", is_active=True)
                db.session.add(p)

        for name, contact, email, phone, addr, city in [
            ("Longi Solar Pakistan", "Mr. Ahmed", "ahmed@longi.pk", "021-34567890", "PLOT 12, SITE AREA", "Karachi"),
            ("JA Solar Technologies", "Mr. Usman", "usman@jasolar.com", "042-35678901", "23-G, Gulberg III", "Lahore"),
            ("BYD Energy Solutions", "Mr. Kamran", "kamran@byd.com", "021-36789012", "Business Bay, Clifton", "Karachi"),
            ("Growatt Inverters", "Mr. Hassan", "hassan@growatt.com", "042-37890123", "55 Main Boulevard", "Lahore"),
            ("Al-Rashid Traders", "Mr. Rashid", "rashid@alrashid.com", "0315-1234567", "Steel Market, Bolton Market", "Karachi"),
            ("Pakistan Cable Co.", "Mr. Faisal", "faisal@pakcable.com", "021-38901234", "Industrial Area, Kot Lakhpat", "Lahore"),
        ]:
            if not InvSupplier.query.filter_by(name=name).first():
                s = InvSupplier(name=name, contact_person=contact, email=email, phone=phone,
                               address=addr, city=city, is_active=True)
                db.session.add(s)

        for name, email, phone, addr, city, cl in [
            ("SolarTech Solutions", "imran@solartech.com", "0300-1111111", "7-A, Johar Town", "Lahore", 1000000),
            ("Green Energy Pakistan", "fatima@greenenergy.com", "0300-2222222", "15-B, Phase 2, DHA", "Karachi", 2000000),
            ("BuildRight Construction", "ali@buildright.com", "0300-3333333", "3rd Floor, Al-Falah Plaza", "Islamabad", 1500000),
            ("Home Solutions Ltd.", "zafar@homesol.com", "0300-4444444", "88, Garden Town", "Lahore", 500000),
        ]:
            if not InvCustomer.query.filter_by(name=name).first():
                c = InvCustomer(name=name, email=email, phone=phone, address=addr,
                               city=city, credit_limit=cl, is_active=True)
                db.session.add(c)

        InventorySettings.get()

        from shared.models.company_settings import CompanyInfo, AccountingPeriod, FiscalYearRule, ReportSettings
        from datetime import timedelta
        CompanyInfo.get()
        rule = FiscalYearRule.get()
        existing = AccountingPeriod.query.first()
        if not existing:
            rule.generate_periods()
        else:
            has_monthly = any(
                (p.end_date - p.start_date).days < 330
                for p in AccountingPeriod.query.all()
            )
            if has_monthly:
                rule.generate_periods()
        ReportSettings.get()

        # Backfill level-4 subledger accounts for entities created before the
        # auto-ledger feature (idempotent — keyed on deterministic codes).
        from shared.ledger_utils import create_entity_account
        from hr_app.models.loan import LoanAdvanceRequest
        for s in InvSupplier.query.all():
            create_entity_account("supplier", s.id, s.name)
        for c in InvCustomer.query.all():
            create_entity_account("customer", c.id, c.name)
        for p in InvProduct.query.all():
            create_entity_account("product", p.id, f"{p.name} ({p.sku})")
        for u in User.query.all():
            create_entity_account("employee", u.id, f"{u.full_name} ({u.employee_code})")
            # Login id defaults to the email until the user/admin changes it.
            if not u.login_id:
                u.login_id = u.email
        for ln in LoanAdvanceRequest.query.filter(
                LoanAdvanceRequest.status.in_(["pending", "approved"])).all():
            create_entity_account("loan", ln.id,
                                  f"{ln.request_type.title()} #{ln.id} - {ln.user.full_name if ln.user else ''}")

        # Costing engine: products holding stock from before the engine get an
        # opening cost layer so every future issue has a historic cost basis.
        from shared.models.invoice_template import InvoiceTemplate
        InvoiceTemplate.seed_defaults()
        # Templates saved under an older page layout still print from their
        # stored HTML, so bring the design-built ones up to the current sheet.
        InvoiceTemplate.refresh_designs()

        from shared.costing import ensure_opening_balances, backfill_layers
        ensure_opening_balances(created_by=1)
        # Stock tracked before cost layers existed gets one layer at current
        # book value, so layer value == ledger running_cost from here on.
        backfill_layers(created_by=1)

        # Second idempotent pass: fresh databases seed their users AFTER the
        # early bootstrap, so memberships for those users are created here.
        _bootstrap_default_company(db)

        db.session.commit()
        print("Seed data OK")


# Export at module level for Vercel.
# _create_app() is lazy — it runs fast at import time.
# DB init happens on first request via before_request hook.
app = _create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
