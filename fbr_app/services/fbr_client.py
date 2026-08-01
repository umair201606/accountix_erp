import json
import requests
from datetime import datetime
from flask import current_app
from shared.extensions import db
from shared.models.fbr import FBRConfig, FBRInvoiceRecord
from shared.tenancy import scoped_get_404


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
            return r.ok, r.status_code, r.json() if r.text else {}
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
            return r.ok, r.status_code, r.json() if r.text else {}
        except requests.RequestException as e:
            return False, 0, {"error": str(e)}

    def get_invoice_status(self, fbr_invoice_no):
        try:
            r = requests.get(
                self._url(f"/sales/{fbr_invoice_no}"),
                headers=self._headers(),
                timeout=self.TIMEOUT,
            )
            return r.ok, r.status_code, r.json() if r.text else {}
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

    def retry_submission(self, record_id, submitted_by):
        record = scoped_get_404(FBRInvoiceRecord, record_id)
        payload = json.loads(record.request_json) if record.request_json else {}
        ok, sc, data = self.submit_invoice(payload)
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
