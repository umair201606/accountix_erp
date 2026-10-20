"""FBR Retry reports the invoice as it is now, once, and never crashes on a
gateway's HTML error page.

Retry re-sent the request saved from the failed attempt: an invoice that was
unapproved, corrected and approved again in between reached FBR with the old
figures. It also skipped every guard Send has — an invoice already reported
(by Send, or by a second retry pressed at the same moment) was reported
again, which cannot be taken back. And ``r.json()`` on a non-JSON answer
raised outside the network-error handler, so the route 500'd and no record of
the attempt was kept.
"""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_fbr_retry.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import CompanyMembership
    from shared.models.fbr import FBRConfig
    from shared.tenancy import set_current_company
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice
    with flask_app.app_context():
        user = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        cid = CompanyMembership.query.filter_by(user_id=user.id, status="active").first().company_id
        set_current_company(cid)
        cfg = FBRConfig.get()
        cfg.is_active, cfg.access_token = True, "test-token"
        cust = InvCustomer(name="FBR Retry Buyer")
        db.session.add(cust)
        db.session.flush()
        inv = InvInvoice(invoice_number="SI-FBR-RETRY", voucher_number="SV-FBR-RETRY",
                         customer_id=cust.id, voucher_status="approved",
                         total_amount=1000)
        db.session.add(inv)
        db.session.commit()
        ids = {"company": cid, "invoice": inv.id, "user": user.id}
    with c.session_transaction() as s:
        s["_user_id"] = str(ids["user"])
        s["_fresh"] = True
        s["company_id"] = cid
    ids["client"] = c
    return ids


@pytest.fixture
def fbr(monkeypatch, world):
    """FBR stand-in: records what was sent, answers as told; the payload is
    built from the invoice's current total."""
    from fbr_app.routes import dashboard
    from fbr_app.services.fbr_client import FBRApiClient
    from inventory_app.models.invoice import InvInvoice
    sent, answers = [], []

    def build(invoice_id):
        inv = db.session.get(InvInvoice, invoice_id)
        return {"fbrInvoice": {"invoiceType": "S", "total": float(inv.total_amount)}}

    def submit(self, payload):
        sent.append(payload)
        return answers.pop(0) if answers else (False, 500, {"error": "down"})

    monkeypatch.setattr(dashboard, "build_fbr_invoice", build)
    monkeypatch.setattr(FBRApiClient, "submit_invoice", submit)
    return {"sent": sent, "answers": answers}


def _set(world, **fields):
    from inventory_app.models.invoice import InvInvoice
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        inv = db.session.get(InvInvoice, world["invoice"])
        for k, v in fields.items():
            setattr(inv, k, v)
        db.session.commit()


def _records(world):
    from shared.models.fbr import FBRInvoiceRecord
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        return [(r.id, r.status) for r in FBRInvoiceRecord.query.filter_by(
            inv_invoice_id=world["invoice"]).order_by(FBRInvoiceRecord.id)]


def test_retry_sends_the_invoice_as_it_is_now(world, fbr):
    c = world["client"]
    r = c.post(f"/fbr/submit/{world['invoice']}")
    assert r.status_code == 400                       # FBR down: failed record
    record_id = r.get_json()["record_id"]
    _set(world, total_amount=1250)                    # corrected since
    fbr["answers"].append((True, 200, {"fbrInvoiceNumber": "FBR-1", "submissionId": "S1"}))
    r = c.post(f"/fbr/retry/{record_id}")
    assert r.status_code == 200, r.get_json()
    assert fbr["sent"][-1]["fbrInvoice"]["total"] == 1250.0


def test_a_reported_invoice_is_not_reported_again(world, fbr):
    rec_id = _records(world)[-1][0]
    r = world["client"].post(f"/fbr/retry/{rec_id}")
    assert r.status_code == 400 and "already submitted" in r.get_json()["error"]
    assert fbr["sent"] == []


def test_an_unapproved_invoice_is_not_retried(world, fbr):
    from shared.models.fbr import FBRInvoiceRecord
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        FBRInvoiceRecord.query.filter_by(inv_invoice_id=world["invoice"]).update(
            {"status": "failed"})
        db.session.commit()
    _set(world, voucher_status="draft")
    rec_id = _records(world)[-1][0]
    r = world["client"].post(f"/fbr/retry/{rec_id}")
    assert r.status_code == 400 and "approved" in r.get_json()["error"]
    assert fbr["sent"] == []
    _set(world, voucher_status="approved")


def test_an_html_error_page_is_recorded_not_a_crash(world, monkeypatch):
    import requests
    from fbr_app.routes import dashboard

    class Gateway:
        ok, status_code, text = False, 502, "<html><body>Bad Gateway</body></html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(dashboard, "build_fbr_invoice",
                        lambda _id: {"fbrInvoice": {"invoiceType": "S"}})
    monkeypatch.setattr(requests, "post", lambda *a, **k: Gateway())
    before = len(_records(world))
    r = world["client"].post(f"/fbr/submit/{world['invoice']}")
    assert r.status_code == 400
    assert "502" in r.get_json()["error"]
    assert len(_records(world)) == before + 1
