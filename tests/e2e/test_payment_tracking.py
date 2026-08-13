"""Invoicing > Invoice Tracking: the feed, the AR/AP registers and assignment.

The point of this module is the guarantee the feature is built on: assigning a
receipt to invoices is SUBLEDGER MATCHING ONLY. It moves invoice-level settled
figures and nothing else — no journal entry, no ledger row, no account balance.
``test_assignment_leaves_the_general_ledger_untouched`` is the one that would
catch a regression there, and it is why the rest of the fixtures bother to
build real approved vouchers instead of stubbing the engine.
"""
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_payment_tracking.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with flask_app.test_client() as c:
        c.get("/")  # triggers lazy create_all + migrate + seed
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


def _customer_account(customer_id):
    """The deterministic Trade Debtors subledger account for a customer.

    ``party_account_index()`` identifies the payer purely from this code, so a
    receipt line posted anywhere else is (correctly) not assignable.
    """
    from shared.coa import ENTITY_PARENT_CODES, ENTITY_ID_OFFSET
    from shared.models.ledger import ChartOfAccount
    code = (f"{ENTITY_PARENT_CODES['customer']}-"
            f"{customer_id + ENTITY_ID_OFFSET:04d}")
    acct = ChartOfAccount.query.filter_by(code=code).first()
    if acct is None:
        parent = ChartOfAccount.query.filter_by(
            code=ENTITY_PARENT_CODES["customer"]).first()
        acct = ChartOfAccount(code=code, name="Test Customer", type="Asset",
                              parent_id=parent.id if parent else None, level=5)
        db.session.add(acct)
        db.session.commit()
    return acct


@pytest.fixture(scope="module")
def scenario(client):
    """One customer, two approved sales invoices, one approved 5,000 receipt.

    Invoice A is 3,000 and long overdue; invoice B is 4,000 and not yet due.
    That shape lets one receipt exercise a multi-invoice split, every payment
    state, and the overdue filter from a single fixture.
    """
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from shared.models.ledger import ChartOfAccount
    from hr_app.models.user import User

    with flask_app.app_context():
        from shared.models.company import Company
        from shared.tenancy import set_current_company, unscoped
        # Tenancy fails closed, so pick the seeded tenant before creating any
        # scoped row — the stamping hook reads the active company, not the row.
        with unscoped():
            company = Company.query.order_by(Company.id).first()
        assert company is not None, "seed produced no companies"
        set_current_company(company.id)

        cust = InvCustomer(name="Tracking Test Customer")
        db.session.add(cust)
        db.session.commit()

        uid = User.query.first().id
        today = datetime.utcnow()

        inv_a = InvInvoice(invoice_number="TRK-A", voucher_number="TRK-A",
                           customer_id=cust.id, total_amount=3000.0,
                           invoice_date=today - timedelta(days=90),
                           due_date=today - timedelta(days=60),
                           voucher_status="approved", payment_status="unpaid",
                           paid_amount=0.0, created_by=uid)
        inv_b = InvInvoice(invoice_number="TRK-B", voucher_number="TRK-B",
                           customer_id=cust.id, total_amount=4000.0,
                           invoice_date=today,
                           due_date=today + timedelta(days=30),
                           voucher_status="approved", payment_status="unpaid",
                           paid_amount=0.0, created_by=uid)
        db.session.add_all([inv_a, inv_b])
        db.session.commit()

        acct = _customer_account(cust.id)
        cash = (ChartOfAccount.query.filter(ChartOfAccount.code.like("1-01-01%"),
                                            ChartOfAccount.level == 5).first()
                or acct)

        # A cash receipt voucher: Dr Cash / Cr Customer. The credit on the
        # customer line is the money the feed offers for assignment.
        voucher = AccountingVoucher(
            voucher_type="CRV", voucher_number="TRK-CRV-1",
            voucher_date=today, cash_bank_account_id=cash.id,
            status="approved", created_by=uid, approved_by=uid)
        db.session.add(voucher)
        db.session.commit()
        db.session.add_all([
            AccountingVoucherLine(voucher_id=voucher.id, line_no=1,
                                  account_id=cash.id, description="Cash in",
                                  debit=Decimal("5000"), credit=Decimal("0")),
            AccountingVoucherLine(voucher_id=voucher.id, line_no=2,
                                  account_id=acct.id, description="From customer",
                                  debit=Decimal("0"), credit=Decimal("5000")),
        ])
        db.session.commit()

        line = AccountingVoucherLine.query.filter_by(
            voucher_id=voucher.id, account_id=acct.id).first()
        return {"customer_id": cust.id, "company_id": company.id,
                "inv_a": inv_a.id, "inv_b": inv_b.id,
                "line_id": line.id, "voucher_id": voucher.id}


@contextmanager
def _tenant_ctx(scenario):
    """An app context with the scenario's company active.

    The active company lives on Flask's ``g``, which is per app-context — so
    engine calls have to happen inside one of these or tenancy fails closed.
    """
    with flask_app.app_context():
        from shared.tenancy import set_current_company
        set_current_company(scenario["company_id"])
        yield


def _reset_allocations(scenario):
    from shared.models.payment_allocation import PaymentAllocation
    from shared import payment_tracking as pt
    PaymentAllocation.query.delete()
    db.session.commit()
    for doc_id in (scenario["inv_a"], scenario["inv_b"]):
        pt.recompute_doc(pt.SALES, doc_id)
    db.session.commit()


@pytest.fixture
def engine(scenario):
    """For tests that call the tracking engine directly: holds the app context
    open for the whole test, with allocations reset to nothing."""
    with _tenant_ctx(scenario):
        _reset_allocations(scenario)
        yield scenario


@pytest.fixture
def reset(scenario):
    """For tests that go through HTTP: resets allocations, then releases the
    app context before the request runs.

    Holding one open across ``client.get()`` makes Flask tear down the wrong
    context and the session teardown blows up — the request pipeline pushes its
    own context and picks the company itself.
    """
    with _tenant_ctx(scenario):
        _reset_allocations(scenario)
    yield scenario


# ── The feed ────────────────────────────────────────────────────────────────

def test_receipt_appears_in_the_feed_as_unassigned(engine, scenario):
    from shared import payment_tracking as pt
    row = pt.feed_row(scenario["line_id"])
    assert row is not None, "approved CRV customer credit must be assignable"
    assert row["flow"] == "receipt"
    assert row["doc_type"] == pt.SALES
    assert row["party_id"] == scenario["customer_id"]
    assert row["amount"] == pytest.approx(5000.0)
    assert row["unassigned"] == pytest.approx(5000.0)
    assert row["state"] == "unassigned"


def test_the_invoices_own_ar_line_is_not_offered_as_cash(engine, scenario):
    """A customer *debited* is the invoice's own posting, not a receipt."""
    from shared import payment_tracking as pt
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from hr_app.models.user import User
    acct = _customer_account(scenario["customer_id"])
    uid = User.query.first().id
    v = AccountingVoucher(voucher_type="CRV", voucher_number="TRK-CRV-DR",
                          voucher_date=datetime.utcnow(), status="approved",
                          created_by=uid, approved_by=uid)
    db.session.add(v)
    db.session.commit()
    db.session.add(AccountingVoucherLine(
        voucher_id=v.id, line_no=1, account_id=acct.id,
        debit=Decimal("1000"), credit=Decimal("0")))
    db.session.commit()
    line = AccountingVoucherLine.query.filter_by(voucher_id=v.id).first()
    assert pt.feed_row(line.id) is None

    db.session.delete(line)
    db.session.delete(v)
    db.session.commit()


def test_feed_covers_the_five_settlement_voucher_types(engine, scenario):
    """Receipts arrive on CRV/BRV/JV, payments on CPV/BPV/JV — and direction
    decides which: a customer is credited, a supplier is debited."""
    from shared import payment_tracking as pt
    assert set(pt.RECEIPT_VOUCHERS) == {"CRV", "BRV", "JV"}
    assert set(pt.PAYMENT_VOUCHERS) == {"CPV", "BPV", "JV"}
    assert set(pt.RECEIPT_VOUCHERS) | set(pt.PAYMENT_VOUCHERS) == \
        {"CRV", "BRV", "CPV", "BPV", "JV"}


def test_feed_is_customers_and_suppliers_only(engine, scenario):
    """The party index must not pick up employees, loans, stock or assets —
    only the two subledgers that settle invoices."""
    from shared import payment_tracking as pt
    assert set(pt.DOC_FOR_PARTY) == {"customer", "supplier"}
    kinds = {kind for kind, _pid, _name in pt.party_account_index().values()}
    assert kinds <= {"customer", "supplier"}


# ── Whole-ledger payers ─────────────────────────────────────────────────────

def _ledger_voucher(code, vtype, debit=0, credit=0, number="TRK-LEDGER"):
    """An approved voucher settling a subledger account directly."""
    from shared.models.ledger import ChartOfAccount
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    from hr_app.models.user import User
    acct = ChartOfAccount.query.filter_by(code=code).first()
    assert acct is not None, f"{code} should exist in the seeded chart"
    uid = User.query.first().id
    v = AccountingVoucher(voucher_type=vtype, voucher_number=number,
                          voucher_date=datetime.utcnow(), status="approved",
                          created_by=uid, approved_by=uid)
    db.session.add(v)
    db.session.commit()
    db.session.add(AccountingVoucherLine(
        voucher_id=v.id, line_no=1, account_id=acct.id,
        debit=Decimal(str(debit)), credit=Decimal(str(credit))))
    db.session.commit()
    line = AccountingVoucherLine.query.filter_by(voucher_id=v.id).first()
    return acct, v, line


def _drop(v, line):
    db.session.delete(line)
    db.session.delete(v)
    db.session.commit()


def test_a_payment_against_the_payables_ledger_reaches_the_feed(engine, scenario):
    """Books that carry no supplier record for every payee still settle through
    Trade Creditors, and that is a payment like any other."""
    from shared import payment_tracking as pt
    acct, v, line = _ledger_voucher("2-01-01-01", "BPV", debit=100000)
    try:
        row = pt.feed_row(line.id)
        assert row is not None, "a debited payables ledger is a payment"
        assert row["flow"] == "payment"
        assert pt.is_ledger_party(row["party_id"])
        assert row["party_id"] == pt.ledger_party_id(acct.id)
        assert row["amount"] == pytest.approx(100000.0)
    finally:
        _drop(v, line)


def test_a_receipt_against_the_receivables_ledger_reaches_the_feed(engine, scenario):
    from shared import payment_tracking as pt
    acct, v, line = _ledger_voucher("1-01-02-01", "BRV", credit=4200)
    try:
        row = pt.feed_row(line.id)
        assert row is not None
        assert row["flow"] == "receipt"
        assert pt.is_ledger_party(row["party_id"])
    finally:
        _drop(v, line)


def test_direction_still_decides_for_a_ledger_payer(engine, scenario):
    """Being a ledger does not exempt it: a credited payables ledger is the
    bill's own posting, not a payment."""
    from shared import payment_tracking as pt
    _acct, v, line = _ledger_voucher("2-01-01-01", "BPV", credit=500)
    try:
        assert pt.feed_row(line.id) is None
    finally:
        _drop(v, line)


def test_a_real_party_keeps_its_identity_over_the_ledger(engine, scenario):
    """The subledger sweep runs last, so an account belonging to a customer is
    still that customer — never demoted to the ledger it sits under."""
    from shared import payment_tracking as pt
    acct = _customer_account(scenario["customer_id"])
    kind, party_id, _name = pt.party_account_index()[acct.id]
    assert kind == "customer"
    assert party_id == scenario["customer_id"]
    assert not pt.is_ledger_party(party_id), "a real customer is not a ledger"


def test_ledger_party_ids_cannot_collide_with_real_ones(engine, scenario):
    """Ledger ids are negative precisely so they can never be read as entity
    ids anywhere that stores or filters on a party."""
    from shared import payment_tracking as pt
    index = pt.party_account_index()
    ledger_ids = {pid for _k, pid, _n in index.values() if pt.is_ledger_party(pid)}
    real_ids = {pid for _k, pid, _n in index.values()
                if not pt.is_ledger_party(pid)}
    assert ledger_ids, "the seeded chart has subledger accounts to claim"
    assert ledger_ids & real_ids == set()
    assert all(i < 0 for i in ledger_ids)
    assert all(i > 0 for i in real_ids)


def test_a_ledger_payer_has_no_invoices_to_assign(engine, scenario):
    """It settles a ledger, not a party, so there is nothing to match it to —
    the workspace can only close it with a reason."""
    from shared import payment_tracking as pt
    _acct, v, line = _ledger_voucher("2-01-01-01", "BPV", debit=900)
    try:
        row = pt.feed_row(line.id)
        assert pt.open_invoices(row["party_type"], row["party_id"]) == []
        pt.force_close(line.id, 900, "no invoice — ledger settlement")
        assert pt.feed_row(line.id)["state"] == "forced"
    finally:
        _drop(v, line)


def test_feed_search_matches_the_ledger_the_money_moved_through(engine, scenario):
    """Search reaches the account, not just the voucher's own text.

    The code is the load-bearing half of this test: it appears nowhere in the
    voucher number, the line description or the notes, so a hit on it can only
    have come from the ledger being searched.
    """
    from shared import payment_tracking as pt
    acct = _customer_account(scenario["customer_id"])
    line_id = scenario["line_id"]

    assert line_id in {r["line_id"] for r in pt.feed_rows(search=acct.code)}, \
        "the ledger's code should find the receipt that settled through it"
    assert line_id in {r["line_id"] for r in pt.feed_rows(search=acct.name)}, \
        "the ledger's name should find it too"
    assert line_id not in {
        r["line_id"] for r in pt.feed_rows(search="zzz-no-such-ledger")}


# ── Allocation across multiple invoices ─────────────────────────────────────

def test_one_receipt_splits_across_two_invoices(engine, scenario):
    from shared import payment_tracking as pt
    pt.allocate(scenario["line_id"], [
        {"doc_id": scenario["inv_a"], "amount": 3000},
        {"doc_id": scenario["inv_b"], "amount": 1500},
    ])

    a = pt.invoice_row(pt.SALES, scenario["inv_a"])
    b = pt.invoice_row(pt.SALES, scenario["inv_b"])
    assert a["settled"] == pytest.approx(3000.0)
    assert a["pay_state"] == "paid"
    assert a["outstanding"] == pytest.approx(0.0)
    assert b["settled"] == pytest.approx(1500.0)
    assert b["pay_state"] == "partial"
    assert b["outstanding"] == pytest.approx(2500.0)

    row = pt.feed_row(scenario["line_id"])
    assert row["assigned"] == pytest.approx(4500.0)
    assert row["unassigned"] == pytest.approx(500.0)
    assert row["state"] == "partial"


def test_reassigning_the_same_line_replaces_rather_than_stacks(engine, scenario):
    """The bug the unique constraint exists for: a second assign must not add
    a second row and double the invoice's settled figure."""
    from shared import payment_tracking as pt
    from shared.models.payment_allocation import PaymentAllocation
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 1000}])
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 2000}])
    rows = PaymentAllocation.query.filter_by(
        voucher_line_id=scenario["line_id"],
        doc_id=scenario["inv_a"]).all()
    assert len(rows) == 1
    assert pt.invoice_row(pt.SALES, scenario["inv_a"])["settled"] == \
        pytest.approx(2000.0)


def test_split_cannot_exceed_the_money_that_moved(engine, scenario):
    from shared import payment_tracking as pt
    with pytest.raises(pt.AllocationError):
        pt.allocate(scenario["line_id"], [
            {"doc_id": scenario["inv_a"], "amount": 3000},
            {"doc_id": scenario["inv_b"], "amount": 4000},   # 7000 > 5000
        ])
    # Refused whole, not half-applied.
    assert pt.feed_row(scenario["line_id"])["assigned"] == pytest.approx(0.0)


def test_cannot_over_assign_a_single_invoice(engine, scenario):
    from shared import payment_tracking as pt
    with pytest.raises(pt.AllocationError):
        pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                           "amount": 3500}])  # inv is 3000


def test_unassign_returns_the_money_to_the_feed(engine, scenario):
    from shared import payment_tracking as pt
    from shared.models.payment_allocation import PaymentAllocation
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    alloc = PaymentAllocation.query.filter_by(
        voucher_line_id=scenario["line_id"]).first()
    pt.unallocate(alloc.id)

    assert pt.feed_row(scenario["line_id"])["unassigned"] == \
        pytest.approx(5000.0)
    back = pt.invoice_row(pt.SALES, scenario["inv_a"])
    assert back["pay_state"] == "unpaid"
    assert back["settled"] == pytest.approx(0.0)


# ── Force-closing money that matches no invoice ─────────────────────────────

def test_force_close_settles_a_receipt_with_no_invoice(engine, scenario):
    from shared import payment_tracking as pt
    row = pt.force_close(scenario["line_id"], reason="customer advance")
    assert row["state"] == "forced"
    assert row["forced"] == pytest.approx(5000.0)
    assert row["to_invoices"] == pytest.approx(0.0)
    assert row["unassigned"] == pytest.approx(0.0)
    assert row["forced_reason"] == "customer advance"

    # The whole point: no invoice moved.
    for doc_id in (scenario["inv_a"], scenario["inv_b"]):
        assert pt.invoice_row(pt.SALES, doc_id)["settled"] == pytest.approx(0.0)
        assert pt.invoice_row(pt.SALES, doc_id)["pay_state"] == "unpaid"


def test_partial_assignment_plus_forced_remainder(engine, scenario):
    """The mixed case: some of it matched an invoice, a person closed the rest.
    The row is finished, but it must report as forced, not as a clean match."""
    from shared import payment_tracking as pt
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    row = pt.force_close(scenario["line_id"], reason="rounding difference")

    assert row["state"] == "forced"
    assert row["to_invoices"] == pytest.approx(3000.0)
    assert row["forced"] == pytest.approx(2000.0)
    assert row["unassigned"] == pytest.approx(0.0)
    assert pt.invoice_row(pt.SALES, scenario["inv_a"])["pay_state"] == "paid"


def test_forced_amount_cannot_exceed_what_is_loose(engine, scenario):
    from shared import payment_tracking as pt
    with pytest.raises(pt.AllocationError):
        pt.force_close(scenario["line_id"], amount=6000, reason="too much")


def test_force_close_requires_a_reason(engine, scenario):
    from shared import payment_tracking as pt
    with pytest.raises(pt.AllocationError):
        pt.force_close(scenario["line_id"], reason="   ")


def test_forced_money_is_not_available_for_assignment(engine, scenario):
    """A forced close spends the money; a later assignment must see that."""
    from shared import payment_tracking as pt
    pt.force_close(scenario["line_id"], amount=4000, reason="advance")
    with pytest.raises(pt.AllocationError):
        pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                           "amount": 3000}])   # only 1000 left
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 1000}])
    assert pt.feed_row(scenario["line_id"])["unassigned"] == pytest.approx(0.0)


def test_releasing_a_forced_close_returns_it_to_the_queue(engine, scenario):
    from shared import payment_tracking as pt
    pt.force_close(scenario["line_id"], reason="advance")
    row = pt.release_force(scenario["line_id"])
    assert row["state"] == "unassigned"
    assert row["forced"] == pytest.approx(0.0)
    assert row["unassigned"] == pytest.approx(5000.0)


def test_feed_filters_by_every_assignment_state(engine, scenario):
    from shared import payment_tracking as pt
    line = scenario["line_id"]

    def states(*want):
        return {r["line_id"] for r in pt.feed_rows(assign_states=want)}

    assert line in states("unassigned")

    pt.allocate(line, [{"doc_id": scenario["inv_a"], "amount": 1000}])
    assert line in states("partial")
    assert line not in states("unassigned")

    pt.force_close(line, reason="advance")
    assert line in states("forced")
    assert line not in states("partial")
    assert line not in states("assigned"), \
        "a force-closed line must not pass as a clean invoice match"

    pt.release_force(line)
    pt.allocate(line, [{"doc_id": scenario["inv_a"], "amount": 3000},
                       {"doc_id": scenario["inv_b"], "amount": 2000}])
    assert line in states("assigned")
    assert line in states("unassigned", "partial", "assigned", "forced")


def test_forced_close_writes_no_journal(engine, scenario):
    from shared import payment_tracking as pt
    before = _ledger_fingerprint()
    pt.force_close(scenario["line_id"], reason="advance")
    assert _ledger_fingerprint() == before
    pt.release_force(scenario["line_id"])
    assert _ledger_fingerprint() == before


def test_forced_close_shows_in_the_lines_history(engine, scenario):
    from shared import payment_tracking as pt
    pt.force_close(scenario["line_id"], reason="customer advance")
    entries = pt.history_for_line(scenario["line_id"])
    assert len(entries) == 1
    assert entries[0]["forced"] is True
    assert "Not applicable" in entries[0]["number"]
    assert entries[0]["note"] == "customer advance"


# ── Filters: paid / partial / unpaid, and overdue ───────────────────────────

def test_pay_state_and_overdue_filters(engine, scenario):
    from shared import payment_tracking as pt
    pt.allocate(scenario["line_id"], [
        {"doc_id": scenario["inv_a"], "amount": 3000},   # paid
        {"doc_id": scenario["inv_b"], "amount": 1500},   # partial
    ])

    def ids(**kw):
        return {r["id"] for r in pt.invoice_rows(pt.SALES, **kw)}

    assert scenario["inv_a"] in ids(pay_states=["paid"])
    assert scenario["inv_b"] not in ids(pay_states=["paid"])
    assert scenario["inv_b"] in ids(pay_states=["partial"])
    assert scenario["inv_a"] not in ids(pay_states=["partial"])
    assert ids(pay_states=["paid", "partial"]) >= {scenario["inv_a"],
                                                   scenario["inv_b"]}

    # Overdue is about the due date, not the balance: invoice A is settled
    # so it must drop out even though its due date is 60 days past.
    overdue = ids(overdue_only=True)
    assert scenario["inv_a"] not in overdue
    assert scenario["inv_b"] not in overdue  # not due for another 30 days


def test_unpaid_overdue_invoice_is_flagged_and_aged(engine, scenario):
    from shared import payment_tracking as pt
    row = pt.invoice_row(pt.SALES, scenario["inv_a"])
    assert row["pay_state"] == "unpaid"
    assert row["overdue_days"] >= 55
    assert row["age_bucket"], "an overdue invoice must land in an age bucket"
    assert scenario["inv_a"] in {r["id"] for r in
                                 pt.invoice_rows(pt.SALES, overdue_only=True)}


def test_outstanding_summary_tracks_assignment(engine, scenario):
    from shared import payment_tracking as pt
    before = pt.outstanding_summary(pt.SALES)["outstanding"]
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    after = pt.outstanding_summary(pt.SALES)["outstanding"]
    assert after == pytest.approx(before - 3000.0)


# ── Allocation history ──────────────────────────────────────────────────────

def test_history_links_invoice_and_receipt_both_ways(engine, scenario):
    from shared import payment_tracking as pt
    pt.allocate(scenario["line_id"], [
        {"doc_id": scenario["inv_a"], "amount": 3000},
        {"doc_id": scenario["inv_b"], "amount": 1500},
    ], note="split across two")

    by_doc = pt.history_for_doc(pt.SALES, scenario["inv_a"])
    assert len(by_doc) == 1
    assert by_doc[0]["amount"] == pytest.approx(3000.0)
    assert by_doc[0]["voucher_number"] == "TRK-CRV-1"

    by_line = pt.history_for_line(scenario["line_id"])
    assert {h["doc_id"] for h in by_line} == {scenario["inv_a"],
                                              scenario["inv_b"]}
    assert sum(h["amount"] for h in by_line) == pytest.approx(4500.0)


def test_auto_split_proposes_oldest_first(engine, scenario):
    from shared import payment_tracking as pt
    proposal = pt.auto_split(scenario["line_id"])
    assert proposal, "5,000 against 7,000 of open invoices must propose a split"
    first = proposal[0]
    assert first["doc_id"] == scenario["inv_a"], "oldest invoice first"
    assert first["amount"] == pytest.approx(3000.0)
    assert sum(p["amount"] for p in proposal) == pytest.approx(5000.0)
    # A proposal only — nothing is written until the user confirms.
    assert pt.feed_row(scenario["line_id"])["assigned"] == pytest.approx(0.0)


# ── Vouchers and invoices edited after assignment ───────────────────────────

def _line(scenario):
    from shared.models.accounting_voucher import AccountingVoucherLine
    return AccountingVoucherLine.query.get(scenario["line_id"])


def test_shrinking_the_voucher_flags_the_line_for_reassignment(engine, scenario):
    """The voucher is edited down below its own split: the old assignment is
    kept and shown, and the line is pushed back into the feed to be re-cut."""
    from shared import payment_tracking as pt
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    line = _line(scenario)
    line.credit = Decimal("1000")           # was 5000
    db.session.commit()
    try:
        row = pt.feed_row(scenario["line_id"])
        assert row["state"] == "review"
        assert row["amount"] == pytest.approx(1000.0)
        assert row["assigned"] == pytest.approx(3000.0), \
            "the previous assignment must still be visible to re-cut"
        assert row["over_assigned"] == pytest.approx(2000.0)
        assert scenario["line_id"] in {
            r["line_id"] for r in pt.feed_rows(assign_states=["review"])}
    finally:
        line.credit = Decimal("5000")
        db.session.commit()


def test_unapproving_the_voucher_reverts_the_invoice(engine, scenario):
    """Only approved money settles anything."""
    from shared import payment_tracking as pt
    from shared.models.accounting_voucher import AccountingVoucher
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    assert pt.invoice_row(pt.SALES, scenario["inv_a"])["pay_state"] == "paid"

    voucher = AccountingVoucher.query.get(scenario["voucher_id"])
    voucher.status = "unapproved"
    db.session.commit()
    try:
        back = pt.invoice_row(pt.SALES, scenario["inv_a"])
        assert back["pay_state"] == "unpaid"
        assert back["settled"] == pytest.approx(0.0)
        assert back["outstanding"] == pytest.approx(3000.0)
        # And the cached column follows once the hook runs.
        pt.reconcile_voucher(scenario["voucher_id"])
        from inventory_app.models.invoice import InvInvoice
        assert InvInvoice.query.get(scenario["inv_a"]).payment_status == "unpaid"
    finally:
        voucher.status = "approved"
        db.session.commit()


def test_removing_the_party_from_the_voucher_reverts_the_invoice(engine,
                                                                 scenario):
    """The customer line is repointed at a non-party account — the assignment
    no longer has a payer behind it, so the invoice goes back to unpaid."""
    from shared import payment_tracking as pt
    from shared.models.ledger import ChartOfAccount
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    line = _line(scenario)
    original = line.account_id
    other = ChartOfAccount.query.filter(
        ChartOfAccount.id != original, ChartOfAccount.level == 5).first()
    line.account_id = other.id
    db.session.commit()
    try:
        back = pt.invoice_row(pt.SALES, scenario["inv_a"])
        assert back["pay_state"] == "unpaid"
        assert back["settled"] == pytest.approx(0.0)
        stale = pt.stale_allocations()
        assert any(s["doc_id"] == scenario["inv_a"] for s in stale)
    finally:
        line.account_id = original
        db.session.commit()


def test_raising_an_invoice_total_makes_it_partially_paid(engine, scenario):
    """Same rule from the invoice side: a fully settled 3,000 invoice raised to
    5,000 is partially paid, not paid."""
    from shared import payment_tracking as pt
    from inventory_app.models.invoice import InvInvoice
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    inv = InvInvoice.query.get(scenario["inv_a"])
    inv.total_amount = 5000.0
    db.session.commit()
    try:
        pt.sync_invoice(pt.SALES, inv.id)
        row = pt.invoice_row(pt.SALES, scenario["inv_a"])
        assert row["pay_state"] == "partial"
        assert row["outstanding"] == pytest.approx(2000.0)
        assert InvInvoice.query.get(scenario["inv_a"]).payment_status == "partial"
    finally:
        inv.total_amount = 3000.0
        db.session.commit()
        pt.sync_invoice(pt.SALES, inv.id)


def test_unapproving_the_invoice_returns_the_money_to_the_feed(engine, scenario):
    from shared import payment_tracking as pt
    from inventory_app.models.invoice import InvInvoice
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    inv = InvInvoice.query.get(scenario["inv_a"])
    inv.voucher_status = "unapproved"
    db.session.commit()
    try:
        row = pt.feed_row(scenario["line_id"])
        assert row["state"] == "unassigned"
        assert row["unassigned"] == pytest.approx(5000.0), \
            "money assigned to an unapproved invoice is loose again"
    finally:
        inv.voucher_status = "approved"
        db.session.commit()


def test_stale_allocations_describe_what_broke(engine, scenario):
    from shared import payment_tracking as pt
    from shared.models.accounting_voucher import AccountingVoucher
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    voucher = AccountingVoucher.query.get(scenario["voucher_id"])
    voucher.status = "unapproved"
    db.session.commit()
    try:
        stale = pt.stale_allocations()
        assert len(stale) == 1
        row = stale[0]
        assert row["voucher_number"] == "TRK-CRV-1"
        assert row["doc_number"] == "TRK-A"
        assert row["party_name"] == "Tracking Test Customer"
        assert row["amount"] == pytest.approx(3000.0)
        assert row["reason"], "every stale row must say why it broke"
    finally:
        voucher.status = "approved"
        db.session.commit()


def test_clearing_stale_allocations_removes_only_the_broken_ones(engine,
                                                                 scenario):
    from shared import payment_tracking as pt
    from shared.models.accounting_voucher import AccountingVoucher
    from shared.models.payment_allocation import PaymentAllocation
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000},
                                      {"doc_id": scenario["inv_b"],
                                       "amount": 1000}])
    voucher = AccountingVoucher.query.get(scenario["voucher_id"])
    voucher.status = "unapproved"
    db.session.commit()
    voucher.status = "approved"          # only inv_b is broken, below
    db.session.commit()

    # Break exactly one side: unapprove invoice B.
    from inventory_app.models.invoice import InvInvoice
    inv_b = InvInvoice.query.get(scenario["inv_b"])
    inv_b.voucher_status = "unapproved"
    db.session.commit()
    try:
        assert len(pt.stale_allocations()) == 1
        assert pt.clear_stale_allocations() == 1
        assert pt.stale_allocations() == []
        # The good half survived.
        remaining = PaymentAllocation.query.filter_by(
            voucher_line_id=scenario["line_id"]).all()
        assert {int(a.doc_id) for a in remaining} == {scenario["inv_a"]}
        assert pt.invoice_row(pt.SALES, scenario["inv_a"])["pay_state"] == "paid"
    finally:
        inv_b.voucher_status = "approved"
        db.session.commit()


def test_clearing_stale_writes_no_journal(engine, scenario):
    from shared import payment_tracking as pt
    from inventory_app.models.invoice import InvInvoice
    pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                       "amount": 3000}])
    inv = InvInvoice.query.get(scenario["inv_a"])
    inv.voucher_status = "unapproved"
    db.session.commit()
    before = _ledger_fingerprint()
    try:
        pt.clear_stale_allocations()
        assert _ledger_fingerprint() == before
    finally:
        inv.voucher_status = "approved"
        db.session.commit()


def test_reconcile_is_safe_to_run_with_nothing_to_do(engine, scenario):
    from shared import payment_tracking as pt
    assert pt.reconcile() == 0
    assert pt.reconcile_voucher(scenario["voucher_id"]) == 0


# ── The guarantee: no accounting impact ─────────────────────────────────────

def _ledger_fingerprint():
    """Everything the general ledger is made of, as one comparable value."""
    from shared.models.accounting_voucher import (AccountingVoucher,
                                                  AccountingVoucherLine)
    lines = (AccountingVoucherLine.query
             .order_by(AccountingVoucherLine.id).all())
    return {
        "voucher_count": AccountingVoucher.query.count(),
        "line_count": len(lines),
        "lines": [(l.id, l.voucher_id, l.account_id,
                   str(l.debit), str(l.credit)) for l in lines],
        "total_debit": sum(Decimal(str(l.debit or 0)) for l in lines),
        "total_credit": sum(Decimal(str(l.credit or 0)) for l in lines),
    }


def test_assignment_leaves_the_general_ledger_untouched(engine, scenario):
    """The core promise of this module: tracking is a separate section that
    records who paid which invoice. The money already hit the ledger when the
    voucher was approved; assigning it must not post, reverse or amend a thing.
    """
    from shared import payment_tracking as pt
    from shared.models.payment_allocation import PaymentAllocation

    before = _ledger_fingerprint()

    pt.allocate(scenario["line_id"], [
        {"doc_id": scenario["inv_a"], "amount": 3000},
        {"doc_id": scenario["inv_b"], "amount": 1500},
    ])
    assert _ledger_fingerprint() == before, \
        "assigning a payment must not write to the general ledger"

    alloc = PaymentAllocation.query.filter_by(
        voucher_line_id=scenario["line_id"]).first()
    pt.unallocate(alloc.id)
    assert _ledger_fingerprint() == before, \
        "un-assigning a payment must not write to the general ledger"


def test_allocations_carry_no_account_or_posting_columns():
    """A structural guard: if someone ever adds a debit/credit/account_id to
    this table, the "no accounting impact" claim quietly stops being true."""
    from shared.models.payment_allocation import PaymentAllocation
    cols = set(PaymentAllocation.__table__.c.keys())
    assert not (cols & {"debit", "credit", "account_id", "journal_id",
                        "ledger_entry_id", "voucher_type"})


# ── Pages render ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path_key", ["feed", "sales", "purchases"])
def test_tracking_pages_render(client, scenario, path_key):
    paths = {"feed": "/invoicing/tracking/",
             "sales": "/invoicing/tracking/sales",
             "purchases": "/invoicing/tracking/purchases"}
    resp = client.get(paths[path_key])
    assert resp.status_code == 200


def test_assign_workspace_and_invoice_history_render(client, reset, scenario):
    """These two take an id, so the route smoke test only ever sees them
    redirect — they need a real receipt and a real invoice to prove out."""
    resp = client.get(f"/invoicing/tracking/assign/{scenario['line_id']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "TRK-A" in body, "the customer's open invoices must be listed"

    resp = client.get(f"/invoicing/tracking/invoice/SI/{scenario['inv_a']}")
    assert resp.status_code == 200
    assert "TRK-A" in resp.get_data(as_text=True)


def test_filter_chips_survive_a_round_trip(client, scenario):
    """`?state=` present-but-empty means "all" — the Clear action depends on it
    being distinguishable from an absent parameter."""
    assert client.get("/invoicing/tracking/?state=").status_code == 200
    for state in ("unassigned", "partial", "assigned", "forced"):
        assert client.get(
            f"/invoicing/tracking/?state={state}").status_code == 200
    assert client.get(
        "/invoicing/tracking/sales?pay=paid&pay=partial&overdue=1"
    ).status_code == 200


def test_force_close_api_round_trip(client, reset, scenario):
    line = scenario["line_id"]
    resp = client.post(f"/invoicing/tracking/api/force-close/{line}",
                       json={"reason": "customer advance"})
    assert resp.status_code == 200
    row = resp.get_json()["row"]
    assert row["state"] == "forced"
    assert row["forced"] == pytest.approx(5000.0)

    resp = client.post(f"/invoicing/tracking/api/release-force/{line}")
    assert resp.status_code == 200
    assert resp.get_json()["row"]["state"] == "unassigned"


def test_workspace_renders_a_forced_split(client, reset, scenario):
    """A forced row points at no document — the workspace must not try to link
    it to an invoice."""
    line = scenario["line_id"]
    assert client.post(f"/invoicing/tracking/api/force-close/{line}",
                       json={"amount": 500,
                             "reason": "rounding"}).status_code == 200
    resp = client.get(f"/invoicing/tracking/assign/{line}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "rounding" in body
    assert "/invoicing/tracking/invoice/NA/0" not in body


def test_feed_renders_the_forced_row(client, reset, scenario):
    """Exercise the forced branch of the row template, not just the engine."""
    line = scenario["line_id"]
    client.post(f"/invoicing/tracking/api/force-close/{line}",
                json={"reason": "customer advance"})
    body = client.get("/invoicing/tracking/?state=forced").get_data(as_text=True)
    assert "customer advance" in body
    assert "No invoice" in body
    client.post(f"/invoicing/tracking/api/release-force/{line}")


def test_needs_attention_panel_renders_and_clears(client, reset, scenario):
    """The panel only appears when something is actually broken, explains why,
    and the clear action removes the link without touching the money."""
    from shared import payment_tracking as pt
    from inventory_app.models.invoice import InvInvoice

    body = client.get("/invoicing/tracking/").get_data(as_text=True)
    assert "needs attention" not in body

    with _tenant_ctx(scenario):
        pt.allocate(scenario["line_id"], [{"doc_id": scenario["inv_a"],
                                           "amount": 3000}])
        inv = InvInvoice.query.get(scenario["inv_a"])
        inv.voucher_status = "unapproved"
        db.session.commit()
    try:
        body = client.get("/invoicing/tracking/").get_data(as_text=True)
        assert "needs attention" in body
        assert "TRK-A" in body
        assert "no longer approved" in body

        resp = client.post("/invoicing/tracking/api/clear-stale", json={})
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] == 1

        body = client.get("/invoicing/tracking/").get_data(as_text=True)
        assert "needs attention" not in body
    finally:
        with _tenant_ctx(scenario):
            inv = InvInvoice.query.get(scenario["inv_a"])
            inv.voucher_status = "approved"
            db.session.commit()


def test_force_close_api_refuses_a_missing_reason(client, reset, scenario):
    resp = client.post(
        f"/invoicing/tracking/api/force-close/{scenario['line_id']}",
        json={"reason": ""})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_assign_api_rejects_a_bad_split(client, reset, scenario):
    resp = client.post(f"/invoicing/tracking/api/assign/{scenario['line_id']}",
                       json={"entries": [{"doc_id": scenario["inv_a"],
                                          "amount": 99999}]})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
