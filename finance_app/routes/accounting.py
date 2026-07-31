from datetime import datetime
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.stock_ledger import VoucherNumber
from shared.models.ledger import ChartOfAccount
from shared.models.accounting_voucher import AccountingVoucher, AccountingVoucherLine
from shared.models.company_settings import AccountingPeriod
from shared.ledger_utils import post_journal_entry, reverse_journal_entry
from shared.permissions import VOUCHER_SECTION, deny_page
from shared.tenancy import scoped_get, scoped_get_404

acct_bp = Blueprint("accounting", __name__, url_prefix="/accounting",
                     template_folder="../../finance_app/templates")

VOUCHER_LABELS = {
    "CPV": "Cash Payment Voucher",
    "CRV": "Cash Receipt Voucher",
    "BPV": "Bank Payment Voucher",
    "BRV": "Bank Receipt Voucher",
    "JV": "Journal Voucher",
}

CASH_BANK_TYPES = ("CPV", "CRV", "BPV", "BRV")


@acct_bp.route("/")
@acct_bp.route("/dashboard")
@login_required
def dashboard():
    total = AccountingVoucher.query.count()
    unapproved = AccountingVoucher.query.filter_by(status="unapproved").count()
    approved = AccountingVoucher.query.filter_by(status="approved").count()
    by_type = {}
    for vt in VOUCHER_LABELS:
        by_type[vt] = AccountingVoucher.query.filter_by(voucher_type=vt).count()
    recent = AccountingVoucher.query.order_by(AccountingVoucher.id.desc()).limit(10).all()
    return render_template("accounting/dashboard.html",
                           stats={"total": total, "unapproved": unapproved,
                                  "approved": approved, "by_type": by_type},
                           recent=recent)


@acct_bp.route("/vouchers", methods=["GET", "POST"])
@acct_bp.route("/vouchers/<int:id>", methods=["GET", "POST"])
@login_required
def voucher_form(id=None):
    voucher = scoped_get(AccountingVoucher, id) if id else None
    is_approved = voucher and voucher.status == "approved"
    edit_mode = request.args.get("edit") == "1" if (voucher and not is_approved) else False

    if request.method == "POST" and not is_approved:
        is_new = voucher is None
        vtype_for_perm = voucher.voucher_type if voucher else request.form.get("voucher_type", "CPV")
        if deny_page(VOUCHER_SECTION.get(vtype_for_perm, "journal_vouchers"),
                     "create" if is_new else "edit"):
            return redirect(url_for("accounting.voucher_list"))
        if is_new:
            vtype = request.form.get("voucher_type", "CPV")
            voucher = AccountingVoucher(
                voucher_type=vtype,
                voucher_number=VoucherNumber.next(vtype),
                created_by=current_user.id,
            )
            db.session.add(voucher)
        else:
            AccountingVoucherLine.query.filter_by(voucher_id=voucher.id).delete()
            db.session.flush()

        dt_str = request.form.get("voucher_date", "")
        if dt_str:
            parsed = False
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
                try:
                    voucher.voucher_date = datetime.strptime(dt_str, fmt)
                    parsed = True
                    break
                except ValueError:
                    continue
            if not parsed:
                voucher.voucher_date = datetime.utcnow()
        else:
            voucher.voucher_date = datetime.utcnow()

        cb_id = request.form.get("cash_bank_account_id", "").strip()
        if cb_id:
            try:
                voucher.cash_bank_account_id = int(cb_id)
            except ValueError:
                voucher.cash_bank_account_id = None
        else:
            voucher.cash_bank_account_id = None
        voucher.notes = request.form.get("notes", "")
        action = request.form.get("action", "save")

        db.session.flush()

        accounts = request.form.getlist("account_id[]")
        descs = request.form.getlist("description[]")
        debits = request.form.getlist("debit[]")
        credits = request.form.getlist("credit[]")

        has_lines = False
        for i in range(len(accounts)):
            aid = accounts[i].strip() if i < len(accounts) else ""
            if not aid:
                continue
            try:
                d = Decimal(str(float(debits[i]))) if i < len(debits) and debits[i].strip() else Decimal("0")
            except (ValueError, TypeError):
                d = Decimal("0")
            try:
                c = Decimal(str(float(credits[i]))) if i < len(credits) and credits[i].strip() else Decimal("0")
            except (ValueError, TypeError):
                c = Decimal("0")
            if d == 0 and c == 0:
                continue
            # For cash/bank vouchers the counter accounts must sit on the side
            # OPPOSITE the cash/bank line (receipt -> cash Dr, counters Cr;
            # payment -> cash Cr, counters Dr). Take the amount the user entered
            # in either column and force it onto the correct side so the voucher
            # can never post lopsided (the bug that unbalanced the BRV).
            if voucher.voucher_type in CASH_BANK_TYPES:
                amt = d + c
                if voucher.voucher_type in ("CRV", "BRV"):
                    d, c = Decimal("0"), amt
                else:
                    d, c = amt, Decimal("0")
            try:
                acct_id = int(aid)
            except ValueError:
                continue
            line = AccountingVoucherLine(
                voucher_id=voucher.id,
                line_no=i + 1,
                account_id=acct_id,
                description=descs[i] if i < len(descs) else "",
                debit=d,
                credit=c,
            )
            db.session.add(line)
            has_lines = True

        _err_ctx = {"accounts": ChartOfAccount.query.filter_by(is_active=True)
                     .order_by(ChartOfAccount.code).all(),
                     "labels": VOUCHER_LABELS, "initial_type": "",
                     "edit_mode": edit_mode}

        if not has_lines:
            flash("Add at least one line with amount.", "error")
            return render_template("accounting/voucher_form.html", voucher=voucher, **_err_ctx)

        def _safe_dec(val):
            try:
                return Decimal(str(float(val)))
            except (ValueError, TypeError):
                return Decimal("0")

        if voucher.voucher_type in CASH_BANK_TYPES:
            if not voucher.cash_bank_account_id:
                flash("Select a Cash/Bank account.", "error")
                return render_template("accounting/voucher_form.html", voucher=voucher, **_err_ctx)
            total = sum(
                (_safe_dec(d) for d in debits if d.strip()),
                Decimal("0"),
            ) + sum(
                (_safe_dec(c) for c in credits if c.strip()),
                Decimal("0"),
            )
            if total == 0:
                flash("Total amount must be greater than 0.", "error")
                return render_template("accounting/voucher_form.html", voucher=voucher, **_err_ctx)

            cb_debit, cb_credit = Decimal("0"), Decimal("0")
            if voucher.voucher_type in ("CPV", "BPV"):
                cb_credit = total
            else:
                cb_debit = total
            cb_line = AccountingVoucherLine(
                voucher_id=voucher.id,
                line_no=0,
                account_id=voucher.cash_bank_account_id,
                description=f"{VOUCHER_LABELS[voucher.voucher_type]} - {'Payment' if voucher.voucher_type in ('CPV','BPV') else 'Receipt'}",
                debit=cb_debit,
                credit=cb_credit,
            )
            db.session.add(cb_line)

        if voucher.voucher_type == "JV":
            total_d = sum(
                (
                    _safe_dec(d)
                    for i, d in enumerate(debits)
                    if d.strip() and i < len(accounts) and accounts[i].strip()
                ),
                Decimal("0"),
            )
            total_c = sum(
                (
                    _safe_dec(c)
                    for i, c in enumerate(credits)
                    if c.strip() and i < len(accounts) and accounts[i].strip()
                ),
                Decimal("0"),
            )
            if total_d != total_c:
                flash(
                    f"Debit total ({total_d}) does not match Credit total ({total_c}).",
                    "error",
                )
                return render_template("accounting/voucher_form.html", voucher=voucher, **_err_ctx)

        if action == "approve":
            errors = _approve_voucher(voucher)
            if errors:
                flash(errors, "error")
                return render_template("accounting/voucher_form.html", voucher=voucher, **_err_ctx)

        voucher.status = "unapproved" if action == "save" else "approved"
        db.session.commit()
        flash(
            f"{VOUCHER_LABELS[voucher.voucher_type]} {voucher.voucher_number} {'approved' if action == 'approve' else 'saved'}.",
            "success",
        )
        return redirect(url_for("accounting.voucher_form", id=voucher.id))

    accounts = ChartOfAccount.query.filter_by(is_active=True).order_by(ChartOfAccount.code).all()
    initial_type = request.args.get("type", "")
    if initial_type not in VOUCHER_LABELS:
        initial_type = ""
    return render_template(
        "accounting/voucher_form.html",
        voucher=voucher,
        accounts=accounts,
        labels=VOUCHER_LABELS,
        initial_type=initial_type,
        edit_mode=edit_mode,
    )


def _approve_voucher(v):
    lines = []
    for line in v.lines.all():
        lines.append({
            "account_id": line.account_id,
            "debit": float(line.debit),
            "credit": float(line.credit),
            "description": line.description,
        })
    if not lines:
        return "No lines to post."
    post_journal_entry(
        voucher_type=v.voucher_type,
        voucher_id=v.id,
        voucher_number=v.voucher_number,
        description=f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number}",
        lines=lines,
        entry_date=v.voucher_date,
        created_by=current_user.id,
    )
    v.approved_by = current_user.id
    v.approved_at = datetime.utcnow()
    return None


def _resolve_voucher_period():
    from .reports import _parse_date, _default_period

    filter_mode = request.args.get("filter_mode", "period")
    period_id = request.args.get("period_id", type=int)
    from_str = request.args.get("from", "").strip()
    to_str = request.args.get("to", "").strip()
    from_date = _parse_date(from_str) if from_str else None
    to_date = _parse_date(to_str) if to_str else None
    if from_date:
        from_date = datetime.combine(from_date, datetime.min.time())
    if to_date:
        to_date = datetime.combine(to_date, datetime.max.time())

    periods = AccountingPeriod.query.order_by(AccountingPeriod.start_date.desc()).all()
    selected_period_id = period_id

    if filter_mode == "period" and period_id:
        period = scoped_get(AccountingPeriod, period_id)
        if period:
            from_date = datetime.combine(period.start_date, datetime.min.time())
            to_date = datetime.combine(period.end_date, datetime.max.time())

    if not from_date and not to_date:
        active = _default_period()
        if active:
            from_date = datetime.combine(active.start_date, datetime.min.time())
            to_date = datetime.combine(active.end_date, datetime.max.time())
            if not selected_period_id:
                selected_period_id = active.id
    elif filter_mode == "period" and not selected_period_id:
        active = _default_period()
        if active:
            selected_period_id = active.id

    return from_date, to_date, periods, selected_period_id, filter_mode, from_str, to_str


@acct_bp.route("/vouchers/list")
@login_required
def voucher_list():
    q = AccountingVoucher.query
    vtype = request.args.get("vtype", "")
    status = request.args.get("status", "")
    from_date, to_date, periods, selected_period_id, filter_mode, from_str, to_str = _resolve_voucher_period()

    # Exclude auto-generated reversal vouchers (no UI to create/manage them)
    q = q.filter(~AccountingVoucher.voucher_number.like("%-REV"))

    if vtype:
        q = q.filter_by(voucher_type=vtype)
    if status:
        q = q.filter_by(status=status)
    if from_date:
        q = q.filter(AccountingVoucher.voucher_date >= from_date)
    if to_date:
        q = q.filter(AccountingVoucher.voucher_date <= to_date)

    vouchers = q.order_by(AccountingVoucher.id.desc()).all()
    return render_template(
        "accounting/voucher_list.html",
        vouchers=vouchers,
        labels=VOUCHER_LABELS,
        periods=periods,
        selected_period_id=selected_period_id,
        filter_mode=filter_mode,
        from_str=from_str,
        to_str=to_str,
        filters={"vtype": vtype, "status": status},
    )


@acct_bp.route("/vouchers/<int:id>/approve", methods=["POST"])
@login_required
def approve_voucher(id):
    v = scoped_get_404(AccountingVoucher, id)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "approve"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status == "approved":
        flash("Already approved.", "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    err = _approve_voucher(v)
    if err:
        flash(err, "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    v.status = "approved"
    db.session.commit()
    flash(f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number} approved.", "success")
    return redirect(url_for("accounting.voucher_form", id=v.id))


@acct_bp.route("/vouchers/<int:id>/unapprove", methods=["POST"])
@login_required
def unapprove_voucher(id):
    v = scoped_get_404(AccountingVoucher, id)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "approve"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status != "approved":
        flash("Voucher is not approved.", "error")
        return redirect(url_for("accounting.voucher_list"))
    reverse_journal_entry(v.voucher_type, v.id, created_by=current_user.id)
    v.status = "unapproved"
    v.approved_by = None
    v.approved_at = None
    db.session.commit()
    flash(f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number} unapproved.", "success")
    return redirect(url_for("accounting.voucher_form", id=v.id))


@acct_bp.route("/vouchers/<int:id>/preview")
@login_required
def voucher_preview(id):
    v = scoped_get_404(AccountingVoucher, id)
    lines = v.lines.order_by(AccountingVoucherLine.line_no).all()
    total_debit = sum(float(l.debit) for l in lines)
    total_credit = sum(float(l.credit) for l in lines)
    return render_template("accounting/voucher_preview.html",
                           voucher=v, lines=lines,
                           total_debit=total_debit, total_credit=total_credit,
                           labels=VOUCHER_LABELS)

@acct_bp.route("/vouchers/<int:id>/export")
@login_required
def voucher_export(id):
    fmt = request.args.get("fmt", "pdf")
    v = scoped_get_404(AccountingVoucher, id)
    lines = v.lines.order_by(AccountingVoucherLine.line_no).all()
    total_debit = sum(float(l.debit) for l in lines)
    total_credit = sum(float(l.credit) for l in lines)

    headers = ["#", "A/c Code", "Account Name", "Description", "Debit", "Credit"]
    rows = []
    for i, line in enumerate(lines, 1):
        rows.append([i, line.account.code, line.account.name,
                     line.description or "",
                     float(line.debit) or 0, float(line.credit) or 0])
    rows.append(["", "", "", "Total", total_debit, total_credit])

    title = f"{VOUCHER_LABELS[v.voucher_type]} - {v.voucher_number}"

    if fmt == "excel":
        from .reports import _build_excel_wb
        out = _build_excel_wb(title, headers, rows)
        return send_file(out, as_attachment=True,
                         download_name=f"voucher_{v.voucher_number}.xlsx")

    from .reports import _build_pdf
    pdf_out = _build_pdf(title, headers, rows)
    return send_file(pdf_out, as_attachment=True,
                     download_name=f"voucher_{v.voucher_number}.pdf",
                     mimetype="application/pdf")

@acct_bp.route("/vouchers/<int:id>/delete", methods=["POST"])
@login_required
def delete_voucher(id):
    v = scoped_get_404(AccountingVoucher, id)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "delete"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status == "approved":
        flash("Cannot delete an approved voucher. Unapprove it first.", "error")
        return redirect(url_for("accounting.voucher_list"))
    db.session.delete(v)
    db.session.commit()
    flash(f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number} deleted.", "success")
    return redirect(url_for("accounting.voucher_list"))


@acct_bp.route("/api/accounts")
@login_required
def api_accounts():
    q = request.args.get("q", "").strip()
    exclude = request.args.get("exclude", type=int)
    # Only level-5 operational accounts are postable; aggregating accounts
    # (levels 1-4) must never appear in a posting picker.
    query = ChartOfAccount.query.filter_by(is_active=True).filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL)
    if q:
        query = query.filter(
            db.or_(
                ChartOfAccount.name.ilike(f"%{q}%"),
                ChartOfAccount.code.ilike(f"%{q}%"),
            )
        )
    if exclude:
        query = query.filter(ChartOfAccount.id != exclude)
    accounts = query.order_by(ChartOfAccount.code).limit(30).all()
    return jsonify([
        {"id": a.id, "code": a.code, "name": a.name, "type": a.type}
        for a in accounts
    ])


@acct_bp.route("/api/cash-bank-accounts")
@login_required
def api_cash_bank_accounts():
    q = request.args.get("q", "").strip()
    query = ChartOfAccount.query.filter(
        ChartOfAccount.is_active == True,
        ChartOfAccount.type == "asset",
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        db.or_(
            ChartOfAccount.name.ilike("%cash%"),
            ChartOfAccount.name.ilike("%bank%"),
            # Anything under Cash & Cash Equivalents (1-01-01-...) counts,
            # whatever it is named (e.g. "Meezan Riyadh Branch").
            ChartOfAccount.code.like("1-01-01-%"),
        ),
    )
    if q:
        query = query.filter(ChartOfAccount.name.ilike(f"%{q}%"))
    accounts = query.order_by(ChartOfAccount.code).all()
    return jsonify([
        {"id": a.id, "code": a.code, "name": a.name, "type": a.type}
        for a in accounts
    ])
