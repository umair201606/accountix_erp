from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from shared.extensions import db
from shared.models.fbr import FBRConfig
from fbr_app.services.fbr_client import FBRApiClient

fbr_settings_bp = Blueprint("fbr_settings", __name__, url_prefix="/fbr/settings",
                             template_folder="../templates/fbr")


@fbr_settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if not current_user.is_admin():
        flash("Only administrators can access FBR settings.", "error")
        return redirect(url_for("dashboard.hub"))

    config = FBRConfig.get()

    if request.method == "POST":
        config.environment = request.form.get("environment", "sandbox")
        config.access_token = request.form.get("access_token", "")
        config.seller_ntn = request.form.get("seller_ntn", "")
        config.seller_strn = request.form.get("seller_strn", "")
        config.seller_cnic = request.form.get("seller_cnic", "")
        config.seller_name = request.form.get("seller_name", "")
        config.seller_business_name = request.form.get("seller_business_name", "")
        config.seller_address = request.form.get("seller_address", "")
        config.seller_phone = request.form.get("seller_phone", "")
        config.seller_email = request.form.get("seller_email", "")
        config.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash("FBR configuration saved.", "success")
        return redirect(url_for("fbr_settings.index"))

    return render_template("settings.html", config=config, now=datetime.utcnow())


@fbr_settings_bp.route("/test-connection", methods=["POST"])
@login_required
def test_connection():
    if not current_user.is_admin():
        return jsonify({"ok": False, "error": "Admin only"}), 403

    client = FBRApiClient()
    ok, message = client.heartbeat()
    if ok:
        return jsonify({"ok": True, "message": "Connection successful: " + message})
    return jsonify({"ok": False, "message": "Connection failed: " + message})
