"""Executive Reports: receivables and payables off live ledger balances.

The module's promise is that it holds no figures of its own. Everything is a
read of posted journal lines, so a voucher edited, reversed or deleted shows
here on the next page load, and a party whose balance changes sign moves from
one register to the other by itself. The tests below are written against that
promise rather than against any stored state.
"""
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_exec_reports.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Deliberately NOT `with flask_app.test_client() as c`: as a context
    # manager the client sets preserve_context, so every request leaves its app
    # context pushed. That leaked context sits on top of the one `scope` holds
    # open, and the two then pop out of order at teardown.
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
def chart(client):
    """A parent control account with two child party accounts under it."""
    from shared.models.company import Company
    from shared.models.ledger import ChartOfAccount
    from shared.tenancy import set_current_company, unscoped

    with flask_app.app_context():
        with unscoped():
            company = Company.query.order_by(Company.id).first()
        assert company is not None
        set_current_company(company.id)

        parent = ChartOfAccount(code="9-90-01-01", name="Exec Test Control",
                                type="Asset", level=4)
        db.session.add(parent)
        db.session.commit()
        kids = []
        for i, name in enumerate(("Exec Party One", "Exec Party Two"), start=1):
            k = ChartOfAccount(code=f"9-90-01-01-000{i}", name=name,
                               type="Asset", parent_id=parent.id, level=5)
            db.session.add(k)
            kids.append(k)
        # A third account outside the control account, to prove scope holds.
        outside = ChartOfAccount(code="9-90-02-01", name="Exec Outside",
                                 type="Asset", level=4)
        db.session.add(outside)
        db.session.commit()
        return {"company_id": company.id, "parent": parent.id,
                "a": kids[0].id, "b": kids[1].id, "outside": outside.id}


def _post(account_id, debit=0, credit=0, number="EXEC-JV", when=None):
    """One posted journal line against an account, with its balancing half."""
    from shared.models.ledger import JournalEntry, JournalLine
    from hr_app.models.user import User
    uid = User.query.first().id
    entry = JournalEntry(voucher_type="JV", voucher_id=0,
                         voucher_number=number, description="exec test",
                         entry_date=when or datetime.utcnow(), created_by=uid,
                         is_posted=True)
    db.session.add(entry)
    db.session.commit()
    db.session.add(JournalLine(journal_entry_id=entry.id, account_id=account_id,
                               debit=Decimal(str(debit)),
                               credit=Decimal(str(credit))))
    db.session.commit()
    return entry


@pytest.fixture
def scope(chart):
    """Watch the parent control account, with children, and start clean."""
    from shared.models.ledger import JournalEntry, JournalLine
    from shared.models.exec_report import ExecAccountSelection
    from shared.tenancy import set_current_company
    from shared import executive_reports as er

    with flask_app.app_context():
        set_current_company(chart["company_id"])
        # Take the lines out by entry as well as by account. Leaving a line
        # behind is not harmless: SQLite hands the freed rowid to the next
        # entry inserted, and the orphan is silently adopted by a new voucher,
        # so the previous test's amount reappears in this test's totals.
        stale = [e.id for e in JournalEntry.query.filter(
            JournalEntry.voucher_number.like("EXEC-%")).all()]
        JournalLine.query.filter(db.or_(
            JournalLine.account_id.in_(
                [chart["parent"], chart["a"], chart["b"], chart["outside"]]),
            JournalLine.journal_entry_id.in_(stale),
        )).delete(synchronize_session=False)
        JournalEntry.query.filter(
            JournalEntry.voucher_number.like("EXEC-%")).delete(
            synchronize_session=False)
        ExecAccountSelection.query.delete()
        db.session.commit()
        er.set_selection([chart["parent"]], [chart["parent"]])
        yield chart


# ── Which side a party lands on ─────────────────────────────────────────────

def test_debit_balance_is_a_receivable(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    rows = er.party_rows(side=er.RECEIVABLE)
    assert [r["account_id"] for r in rows] == [scope["a"]]
    assert rows[0]["amount"] == pytest.approx(1000.0)
    assert er.party_rows(side=er.PAYABLE) == []


def test_credit_balance_is_a_payable_even_on_an_asset_account(scope):
    """The rule the module turns on: the sign decides, not the account type.

    These accounts are typed Asset (trade-debtor shaped). A credit balance on
    one still belongs in Payables.
    """
    from shared import executive_reports as er
    _post(scope["a"], credit=700)
    rows = er.party_rows(side=er.PAYABLE)
    assert [r["account_id"] for r in rows] == [scope["a"]]
    assert rows[0]["amount"] == pytest.approx(700.0)
    assert rows[0]["type"].lower() == "asset", "still an asset account"
    assert er.party_rows(side=er.RECEIVABLE) == []


def test_a_party_moves_sides_when_the_balance_flips(scope):
    """A receivable that is over-received becomes a payable on its own."""
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    assert [r["account_id"] for r in er.party_rows(side=er.RECEIVABLE)] == \
        [scope["a"]]

    _post(scope["a"], credit=1600)          # now 600 the other way
    assert er.party_rows(side=er.RECEIVABLE) == []
    payable = er.party_rows(side=er.PAYABLE)
    assert [r["account_id"] for r in payable] == [scope["a"]]
    assert payable[0]["amount"] == pytest.approx(600.0)


def test_a_settled_party_drops_off_both_registers(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=500)
    _post(scope["a"], credit=500)
    assert er.party_rows() == [], "a net-zero party owes nothing either way"


# ── Reacting to edited and deleted vouchers ─────────────────────────────────

def test_deleting_a_posting_changes_the_figures(scope):
    from shared import executive_reports as er
    from shared.models.ledger import JournalLine
    _post(scope["a"], debit=1000)
    entry = _post(scope["a"], debit=250)
    assert er.party_rows()[0]["amount"] == pytest.approx(1250.0)

    JournalLine.query.filter_by(journal_entry_id=entry.id).delete()
    db.session.delete(entry)
    db.session.commit()
    assert er.party_rows()[0]["amount"] == pytest.approx(1000.0)


def test_editing_a_posting_changes_the_figures(scope):
    from shared import executive_reports as er
    from shared.models.ledger import JournalLine
    entry = _post(scope["a"], debit=1000)
    line = JournalLine.query.filter_by(journal_entry_id=entry.id).first()
    line.debit = Decimal("400")
    db.session.commit()
    assert er.party_rows()[0]["amount"] == pytest.approx(400.0)


def test_unposted_entries_are_ignored(scope):
    """Only posted lines count — an unapproved voucher has moved nothing."""
    from shared import executive_reports as er
    entry = _post(scope["a"], debit=900)
    entry.is_posted = False
    db.session.commit()
    assert er.party_rows() == []


def test_a_reversal_nets_the_party_back_off_the_register(scope):
    """Unapproving posts a reversal rather than deleting; the effect is the
    same here because the balance is derived, never accumulated."""
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    _post(scope["a"], credit=1000, number="EXEC-REV")
    assert er.party_rows() == []


# ── Selecting whole parents vs individual children ──────────────────────────

def test_selecting_a_parent_takes_every_child(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=100)
    _post(scope["b"], debit=200)
    assert {r["account_id"] for r in er.party_rows()} == {scope["a"], scope["b"]}


def test_selecting_one_child_excludes_its_siblings(scope):
    from shared import executive_reports as er
    er.set_selection([scope["b"]], [])       # just that child, no descendants
    _post(scope["a"], debit=100)
    _post(scope["b"], debit=200)
    assert {r["account_id"] for r in er.party_rows()} == {scope["b"]}


def test_a_parent_can_be_watched_without_its_children(scope):
    from shared import executive_reports as er
    er.set_selection([scope["parent"]], [])  # parent alone
    _post(scope["a"], debit=100)
    _post(scope["parent"], debit=50)
    assert {r["account_id"] for r in er.party_rows()} == {scope["parent"]}


def test_accounts_outside_the_selection_never_appear(scope):
    from shared import executive_reports as er
    _post(scope["outside"], debit=5000)
    assert scope["outside"] not in {r["account_id"] for r in er.party_rows()}


def test_selection_is_replaced_not_merged(scope):
    """Unticking has to be able to remove an account."""
    from shared import executive_reports as er
    er.set_selection([scope["a"], scope["b"]], [])
    assert len(er.selections()) == 2
    er.set_selection([scope["a"]], [])
    assert [int(s.account_id) for s in er.selections()] == [scope["a"]]


# ── Totals and the as-of date ───────────────────────────────────────────────

def test_totals_are_amount_and_party_count(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    _post(scope["b"], debit=250)
    t = er.totals(er.party_rows(side=er.RECEIVABLE))
    assert t["amount"] == pytest.approx(1250.0)
    assert t["parties"] == 2


def test_both_sides_are_counted_separately(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    _post(scope["b"], credit=400)
    sides = er.both_sides()
    assert sides[er.RECEIVABLE] == {"amount": pytest.approx(1000.0), "parties": 1}
    assert sides[er.PAYABLE] == {"amount": pytest.approx(400.0), "parties": 1}


def test_as_of_date_excludes_later_postings(scope):
    from shared import executive_reports as er
    today = datetime.utcnow()
    _post(scope["a"], debit=1000, when=today - timedelta(days=10))
    _post(scope["a"], debit=500, when=today + timedelta(days=10))
    assert er.party_rows()[0]["amount"] == pytest.approx(1500.0)
    assert er.party_rows(as_of=today)[0]["amount"] == pytest.approx(1000.0)


def test_party_ledger_shows_the_postings_behind_a_balance(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=1000, number="EXEC-ONE")
    _post(scope["a"], credit=300, number="EXEC-TWO")
    lines = er.account_ledger(scope["a"])
    assert {l["voucher_number"] for l in lines} == {"EXEC-ONE", "EXEC-TWO"}
    assert sum(l["debit"] for l in lines) == pytest.approx(1000.0)
    assert sum(l["credit"] for l in lines) == pytest.approx(300.0)


# ── The guarantee: no accounting impact ─────────────────────────────────────

def _ledger_fingerprint():
    from shared.models.ledger import JournalEntry, JournalLine
    lines = JournalLine.query.order_by(JournalLine.id).all()
    return {
        "entries": JournalEntry.query.count(),
        "lines": [(l.id, l.account_id, str(l.debit), str(l.credit))
                  for l in lines],
    }


def test_reading_and_configuring_writes_no_journal(scope):
    """The module must never post. Reading obviously does not, and neither
    does changing which accounts it watches."""
    from shared import executive_reports as er
    _post(scope["a"], debit=1000)
    before = _ledger_fingerprint()

    er.party_rows()
    er.both_sides()
    er.account_ledger(scope["a"])
    er.set_selection([scope["a"], scope["b"]], [scope["a"]])
    er.set_selection([], [])

    assert _ledger_fingerprint() == before


def test_selection_table_carries_no_amount_columns():
    """Structural guard: an amount here would mean a figure that can go stale
    against the ledger it is supposed to be derived from."""
    from shared.models.exec_report import ExecAccountSelection
    cols = set(ExecAccountSelection.__table__.c.keys())
    assert not (cols & {"amount", "balance", "debit", "credit", "side",
                        "outstanding"})


# ── Pages ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/executive/", "/executive/receivables",
                                  "/executive/payables", "/executive/settings"])
def test_pages_render(client, chart, path):
    assert client.get(path).status_code == 200


def test_registers_show_the_party_on_the_right_side(client, scope):
    # The `scope` fixture already holds an app context with the company set;
    # nesting another one here pops the two out of order at teardown.
    _post(scope["a"], debit=1000)
    _post(scope["b"], credit=400)

    recv = client.get("/executive/receivables").get_data(as_text=True)
    assert "Exec Party One" in recv
    assert "Exec Party Two" not in recv

    pay = client.get("/executive/payables").get_data(as_text=True)
    assert "Exec Party Two" in pay
    assert "Exec Party One" not in pay


# ── Aging ────────────────────────────────────────────────────────────────────

def test_aging_ages_each_side_from_its_posting_layers(scope):
    from shared import executive_reports as er
    today = datetime.utcnow()
    _post(scope["a"], debit=1000, when=today - timedelta(days=45))
    _post(scope["a"], credit=400, when=today - timedelta(days=5))
    _post(scope["b"], credit=400, when=today - timedelta(days=5))

    sides = er.aging()
    recv = sides[er.RECEIVABLE]
    pay = sides[er.PAYABLE]

    # The 400 received 5 days ago came off the oldest layer first: 600 of the
    # receivable is still 45 days old.
    assert recv["total"] == pytest.approx(600.0)
    assert recv["avg_days"] == pytest.approx(45.0)
    assert recv["oldest_days"] == 45
    assert recv["oldest_name"] == "Exec Party One"
    by_key = {b["key"]: b for b in recv["buckets"]}
    assert set(by_key) == {"current", "d31", "d61", "d91"}
    assert by_key["d31"]["amount"] == pytest.approx(600.0)
    assert by_key["d31"]["share"] == pytest.approx(100.0)
    assert by_key["current"]["amount"] == pytest.approx(0.0)

    assert pay["total"] == pytest.approx(400.0)
    assert pay["avg_days"] == pytest.approx(5.0)
    assert pay["oldest_days"] == 5
    assert pay["buckets"][0]["amount"] == pytest.approx(400.0)


def test_aging_consumes_layers_in_fifo_order(scope):
    from shared import executive_reports as er
    today = datetime.utcnow()
    _post(scope["a"], debit=1000, when=today - timedelta(days=20))
    _post(scope["a"], debit=500, when=today - timedelta(days=10))
    _post(scope["a"], credit=1200, when=today - timedelta(days=2))

    recv = er.aging()[er.RECEIVABLE]
    # 1200 paid off: the whole 20-day layer (1000) and 200 of the 10-day one.
    assert recv["total"] == pytest.approx(300.0)
    assert recv["avg_days"] == pytest.approx(10.0)
    assert recv["oldest_days"] == 10
    assert recv["buckets"][0]["amount"] == pytest.approx(300.0)


def test_aging_top_parties_carries_the_biggest_first(scope):
    from shared import executive_reports as er
    _post(scope["a"], debit=900)
    _post(scope["b"], debit=100)
    top = er.aging()[er.RECEIVABLE]["top"]
    assert [t["name"] for t in top] == ["Exec Party One", "Exec Party Two"]
    assert top[0]["share"] == pytest.approx(90.0)
    assert top[1]["share"] == pytest.approx(10.0)


def test_aging_without_selection_is_empty(scope):
    from shared import executive_reports as er
    er.set_selection([], [])
    _post(scope["a"], debit=1000)
    sides = er.aging()
    assert sides[er.RECEIVABLE]["total"] == 0.0
    assert sides[er.PAYABLE]["parties"] == 0
    assert sides[er.PAYABLE]["avg_days"] == 0.0
    assert [b["amount"] for b in sides[er.RECEIVABLE]["buckets"]] == [0.0] * 4


def test_aging_as_of_date_excludes_later_postings(scope):
    from shared import executive_reports as er
    today = datetime.utcnow()
    _post(scope["a"], debit=1000, when=today - timedelta(days=10))
    _post(scope["a"], debit=500, when=today + timedelta(days=10))
    assert er.aging()[er.RECEIVABLE]["total"] == pytest.approx(1500.0)
    assert er.aging(as_of=today)[er.RECEIVABLE]["total"] == pytest.approx(1000.0)
    assert er.aging(as_of=today)[er.RECEIVABLE]["avg_days"] == pytest.approx(10.0)


# ── Liquidity snapshot ───────────────────────────────────────────────────────

def test_liquidity_tracks_net_assets_and_current_assets(scope):
    from shared import executive_reports as er
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    before = er.liquidity()
    assert isinstance(before["net_assets"], float)
    assert before["current_ratio"] is None or isinstance(
        before["current_ratio"], float)

    cash = ChartOfAccount.query.filter_by(name="Main Cash").first()
    assert cash is not None, "seed chart has a cash account under Current Assets"
    entry = _post(cash.id, debit=500)

    after = er.liquidity()
    assert after["total_assets"] - before["total_assets"] == pytest.approx(500.0)
    assert after["current_assets"] - before["current_assets"] == pytest.approx(500.0)
    assert after["net_assets"] - before["net_assets"] == pytest.approx(500.0)
    assert after["inventory"] == pytest.approx(before["inventory"])

    JournalLine.query.filter_by(journal_entry_id=entry.id).delete()
    db.session.delete(entry)
    db.session.commit()


def test_ratios_are_the_documented_formulas(scope):
    from shared import executive_reports as er
    liq = er.liquidity()
    if liq["current_liabilities"] > 0:
        assert liq["current_ratio"] == pytest.approx(
            liq["current_assets"] / liq["current_liabilities"])
        assert liq["quick_ratio"] == pytest.approx(
            (liq["current_assets"] - liq["inventory"])
            / liq["current_liabilities"])
    else:
        # Zero or negative current liabilities is not a real ratio: "—".
        assert liq["current_ratio"] is None
        assert liq["quick_ratio"] is None


def test_healthy_payables_give_a_real_ratio(scope):
    """A normal payable must yield a positive current-liability magnitude and
    a real ratio. Liabilities are held in credit, and the liquidity snapshot
    used to net them debit-minus-credit — negative on healthy books, so the
    `cl > 0` guard never passed and the ratio tiles read "—" for every
    company that was not in trouble."""
    from shared import executive_reports as er
    from shared.models.ledger import ChartOfAccount

    payables = ChartOfAccount.query.filter_by(
        name="Trade Creditors — General").first()
    assert payables is not None, "seed chart has a current-liability account"
    _post(payables.id, credit=400)

    liq = er.liquidity()
    assert liq["current_liabilities"] > 0
    assert isinstance(liq["current_ratio"], float)
    assert isinstance(liq["quick_ratio"], float)
    assert liq["current_ratio"] == pytest.approx(
        liq["current_assets"] / liq["current_liabilities"])


def test_dashboard_leads_with_kpis_aging_and_exposures(client, scope):
    today = datetime.utcnow()
    _post(scope["a"], debit=1000, when=today - timedelta(days=45))
    _post(scope["b"], credit=400)
    page = client.get("/executive/").get_data(as_text=True)

    for label in ("Net assets", "Current ratio", "Quick ratio",
                  "Avg collection days", "Avg payment days",
                  "Oldest receivable", "Oldest payable"):
        assert label in page
    assert "Aging profile" in page
    assert "Largest exposures" in page
    assert "31\u201360 days" in page
    assert "Exec Party One" in page
    assert "Exec Party Two" in page
