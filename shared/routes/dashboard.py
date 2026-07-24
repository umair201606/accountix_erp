from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/")
@login_required
def hub():
    is_admin = current_user.is_admin()
    has_hr = current_user.module_access("hr")
    has_inv = current_user.module_access("inventory")
    has_invoicing = current_user.module_access("invoicing")
    has_finance = current_user.module_access("finance")
    has_accounting = current_user.module_access("accounting")
    has_fbr = current_user.module_access("fbr")
    if not (has_hr or has_inv or has_invoicing or has_finance or has_accounting or has_fbr):
        return render_template("dashboard/access_denied.html")
    return render_template("dashboard/hub.html",
                           has_hr=has_hr, has_inv=has_inv,
                           has_invoicing=has_invoicing,
                           has_finance=has_finance, has_accounting=has_accounting,
                           has_fbr=has_fbr,
                           has_settings=is_admin)
