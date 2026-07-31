from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func
from ..extensions import db
from shared.tenancy import scoped_get_404
from ..models.pf import ProvidentFundConfig, PFContribution, PFLedger, PFWithdrawalRequest, PFLoanRequest
from ..models.holiday import PFProfitDistribution, PFSettlement, ButtonPermission
from ..models.communication import Notification, NotificationRecipient

pf_bp = Blueprint("pf", __name__, url_prefix="/pf")


def _has_button_perm(button_code):
    if current_user.is_admin():
        return True
    bp = ButtonPermission.query.filter_by(role_id=current_user.role_id, button_code=button_code, is_granted=True).first()
    return bp is not None


@pf_bp.route("/")
@login_required
def index():
    config = ProvidentFundConfig.query.first()
    if current_user.is_admin():
        contributions = PFContribution.query.order_by(PFContribution.year.desc(), PFContribution.month.desc()).limit(50).all()
        withdrawals = PFWithdrawalRequest.query.order_by(PFWithdrawalRequest.created_at.desc()).limit(20).all()
        loans = PFLoanRequest.query.order_by(PFLoanRequest.created_at.desc()).limit(20).all()
        profits = PFProfitDistribution.query.order_by(PFProfitDistribution.distributed_at.desc()).limit(12).all()
        settlements = PFSettlement.query.order_by(PFSettlement.created_at.desc()).limit(10).all()
        perms = ButtonPermission.query.all()
        return render_template("pf/index.html", config=config, contributions=contributions,
                               withdrawals=withdrawals, loans=loans, profits=profits,
                               settlements=settlements, perms=perms,
                               can_calc=_has_button_perm("BUTTON_CALCULATE_CONTRIBUTIONS"),
                               can_profit=_has_button_perm("BUTTON_CALCULATE_PROFIT"),
                               can_settle=_has_button_perm("BUTTON_PROCESS_SETTLEMENT"))
    my_contribs = PFContribution.query.filter_by(user_id=current_user.id).order_by(
        PFContribution.year.desc(), PFContribution.month.desc()).all()
    ledger = PFLedger.query.filter_by(user_id=current_user.id).order_by(PFLedger.transaction_date.desc()).limit(30).all()
    balance = db.session.query(func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).filter(
        PFLedger.user_id == current_user.id).scalar() or 0
    employee_share = db.session.query(func.sum(PFContribution.employee_amount)).filter(
        PFContribution.user_id == current_user.id).scalar() or 0
    employer_share = db.session.query(func.sum(PFContribution.employer_amount)).filter(
        PFContribution.user_id == current_user.id).scalar() or 0
    my_withdrawals = PFWithdrawalRequest.query.filter_by(user_id=current_user.id).order_by(
        PFWithdrawalRequest.created_at.desc()).all()
    my_loans = PFLoanRequest.query.filter_by(user_id=current_user.id).order_by(
        PFLoanRequest.created_at.desc()).all()
    profits_share = db.session.query(func.sum(PFProfitDistribution.total_profit)).filter(
        # approximate - in production use a proper distribution ledger
    ).scalar() or 0
    return render_template("pf/employee.html", config=config, contributions=my_contribs,
                           ledger=ledger, balance=balance, employee_share=employee_share,
                           employer_share=employer_share, withdrawals=my_withdrawals, loans=my_loans,
                           profits_share=profits_share)


@pf_bp.route("/config", methods=["GET", "POST"])
@login_required
def config():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    cfg = ProvidentFundConfig.query.first()
    if not cfg:
        cfg = ProvidentFundConfig()
        db.session.add(cfg)
    if request.method == "POST":
        cfg.employee_contribution_pct = request.form.get("employee_pct", 5.0, type=float)
        cfg.employer_contribution_pct = request.form.get("employer_pct", 5.0, type=float)
        cfg.max_loan_percentage = request.form.get("max_loan_pct", 50.0, type=float)
        cfg.interest_rate = request.form.get("interest_rate", 0.0, type=float)
        cfg.min_service_months_for_loan = request.form.get("min_months", 12, type=int)
        db.session.commit()
        flash("PF configuration updated.", "success")
        return redirect(url_for("pf.index"))
    return render_template("pf/config.html", config=cfg)


@pf_bp.route("/request-withdrawal", methods=["POST"])
@login_required
def request_withdrawal():
    amount = request.form.get("amount", type=float)
    reason = request.form.get("reason", "").strip()
    if not amount or not reason:
        flash("Amount and reason required.", "danger")
        return redirect(url_for("pf.index"))
    wr = PFWithdrawalRequest(user_id=current_user.id, amount=amount, reason=reason)
    db.session.add(wr)
    db.session.commit()
    flash("Withdrawal request submitted.", "success")
    return redirect(url_for("pf.index"))


@pf_bp.route("/request-loan", methods=["POST"])
@login_required
def request_loan():
    amount = request.form.get("amount", type=float)
    installments = request.form.get("installments", 12, type=int)
    purpose = request.form.get("purpose", "").strip()
    if not amount or not purpose:
        flash("Amount and purpose required.", "danger")
        return redirect(url_for("pf.index"))
    config = ProvidentFundConfig.query.first()
    balance = db.session.query(func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).filter(
        PFLedger.user_id == current_user.id).scalar() or 0
    max_loan = balance * (config.max_loan_percentage / 100) if config else balance * 0.5
    if amount > max_loan:
        flash(f"Loan amount exceeds maximum permissible limit (Rs.{max_loan:,.0f}).", "danger")
        return redirect(url_for("pf.index"))
    service_months = 0
    if current_user.date_of_joining:
        service_months = (date.today().year - current_user.date_of_joining.year) * 12 + \
                         (date.today().month - current_user.date_of_joining.month)
    if config and service_months < config.min_service_months_for_loan:
        flash(f"Minimum {config.min_service_months_for_loan} months of service required for loan.", "danger")
        return redirect(url_for("pf.index"))
    monthly = round(amount / installments, 2)
    loan = PFLoanRequest(user_id=current_user.id, amount=amount, installment_months=installments,
                          purpose=purpose, monthly_installment=monthly, remaining_amount=amount)
    db.session.add(loan)
    db.session.commit()
    flash("Loan request submitted.", "success")
    return redirect(url_for("pf.index"))


@pf_bp.route("/compliance-check/<int:lid>")
@login_required
def compliance_check(lid):
    loan = scoped_get_404(PFLoanRequest, lid)
    balance = db.session.query(func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).filter(
        PFLedger.user_id == loan.user_id).scalar() or 0
    config = ProvidentFundConfig.query.first()
    max_loan = balance * (config.max_loan_percentage / 100) if config else balance * 0.5
    issues = []
    if loan.amount > max_loan:
        issues.append(f"Amount exceeds max permissible ({max_loan:,.0f})")
    if loan.installment_months > 60:
        issues.append("Installment period exceeds 60 months")
    return jsonify({"compliant": len(issues) == 0, "issues": issues})


@pf_bp.route("/approve-withdrawal/<int:wid>", methods=["POST"])
@login_required
def approve_withdrawal(wid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    wr = scoped_get_404(PFWithdrawalRequest, wid)
    wr.status = "approved"
    wr.approved_by = current_user.id
    wr.approved_at = datetime.utcnow()
    db.session.add(PFLedger(user_id=wr.user_id, transaction_date=date.today(),
                             transaction_type="withdrawal", description=f"PF withdrawal approved",
                             credit=0, debit=wr.amount))
    db.session.commit()
    return jsonify({"success": True})


@pf_bp.route("/approve-loan/<int:lid>", methods=["POST"])
@login_required
def approve_loan(lid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    if not _has_button_perm("BUTTON_APPROVE_PF_LOAN"):
        return jsonify({"error": "No permission to approve loans"}), 403
    loan = scoped_get_404(PFLoanRequest, lid)
    loan.status = "approved"
    loan.approved_by = current_user.id
    loan.approved_at = datetime.utcnow()
    db.session.add(PFLedger(user_id=loan.user_id, transaction_date=date.today(),
                             transaction_type="loan", description=f"PF loan approved",
                             credit=loan.amount, debit=0))
    db.session.commit()
    return jsonify({"success": True})


@pf_bp.route("/distribute-profit", methods=["POST"])
@login_required
def distribute_profit():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    if not _has_button_perm("BUTTON_DISTRIBUTE_PROFIT"):
        return jsonify({"error": "No permission"}), 403
    month = request.form.get("month", type=int)
    year = request.form.get("year", type=int)
    total_profit = request.form.get("total_profit", type=float)
    if not month or not year or not total_profit:
        return jsonify({"error": "Month, year, and total profit required"}), 400
    dist = PFProfitDistribution(month=month, year=year, total_profit=total_profit,
                                  distributed_by=current_user.id)
    db.session.add(dist)
    total_balance = db.session.query(func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).scalar() or 1
    users = db.session.query(PFLedger.user_id,
                              (func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).label("bal")).group_by(PFLedger.user_id).all()
    for u in users:
        if u.bal > 0:
            share = round(total_profit * (u.bal / total_balance), 2)
            db.session.add(PFLedger(user_id=u.user_id, transaction_date=date.today(),
                                     transaction_type="profit", description=f"Profit share {month}/{year}",
                                     credit=share, debit=0))
    db.session.commit()
    flash(f"Profit of Rs.{total_profit:,.0f} distributed.", "success")
    return redirect(url_for("pf.index"))


@pf_bp.route("/settle/<int:uid>", methods=["POST"])
@login_required
def settle(uid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    if not _has_button_perm("BUTTON_PROCESS_SETTLEMENT"):
        return jsonify({"error": "No permission"}), 403
    employee_contrib = db.session.query(func.sum(PFContribution.employee_amount)).filter(
        PFContribution.user_id == uid).scalar() or 0
    employer_contrib = db.session.query(func.sum(PFContribution.employer_amount)).filter(
        PFContribution.user_id == uid).scalar() or 0
    profit_share = 0
    outstanding_loan = db.session.query(func.sum(PFLoanRequest.remaining_amount)).filter(
        PFLoanRequest.user_id == uid, PFLoanRequest.status == "approved").scalar() or 0
    net = employee_contrib + employer_contrib + profit_share - outstanding_loan
    settlement = PFSettlement(
        user_id=uid, total_employee_contrib=employee_contrib, total_employer_contrib=employer_contrib,
        total_profit_distributed=profit_share, outstanding_loan=outstanding_loan,
        net_settlement=net, settled_by=current_user.id, settled_at=datetime.utcnow(), status="completed"
    )
    db.session.add(settlement)
    db.session.add(PFLedger(user_id=uid, transaction_date=date.today(),
                             transaction_type="settlement", description=f"Final PF settlement",
                             credit=0, debit=net))
    db.session.commit()
    flash(f"Settlement calculated: Rs.{net:,.0f}.", "success")
    return redirect(url_for("pf.index"))


@pf_bp.route("/button-permissions", methods=["POST"])
@login_required
def button_permissions():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    role_id = request.form.get("role_id", type=int)
    button_code = request.form.get("button_code")
    granted = request.form.get("granted") == "true"
    bp = ButtonPermission.query.filter_by(role_id=role_id, button_code=button_code).first()
    if not bp:
        bp = ButtonPermission(role_id=role_id, button_code=button_code, module="pf",
                               description=f"Permission for {button_code}")
        db.session.add(bp)
    bp.is_granted = granted
    db.session.commit()
    return jsonify({"success": True})


@pf_bp.route("/add-ledger-entry", methods=["POST"])
@login_required
def add_ledger_entry():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    user_id = request.form.get("user_id", type=int)
    ttype = request.form.get("type")
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", type=float)
    if not user_id or not ttype or not amount:
        return jsonify({"error": "All fields required"}), 400
    entry = PFLedger(user_id=user_id, transaction_date=date.today(),
                      transaction_type=ttype, description=description,
                      credit=amount if ttype == "credit" else 0,
                      debit=amount if ttype == "debit" else 0)
    db.session.add(entry)
    db.session.commit()
    return jsonify({"success": True})
