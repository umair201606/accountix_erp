"""Project labels: the label dimension on vouchers, invoices and reports.

Labels are the project dimension in ONE shared pool used by vouchers and
invoices alike: the header pick lands on the bank/cash settlement line,
per-line picks on the economic lines, the party pick lands on the AR/AP line,
per-item picks on the item lines. Item lines inherit the header/party label
by default unless a per-line pick overrides it; each document class keeps its
own Settings default (voucher default / invoice default) that fills every
untouched slot. These tests pin the posting rules and the report filters end
to end.
"""
import os
import tempfile

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_project_labels.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    c = flask_app.test_client()
    c.get("/")  # lazy create_all + migrate + seed
    from hr_app.models.user import User
    with flask_app.app_context():
        user = (User.query.filter(User.email.ilike("admin%")).first()
                or User.query.first())
        assert user is not None, "seed produced no users"
        uid = user.id
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    yield c


@pytest.fixture(scope="module")
def company_ctx(client):
    """Active-company app context for tenant-scoped reads."""
    from contextlib import contextmanager
    from shared.models.company import Company
    from shared.tenancy import set_current_company, unscoped

    @contextmanager
    def _ctx():
        with flask_app.app_context():
            with unscoped():
                comp = Company.query.first()
            assert comp is not None, "seed produced no company"
            set_current_company(comp.id)
            yield comp

    return _ctx


@pytest.fixture(scope="module")
def labels(client, company_ctx):
    from shared.models.project_label import ProjectLabel
    with company_ctx():
        a = ProjectLabel(name="Highway North")
        b = ProjectLabel(name="Solar Park")
        db.session.add_all([a, b])
        db.session.commit()
        yield a, b


@pytest.fixture(scope="module")
def accounts(client, company_ctx):
    from shared.models.ledger import ChartOfAccount
    with company_ctx():
        expense = (ChartOfAccount.query
                   .filter(ChartOfAccount.type == "expense",
                           ChartOfAccount.level >= 5)
                   .order_by(ChartOfAccount.code).first())
        cash = (ChartOfAccount.query
                .filter(ChartOfAccount.type == "asset",
                        ChartOfAccount.code.like("1-01-0%"),
                        ChartOfAccount.level >= 5)
                .order_by(ChartOfAccount.code).first())
        assert expense and cash
        yield expense, cash


def _labeled_expense_total(company_ctx, label_id):
    """Sum of posted debit on expense accounts carrying the label."""
    from decimal import Decimal
    from shared.models.ledger import ChartOfAccount, JournalLine
    with company_ctx():
        rows = (db.session.query(JournalLine)
                .join(ChartOfAccount,
                      JournalLine.account_id == ChartOfAccount.id)
                .filter(JournalLine.label_id == label_id,
                        ChartOfAccount.type == "expense")
                .all())
        return sum(Decimal(str(ln.debit)) for ln in rows)


def _invoice_journal_lines(company_ctx, voucher_type, invoice_id):
    """Posted journal lines of one SI/PI, joined through the entry header.

    Eager-loads the account so callers can read ``ln.account.type`` after
    the session has closed.
    """
    from sqlalchemy.orm import joinedload
    from shared.models.ledger import JournalEntry, JournalLine
    with company_ctx():
        return (JournalLine.query
                .options(joinedload(JournalLine.account))
                .join(JournalEntry,
                      JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == voucher_type,
                        JournalEntry.voucher_id == invoice_id)
                .all())


# ── labels CRUD page (lives in Settings — the universal label dimension) ─────

def test_labels_crud(client, company_ctx):
    from shared.models.project_label import ProjectLabel

    resp = client.post("/settings/labels", data={"name": "Beta II"})
    assert resp.status_code == 302

    # Flash is rendered on the next GET, not inside the redirect response.
    resp = client.post("/settings/labels", data={"name": "Beta II"})
    assert resp.status_code == 302
    resp = client.get("/settings/?tab=labels")
    assert b"already exists" in resp.data

    with company_ctx():
        beta = ProjectLabel.query.filter_by(name="Beta II").first()
        assert beta is not None
        beta_id = beta.id

    client.post(f"/settings/labels/{beta_id}/rename",
                data={"name": "Beta 3"})
    client.post(f"/settings/labels/{beta_id}/toggle")

    with company_ctx():
        beta = db.session.get(ProjectLabel, beta_id)
        assert beta.name == "Beta 3"
        assert beta.is_active is False

    client.post(f"/settings/labels/{beta_id}/toggle")

    with company_ctx():
        assert db.session.get(ProjectLabel, beta_id).is_active is True

    resp = client.get("/settings/?tab=labels")
    assert b"Beta 3" in resp.data


# ── one shared pool: no voucher/invoice split ──────────────────────────────

def test_settings_creates_one_shared_pool(client, company_ctx):
    from shared.models.project_label import ProjectLabel

    assert client.post("/settings/labels",
                       data={"name": "Shared One"}).status_code == 302
    # A stale "kind" field is ignored: one pool serves both document classes.
    assert client.post("/settings/labels",
                       data={"name": "Shared Two", "kind": "invoice"}).status_code == 302

    with company_ctx():
        assert ProjectLabel.query.filter_by(name="Shared One").first() is not None
        assert ProjectLabel.query.filter_by(name="Shared Two").first() is not None

    resp = client.get("/settings/?tab=labels")
    assert b"Shared One" in resp.data
    assert b"Shared Two" in resp.data
    assert b"Voucher Labels" not in resp.data
    assert b"Invoice Labels" not in resp.data


# ── default label: set in Settings, auto-assigned on save ────────────────────

@pytest.fixture(scope="module")
def default_label(client, company_ctx):
    """A label set as both the voucher and invoice default; restored."""
    from shared.models.inventory_settings import InventorySettings
    from shared.models.project_label import ProjectLabel
    with company_ctx():
        d = ProjectLabel.query.filter_by(name="Default Proj").first()
        if d is None:
            d = ProjectLabel(name="Default Proj")
            db.session.add(d)
            db.session.commit()
        did = d.id
    client.post("/settings/labels/defaults",
                data={"default_voucher_label_id": str(did),
                      "default_invoice_label_id": str(did)})
    yield did
    with company_ctx():
        d = db.session.get(ProjectLabel, did)
        if d:
            d.is_active = True
            db.session.commit()
        s = InventorySettings.get()
        s.default_voucher_label_id = None
        s.default_invoice_label_id = None
        db.session.commit()


def test_settings_labels_tab_renders(client, default_label):
    resp = client.get("/settings/?tab=labels")
    assert resp.status_code == 200
    assert b"Default Proj" in resp.data
    assert b"Save defaults" in resp.data
    assert b"Vouchers" in resp.data
    assert b"Invoices" in resp.data
    # The deep-link route renders the same tab without a redirect hop.
    resp = client.get("/settings/labels")
    assert resp.status_code == 200
    assert b"Default Proj" in resp.data


def test_defaults_are_separate_per_class(client, company_ctx, default_label):
    from shared.models.inventory_settings import InventorySettings
    from shared.models.project_label import ProjectLabel
    with company_ctx():
        other = (ProjectLabel.query
                 .filter(ProjectLabel.id != default_label)
                 .order_by(ProjectLabel.id).first())
        assert other is not None
        other_id = other.id
    # The two class defaults move independently: swapping the voucher default
    # leaves the invoice default alone.
    client.post("/settings/labels/defaults",
                data={"default_voucher_label_id": str(other_id),
                      "default_invoice_label_id": str(default_label)})
    with company_ctx():
        s = InventorySettings.get()
        assert s.default_voucher_label_id == other_id
        assert s.default_invoice_label_id == default_label
        assert s.default_voucher_label().id == other_id
        assert s.default_invoice_label().id == default_label
    # Clearing one default leaves the other untouched.
    client.post("/settings/labels/defaults",
                data={"default_voucher_label_id": "",
                      "default_invoice_label_id": str(default_label)})
    with company_ctx():
        s = InventorySettings.get()
        assert s.default_voucher_label_id is None
        assert s.default_voucher_label() is None
        assert s.default_invoice_label_id == default_label
    # Restore the voucher default for the save tests below.
    client.post("/settings/labels/defaults",
                data={"default_voucher_label_id": str(default_label),
                      "default_invoice_label_id": str(default_label)})


def test_deactivating_default_clears_it(client, company_ctx, default_label):
    from shared.models.inventory_settings import InventorySettings
    client.post(f"/settings/labels/{default_label}/toggle")
    with company_ctx():
        s = InventorySettings.get()
        assert s.default_voucher_label() is None
        assert s.default_invoice_label() is None
        assert s.default_voucher_label_id is None
        assert s.default_invoice_label_id is None
    # Activate again and restore the defaults for the save tests below.
    client.post(f"/settings/labels/{default_label}/toggle")
    client.post("/settings/labels/defaults",
                data={"default_voucher_label_id": str(default_label),
                      "default_invoice_label_id": str(default_label)})
    with company_ctx():
        s = InventorySettings.get()
        assert s.default_voucher_label().id == default_label
        assert s.default_invoice_label().id == default_label


def test_voucher_saves_default_label_when_none_picked(client, company_ctx,
                                                      accounts, default_label):
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    expense, cash = accounts
    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "CPV",
        "voucher_date": "2030-06-05T10:00",
        "cash_bank_account_id": str(cash.id),
        "account_id[]": [str(expense.id)],
        "description[]": ["Auto-default expense"],
        "debit[]": ["125.00"],
        "credit[]": [""],
        "label_id[]": [""],
    })
    assert resp.status_code == 302
    with company_ctx():
        v = (AccountingVoucher.query.filter_by(voucher_type="CPV")
             .order_by(AccountingVoucher.id.desc()).first())
        assert v is not None
        exp_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=1).first()
        cb_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=0).first()
        # No header label field was posted, so the bank line still belongs to
        # no project while the economic line picks up the default.
        assert exp_line.label_id == default_label
        assert exp_line.label_id is not None
        assert cb_line.label_id is None
    # An explicit pick still wins over the default.
    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "JV",
        "voucher_date": "2030-06-06T10:00",
        "cash_bank_account_id": str(cash.id),
        "account_id[]": [str(expense.id), str(expense.id)],
        "description[]": ["Explicit", "Credit side"],
        "debit[]": ["50.00", ""],
        "credit[]": ["", "50.00"],
        "label_id[]": ["", str(default_label)],
    })
    assert resp.status_code == 302
    with company_ctx():
        v = (AccountingVoucher.query.filter_by(voucher_type="JV")
             .order_by(AccountingVoucher.id.desc()).first())
        lines = (AccountingVoucherLine.query.filter_by(voucher_id=v.id)
                 .order_by(AccountingVoucherLine.line_no).all())
        assert [ln.label_id for ln in lines] == [default_label, default_label]


def test_invoice_saves_default_label_when_none_picked(client, company_ctx,
                                                      default_label):
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice
    from inventory_app.models.product import InvProduct

    with company_ctx():
        cust = InvCustomer.query.first()
        prod = InvProduct.query.first()
        assert cust and prod

    resp = client.post("/inventory/invoices/save", json={
        "customer_id": cust.id,
        "due_date": "2030-07-05",
        "discount_mode": "general",
        "charges_mode": "general",
        "tax_mode": "general",
        "global_discount_pct": 0,
        "global_discount_value": 0,
        "global_delivery": 0,
        "global_installation": 0,
        "global_sales_tax_pct": 0,
        "further_tax_pct": 0,
        "apply_further_tax": False,
        "withholding_tax_pct": 0,
        "subtotal": 200,
        "total_discount": 0,
        "total_charges": 0,
        "total_tax": 0,
        "total_amount": 200,
        "items": [{
            "product_id": prod.id,
            "quantity": 1,
            "unit_price": 200,
            "description": "Auto-default sale",
            "unit": "pcs",
            "discount_pct": 0,
            "delivery": 0,
            "installation": 0,
            "sales_tax_pct": 0,
            "total_before_discount": 200,
            "total_after_discount": 200,
        }],
        "action": "approve",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    body = resp.get_json() or {}
    assert body.get("ok") is not False, resp.get_data(as_text=True)[:600]

    with company_ctx():
        from inventory_app.models.invoice import InvInvoiceItem
        inv = InvInvoice.query.order_by(InvInvoice.id.desc()).first()
        assert inv.label_id == default_label
        # Item rows inherit the party slot: no pick anywhere, so the first
        # item carries the default just like the AR line.
        item = InvInvoiceItem.query.filter_by(invoice_id=inv.id).first()
        assert item.label_id == default_label


# ── voucher line labels: tag expense, never the cash side ────────────────────

def test_voucher_posts_line_label_and_bank_stays_clean(client, company_ctx,
                                                       accounts, labels):
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from shared.models.ledger import JournalLine

    expense, cash = accounts
    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "CPV",
        "voucher_date": "2030-06-01T10:00",
        "cash_bank_account_id": str(cash.id),
        "account_id[]": [str(expense.id)],
        "description[]": ["Project tagged expense"],
        "debit[]": ["250.00"],
        "credit[]": [""],
        "label_id[]": [str(labels[0].id)],
    })
    assert resp.status_code == 302
    resp = client.get(resp.headers["Location"])
    assert b"approved" in resp.data

    with company_ctx():
        v = AccountingVoucher.query.order_by(AccountingVoucher.id.desc()).first()
        assert v.status == "approved"
        exp_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=1).first()
        cb_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=0).first()
        assert exp_line.label_id == labels[0].id
        assert cb_line.label_id is None

        labeled = JournalLine.query.filter_by(account_id=expense.id,
                                              label_id=labels[0].id).all()
        bank_lines = JournalLine.query.filter_by(account_id=cash.id).all()
        assert len(labeled) == 1
        assert labeled[0].debit == 250.00
        assert bank_lines and all(ln.label_id is None for ln in bank_lines)


def test_label_filter_balance_excludes_bank(client, company_ctx, accounts,
                                            labels):
    from finance_app.routes.reports import _get_account_balance

    expense, cash = accounts
    with company_ctx():
        dr, cr = _get_account_balance(expense.id, label_ids=[labels[0].id])
        assert dr + cr > 0
        # Acceptance criterion: the bank ledger under a label filter is zero —
        # the cash side of a labeled posting belongs to no project.
        dr_b, cr_b = _get_account_balance(cash.id, label_ids=[labels[0].id])
        assert dr_b == 0 and cr_b == 0


def test_jv_tags_both_sides(client, company_ctx, accounts, labels):
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from shared.models.ledger import (ChartOfAccount, JournalEntry,
                                      JournalLine)

    expense, cash = accounts
    with company_ctx():
        rev = (ChartOfAccount.query
               .filter(ChartOfAccount.type == "revenue",
                       ChartOfAccount.level >= 5)
               .order_by(ChartOfAccount.code).first())
        assert rev

    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "JV",
        "voucher_date": "2030-06-02T10:00",
        "cash_bank_account_id": str(cash.id),
        "account_id[]": [str(expense.id), str(rev.id)],
        "description[]": ["JV tagged debit", "JV tagged credit"],
        "debit[]": ["100.00", ""],
        "credit[]": ["", "100.00"],
        "label_id[]": [str(labels[0].id), str(labels[1].id)],
    })
    assert resp.status_code == 302

    with company_ctx():
        v = (AccountingVoucher.query
             .filter_by(voucher_type="JV")
             .order_by(AccountingVoucher.id.desc()).first())
        assert v is not None
        lines = (AccountingVoucherLine.query.filter_by(voucher_id=v.id)
                 .order_by(AccountingVoucherLine.line_no).all())
        assert [ln.label_id for ln in lines] == [labels[0].id, labels[1].id]
        jls = (JournalLine.query
               .join(JournalEntry,
                     JournalLine.journal_entry_id == JournalEntry.id)
               .filter(JournalEntry.voucher_type == "JV",
                       JournalEntry.voucher_id == v.id).all())
        assert len(jls) == 2


# ── voucher HEADER label: the bank/cash line carries it, lines inherit ───────

def test_voucher_header_label_stamps_bank_line_and_inherits(
        client, company_ctx, accounts, labels):
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from shared.models.ledger import JournalEntry, JournalLine

    expense, cash = accounts
    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "CPV",
        "voucher_date": "2030-06-07T10:00",
        "cash_bank_account_id": str(cash.id),
        "label_id": str(labels[1].id),
        "account_id[]": [str(expense.id)],
        "description[]": ["Header inherited expense"],
        "debit[]": ["300.00"],
        "credit[]": [""],
        "label_id[]": [""],
    })
    assert resp.status_code == 302
    with company_ctx():
        v = (AccountingVoucher.query.filter_by(voucher_type="CPV")
             .order_by(AccountingVoucher.id.desc()).first())
        exp_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=1).first()
        cb_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=0).first()
        assert v.label_id == labels[1].id
        # The settlement line takes the header label; the blank item line
        # inherits the same label on the server side too.
        assert cb_line.label_id == labels[1].id
        assert exp_line.label_id == labels[1].id
        bank = (JournalLine.query
                .join(JournalEntry,
                      JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_id == v.id,
                        JournalLine.account_id == cash.id).all())
        assert bank and all(ln.label_id == labels[1].id for ln in bank)

    # An explicit per-line pick still beats the header on that line.
    resp = client.post("/accounting/vouchers", data={
        "action": "approve",
        "voucher_type": "CPV",
        "voucher_date": "2030-06-08T10:00",
        "cash_bank_account_id": str(cash.id),
        "label_id": str(labels[1].id),
        "account_id[]": [str(expense.id)],
        "description[]": ["Explicit line beats header"],
        "debit[]": ["175.00"],
        "credit[]": [""],
        "label_id[]": [str(labels[0].id)],
    })
    assert resp.status_code == 302
    with company_ctx():
        v = (AccountingVoucher.query.filter_by(voucher_type="CPV")
             .order_by(AccountingVoucher.id.desc()).first())
        exp_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=1).first()
        cb_line = AccountingVoucherLine.query.filter_by(
            voucher_id=v.id, line_no=0).first()
        assert exp_line.label_id == labels[0].id
        assert cb_line.label_id == labels[1].id


# ── report filter panels and outputs ─────────────────────────────────────────

def test_profit_loss_filters_by_label(client, company_ctx, accounts, labels):
    from shared.models.project_label import ProjectLabel

    expense, _ = accounts
    # The "other" fixture label is unusable as the negative control here: the
    # header-label tests above stamped it onto this very account. A fresh
    # label that no posting ever touched must hide the account completely.
    with company_ctx():
        untouched = ProjectLabel(name="Untouched Filter")
        db.session.add(untouched)
        db.session.commit()
        untouched_id = untouched.id
    total = _labeled_expense_total(company_ctx, labels[0].id)
    expected = f"{total:.2f}"

    url = ("/finance/profit-loss?filter_mode=custom&from=2020-01-01"
           "&to=2031-12-31&label_ids=" + str(labels[0].id))
    resp = client.get(url)
    assert resp.status_code == 200
    assert expense.name.encode() in resp.data
    assert expected.encode() in resp.data
    # An unfiltered P&L sees the same lines…
    resp_all = client.get(url.replace("label_ids=" + str(labels[0].id), ""))
    assert expense.name.encode() in resp_all.data
    # …and a filter on the untouched label sees none of them: the account row
    # vanishes from the statement entirely.
    resp_other = client.get(url.replace("label_ids=" + str(labels[0].id),
                                        "label_ids=" + str(untouched_id)))
    assert expense.name.encode() not in resp_other.data


def test_ledger_label_filter_shows_no_bank_lines(client, company_ctx, accounts,
                                                 labels):
    expense, _ = accounts
    total = _labeled_expense_total(company_ctx, labels[0].id)
    url = ("/finance/ledger?mode=all&filter_mode=custom&from=2020-01-01"
           "&to=2031-12-31&label_ids=" + str(labels[0].id))
    resp = client.get(url)
    assert resp.status_code == 200
    assert expense.name.encode() in resp.data
    assert f"{total:.2f}".encode() in resp.data


def test_balance_sheet_label_filter(client, company_ctx, labels):
    with company_ctx():
        from shared.models.ledger import JournalLine
        total = _labeled_expense_total(company_ctx, labels[0].id)
        rev_labeled = (JournalLine.query
                       .filter(JournalLine.label_id == labels[0].id,
                               JournalLine.credit > 0).count())
    resp = client.get("/finance/balance-sheet?filter_mode=custom"
                      "&from=2020-01-01&to=2031-12-31&label_ids="
                      + str(labels[0].id))
    assert resp.status_code == 200
    # Label 1 has only expense debits (250 + 100); its credit lines belong to
    # label 2, so the labeled BS shows a net loss of the expense total —
    # rendered with the shared positive-bracket style.
    assert b"Net Loss" in resp.data
    assert f"({total:.2f})".encode() in resp.data
    assert rev_labeled == 0


def test_voucher_list_hides_label_panel(client):
    resp = client.get("/accounting/vouchers/list")
    assert resp.status_code == 200
    assert b"labelDDTrigger" not in resp.data


def test_report_filter_panel_lists_labels(client, labels):
    resp = client.get("/finance/profit-loss?filter_mode=custom"
                      "&from=2020-01-01&to=2031-12-31")
    assert b"labelDDTrigger" in resp.data
    assert b"Highway North" in resp.data
    assert b"Solar Park" in resp.data
    assert b'name="label_ids"' in resp.data


# ── invoice label rules: party label on AR/AP, line label on economics ───────
# Purchase first: the products are seeded without FIFO layers, and a sales
# invoice consumes stock — record_out refuses to issue uncovered units.

def test_purchase_invoice_party_label_on_ap_line_label_on_inventory(
        client, company_ctx, labels):
    from inventory_app.models.product import InvProduct
    from inventory_app.models.purchase_invoice import InvPurchaseInvoice
    from inventory_app.models.supplier import InvSupplier

    with company_ctx():
        supp = InvSupplier.query.first()
        prod = InvProduct.query.first()
        assert supp and prod

    resp = client.post("/inventory/purchase-invoice/save", json={
        "supplier_id": supp.id,
        "label_id": labels[1].id,
        "discount_mode": "general",
        "expenses_mode": "general",
        "tax_mode": "general",
        "global_discount_pct": 0,
        "global_discount_value": 0,
        "global_commission": 0,
        "global_freight": 0,
        "global_loading": 0,
        "global_sales_tax_pct": 0,
        "withholding_tax_pct": 0,
        "subtotal": 400,
        "total_discount": 0,
        "net_payable": 400,
        "total_amount": 400,
        "items": [{
            "product_id": prod.id,
            "quantity": 5,
            "unit_price": 80,
            "discount_pct": 0,
            "label_id": labels[0].id,
            "total_before_discount": 400,
            "total_after_discount": 400,
        }],
        "action": "approve",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    body = resp.get_json() or {}
    assert body.get("ok") is not False, resp.get_data(as_text=True)[:600]

    with company_ctx():
        from inventory_app.models.purchase_invoice import InvPurchaseInvoiceItem
        inv = InvPurchaseInvoice.query.order_by(
            InvPurchaseInvoice.id.desc()).first()
        assert inv.label_id == labels[1].id
        item = InvPurchaseInvoiceItem.query.filter_by(
            invoice_id=inv.id).first()
        assert item.label_id == labels[0].id
        jls = _invoice_journal_lines(company_ctx, "PI", inv.id)
        inventory = [ln for ln in jls
                     if ln.account.type == "asset"
                     and "Inventory" in (ln.description or "")]
        ap = [ln for ln in jls if "AP -" in (ln.description or "")]
        assert inventory, "no inventory line posted"
        assert all(ln.label_id == labels[0].id for ln in inventory)
        assert ap, "no AP line posted"
        assert all(ln.label_id == labels[1].id for ln in ap)
        # The tax side never carries either label.
        tax = [ln for ln in jls if "Input Tax" in (ln.description or "")
               or "WHT" in (ln.description or "")]
        assert all(ln.label_id is None for ln in tax)

    # Without a per-item pick the item inherits the party label.
    resp = client.post("/inventory/purchase-invoice/save", json={
        "supplier_id": supp.id,
        "label_id": labels[1].id,
        "discount_mode": "general",
        "expenses_mode": "general",
        "tax_mode": "general",
        "global_discount_pct": 0,
        "global_discount_value": 0,
        "global_commission": 0,
        "global_freight": 0,
        "global_loading": 0,
        "global_sales_tax_pct": 0,
        "withholding_tax_pct": 0,
        "subtotal": 160,
        "total_discount": 0,
        "net_payable": 160,
        "total_amount": 160,
        "items": [{
            "product_id": prod.id,
            "quantity": 2,
            "unit_price": 80,
            "discount_pct": 0,
            "total_before_discount": 160,
            "total_after_discount": 160,
        }],
        "action": "approve",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    with company_ctx():
        inv2 = InvPurchaseInvoice.query.order_by(
            InvPurchaseInvoice.id.desc()).first()
        assert inv2.label_id == labels[1].id
        item2 = InvPurchaseInvoiceItem.query.filter_by(
            invoice_id=inv2.id).first()
        assert item2.label_id == labels[1].id


def test_sales_invoice_party_label_on_ar_line_label_on_revenue(
        client, company_ctx, labels):
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice
    from inventory_app.models.product import InvProduct

    with company_ctx():
        cust = InvCustomer.query.first()
        prod = InvProduct.query.first()
        assert cust and prod

    resp = client.post("/inventory/invoices/save", json={
        "customer_id": cust.id,
        "label_id": labels[1].id,
        "due_date": "2030-07-01",
        "discount_mode": "general",
        "charges_mode": "general",
        "tax_mode": "general",
        "global_discount_pct": 0,
        "global_discount_value": 0,
        "global_delivery": 0,
        "global_installation": 0,
        "global_sales_tax_pct": 0,
        "further_tax_pct": 0,
        "apply_further_tax": False,
        "withholding_tax_pct": 0,
        "subtotal": 300,
        "total_discount": 0,
        "total_charges": 0,
        "total_tax": 0,
        "total_amount": 300,
        "items": [{
            "product_id": prod.id,
            "quantity": 2,
            "unit_price": 150,
            "description": "Labeled sale",
            "unit": "pcs",
            "discount_pct": 0,
            "label_id": labels[0].id,
            "delivery": 0,
            "installation": 0,
            "sales_tax_pct": 0,
            "total_before_discount": 300,
            "total_after_discount": 300,
        }],
        "action": "approve",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    body = resp.get_json() or {}
    assert body.get("ok") is not False, resp.get_data(as_text=True)[:600]

    with company_ctx():
        from inventory_app.models.invoice import InvInvoiceItem
        inv = InvInvoice.query.order_by(InvInvoice.id.desc()).first()
        assert inv.label_id == labels[1].id
        item = InvInvoiceItem.query.filter_by(invoice_id=inv.id).first()
        assert item.label_id == labels[0].id
        jls = _invoice_journal_lines(company_ctx, "SI", inv.id)
        revenue = [ln for ln in jls if ln.account.type == "revenue"]
        ar = [ln for ln in jls if "AR -" in (ln.description or "")]
        cogs = [ln for ln in jls if "COGS -" in (ln.description or "")]
        assert revenue, "no revenue line posted"
        assert all(ln.label_id == labels[0].id for ln in revenue)
        assert ar, "no AR line posted"
        assert all(ln.label_id == labels[1].id for ln in ar)
        # COGS consumes the FIFO layers the purchase test established.
        assert cogs, "no COGS line posted"
        assert all(ln.label_id == labels[0].id for ln in cogs)
        # The machine-built posting never tags the tax side.
        tax = [ln for ln in jls if ln.account.type == "liability"
               and "AR" not in (ln.description or "")]
        assert all(ln.label_id is None for ln in tax)

# ── browser checks: pickers and mobile fit ──────────────────────────────────
# These run against the harness server (tests/e2e/conftest.py), which seeds a
# fresh DB per session, so labels are created through the Settings UI itself.

BASE_URL = "http://127.0.0.1:" + os.environ.get("E2E_PORT", "5050")


def _super_admin_page(page, flask_server):
    page.goto(f"{BASE_URL}/superadmin/login")
    page.fill("#login", "admin@gmail.com")
    page.fill("#password", "admin123")
    page.click("button[type='submit']")
    page.wait_for_url("**/superadmin/**")
    return page


def _ensure_label(page, name, make_default=False):
    """Create a label through the Settings UI (idempotent); optionally make
    it the voucher default — the state the voucher pickers prefill."""
    page.goto(f"{BASE_URL}/settings/?tab=labels")
    if page.locator(".stbl tbody").get_by_text(name, exact=True).count() == 0:
        page.locator("input[placeholder='e.g. Highway Project - North']").first.fill(name)
        page.get_by_role("button", name="+ Create Label").first.click()
        page.wait_for_load_state("networkidle")
    if make_default:
        page.select_option("select[name='default_voucher_label_id']", label=name)
        page.get_by_role("button", name="Save defaults").click()
        page.wait_for_load_state("networkidle")


def test_settings_labels_tab_fits_mobile(page, flask_server):
    page.set_viewport_size({"width": 320, "height": 700})
    page = _super_admin_page(page, flask_server)
    _ensure_label(page, "PW Mobile")

    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth")
    box = page.locator(".tbl-scroll").first.bounding_box()
    assert box is not None
    assert box["x"] + box["width"] <= 320 + 0.5, \
        "the label table must scroll inside its card, never widen the page"


def test_profit_loss_filter_fits_mobile(page, flask_server):
    page.set_viewport_size({"width": 320, "height": 700})
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/finance/profit-loss")

    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth")
    # The label panel is inside the same filter bar: opening it must not
    # push the page sideways either.
    page.click("#labelDDTrigger")
    panel = page.locator("#labelDDPanel")
    box = panel.bounding_box()
    assert box is not None
    assert box["x"] + box["width"] <= 320 + 0.5


def _enable_per_line(page, scope="voucher"):
    """Flip a per-line labeling toggle switch in Settings (auto-submits on
    change) for the given document class — the row label column is hidden
    unless its own toggle is on."""
    page.goto(f"{BASE_URL}/settings/?tab=labels")
    fid = "perLineFormV" if scope == "voucher" else "perLineFormI"
    sw = page.locator(f"#{fid} input[type='checkbox']")
    if not sw.is_checked():
        sw.check()
        page.wait_for_load_state("networkidle")


def test_voucher_label_picker_click_and_space_show_full_list(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    page = _super_admin_page(page, flask_server)
    _ensure_label(page, "PW Solar", make_default=True)
    _ensure_label(page, "PW Maint")
    _enable_per_line(page)

    page.goto(f"{BASE_URL}/accounting/vouchers?type=JV")
    inp = page.locator(".ld-inp").first
    assert inp.input_value() == "PW Solar", "default label must prefill the row"

    # A click on the prefilled input opens the FULL list — not a list that
    # only matches the current value (regression: input value acted as a
    # filter, so Solar would be the only option visible).
    inp.click()
    dd = page.locator(".ld-dd.show")
    assert dd.get_by_text("PW Solar", exact=True).is_visible()
    assert dd.get_by_text("PW Maint", exact=True).is_visible()

    # Typing filters to the one matching label…
    inp.fill("Maint")
    assert dd.get_by_text("PW Maint", exact=True).is_visible()
    assert dd.get_by_text("PW Solar", exact=True).is_hidden()

    # …and Space snaps back to the full list even while text sits in the box.
    inp.press(" ")
    assert dd.get_by_text("PW Solar", exact=True).is_visible()
    assert dd.get_by_text("PW Maint", exact=True).is_visible()

    inp.press("Escape")
    assert dd.count() == 0 or not dd.is_visible()


def test_invoice_label_pickers_stay_in_viewport_on_mobile(page, flask_server):
    page.set_viewport_size({"width": 320, "height": 700})
    page = _super_admin_page(page, flask_server)
    _ensure_label(page, "PW Solar", make_default=True)

    for path in ("/inventory/invoices/", "/inventory/purchase-invoice/"):
        page.goto(f"{BASE_URL}{path}")
        gate = page.locator("#gateBlank")
        if gate.count():
            gate.click()
            page.locator("#gateContinue").click()
            page.wait_for_timeout(150)
        page.click("#invLabelSearch")
        dd = page.locator(".ac-dd.show")
        box = dd.bounding_box()
        assert box is not None, f"label dropdown did not open on {path}"
        assert box["x"] >= 0, f"dropdown clips the left edge on {path}"
        assert box["x"] + box["width"] <= 320 + 0.5, \
            f"dropdown sticks out of the viewport on {path}"