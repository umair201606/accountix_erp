import json
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from shared.extensions import db
from shared.models.fbr import FBRConfig, FBRInvoiceRecord
from fbr_app.services.fbr_client import FBRApiClient
from fbr_app.services.fbr_mapper import build_fbr_invoice
from inventory_app.models.invoice import InvInvoice
from shared.tenancy import scoped_get_404

fbr_dashboard_bp = Blueprint("fbr_dashboard", __name__, url_prefix="/fbr",
                             template_folder="../templates/fbr")


@fbr_dashboard_bp.route("/")
@login_required
def dashboard():
    if not current_user.is_admin() and not current_user.has_fbr_access:
        flash("Access denied.", "error")
        return redirect(url_for("dashboard.hub"))

    config = FBRConfig.get()
    if not config.is_active or not config.access_token:
        flash("FBR module is not configured. Ask an administrator to configure FBR settings first.", "warning")
        return render_template("dashboard.html", records=[], config=config, now=datetime.utcnow())

    status = request.args.get("status", "")
    query = FBRInvoiceRecord.query.order_by(FBRInvoiceRecord.id.desc())
    if status:
        query = query.filter_by(status=status)

    records = query.all()
    return render_template("dashboard.html", records=records, config=config, now=datetime.utcnow())


@fbr_dashboard_bp.route("/submit/<int:invoice_id>", methods=["POST"])
@login_required
def submit_invoice(invoice_id):
    if not current_user.is_admin() and not current_user.has_fbr_access:
        return jsonify({"ok": False, "error": "Access denied"}), 403

    config = FBRConfig.get()
    if not config.is_active or not config.access_token:
        return jsonify({"ok": False, "error": "FBR not configured"}), 400

    invoice = scoped_get_404(InvInvoice, invoice_id)
    if invoice.voucher_status != "approved":
        return jsonify({"ok": False, "error": "Only approved invoices can be submitted to FBR"}), 400

    existing = FBRInvoiceRecord.query.filter_by(
        inv_invoice_id=invoice_id, status="success"
    ).first()
    if existing:
        return jsonify({"ok": False, "error": "Invoice already submitted successfully to FBR"}), 400

    try:
        payload = build_fbr_invoice(invoice_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to build payload: {str(e)}"}), 500

    client = FBRApiClient()
    ok, sc, data = client.submit_invoice(payload)
    record = FBRApiClient.record_submission(
        inv_invoice_id=invoice_id,
        payload=payload,
        ok=ok,
        status_code=sc,
        response_data=data,
        submitted_by=current_user.id,
        invoice_type=payload.get("fbrInvoice", {}).get("invoiceType", "S"),
    )

    if ok:
        return jsonify({
            "ok": True,
            "message": "Invoice submitted to FBR successfully.",
            "record_id": record.id,
            "fbr_invoice_number": record.fbr_invoice_number,
        })
    return jsonify({
        "ok": False,
        "error": record.error_message or "FBR submission failed",
        "record_id": record.id,
    }), 400


@fbr_dashboard_bp.route("/validate/<int:invoice_id>", methods=["POST"])
@login_required
def validate_invoice(invoice_id):
    if not current_user.is_admin() and not current_user.has_fbr_access:
        return jsonify({"ok": False, "error": "Access denied"}), 403

    config = FBRConfig.get()
    if not config.is_active or not config.access_token:
        return jsonify({"ok": False, "error": "FBR not configured"}), 400

    invoice = scoped_get_404(InvInvoice, invoice_id)
    try:
        payload = build_fbr_invoice(invoice_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to build payload: {str(e)}"}), 500

    client = FBRApiClient()
    ok, sc, data = client.validate_invoice(payload)
    if ok:
        return jsonify({"ok": True, "message": "Invoice validated successfully.", "data": data})
    return jsonify({
        "ok": False,
        "error": data.get("error", data.get("message", "Validation failed")) if isinstance(data, dict) else str(data),
        "data": data,
    }), 400


@fbr_dashboard_bp.route("/retry/<int:record_id>", methods=["POST"])
@login_required
def retry_submission(record_id):
    if not current_user.is_admin() and not current_user.has_fbr_access:
        return jsonify({"ok": False, "error": "Access denied"}), 403

    config = FBRConfig.get()
    if not config.is_active or not config.access_token:
        return jsonify({"ok": False, "error": "FBR not configured"}), 400

    client = FBRApiClient()
    record, ok, data = client.retry_submission(record_id, current_user.id)

    if ok:
        return jsonify({
            "ok": True,
            "message": "Retry successful.",
            "record_id": record.id,
            "fbr_invoice_number": record.fbr_invoice_number,
        })
    return jsonify({
        "ok": False,
        "error": record.error_message or "Retry failed",
        "record_id": record.id,
    }), 400


@fbr_dashboard_bp.route("/record/<int:record_id>")
@login_required
def view_record(record_id):
    record = scoped_get_404(FBRInvoiceRecord, record_id)
    return jsonify({
        "id": record.id,
        "inv_invoice_id": record.inv_invoice_id,
        "fbr_invoice_number": record.fbr_invoice_number,
        "fbr_submission_id": record.fbr_submission_id,
        "status": record.status,
        "error_message": record.error_message,
        "attempt_count": record.attempt_count,
        "invoice_type": record.invoice_type,
        "submitted_at": record.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if record.submitted_at else "",
        "submitted_by": record.submitter.full_name if record.submitter else "",
        "request_json": record.request_json,
        "response_json": record.response_json,
    })


@fbr_dashboard_bp.route("/status/<int:invoice_id>")
@login_required
def check_status(invoice_id):
    record = FBRInvoiceRecord.latest_for_invoice(invoice_id)
    if not record:
        return jsonify({"ok": False, "error": "No FBR record found"}), 404

    if record.status == "success" and record.fbr_invoice_number:
        client = FBRApiClient()
        ok, sc, data = client.get_invoice_status(record.fbr_invoice_number)
        if ok:
            return jsonify({"ok": True, "data": data})
    return jsonify({
        "ok": True,
        "status": record.status,
        "message": record.error_message or "Pending",
    })
