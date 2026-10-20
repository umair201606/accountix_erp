import json
import requests
from datetime import datetime
from flask import current_app
from shared.extensions import db
from shared.models.fbr import FBRConfig, FBRInvoiceRecord
from shared.tenancy import scoped_get_404


def _body(r):
    """FBR's JSON answer — or, when a gateway answers with an HTML error page,
    its first lines as the error. ``r.json()`` raised on those, outside the
    RequestException handler, so the route 500'd and kept no record."""
    if not r.text:
        return {}
    try:
        return r.json()
    except ValueError:
        return {"error": f"FBR answered {r.status_code}: {r.text[:300]}"}


class FBRApiClient:
    TIMEOUT = 30

    def __init__(self):
        self.config = FBRConfig.get()

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.access_token}",
        }

    def _url(self, path):
        return f"{self.config.base_url()}{path}"

    def heartbeat(self):
        try:
            r = requests.get(
                self._url("/heartbeat"),
                headers=self._headers(),
                timeout=self.TIMEOUT,
            )
            return r.ok, r.text
        except requests.RequestException as e:
            return False, str(e)

    def validate_invoice(self, payload):
        try:
            r = requests.post(
                self._url("/sales/validate"),
                headers=self._headers(),
                json=payload,
                timeout=self.TIMEOUT,
            )
            return r.ok, r.status_code, _body(r)
        except requests.RequestException as e:
            return False, 0, {"error": str(e)}

    def submit_invoice(self, payload):
        try:
            r = requests.post(
                self._url("/sales/send"),
                headers=self._headers(),
                json=payload,
                timeout=self.TIMEOUT,
            )
            return r.ok, r.status_code, _body(r)
        except requests.RequestException as e:
            return False, 0, {"error": str(e)}

    def get_invoice_status(self, fbr_invoice_no):
        try:
            r = requests.get(
                self._url(f"/sales/{fbr_invoice_no}"),
                headers=self._headers(),
                timeout=self.TIMEOUT,
            )
            return r.ok, r.status_code, _body(r)
        except requests.RequestException as e:
            return False, 0, {"error": str(e)}

    @staticmethod
    def record_submission(inv_invoice_id, payload, ok, status_code, response_data,
                          submitted_by, invoice_type="S"):
        record = FBRInvoiceRecord(
            inv_invoice_id=inv_invoice_id,
            invoice_type=invoice_type,
            request_json=json.dumps(payload, indent=2, default=str)
            if isinstance(payload, dict) else (payload or ""),
            response_json=json.dumps(response_data, indent=2, default=str)
            if isinstance(response_data, dict) else (response_data or ""),
            submitted_by=submitted_by,
            submitted_at=datetime.utcnow(),
            attempt_count=1,
        )

        if ok:
            record.status = "success"
            record.fbr_invoice_number = response_data.get("fbrInvoiceNumber", "")
            record.fbr_submission_id = response_data.get("submissionId", "")
        else:
            record.status = "failed"
            if isinstance(response_data, dict):
                record.error_message = response_data.get("error", response_data.get("message", str(response_data)))
            else:
                record.error_message = str(response_data)

        db.session.add(record)
        db.session.commit()
        return record

    def retry_submission(self, record, payload, submitted_by):
        """Send a failed record again with ``payload`` — the invoice as it
        is NOW. The old version re-sent the request saved from the failed
        attempt, so an invoice unapproved, corrected and re-approved since
        reached FBR with figures the books no longer had. The caller locks
        the invoice and checks it is approved and not yet reported."""
        ok, sc, data = self.submit_invoice(payload)
        record.request_json = json.dumps(payload, indent=2, default=str)
        record.attempt_count = (record.attempt_count or 0) + 1
        record.response_json = json.dumps(data, indent=2, default=str)
        record.submitted_at = datetime.utcnow()
        record.submitted_by = submitted_by
        if ok:
            record.status = "success"
            record.error_message = ""
            record.fbr_invoice_number = data.get("fbrInvoiceNumber", "")
            record.fbr_submission_id = data.get("submissionId", "")
        else:
            record.status = "failed"
            if isinstance(data, dict):
                record.error_message = data.get("error", data.get("message", str(data)))
            else:
                record.error_message = str(data)
        db.session.commit()
        return record, ok, data
