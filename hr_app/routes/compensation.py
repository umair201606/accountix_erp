import json
import io
import os
import csv
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, send_file, abort
from flask_login import login_required, current_user
from sqlalchemy import extract, func
from ..extensions import db
from shared.tenancy import scoped_get_404, get_member, current_company_id
from ..models.compensation import PayrollProfile, PayrollComponent, PayrollRun, PayrollSlip, SalaryRevision
from ..models.user import User
from ..models.attendance import Attendance
from ..models.holiday import OvertimeAccount, PayrollAuditLog
from ..models.pf import ProvidentFundConfig, PFContribution, PFLedger
from ..models.communication import Notification, NotificationRecipient
from ..models.tax import IncomeTaxSlab
from ..models.loan import LoanAdvanceRequest, LoanRepayment
from shared.formatting import format_amount as _m
from shared.forms import REQUIRED, FormInputError, form_date, form_float, form_int
from .. import payroll_engine as pe

comp_bp = Blueprint("compensation", __name__, url_prefix="/compensation")


def _member_user_by_code(code):
    """Resolve an employee code/email to a user IN the active company.

    Employee codes are per-company (EMP100 can exist in two companies) and
    the users table is global, so a bare ``User.filter_by(employee_code)``
    can resolve to another tenant's person — and apply their payroll
    adjustments to the wrong employee. Membership first, always.
    """
    from shared.models.company import CompanyMembership
    cid = current_company_id()
    if not code or cid is None:
        return None
    m = CompanyMembership.query.filter_by(
        company_id=cid, employee_code=code,
        status=CompanyMembership.ACTIVE).first()
    if m is not None:
        return User.query.get(m.user_id)
    user = User.query.filter_by(email=code).first()
    if user is None or not user.is_active:
        return None
    if not CompanyMembership.query.filter_by(
            company_id=cid, user_id=user.id,
            status=CompanyMembership.ACTIVE).first():
        return None
    return user


@comp_bp.route("/")
@login_required
def index():
    if current_user.is_admin():
        profiles = PayrollProfile.query.all()
        runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).limit(12).all()
        slabs = IncomeTaxSlab.query.filter_by(is_active=True).order_by(IncomeTaxSlab.min_income).all()
        return render_template("compensation/index.html", profiles=profiles, runs=runs, slabs=slabs)
    profile = PayrollProfile.query.filter_by(user_id=current_user.id).first()
    slips = PayrollSlip.query.filter_by(user_id=current_user.id).order_by(PayrollSlip.created_at.desc()).all()
    return render_template("compensation/employee.html", profile=profile, slips=slips)


# ── Income Tax Slabs ──

@comp_bp.route("/tax-settings", methods=["GET", "POST"])
@login_required
def tax_settings():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            slab = IncomeTaxSlab(
                min_income=form_float("min_income", REQUIRED),
                max_income=form_float("max_income", REQUIRED),
                rate_pct=form_float("rate_pct", REQUIRED),
                fixed_amount=form_float("fixed_amount", 0),
            )
            if (slab.min_income < 0 or slab.max_income <= slab.min_income
                    or not 0 <= slab.rate_pct <= 100 or slab.fixed_amount < 0):
                raise FormInputError("A slab needs 0 <= from < to, a rate between 0 and "
                                     "100% and a fixed amount of zero or more.")
            db.session.add(slab)
            flash("Tax slab added.", "success")
        elif action == "delete":
            slab = scoped_get_404(IncomeTaxSlab, form_int("slab_id", REQUIRED))
            db.session.delete(slab)
            flash("Tax slab deleted.", "success")
        elif action == "seed_defaults":
            # Salaried slabs for tax year 2026 (Finance Act 2025). Replaces the
            # active set: mixing in a previous year's rows (the old seed was
            # tax year 2025 and skipped any bracket that already existed)
            # gave a table no year ever had.
            from ..payroll_engine import SLABS_TY2026
            IncomeTaxSlab.query.filter_by(is_active=True).update({"is_active": False})
            for lo, hi, rate, fixed in SLABS_TY2026:
                db.session.add(IncomeTaxSlab(min_income=lo, max_income=hi,
                                             rate_pct=rate, fixed_amount=fixed))
            flash("Tax year 2026 salaried slabs loaded (previous slabs deactivated).", "success")
        elif action == "policy":
            from ..models.tax import PayrollSettings
            s = PayrollSettings.get()
            mult = form_float("overtime_multiplier", 1.5, label="Overtime rate")
            hours = form_float("overtime_hours_basis", 160, label="Hours a month")
            base = form_float("eobi_wage_base", 0, label="EOBI wage base")
            ee = form_float("eobi_employee_pct", 0, label="EOBI employee %")
            er = form_float("eobi_employer_pct", 0, label="EOBI employer %")
            thr = form_float("surcharge_threshold", 0, label="Surcharge threshold")
            sur = form_float("surcharge_pct", 0, label="Surcharge %")
            if (not 1 <= mult <= 5 or not 1 <= hours <= 400 or base < 0
                    or not 0 <= ee <= 100 or not 0 <= er <= 100 or thr < 0
                    or not 0 <= sur <= 100):
                raise FormInputError("Overtime rate 1–5x, hours 1–400, percentages 0–100 "
                                     "and amounts of zero or more.")
            s.overtime_multiplier, s.overtime_hours_basis = mult, hours
            s.eobi_enabled = request.form.get("eobi_enabled") == "1"
            s.eobi_wage_base, s.eobi_employee_pct, s.eobi_employer_pct = base, ee, er
            s.surcharge_threshold, s.surcharge_pct = thr, sur
            days = form_float("gratuity_days_per_year", 30, label="Gratuity days a year")
            if not 0 <= days <= 365:
                raise FormInputError("Gratuity days a year must be between 0 and 365.")
            s.gratuity_enabled = request.form.get("gratuity_enabled") == "1"
            s.gratuity_basis = "gross" if request.form.get("gratuity_basis") == "gross" else "basic"
            s.gratuity_days_per_year = days
            s.leave_encashment_enabled = request.form.get("leave_encashment_enabled") == "1"
            flash("Payroll settings saved.", "success")
        db.session.commit()
        return redirect(url_for("compensation.tax_settings"))
    # Active slabs only: loading a new tax year deactivates the old rows.
    from ..models.tax import PayrollSettings
    slabs = IncomeTaxSlab.query.filter_by(is_active=True).order_by(IncomeTaxSlab.min_income).all()
    policy = PayrollSettings.get()
    db.session.commit()
    return render_template("compensation/tax_settings.html", slabs=slabs, policy=policy)


# ── Create Payroll Profile ──

@comp_bp.route("/create-profile", methods=["GET", "POST"])
@login_required
def create_profile():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("compensation.index"))
    employees = User.employees().filter(User.is_active.is_(True)).all()
    if request.method == "POST":
        uid = request.form.get("user_id", type=int)
        basic = request.form.get("basic_salary", type=float)
        effective = request.form.get("effective_from")
        if not uid or not basic or not effective:
            flash("All fields required.", "danger")
            return redirect(url_for("compensation.create_profile"))
        if basic <= 0 or get_member(uid) is None:
            flash("Pick an employee of this company and a basic salary above zero.", "danger")
            return redirect(url_for("compensation.create_profile"))
        if PayrollProfile.query.filter_by(user_id=uid).first():
            flash("Profile already exists for this employee.", "warning")
            return redirect(url_for("compensation.edit_profile", uid=uid))
        try:
            eff_date = datetime.strptime(effective, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            flash("Invalid date format.", "danger")
            return redirect(url_for("compensation.create_profile"))
        profile = PayrollProfile(user_id=uid, basic_salary=basic, effective_from=eff_date)
        db.session.add(profile)
        db.session.flush()
        # Basic is the profile's own figure and gross = basic + allowances, so
        # a "Basic Salary" allowance (it used to be created here at 100% of
        # basic) paid the basic twice.
        # Starter allowances as a percentage of basic, so they follow every
        # revision (they were stored as fixed amounts and went stale).
        for cname, pct, taxable in (("House Rent", 25, True), ("Medical Allowance", 10, False),
                                    ("Conveyance", 10, True)):
            db.session.add(PayrollComponent(profile_id=profile.id, name=cname, type="allowance",
                                            calculation_method="percent_of_basic", value=pct,
                                            is_taxable=taxable))
        db.session.commit()
        flash(f"Profile created for {get_member(uid).full_name}.", "success")
        return redirect(url_for("compensation.edit_profile", uid=uid))
    return render_template("compensation/create_profile.html", employees=employees)


# ── Employee Salary Package Setup ──

@comp_bp.route("/edit-profile/<int:uid>", methods=["GET", "POST"])
@login_required
def edit_profile(uid):
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    profile = PayrollProfile.query.filter_by(user_id=uid).first()
    emp = get_member(uid) or abort(404)
    if request.method == "POST":
        if not profile:
            from shared.clock import local_today
            profile = PayrollProfile(user_id=uid, basic_salary=0, effective_from=local_today())
            db.session.add(profile)
            db.session.flush()
        basic = form_float("basic_salary", REQUIRED, label="Basic salary")
        if basic <= 0:
            raise FormInputError("Basic salary must be more than zero.")
        try:
            submitted = json.loads(request.form.get("components_json") or "[]")
        except ValueError:
            raise FormInputError("The salary components could not be read; try again.")
        clean = []
        for comp in submitted if isinstance(submitted, list) else []:
            name = str(comp.get("name") or "").strip()
            if not name:
                continue
            ctype = comp.get("type") if comp.get("type") in ("allowance", "deduction") else None
            method = comp.get("method") or "fixed"
            if ctype is None or method not in ("fixed", "percent_of_basic"):
                raise FormInputError(f"{name}: choose allowance/deduction and fixed/% of basic.")
            try:
                value = float(comp.get("value") or 0)
            except (TypeError, ValueError):
                raise FormInputError(f"{name}: the amount must be a number.")
            if value < 0 or (method == "percent_of_basic" and value > 100):
                raise FormInputError(f"{name}: amount must be zero or more (a percentage at most 100).")
            clean.append((name, ctype, method, value, bool(comp.get("is_taxable", True))))
        effective = form_date("effective_from")
        old_basic = float(profile.basic_salary or 0)
        if old_basic and round(basic, 2) != round(old_basic, 2):
            # A raise is a dated revision: payroll pays each month at the rate
            # in force then. With the date left unchanged it takes effect
            # today, not retroactively from the package's original date.
            from shared.clock import local_today
            when = effective if effective != profile.effective_from else local_today()
            db.session.add(SalaryRevision(user_id=uid, previous_basic=old_basic, new_basic=basic,
                                          reason="Salary package edited",
                                          approved_by=current_user.id, effective_from=when))
            effective = when
        profile.basic_salary = basic
        profile.effective_from = effective
        for old in profile.components.all():
            db.session.delete(old)
        for name, ctype, method, value, taxable in clean:
            db.session.add(PayrollComponent(
                profile_id=profile.id, name=name, type=ctype,
                calculation_method=method, value=value,
                is_taxable=taxable if ctype == "allowance" else False,
            ))
        db.session.commit()
        flash(f"Salary package updated for {emp.full_name}.", "success")
        return redirect(url_for("compensation.index"))
    return render_template("compensation/edit_profile.html", profile=profile, employee=emp)


# ── Payroll Run with Preview, Replace, Template Upload ──

def _num(v, default=0.0):
    """An adjustment cell as a number (bulk sheets send text, blanks, commas)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return default


def _preview_employee(pp, month, year, pf_config, adjustments=None):
    """One employee's pay for the month. Used by the preview, the AJAX
    recalculation and the run itself, so what is shown is what is posted.

    The arithmetic that has to be right lives in ``hr_app.payroll_engine``
    (calendar-day proration incl. unpaid leave, percentage components,
    taxable income, cumulative withholding)."""
    from .. import payroll_engine as pe
    adj = adjustments.get(str(pp.user_id), {}) if adjustments else {}
    paid = pe.paid_ratio(pp.user, month, year)
    if paid is None:
        return None
    ratio, days_in_month, unpaid_days = paid
    # The basic in force in THIS month (salary revisions are dated).
    full_basic = pe.basic_for_month(pp, month, year)
    basic = round(full_basic * ratio, 2)

    allowances = 0.0
    full_allowances = 0.0
    taxable_regular = basic
    full_taxable = full_basic
    comps = {"allowances": {}, "deductions": {}}
    component_deductions = 0.0
    allowances_override = adj.get("allowances_override")
    for c in pp.components:
        full = pe.component_amount(c, full_basic)
        val = round(full * ratio, 2)
        if c.type == "allowance":
            if allowances_override in (None, ""):
                allowances += val
                full_allowances += full
                comps["allowances"][c.name] = val
                if c.is_taxable:
                    taxable_regular += val
                    full_taxable += full
        else:
            # Statutory / fixed deductions (EOBI, welfare fund) are not
            # prorated by unpaid leave: they are a flat monthly amount.
            component_deductions += full
            comps["deductions"][c.name] = full
    if allowances_override not in (None, ""):
        allowances = _num(allowances_override)
        taxable_regular = basic + allowances
        full_taxable = full_basic + allowances
        comps["allowances"]["Allowances (as entered)"] = round(allowances, 2)

    ot_hours = db.session.query(func.sum(OvertimeAccount.overtime_hours)).filter(
        OvertimeAccount.user_id == pp.user_id,
        extract("month", OvertimeAccount.date) == month,
        extract("year", OvertimeAccount.date) == year
    ).scalar() or 0
    ot_pay = pe.overtime_pay(ot_hours, full_basic)
    bonus = _num(adj.get("bonus"))
    if ot_pay:
        comps["allowances"]["Overtime"] = ot_pay
    if bonus:
        comps["allowances"]["Bonus"] = round(bonus, 2)
    # Final settlement in the leaving month (company policy; off by default).
    settlement = pe.final_settlement(pp.user, month, year, full_basic,
                                     full_basic + full_allowances)
    for name, amt in settlement.items():
        comps["allowances"][name] = amt
    one_off = ot_pay + bonus + sum(settlement.values())
    allowances += one_off
    gross = round(basic + allowances, 2)
    taxable = round(taxable_regular + one_off, 2)

    tax_override = adj.get("income_tax")
    if tax_override not in (None, ""):
        monthly_tax = round(_num(tax_override), 2)
    else:
        monthly_tax = pe.withholding(pp.user_id, month, year, taxable_regular,
                                     one_off, full_month_taxable=full_taxable)

    pf_employee = pf_employer = 0.0
    if pf_config and pf_config.is_active is not False:
        base = pe.pf_base(pf_config, basic, gross)
        pf_employee = round(base * float(pf_config.employee_contribution_pct or 0) / 100, 2)
        pf_employer = round(base * float(pf_config.employer_contribution_pct or 0) / 100, 2)
    eobi_employee, eobi_employer = pe.eobi(ratio)
    custom_ded = round(_num(adj.get("custom_deduction")), 2)

    # Loans are recovered from what is left after everything else, never
    # beyond it: a big instalment used to drive net pay below zero.
    available = (gross - monthly_tax - pf_employee - eobi_employee
                 - component_deductions - custom_ded)
    loans = []
    loan_deduction = 0.0
    # Only loans actually paid out are recovered: approval alone used to start
    # the deductions on money the employee never received.
    active_loans = LoanAdvanceRequest.query.filter_by(user_id=pp.user_id, status="approved").filter(
        LoanAdvanceRequest.remaining_amount > 0,
        LoanAdvanceRequest.disbursed_at.isnot(None)).order_by(LoanAdvanceRequest.id).all()
    for loan in active_loans:
        remaining = float(loan.remaining_amount or 0)
        if remaining <= 0:
            continue
        installment = adj.get(f"loan_{loan.id}")
        due = (_num(installment) if installment not in (None, "")
               else float(loan.monthly_installment or 0)
               or float(loan.amount or 0) / max(loan.installment_months or 1, 1))
        ded = round(max(0.0, min(due, remaining, available - loan_deduction)), 2)
        if ded > 0:
            loans.append((loan, ded))
            loan_deduction += ded
    loan_deduction = round(loan_deduction, 2)
    # PF loans: repaid into the fund, after everything else.
    from ..models.pf import PFLoanRequest
    pf_loans = []
    pf_loan_deduction = 0.0
    for pfl in (PFLoanRequest.query.filter_by(user_id=pp.user_id, status="approved")
                .filter(PFLoanRequest.remaining_amount > 0).order_by(PFLoanRequest.id).all()):
        remaining = float(pfl.remaining_amount or 0)
        override = adj.get(f"pfloan_{pfl.id}")
        due = (_num(override) if override not in (None, "")
               else float(pfl.monthly_installment or 0)
               or float(pfl.amount or 0) / max(pfl.installment_months or 1, 1))
        ded = round(max(0.0, min(due, remaining,
                                 available - loan_deduction - pf_loan_deduction)), 2)
        if ded > 0:
            pf_loans.append((pfl, ded))
            pf_loan_deduction += ded
    pf_loan_deduction = round(pf_loan_deduction, 2)

    deductions = round(component_deductions + monthly_tax + pf_employee + eobi_employee
                       + loan_deduction + pf_loan_deduction + custom_ded, 2)
    net = round(gross - deductions, 2)
    comps["deductions"]["Income Tax"] = round(monthly_tax, 2)
    if pf_config:
        comps["deductions"]["PF Employee"] = round(pf_employee, 2)
    if eobi_employee:
        comps["deductions"]["EOBI (employee)"] = eobi_employee
    if loan_deduction > 0:
        comps["deductions"]["Loan Repayment"] = loan_deduction
    if pf_loan_deduction > 0:
        comps["deductions"]["PF Loan Repayment"] = pf_loan_deduction
    if custom_ded:
        comps["deductions"]["Additional Deduction"] = custom_ded
    if unpaid_days or ratio < 1:
        comps["info"] = {"Days in month": days_in_month,
                         "Paid days": round(days_in_month * ratio, 2),
                         "Unpaid leave days": unpaid_days}
    return {
        "user_id": pp.user_id,
        "name": pp.user.full_name,
        "designation": pp.user.designation or "",
        "basic": basic,
        "allowances_breakdown": dict(comps["allowances"]),
        "deductions_breakdown": dict(comps["deductions"]),
        "components": comps,
        "allowances_total": round(allowances, 2),
        "bonus": round(bonus, 2),
        "ot_pay": ot_pay,
        "gross": gross,
        "taxable": taxable,
        "monthly_tax": monthly_tax,
        "pf_employee": pf_employee,
        "pf_employer": pf_employer,
        "eobi_employee": eobi_employee,
        "settlement": settlement,
        "eobi_employer": eobi_employer,
        "component_deductions": round(component_deductions, 2),
        "loan_deduction": loan_deduction,
        "loans": loans,
        "pf_loans": pf_loans,
        "pf_loan_deduction": pf_loan_deduction,
        "custom_ded": custom_ded,
        "deductions_total": deductions,
        "net": net,
        "paid_days": round(days_in_month * ratio, 2),
        "unpaid_days": unpaid_days,
        "active_loans": active_loans,
    }


def _undo_run(run):
    """Take a payroll run fully back out, flushed but not committed: its
    journals, the staff-loan and PF-loan instalments it recovered, its PF
    rows, its slips and the run itself. A closed period refuses (raises)."""
    from shared.ledger_utils import reverse_journal_entry
    from ..models.pf import PFLoanRequest
    reverse_journal_entry("PRL", run.id)
    for lr in LoanRepayment.query.filter_by(payroll_run_id=run.id).all():
        loan = LoanAdvanceRequest.query.filter_by(id=lr.loan_id).first()
        if loan is not None:
            loan.remaining_amount = round(float(loan.remaining_amount or 0) + float(lr.amount or 0), 2)
            if loan.status == "paid" and loan.remaining_amount > 0:
                loan.status = "approved"
        db.session.delete(lr)
    for row in PFLedger.query.filter_by(payroll_run_id=run.id).all():
        if row.transaction_type == "loan_repayment" and row.reference_id:
            pfl = PFLoanRequest.query.filter_by(id=row.reference_id).first()
            if pfl is not None:
                pfl.remaining_amount = round(float(pfl.remaining_amount or 0)
                                             + float(row.credit or 0), 2)
                if pfl.status == "repaid":
                    pfl.status = "approved"
        db.session.delete(row)
    PFContribution.query.filter_by(month=run.month, year=run.year).delete()
    # Contribution rows written before runs were linked to them.
    PFLedger.query.filter_by(description=f"PF contribution {run.month}/{run.year}",
                             payroll_run_id=None).delete()
    for slip in run.slips:
        db.session.delete(slip)
    db.session.delete(run)
    db.session.flush()


@comp_bp.route("/run-payroll", methods=["GET", "POST"])
@login_required
def run_payroll():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    pf_config = ProvidentFundConfig.query.first()

    if request.method == "POST":
        month = form_int("month", REQUIRED)
        year = form_int("year", REQUIRED)
        replace_all = request.form.get("replace_all") == "1"
        replace_ids_str = request.form.get("replace_ids", "[]")
        import json
        replace_ids = json.loads(replace_ids_str) if replace_ids_str else []

        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            flash("Pick a valid payroll month.", "danger")
            return redirect(url_for("compensation.run_payroll"))
        import calendar
        # The run posts on the last day of its month: that is the period the
        # salary expense belongs to, whenever the run is actually processed.
        period_end = date(year, month, calendar.monthrange(year, month)[1])

        existing = PayrollRun.query.filter_by(month=month, year=year).first()
        if existing:
            if replace_all:
                if existing.paid_at:
                    flash(f"Payroll {month}/{year} is already paid. Undo the salary "
                          "payment on the run page before replacing it.", "danger")
                    return redirect(url_for("compensation.view_run", rid=existing.id))
                # Flushed, not committed: if the new run is refused below,
                # the rollback keeps the old one.
                _undo_run(existing)
                flash(f"Replaced existing payroll for {month}/{year}.", "info")
            else:
                flash(f"Payroll already run for {month}/{year}. Use 'Replace All' to override.", "danger")
                return redirect(url_for("compensation.run_payroll"))

        profiles = PayrollProfile.query.all()
        if not profiles:
            flash("No payroll profiles found.", "danger")
            return redirect(url_for("compensation.index"))
        adjustments = json.loads(request.form.get("adjustments", "{}"))

        pr = PayrollRun(month=month, year=year, processed_by=current_user.id, status="processing")
        db.session.add(pr)
        db.session.flush()
        total_gross = 0; total_ded = 0; total_net = 0; emp_count = 0
        total_tax = 0; total_pf_ee = 0; total_pf_er = 0; total_loan = 0; total_custom = 0
        total_other = 0
        total_eobi_ee = 0; total_eobi_er = 0; total_pf_loan = 0
        settlement_totals = {}   # "Gratuity" / "Leave encashment" -> amount
        loan_credits = {}   # loan id -> amount recovered this run

        for pp in profiles:
            if not pe.in_payroll(pp.user, month, year):
                continue
            uid_str = str(pp.user_id)
            if replace_ids and uid_str not in replace_ids and not replace_all:
                continue
            preview = _preview_employee(pp, month, year, pf_config, adjustments)
            if preview is None:
                continue
            basic = preview["basic"]
            allowances = preview["allowances_total"]
            deductions = preview["deductions_total"]
            gross = preview["gross"]
            net = preview["net"]
            monthly_tax = preview["monthly_tax"]
            pf_employee = preview["pf_employee"]
            pf_employer = preview["pf_employer"] if pf_config else 0
            loan_deduction = preview["loan_deduction"]
            custom_ded = preview["custom_ded"]
            if net < 0:
                db.session.rollback()
                flash(f"{pp.user.full_name}: deductions ({deductions:,.2f}) exceed pay "
                      f"({gross:,.2f}) for {month}/{year}. Adjust the deductions and run again.",
                      "danger")
                return redirect(url_for("compensation.run_payroll"))
            comps = preview["components"]
            # The loans and amounts the preview recovered — the same figures,
            # not a second calculation that could disagree with the slip.
            for loan, ded in preview["loans"]:
                loan.remaining_amount = round(float(loan.remaining_amount or 0) - ded, 2)
                if loan.remaining_amount <= 0:
                    loan.status = "paid"
                db.session.add(LoanRepayment(loan_id=loan.id, amount=ded, payroll_run_id=pr.id,
                                              notes=f"Deducted via payroll {month}/{year}"))
                loan_credits[loan.id] = (loan, loan_credits.get(loan.id, (None, 0))[1] + ded)
            # PF loans are repaid into the member's fund.
            for pfl, ded in preview["pf_loans"]:
                pfl.remaining_amount = round(float(pfl.remaining_amount or 0) - ded, 2)
                if pfl.remaining_amount <= 0:
                    pfl.status = "repaid"
                db.session.add(PFLedger(user_id=pp.user_id, transaction_date=period_end,
                                        transaction_type="loan_repayment",
                                        description=f"PF loan #{pfl.id} repayment {month}/{year}",
                                        credit=ded, debit=0, reference_id=pfl.id,
                                        payroll_run_id=pr.id))
                total_pf_loan += ded

            slip = PayrollSlip(
                payroll_run_id=pr.id, user_id=pp.user_id, basic_salary=basic,
                allowances=allowances, deductions=deductions,
                gross_pay=gross, total_deductions=deductions,
                net_pay=net, taxable_pay=preview["taxable"],
                components_json=json.dumps(comps),
            )
            db.session.add(slip)
            total_gross += gross; total_ded += deductions; total_net += net; emp_count += 1

            total_tax += monthly_tax
            total_pf_ee += pf_employee
            total_pf_er += pf_employer
            total_loan += loan_deduction
            total_custom += custom_ded
            total_eobi_ee += preview["eobi_employee"]
            for name, amt in preview["settlement"].items():
                settlement_totals[name] = settlement_totals.get(name, 0) + amt
            total_eobi_er += preview["eobi_employer"]
            # Deduction-type profile components (welfare fund, ...): whatever
            # the slip deducted that is not tax / PF / EOBI / loan / custom.
            # Derived from the slip's own total so the journal always balances.
            total_other += round(deductions - monthly_tax - pf_employee
                                 - preview["eobi_employee"] - loan_deduction - custom_ded
                                 - preview["pf_loan_deduction"], 2)

            if pf_config:
                contrib = PFContribution(user_id=pp.user_id, month=month, year=year,
                                          employee_amount=pf_employee, employer_amount=pf_employer,
                                          total_amount=round(pf_employee + pf_employer, 2))
                db.session.add(contrib)
                db.session.add(PFLedger(user_id=pp.user_id, transaction_date=period_end,
                                         transaction_type="contribution",
                                         description=f"PF contribution {month}/{year}",
                                         credit=round(pf_employee + pf_employer, 2), debit=0,
                                         payroll_run_id=pr.id))
            _notify(pp.user_id, "Salary Slip Generated",
                    f"Your salary slip for {month}/{year} is available.", "info", "compensation")
            if pp.user.manager_id:
                _notify(pp.user.manager_id, f"Salary Slip: {pp.user.full_name}",
                        f"Salary for {month}/{year}: Rs.{net:,.0f}", "info", "compensation")

        pr.status = "completed"
        pr.total_gross = round(total_gross, 2)
        pr.total_deductions = round(total_ded, 2)
        pr.total_net = round(total_net, 2)
        pr.employee_count = emp_count

        # Post salary journal entry to general ledger
        from shared.ledger_utils import get_or_create_account, post_journal_entry
        salary_exp = get_or_create_account("5121", "Salary Expense", "expense", parent_code="512")
        salary_payable = get_or_create_account("2121", "Salary Payable", "liability", parent_code="212")
        tax_payable = get_or_create_account("2122", "Income Tax Payable", "liability", parent_code="212")
        pf_payable = get_or_create_account("2123", "PF Payable", "liability", parent_code="212")
        loan_clearing = get_or_create_account("2124", "Loan Deductions Clearing", "liability", parent_code="212")
        pf_exp = get_or_create_account("5122", "PF Employer Expense", "expense", parent_code="512")
        other_payable = get_or_create_account("2-01-02-02-0004", "Other Payroll Deductions Payable",
                                              "liability")
        eobi_payable = get_or_create_account("2125", "EOBI Payable", "liability", parent_code="212")
        eobi_exp = get_or_create_account("5123", "EOBI Employer Expense", "expense", parent_code="512")

        # Final settlements are their own expenses, not salary: an auditor
        # (and the P&L) must see gratuity and leave encashment separately.
        settle_accounts = {
            "Gratuity": get_or_create_account("5124", "Gratuity Expense", "expense",
                                              parent_code="512"),
            "Leave encashment": get_or_create_account("5125", "Leave Encashment Expense",
                                                      "expense", parent_code="512"),
        }
        settled = round(sum(settlement_totals.values()), 2)
        lines = [{"account_id": salary_exp.id, "debit": round(total_gross - settled, 2), "credit": 0,
                   "description": f"Gross salary {month}/{year}"}]
        for name, amt in sorted(settlement_totals.items()):
            if round(amt, 2) > 0:
                lines.append({"account_id": settle_accounts[name].id, "debit": round(amt, 2),
                              "credit": 0, "description": f"{name} on leaving {month}/{year}"})
        if round(total_tax, 2) > 0:
            lines.append({"account_id": tax_payable.id, "debit": 0, "credit": round(total_tax, 2),
                           "description": f"Income tax deducted"})
        if round(total_pf_ee, 2) > 0:
            lines.append({"account_id": pf_payable.id, "debit": 0, "credit": round(total_pf_ee, 2),
                           "description": f"PF employee contribution"})
        if round(total_pf_loan, 2) > 0:
            lines.append({"account_id": pf_payable.id, "debit": 0, "credit": round(total_pf_loan, 2),
                          "description": "PF loan repayments"})
        # Each loan is recovered against its own ledger account (Employee
        # Loans & Advances, where the disbursement is debited). Repayments
        # used to go to a clearing LIABILITY, so the receivable never came
        # down. Clearing keeps only a difference, which should not occur.
        from shared.ledger_utils import create_entity_account
        recovered = 0
        for loan_id, (loan, amt) in sorted(loan_credits.items()):
            amt = round(amt, 2)
            if amt <= 0:
                continue
            acct = create_entity_account(
                "loan", loan_id, f"{loan.request_type.title()} #{loan_id} - {loan.user.full_name}")
            lines.append({"account_id": acct.id, "debit": 0, "credit": amt,
                          "description": f"Loan #{loan_id} instalment"})
            recovered += amt
        if abs(round(total_loan - recovered, 2)) >= 0.01:
            lines.append({"account_id": loan_clearing.id, "debit": 0,
                          "credit": round(total_loan - recovered, 2),
                          "description": "Loan repayment deductions"})
        # Component and custom deductions are withheld from staff, so they are
        # not owed to them: Salary Payable carries the net only (what the bank
        # sheet pays). They used to be missing (unbalanced journal, run 500'd)
        # or credited to Salary Payable.
        if round(total_other + total_custom, 2) > 0:
            lines.append({"account_id": other_payable.id, "debit": 0,
                           "credit": round(total_other + total_custom, 2),
                           "description": "Other payroll deductions"})
        if round(total_eobi_ee, 2) > 0:
            lines.append({"account_id": eobi_payable.id, "debit": 0,
                          "credit": round(total_eobi_ee, 2),
                          "description": "EOBI employee contribution"})
        lines.append({"account_id": salary_payable.id, "debit": 0, "credit": round(total_net, 2),
                       "description": f"Net salary payable"})
        post_journal_entry(voucher_type="PRL", voucher_id=pr.id,
                           voucher_number=f"PRL-{year}{month:02d}",
                           description=f"Payroll Run {month}/{year}",
                           lines=lines, entry_date=period_end,
                           created_by=current_user.id)

        if round(total_pf_er, 2) > 0:
            pf_lines = [{"account_id": pf_exp.id, "debit": round(total_pf_er, 2), "credit": 0,
                          "description": f"PF employer contribution {month}/{year}"},
                        {"account_id": pf_payable.id, "debit": 0, "credit": round(total_pf_er, 2),
                          "description": f"PF employer contribution"}]
            post_journal_entry(voucher_type="PRL", voucher_id=pr.id,
                               voucher_number=f"PRL-{year}{month:02d}-PF",
                               description=f"PF Employer Contribution {month}/{year}",
                               lines=pf_lines, entry_date=period_end,
                               created_by=current_user.id)

        if round(total_eobi_er, 2) > 0:
            post_journal_entry(voucher_type="PRL", voucher_id=pr.id,
                               voucher_number=f"PRL-{year}{month:02d}-EOBI",
                               description=f"EOBI Employer Contribution {month}/{year}",
                               lines=[{"account_id": eobi_exp.id, "debit": round(total_eobi_er, 2),
                                       "credit": 0, "description": f"EOBI employer {month}/{year}"},
                                      {"account_id": eobi_payable.id, "debit": 0,
                                       "credit": round(total_eobi_er, 2),
                                       "description": "EOBI employer contribution"}],
                               entry_date=period_end, created_by=current_user.id)

        db.session.add(PayrollAuditLog(payroll_run_id=pr.id, action="payroll_run",
                                        performed_by=current_user.id, details=f"Run for {month}/{year}",
                                        ip_address=request.remote_addr))
        db.session.commit()
        flash(f"Payroll completed for {emp_count} employees. Net: Rs.{total_net:,.0f}", "success")
        return redirect(url_for("compensation.view_run", rid=pr.id))

    # GET: build preview for all employees
    from shared.clock import local_now, local_today
    today = local_today()
    preview_employees = []
    profiles = PayrollProfile.query.all()
    for pp in profiles:
        if not pe.in_payroll(pp.user, today.month, today.year):
            continue
        p = _preview_employee(pp, today.month, today.year, pf_config)
        if p:
            active_loans = LoanAdvanceRequest.query.filter_by(user_id=pp.user_id, status="approved").filter(
                LoanAdvanceRequest.remaining_amount > 0).all()
            p["active_loans"] = active_loans
            preview_employees.append(p)
    return render_template("compensation/run_payroll.html", employees=preview_employees,
                           pf_config=pf_config, now=local_now(),
                           no_slabs=not IncomeTaxSlab.query.filter_by(is_active=True).first())


@comp_bp.route("/payroll-preview-json", methods=["POST"])
@login_required
def payroll_preview_json():
    """Return computed preview as JSON for AJAX recalculation."""
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    from shared.clock import local_today
    month = form_int("month", local_today().month)
    year = form_int("year", local_today().year)
    adjustments = json.loads(request.form.get("adjustments", "{}"))
    pf_config = ProvidentFundConfig.query.first()
    results = []
    for pp in PayrollProfile.query.all():
        if not pe.in_payroll(pp.user, month, year):
            continue
        p = _preview_employee(pp, month, year, pf_config, adjustments)
        if p:
            # Model objects are not JSON: any employee with an active loan
            # made the whole recalculation 500.
            p["active_loans"] = [{"id": l.id, "remaining_amount": float(l.remaining_amount or 0),
                                  "monthly_installment": float(l.monthly_installment or 0)}
                                 for l in p.get("active_loans") or []]
            p["loans"] = [{"id": l.id, "amount": amt} for l, amt in p.get("loans") or []]
            p["pf_loans"] = [{"id": l.id, "amount": amt} for l, amt in p.get("pf_loans") or []]
            results.append(p)
    return jsonify({"employees": results})


@comp_bp.route("/download-template")
@login_required
def download_payroll_template():
    """Download an Excel template for bulk payroll data upload."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        flash("openpyxl not installed.", "danger")
        return redirect(url_for("compensation.index"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payroll Data"
    # Only columns the upload reads. The old sheet also had Basic Salary,
    # Loan Deduction and Net Pay, which were silently ignored, and one
    # made-up sample row instead of this company's staff.
    headers = [
        "Employee Code", "Full Name", "Bonus", "Income Tax (blank = automatic)",
        "Custom Deduction", "Allowances (blank = salary package)",
    ]
    hf = Font(bold=True, color="FFFFFF", size=11)
    hfill = PatternFill(start_color="1A237E", end_color="1A237E", fill_type="solid")
    ha = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Border(left=Side(style="thin"), right=Side(style="thin"),
                   top=Side(style="thin"), bottom=Side(style="thin"))
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.font = hf; c.fill = hfill; c.alignment = ha; c.border = thin
    row = 1
    for pp in PayrollProfile.query.all():
        if not pp.user or not pp.user.is_active:
            continue
        row += 1
        for ci, val in enumerate([_emp_code(pp.user_id), pp.user.full_name, None, None, None, None], 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.border = thin
    from openpyxl.utils import get_column_letter
    for ci in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 20
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="payroll_template.xlsx")


@comp_bp.route("/upload-bulk-data", methods=["POST"])
@login_required
def upload_bulk_data():
    """Upload bulk payroll data as XLSX/CSV/JSON and apply as adjustments."""
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    adjustments = {}

    if request.content_type and "multipart/form-data" in request.content_type:
        f = request.files.get("file")
        if not f or f.filename == "":
            return jsonify({"error": "No file uploaded"}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        # openpyxl reads .xlsx only — legacy .xls (and anything else) would
        # fail at parse time after the file was already stored.
        if ext not in (".csv", ".xlsx"):
            return jsonify({"error": "Upload a .csv or .xlsx file."}), 400
        if current_company_id() is None:
            return jsonify({"error": "No active company"}), 400
        # Parsed straight from memory: the sheet is only read once, here, and
        # a copy on disk would not survive a serverless instance anyway.
        data = f.read()
        rows = []
        try:
            if ext == ".csv":
                import csv
                reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"),
                                                    newline=""))
                rows = [r for r in reader]
            elif ext == ".xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
                ws = wb.active
                headers = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if all(v is None or str(v).strip() == "" for v in row):
                        continue
                    rows.append(dict(zip(headers, [str(v).strip() if v is not None else "" for v in row])))
                wb.close()
        except Exception as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 400
        for row in rows:
            code = row.get("Employee Code", row.get("employee_code", "")).strip()
            user = _member_user_by_code(code)
            if not user:
                continue
            uid = str(user.id)
            adj = {}
            def cell(*names):
                for n in names:
                    for k, v in row.items():
                        if k and k.strip().lower().startswith(n.lower()) and str(v).strip():
                            return str(v).strip()
                return None
            bonus = cell("Bonus")
            if bonus: adj["bonus"] = bonus
            tax = cell("Income Tax", "income_tax", "tax")
            if tax: adj["income_tax"] = tax
            custom = cell("Custom Deduction", "custom_deduction", "extra_ded")
            if custom: adj["custom_deduction"] = custom
            allowances = cell("Allowances", "allowances_override")
            if allowances: adj["allowances_override"] = allowances
            if adj:
                adjustments[uid] = adj
        msg = f"Bulk data loaded for {len(adjustments)} employees from {f.filename}."
        flash(msg, "success")
        return jsonify({"adjustments": adjustments, "message": msg})

    # JSON upload
    data = request.get_json(silent=True)
    if not data:
        flash("Invalid upload data.", "danger")
        return redirect(url_for("compensation.run_payroll"))
    payroll_data = data.get("data", [])
    for row in payroll_data:
        code = row.get("employee_code", "").strip()
        user = _member_user_by_code(code)
        if not user:
            continue
        uid = str(user.id)
        adjustments[uid] = {
            "bonus": row.get("bonus"),
            "income_tax": row.get("income_tax"),
            "custom_deduction": row.get("custom_deduction"),
        }
    flash(f"Bulk data loaded for {len(adjustments)} employees. Adjustments applied.", "success")
    return jsonify({"adjustments": adjustments})


# ── Payroll Run Detail ──

@comp_bp.route("/run/<int:rid>")
@login_required
def view_run(rid):
    """One payroll run: every slip, the totals by earning and deduction, and
    the accounting entry it posted (PRL, via the floating panel)."""
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    run = scoped_get_404(PayrollRun, rid)
    from shared.models.company import CompanyMembership
    slips = run.slips.join(User, PayrollSlip.user_id == User.id).order_by(User.full_name).all()
    codes = {m.user_id: m.employee_code for m in CompanyMembership.query.filter(
        CompanyMembership.company_id == current_company_id(),
        CompanyMembership.user_id.in_([s.user_id for s in slips] or [0])).all()}
    # Totals by component across the run, in first-seen order.
    earnings = {"Basic Salary": 0.0}
    deductions = {}
    for s in slips:
        earnings["Basic Salary"] += float(s.basic_salary or 0)
        comps = _normalize_comps(json.loads(s.components_json)) if s.components_json else {}
        for name, val in (comps.get("allowances") or {}).items():
            earnings[name] = earnings.get(name, 0.0) + float(val or 0)
        for name, val in (comps.get("deductions") or {}).items():
            deductions[name] = deductions.get(name, 0.0) + float(val or 0)
    # Overtime and bonus are in the slip totals but not its breakdown; show
    # them as one row so each table adds up to the run's totals.
    rest = round(sum(float(s.gross_pay or 0) for s in slips) - sum(earnings.values()), 2)
    if rest:
        earnings["Overtime & bonus"] = rest
    rest = round(sum(float(s.total_deductions or 0) for s in slips) - sum(deductions.values()), 2)
    if rest:
        deductions["Other"] = rest
    import calendar
    from shared.clock import local_today
    from shared.ledger_utils import cash_bank_accounts
    return render_template(
        "compensation/run_detail.html", run=run, slips=slips, codes=codes,
        accounts=cash_bank_accounts(), today=local_today(),
        earnings={k: v for k, v in earnings.items() if round(v, 2)},
        deductions={k: v for k, v in deductions.items() if round(v, 2)},
        period=f"{calendar.month_name[run.month]} {run.year}")


@comp_bp.route("/run/<int:rid>/pay", methods=["POST"])
@login_required
def pay_run(rid):
    """Record the salary payment: Dr Salary Payable (net) / Cr the bank or
    cash account it was paid from. Salary Payable used to stay credited until
    someone remembered a manual voucher."""
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    from shared.ledger_utils import cash_bank_account
    from shared.posting_helpers import parse_doc_date
    from .. import payroll_posting as pp_
    run = scoped_get_404(PayrollRun, rid)
    account = cash_bank_account(request.form.get("account_id", type=int))
    if account is None:
        raise FormInputError("Choose the bank or cash account the salaries were paid from.")
    when = parse_doc_date(request.form.get("paid_on")).date()
    try:
        pp_.record_salary_payment(run, account, when, current_user.id)
    except ValueError as e:
        raise FormInputError(str(e))
    db.session.add(PayrollAuditLog(payroll_run_id=run.id, action="salary_paid",
                                   performed_by=current_user.id,
                                   details=f"Paid from {account.code} on {when}",
                                   ip_address=request.remote_addr))
    db.session.commit()
    flash(f"Salaries of Rs.{float(run.total_net):,.0f} recorded as paid from {account.name}.", "success")
    return redirect(url_for("compensation.view_run", rid=run.id))


@comp_bp.route("/run/<int:rid>/unpay", methods=["POST"])
@login_required
def unpay_run(rid):
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    from .. import payroll_posting as pp_
    run = scoped_get_404(PayrollRun, rid)
    if not run.paid_at:
        raise FormInputError("This payroll is not marked as paid.")
    pp_.undo_salary_payment(run)
    db.session.add(PayrollAuditLog(payroll_run_id=run.id, action="salary_payment_undone",
                                   performed_by=current_user.id, ip_address=request.remote_addr))
    db.session.commit()
    flash("Salary payment reversed.", "success")
    return redirect(url_for("compensation.view_run", rid=run.id))


@comp_bp.route("/run/<int:rid>/delete", methods=["POST"])
@login_required
def delete_run(rid):
    """Delete a run run by mistake: everything it posted comes back out."""
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    run = scoped_get_404(PayrollRun, rid)
    if run.paid_at:
        raise FormInputError("Undo the salary payment before deleting this payroll.")
    label = f"{run.month}/{run.year}"
    _undo_run(run)
    db.session.add(PayrollAuditLog(action="payroll_deleted", performed_by=current_user.id,
                                   details=f"Run for {label} deleted",
                                   ip_address=request.remote_addr))
    db.session.commit()
    flash(f"Payroll {label} deleted; its journals, loan recoveries and PF entries were reversed.",
          "success")
    return redirect(url_for("compensation.index"))


# ── Staff loans & advances (admin) ──

@comp_bp.route("/loans")
@login_required
def loans_admin():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    from shared.ledger_utils import cash_bank_accounts
    rows = LoanAdvanceRequest.query.order_by(LoanAdvanceRequest.created_at.desc()).all()
    from shared.clock import local_today
    return render_template("compensation/loans.html", loans=rows,
                           accounts=cash_bank_accounts(), today=local_today())


@comp_bp.route("/loans/<int:lid>/disburse", methods=["POST"])
@login_required
def disburse_loan(lid):
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    from shared.ledger_utils import cash_bank_account
    from shared.posting_helpers import parse_doc_date
    from .. import payroll_posting as pp_
    loan = scoped_get_404(LoanAdvanceRequest, lid)
    account = cash_bank_account(request.form.get("account_id", type=int))
    if account is None:
        raise FormInputError("Choose the bank or cash account the loan is paid from.")
    try:
        pp_.disburse_loan(loan, account, parse_doc_date(request.form.get("paid_on")).date(),
                          current_user.id)
    except ValueError as e:
        raise FormInputError(str(e))
    _notify(loan.user_id, "Loan Paid", f"Your {loan.request_type} of Rs.{float(loan.amount):,.0f} "
            "has been paid. Instalments start with the next payroll.", "success")
    db.session.commit()
    flash(f"{loan.request_type.title()} #{loan.id} paid out from {account.name}.", "success")
    return redirect(url_for("compensation.loans_admin"))


@comp_bp.route("/loans/<int:lid>/undo-disbursement", methods=["POST"])
@login_required
def undo_loan_disbursement(lid):
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    from .. import payroll_posting as pp_
    loan = scoped_get_404(LoanAdvanceRequest, lid)
    try:
        pp_.undo_disbursement(loan)
    except ValueError as e:
        raise FormInputError(str(e))
    db.session.commit()
    flash("Loan payment reversed.", "success")
    return redirect(url_for("compensation.loans_admin"))


# ── View & Download Slip ──

@comp_bp.route("/slip/<int:sid>")
@login_required
def view_slip(sid):
    slip = scoped_get_404(PayrollSlip, sid)
    if slip.user_id != current_user.id and not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    comps = _normalize_comps(json.loads(slip.components_json)) if slip.components_json else {"allowances": {}, "deductions": {}}
    return render_template("compensation/slip.html", slip=slip, comps=comps,
                           emp_code=_emp_code(slip.user_id))


@comp_bp.route("/slip/<int:sid>/pdf")
@login_required
def download_slip_pdf(sid):
    slip = scoped_get_404(PayrollSlip, sid)
    if slip.user_id != current_user.id and not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib import colors
    except ImportError:
        flash("PDF library not installed.", "danger")
        return redirect(url_for("compensation.index"))
    comps = _normalize_comps(json.loads(slip.components_json)) if slip.components_json else {"allowances": {}, "deductions": {}}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    pw, ph = A4
    margin = 50
    col_mid = pw / 2

    # ── Header ──
    from shared.models.company import Company
    _company = Company.query.get(current_company_id())
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(pw / 2, ph - 40,
                        _company.name if _company else "Accountix ERP")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 11)
    c.drawCentredString(pw / 2, ph - 58, f"Salary Slip - {slip.payroll_run.month}/{slip.payroll_run.year}")
    c.setStrokeColor(colors.HexColor("#1a237e"))
    c.setLineWidth(2)
    c.line(margin, ph - 65, pw - margin, ph - 65)

    # ── Employee Info ──
    y = ph - 88
    c.setFont("Helvetica", 9.5)
    c.drawString(margin, y, f"Employee: {slip.user.full_name}")
    c.drawString(col_mid + 10, y, f"Designation: {slip.user.designation or '-'}")
    y -= 15
    c.drawString(margin, y, f"Department: {slip.user.department or '-'}")
    c.drawString(col_mid + 10, y, f"Code: {_emp_code(slip.user_id)}")
    y -= 15
    c.drawString(margin, y, f"CNIC: {slip.user.cnic or '-'}")
    y -= 22

    # ── Earnings & Deductions Header ──
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, "Earnings")
    c.drawString(col_mid + 10, y, "Deductions")
    c.setFillColor(colors.black)
    y -= 16

    # Build lists
    earnings = [("Basic Salary", slip.basic_salary)]
    for name, val in comps.get("allowances", {}).items():
        if val:
            earnings.append((name, val))
    deductions_list = []
    for name, val in comps.get("deductions", {}).items():
        if val:
            deductions_list.append((name, val))

    # Draw earnings/deductions rows with alternating shading. Long component
    # lists flow onto extra pages instead of drawing off the page.
    max_rows = max(len(earnings), len(deductions_list), 0)
    c.setFont("Helvetica", 9.5)
    for i in range(max_rows):
        if y < 150:
            c.showPage()
            y = ph - 60
            c.setFillColor(colors.HexColor("#1a237e"))
            c.setFont("Helvetica-Bold", 11)
            c.drawString(margin, y, "Earnings (contd.)")
            c.drawString(col_mid + 10, y, "Deductions (contd.)")
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 9.5)
            y -= 16
        if i % 2 == 1:
            c.setFillColor(colors.HexColor("#f0f4ff"))
        else:
            c.setFillColor(colors.white)
        c.rect(margin, y - 2, col_mid - margin - 5, 14, fill=1, stroke=0)
        c.rect(col_mid + 5, y - 2, pw - margin - col_mid - 5, 14, fill=1, stroke=0)
        c.setFillColor(colors.black)
        if i < len(earnings):
            label, val = earnings[i]
            c.drawString(margin + 3, y, str(label))
            c.drawRightString(col_mid - 10, y, f"Rs. {_m(val)}")
        if i < len(deductions_list):
            label, val = deductions_list[i]
            c.drawString(col_mid + 10, y, str(label))
            c.drawRightString(pw - margin, y, f"Rs. {_m(val)}")
        y -= 14
    y -= 6

    # ── Totals ──
    if y < 170:
        c.showPage()
        y = ph - 60
    c.setStrokeColor(colors.HexColor("#1a237e"))
    c.setLineWidth(1)
    c.line(margin, y, pw - margin, y)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y - 14, "Total Earnings")
    c.drawRightString(col_mid - 10, y - 14, f"Rs. {_m(slip.gross_pay)}")
    c.drawString(col_mid + 10, y - 14, "Total Deductions")
    c.drawRightString(pw - margin, y - 14, f"Rs. {_m(slip.total_deductions)}")
    y -= 24

    # ── Net Pay Box ──
    box_y = y - 50
    c.setStrokeColor(colors.HexColor("#1b5e20"))
    c.setFillColor(colors.HexColor("#e8f5e9"))
    c.setLineWidth(2)
    c.roundRect(margin, box_y, pw - 2 * margin, 48, 6, fill=1, stroke=1)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor("#1b5e20"))
    c.drawString(margin + 12, box_y + 16,
                 f"Net Payable: Rs. {_m(slip.net_pay, decimal_places=0)}")
    # Amount in words (wrapped if too long)
    words = _num_to_words(int(round(float(slip.net_pay or 0))))
    c.setFont("Helvetica", 9)
    c.drawRightString(pw - margin - 12, box_y + 18, f"(Rupees {words})")
    y = box_y - 20

    # ── Footer ──
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#9ca3af"))
    info = comps.get("info") or {}
    if info:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(colors.HexColor("#374151"))
        c.drawString(margin, y, "  ".join(f"{k}: {v:g}" if isinstance(v, (int, float)) else f"{k}: {v}"
                                         for k, v in info.items()))
    from shared.clock import local_now
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#9ca3af"))
    c.drawCentredString(pw / 2, 30, f"Generated on {local_now():%d-%b-%Y %H:%M} | This is a computer-generated slip")

    c.save()
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=False,
                     download_name=f"salary_slip_{slip.payroll_run.month}_{slip.payroll_run.year}.pdf")


def _emp_code(user_id):
    """The employee's code in the active company (codes are per company)."""
    from shared.models.company import CompanyMembership
    m = CompanyMembership.query.filter_by(company_id=current_company_id(),
                                          user_id=user_id).first()
    if m is not None and m.employee_code:
        return m.employee_code
    u = User.query.get(user_id)
    return u.employee_code if u else ""


def _normalize_comps(comps):
    """Normalize components_json to {allowances: {}, deductions: {}} regardless of storage format."""
    if isinstance(comps, dict):
        if "allowances" in comps and "deductions" in comps:
            return comps
        result = {"allowances": {}, "deductions": {}}
        for k, v in comps.items():
            result["allowances"][k] = float(v) if v else 0
        return result
    if isinstance(comps, list):
        result = {"allowances": {}, "deductions": {}}
        for item in comps:
            if isinstance(item, dict):
                name = item.get("name", "Item")
                val = float(item.get("value", 0))
                typ = item.get("type", "allowance")
                if typ in ("deduction", "tax", "loan"):
                    result["deductions"][name] = val
                else:
                    result["allowances"][name] = val
        return result
    return {"allowances": {}, "deductions": {}}


def _num_to_words(n):
    if n == 0: return "Zero"
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
    def _convert(num):
        if num < 20: return ones[num]
        if num < 100: return tens[num // 10] + (" " + ones[num % 10] if num % 10 else "")
        if num < 1000: return ones[num // 100] + " Hundred" + (" " + _convert(num % 100) if num % 100 else "")
        if num < 100000: return _convert(num // 1000) + " Thousand" + (" " + _convert(num % 1000) if num % 1000 else "")
        if num < 10000000: return _convert(num // 100000) + " Lakh" + (" " + _convert(num % 100000) if num % 100000 else "")
        return _convert(num // 10000000) + " Crore" + (" " + _convert(num % 10000000) if num % 10000000 else "")
    return _convert(n) + " Only"


def _notify(user_id, title, message, ntype="info", module="compensation", ref_id=None):
    notif = Notification(title=title, message=message, notification_type=ntype,
                         module=module, reference_id=ref_id, created_by=current_user.id)
    db.session.add(notif)
    db.session.flush()
    db.session.add(NotificationRecipient(notification_id=notif.id, user_id=user_id))


# ── Bank Ledger & Revisions ──

@comp_bp.route("/export-bank-ledger/<int:rid>")
@login_required
def export_bank_ledger(rid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    run = scoped_get_404(PayrollRun, rid)
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return jsonify({"error": "openpyxl not installed"}), 500
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Payroll_{run.month}_{run.year}"
    headers = ["Employee Code", "Employee Name", "Bank Name", "Account Title", "Account Number", "Net Pay"]
    hf = Font(bold=True, color="FFFFFF", size=11)
    hfill = PatternFill(start_color="1A237E", end_color="1A237E", fill_type="solid")
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.font = hf; c.fill = hfill; c.alignment = Alignment(horizontal="center")
    # The company's own employee codes (the user row's code is a global
    # placeholder), net pay as a number, nothing for a zero net, and a total
    # that agrees with Salary Payable.
    ri = 1
    total = 0.0
    for slip in run.slips:
        net = round(float(slip.net_pay or 0), 2)
        if net <= 0:
            continue
        ri += 1
        total += net
        u = slip.user
        for ci, val in enumerate([_emp_code(u.id), u.full_name, u.bank_name or "",
                                   u.bank_account_title or "", u.bank_account_number or "",
                                   net], 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.alignment = Alignment(horizontal="left" if ci < 6 else "right")
        ws.cell(row=ri, column=6).number_format = "#,##0.00"
    ws.cell(row=ri + 1, column=5, value="Total").font = Font(bold=True)
    tc = ws.cell(row=ri + 1, column=6, value=round(total, 2))
    tc.font = Font(bold=True)
    tc.number_format = "#,##0.00"
    from openpyxl.utils import get_column_letter
    for ci in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 22
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"bank_ledger_{run.month}_{run.year}.xlsx")


@comp_bp.route("/revisions")
@login_required
def revisions():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    all_revisions = SalaryRevision.query.order_by(SalaryRevision.created_at.desc()).all()
    return render_template("compensation/revisions.html", revisions=all_revisions)
