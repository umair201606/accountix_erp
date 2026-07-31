from datetime import date, timedelta
from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from shared.extensions import db


def register_hr_blueprints(app):
    from .routes.auth import auth_bp
    from .routes.attendance import attendance_bp
    from .routes.leave import leave_bp
    from .routes.ess import ess_bp
    from .routes.reports import reports_bp
    from .routes.mss import mss_bp
    from .routes.workplace import workplace_bp
    from .routes.timesheet import timesheet_bp
    from .routes.digital_file import df_bp
    from .routes.compensation import comp_bp
    from .routes.communication import comm_bp
    from .routes.pf import pf_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(leave_bp)
    app.register_blueprint(ess_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(mss_bp)
    app.register_blueprint(workplace_bp)
    app.register_blueprint(timesheet_bp)
    app.register_blueprint(df_bp)
    app.register_blueprint(comp_bp)
    app.register_blueprint(comm_bp)
    app.register_blueprint(pf_bp)

    @app.route("/dashboard")
    @login_required
    def dashboard():
        chart_data = []
        if current_user.is_admin() or current_user.is_manager():
            from .models.attendance import Attendance
            from shared.models.base import User
            from sqlalchemy import func, extract, case
            import calendar
            if db.engine.name == "sqlite":
                weekday_filter = func.strftime("%w", Attendance.date).in_(["1", "2", "3", "4", "5"])
            else:
                weekday_filter = extract("dow", Attendance.date).in_([1, 2, 3, 4, 5])
            yearly = db.session.query(
                extract("week", Attendance.date).label("week"),
                extract("year", Attendance.date).label("year"),
                func.count(Attendance.id).label("total"),
                func.sum(case((Attendance.is_late == True, 1), else_=0)).label("late"),
                func.sum(case((Attendance.is_half_day == True, 1), else_=0)).label("half"),
            ).filter(
                extract("year", Attendance.date) == date.today().year,
                weekday_filter
            ).group_by("year", "week").order_by("year", "week").all()
            emp_count = User.query.filter_by(is_active=True).count()
            chart_data = []
            for r in yearly:
                wk = int(r.week)
                yr = int(r.year)
                jan1 = date(yr, 1, 1)
                first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
                monday = first_monday + timedelta(weeks=wk - 1)
                weekdays = sum(1 for d in range(7) if (monday + timedelta(days=d)).weekday() < 5)
                possible = emp_count * weekdays
                pct = round((int(r.total) / possible) * 100, 1) if possible else 0
                chart_data.append({
                    "week": wk, "total": int(r.total), "late": int(r.late), "half": int(r.half),
                    "pct": pct, "label": f"{monday:%b %d}"
                })
        return render_template("dashboard/index.html", chart_data=chart_data)

    @app.context_processor
    def inject_notifications():
        ctx = {}
        if current_user.is_authenticated:
            from shared.tenancy import current_company_id
            # NotificationRecipient is tenant-scoped; a user with no active
            # company (no memberships) would otherwise trip the fail-closed
            # tenancy hook and 500 every page render.
            if current_company_id() is not None:
                from .models.communication import NotificationRecipient
                unread_count = NotificationRecipient.query.filter_by(
                    user_id=current_user.id, is_read=False
                ).count()
                recent_notifs = NotificationRecipient.query.filter_by(
                    user_id=current_user.id, is_read=False
                ).order_by(NotificationRecipient.id.desc()).limit(5).all()
                ctx.update({"unread_count": unread_count, "recent_notifications": recent_notifs})
        return ctx

    @app.context_processor
    def inject_back_urls():
        ep = request.endpoint or ""
        back_map = {
            "attendance.overview": "attendance.index",
            "leave.apply": "leave.index",
            "leave.calendar": "leave.index",
            "leave.holidays": "leave.index",
            "leave.workflows": "leave.index",
            "timesheet.projects": "timesheet.index",
            "timesheet.merge_report": "timesheet.index",
            "ess.loans": "ess.index",
            "ess.slips": "ess.index",
            "ess.performance": "ess.index",
            "ess.change_requests": "ess.index",
            "digital_files.admin": "digital_files.index",
            "digital_files.profile": "digital_files.index",
            "compensation.revisions": "compensation.index",
            "compensation.view_slip": "compensation.index",
            "pf.config": "pf.index",
            "pf.button_permissions": "pf.index",
            "pf.request_withdrawal": "pf.index",
            "pf.request_loan": "pf.index",
            "mss.approvals": "mss.index",
            "mss.team": "mss.index",
            "mss.team_calendar": "mss.index",
            "mss.evaluate": "mss.team",
            "attendance.admin_view": "attendance.index",
            "attendance.policies": "attendance.index",
            "workplace.announcements": "workplace.index",
            "workplace.events": "workplace.index",
            "workplace.kanban": "workplace.index",
            "auth.change_password": "dashboard",
            "auth.user_add": "auth.user_list",
            "auth.user_edit": "auth.user_list",
        }
        if ep in back_map:
            try:
                return {"back_url": url_for(back_map[ep])}
            except Exception:
                pass
        return {}

    import traceback
    @app.errorhandler(500)
    def handle_500(e):
        tb = traceback.format_exc()
        return f"<pre style='background:#fef2f2;padding:20px;border:2px solid #ef4444;border-radius:8px;font-size:13px;overflow:auto;max-height:90vh;'>{tb}</pre>", 500

    @app.errorhandler(Exception)
    def handle_all(e):
        tb = traceback.format_exc()
        return f"<pre style='background:#fef2f2;padding:20px;border:2px solid #ef4444;border-radius:8px;font-size:13px;overflow:auto;max-height:90vh;'>{tb}</pre>", 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 0))
    debug = os.environ.get("DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
