from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func
from ..extensions import db
from shared.tenancy import scoped_get_404, get_member
from shared.clock import local_today
from ..models.pf import ProvidentFundConfig, PFContribution, PFLedger, PFWithdrawalRequest, PFLoanRequest
from ..models.holiday import PFProfitDistribution, PFSettlement, ButtonPermission
from ..models.communication import Notification, NotificationRecipient

pf_bp = Blueprint("pf", __name__, url_prefix="/pf")


def _balance(user_id):
    """A member's fund balance: every PF ledger credit less every debit."""
    return float(db.session.query(
        func.coalesce(func.sum(PFLedger.credit), 0) - func.coalesce(func.sum(PFLedger.debit), 0))
        .filter(PFLedger.user_id == user_id).scalar() or 0)


def _reply(ok, msg, status=200):
    """JSON for scripts; flash + back to the PF page for the plain forms
    (they used to land on a bare JSON page)."""
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"success": True, "message": msg} if ok else {"error": msg}), status
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("pf.index"))


def _paid_from():
    """(account, date, None) for the bank/cash account and date a PF payment
    is made from, or (None, None, error reply)."""
    from shared.ledger_utils import cash_bank_account
    from shared.posting_helpers import parse_doc_date
    account = cash_bank_account(request.form.get("account_id", type=int))
    if account is None:
        return None, None, _reply(False, "Choose the bank or cash account the payment is made from.",
                                  400)
    return account, parse_doc_date(request.form.get("paid_on")).date(), None


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
        from shared.ledger_utils import cash_bank_accounts
        balances = [(get_member(uid), round(float(b or 0), 2)) for uid, b in db.session.query(
            PFLedger.user_id,
            func.coalesce(func.sum(PFLedger.credit), 0) - func.coalesce(func.sum(PFLedger.debit), 0))
            .group_by(PFLedger.user_id).all()]
        members = sorted([(m, b) for m, b in balances if m is not None and abs(b) > 0.004],
                         key=lambda x: x[0].full_name or "")
        return render_template("pf/index.html", config=config, contributions=contributions,
                               withdrawals=withdrawals, loans=loans, profits=profits,
                               settlements=settlements, perms=perms,
                               accounts=cash_bank_accounts(), today=local_today(),
                               members=members,
                               can_calc=_has_button_perm("BUTTON_CALCULATE_CONTRIBUTIONS"),
                               can_profit=_has_button_perm("BUTTON_CALCULATE_PROFIT"),
                               can_settle=_has_button_perm("BUTTON_PROCESS_SETTLEMENT"))
    my_contribs = PFContribution.query.filter_by(user_id=current_user.id).order_by(
        PFContribution.year.desc(), PFContribution.month.desc()).all()
    ledger = PFLedger.query.filter_by(user_id=current_user.id).order_by(PFLedger.transaction_date.desc()).limit(30).all()
    balance = _balance(current_user.id)
    employee_share = db.session.query(func.sum(PFContribution.employee_amount)).filter(
        PFContribution.user_id == current_user.id).scalar() or 0
    employer_share = db.session.query(func.sum(PFContribution.employer_amount)).filter(
        PFContribution.user_id == current_user.id).scalar() or 0
    my_withdrawals = PFWithdrawalRequest.query.filter_by(user_id=current_user.id).order_by(
        PFWithdrawalRequest.created_at.desc()).all()
    my_loans = PFLoanRequest.query.filter_by(user_id=current_user.id).order_by(
        PFLoanRequest.created_at.desc()).all()
    # This member's own profit credits — it used to be the whole fund's
    # distributed profit.
    profits_share = float(db.session.query(func.coalesce(func.sum(PFLedger.credit), 0)).filter(
        PFLedger.user_id == current_user.id, PFLedger.transaction_type == "profit").scalar() or 0)
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
        if request.form.get("contribution_base") in ("basic", "gross"):
            cfg.contribution_base = request.form.get("contribution_base")
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
    if not amount or amount <= 0 or not reason:
        flash("Amount and reason required.", "danger")
        return redirect(url_for("pf.index"))
    if amount > _balance(current_user.id):
        flash(f"You can withdraw at most Rs.{_balance(current_user.id):,.0f}.", "danger")
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
    if not amount or amount <= 0 or not purpose:
        flash("Amount and purpose required.", "danger")
        return redirect(url_for("pf.index"))
    if not installments or not (1 <= installments <= 60):
        flash("Instalments must be between 1 and 60 months.", "danger")
        return redirect(url_for("pf.index"))
    config = ProvidentFundConfig.query.first()
    balance = _balance(current_user.id)
    max_loan = balance * (config.max_loan_percentage / 100) if config else balance * 0.5
    if amount > max_loan:
        flash(f"Loan amount exceeds maximum permissible limit (Rs.{max_loan:,.0f}).", "danger")
        return redirect(url_for("pf.index"))
    service_months = 0
    if current_user.date_of_joining:
        today = local_today()
        service_months = (today.year - current_user.date_of_joining.year) * 12 + \
                         (today.month - current_user.date_of_joining.month)
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
    if (loan.user_id != current_user.id
            and not current_user.is_admin()
            and not current_user.is_manager()):
        return jsonify({"error": "Access denied"}), 403
    balance = _balance(loan.user_id)
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
        return _reply(False, "Access denied", 403)
    wr = scoped_get_404(PFWithdrawalRequest, wid)
    # Approving again debited the fund again.
    if wr.status != "pending":
        return _reply(False, "This withdrawal has already been decided.", 400)
    if request.form.get("action") == "reject":
        wr.status = "rejected"
        db.session.commit()
        return _reply(True, "Withdrawal rejected.")
    if float(wr.amount or 0) > _balance(wr.user_id) + 0.005:
        return _reply(False, "The member's PF balance is below this withdrawal.", 400)
    account, when, err = _paid_from()
    if err:
        return err
    wr.status = "approved"
    wr.approved_by = current_user.id
    wr.approved_at = datetime.utcnow()
    wr.disbursed_at = wr.approved_at
    db.session.add(PFLedger(user_id=wr.user_id, transaction_date=when,
                             transaction_type="withdrawal", description=f"PF withdrawal approved",
                             credit=0, debit=wr.amount, reference_id=wr.id))
    # The fund pays the member: PF Payable comes down with the member's
    # balance. It used to move only the members' ledger.
    from .. import payroll_posting as pp_
    pp_.pf_cash_out("PF-WD", wr.id, f"PF-WD-{wr.id}", wr.amount, account, when,
                    current_user.id, f"PF withdrawal - {wr.user.full_name}")
    db.session.commit()
    return _reply(True, "Withdrawal approved and paid.")


@pf_bp.route("/approve-loan/<int:lid>", methods=["POST"])
@login_required
def approve_loan(lid):
    if not current_user.is_admin():
        return _reply(False, "Access denied", 403)
    if not _has_button_perm("BUTTON_APPROVE_PF_LOAN"):
        return _reply(False, "No permission to approve loans", 403)
    loan = scoped_get_404(PFLoanRequest, lid)
    if loan.status != "pending":
        return _reply(False, "This loan has already been decided.", 400)
    if request.form.get("action") == "reject":
        loan.status = "rejected"
        db.session.commit()
        return _reply(True, "Loan rejected.")
    account, when, err = _paid_from()
    if err:
        return err
    loan.status = "approved"
    loan.approved_by = current_user.id
    loan.approved_at = datetime.utcnow()
    if not loan.remaining_amount:
        loan.remaining_amount = loan.amount
    # The loan is paid out of the member's fund: a DEBIT. It was credited,
    # so borrowing from the fund increased the member's balance. Payroll
    # recovers the instalments back into the fund.
    db.session.add(PFLedger(user_id=loan.user_id, transaction_date=when,
                             transaction_type="loan", description=f"PF loan approved",
                             credit=0, debit=loan.amount, reference_id=loan.id))
    from .. import payroll_posting as pp_
    pp_.pf_cash_out("PF-LOAN", loan.id, f"PF-LOAN-{loan.id}", loan.amount, account, when,
                    current_user.id, f"PF loan - {loan.user.full_name}")
    db.session.commit()
    return _reply(True, "Loan approved and paid.")


@pf_bp.route("/distribute-profit", methods=["POST"])
@login_required
def distribute_profit():
    if not current_user.is_admin():
        return _reply(False, "Access denied", 403)
    if not _has_button_perm("BUTTON_DISTRIBUTE_PROFIT"):
        return _reply(False, "No permission", 403)
    month = request.form.get("month", type=int)
    year = request.form.get("year", type=int)
    total_profit = request.form.get("total_profit", type=float)
    if not month or not year or not total_profit or total_profit <= 0:
        return _reply(False, "Month, year, and total profit required", 400)
    users = [(u.user_id, float(u.bal or 0)) for u in db.session.query(
        PFLedger.user_id,
        (func.coalesce(func.sum(PFLedger.credit), 0)
         - func.coalesce(func.sum(PFLedger.debit), 0)).label("bal"))
        .group_by(PFLedger.user_id).order_by(PFLedger.user_id).all()]
    users = [(uid, bal) for uid, bal in users if bal > 0]
    # Shares of the members with a positive balance only — the base used to
    # include negative balances, so the shares did not add up to the profit.
    base = sum(bal for _, bal in users)
    if base <= 0:
        return _reply(False, "No member has a positive PF balance.", 400)
    dist = PFProfitDistribution(month=month, year=year, total_profit=total_profit,
                                  distributed_by=current_user.id)
    db.session.add(dist)
    db.session.flush()
    from .. import payroll_posting as pp_
    pp_.pf_profit(dist, local_today(), current_user.id)
    given = 0.0
    for i, (uid, bal) in enumerate(users):
        share = (round(total_profit - given, 2) if i == len(users) - 1
                 else round(total_profit * bal / base, 2))
        given += share
        db.session.add(PFLedger(user_id=uid, transaction_date=local_today(),
                                 transaction_type="profit", description=f"Profit share {month}/{year}",
                                 credit=share, debit=0))
    db.session.commit()
    return _reply(True, f"Profit of Rs.{total_profit:,.0f} distributed.")


@pf_bp.route("/settle/<int:uid>", methods=["POST"])
@login_required
def settle(uid):
    if not current_user.is_admin():
        return _reply(False, "Access denied", 403)
    if not _has_button_perm("BUTTON_PROCESS_SETTLEMENT"):
        return _reply(False, "No permission", 403)
    if get_member(uid) is None:
        return _reply(False, "Not a member of this company.", 404)
    if PFSettlement.query.filter_by(user_id=uid, status="completed").first():
        return _reply(False, "This member's PF has already been settled.", 400)
    employee_contrib = float(db.session.query(func.coalesce(func.sum(PFContribution.employee_amount), 0))
                             .filter(PFContribution.user_id == uid).scalar() or 0)
    employer_contrib = float(db.session.query(func.coalesce(func.sum(PFContribution.employer_amount), 0))
                             .filter(PFContribution.user_id == uid).scalar() or 0)
    profit_share = float(db.session.query(func.coalesce(func.sum(PFLedger.credit), 0)).filter(
        PFLedger.user_id == uid, PFLedger.transaction_type == "profit").scalar() or 0)
    outstanding_loan = float(db.session.query(func.coalesce(func.sum(PFLoanRequest.remaining_amount), 0))
                             .filter(PFLoanRequest.user_id == uid,
                                     PFLoanRequest.status == "approved").scalar() or 0)
    # What is paid out is the ledger balance: contributions and profit in,
    # withdrawals and loans (debited when paid out) already taken off. It
    # used to be contributions - loan, ignoring withdrawals and profit, so
    # the ledger did not close at zero.
    net = round(_balance(uid), 2)
    if net < 0:
        return _reply(False, f"The member owes the fund Rs.{-net:,.0f}; recover it before settling.",
                      400)
    account, when, err = (None, local_today(), None) if not net else _paid_from()
    if err:
        return err
    settlement = PFSettlement(
        user_id=uid, total_employee_contrib=employee_contrib, total_employer_contrib=employer_contrib,
        total_profit_distributed=profit_share, outstanding_loan=outstanding_loan,
        net_settlement=net, settled_by=current_user.id, settled_at=datetime.utcnow(), status="completed"
    )
    db.session.add(settlement)
    db.session.flush()
    # Any PF loan still open was already netted off the balance paid out.
    for pfl in PFLoanRequest.query.filter_by(user_id=uid, status="approved").all():
        pfl.remaining_amount = 0
        pfl.status = "settled"
    if net:
        db.session.add(PFLedger(user_id=uid, transaction_date=when,
                                 transaction_type="settlement", description=f"Final PF settlement",
                                 credit=0, debit=net, reference_id=settlement.id))
        from .. import payroll_posting as pp_
        member = get_member(uid)
        pp_.pf_cash_out("PF-SETTLE", settlement.id, f"PF-SETTLE-{settlement.id}", net, account,
                        when, current_user.id,
                        f"Final PF settlement - {member.full_name if member else uid}")
    db.session.commit()
    return _reply(True, f"Settlement paid: Rs.{net:,.0f}.")


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
    if not user_id or ttype not in ("credit", "debit") or not amount or amount <= 0:
        return jsonify({"error": "All fields required"}), 400
    if get_member(user_id) is None:
        return jsonify({"error": "Not a member of this company"}), 404
    # A manual fund entry (e.g. an opening balance) needs the other side of
    # the journal, or PF Payable stops agreeing with the members' ledger.
    from shared.models.ledger import ChartOfAccount
    from shared.tenancy import scoped_get
    counter = scoped_get(ChartOfAccount, request.form.get("counter_account_id", type=int) or 0)
    if counter is None or (counter.level or 0) < ChartOfAccount.POSTING_LEVEL:
        return jsonify({"error": "Choose the posting account on the other side"}), 400
    entry = PFLedger(user_id=user_id, transaction_date=local_today(),
                      transaction_type=ttype, description=description,
                      credit=amount if ttype == "credit" else 0,
                      debit=amount if ttype == "debit" else 0)
    db.session.add(entry)
    db.session.flush()
    from shared.ledger_utils import post_journal_entry
    from .. import payroll_posting as pp_
    fund = pp_.pf_payable()
    dr, cr = (counter, fund) if ttype == "credit" else (fund, counter)
    post_journal_entry(voucher_type="PF-ADJ", voucher_id=entry.id,
                       voucher_number=f"PF-ADJ-{entry.id}",
                       description=description or "PF ledger adjustment",
                       entry_date=entry.transaction_date, created_by=current_user.id,
                       lines=[{"account_id": dr.id, "debit": amount, "credit": 0,
                               "description": description or "PF adjustment"},
                              {"account_id": cr.id, "debit": 0, "credit": amount,
                               "description": description or "PF adjustment"}])
    db.session.commit()
    return jsonify({"success": True})
