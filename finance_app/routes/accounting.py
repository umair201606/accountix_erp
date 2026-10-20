from datetime import datetime
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, abort
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.stock_ledger import VoucherNumber
from shared.models.ledger import ChartOfAccount, JournalLine
from shared.models.accounting_voucher import AccountingVoucher, AccountingVoucherLine
from shared.models.inventory_settings import InventorySettings
from shared.models.project_label import ProjectLabel
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


def _reconcile_tracking(voucher_id):
    """Keep Invoicing > Invoice Tracking in step after a voucher changes.

    A receipt can be assigned to invoices, and then the voucher edited: the
    amount reduced, the customer line removed, the whole thing unapproved. The
    tracker derives every figure it shows from live data, so it self-corrects —
    but the ``paid_amount`` / ``payment_status`` cached on each invoice has to
    be re-derived, or the invoice list would still call it paid.

    Imported inside the function to keep the accounting module independent of
    invoicing at import time, and deliberately non-fatal: a voucher save must
    not fail because a subledger cache could not be refreshed.
    """
    try:
        from shared import payment_tracking as pt
        pt.reconcile_voucher(voucher_id)
    except Exception:                                    # pragma: no cover
        db.session.rollback()


def _drop_tracking_for_voucher(voucher_id):
    """Remove assignments belonging to a voucher about to be deleted, then
    refresh the invoices they pointed at."""
    try:
        from shared import payment_tracking as pt
        from shared.models.payment_allocation import PaymentAllocation
        allocs = PaymentAllocation.query.filter_by(
            voucher_id=int(voucher_id)).all()
        if not allocs:
            return
        touched = {(a.doc_type, int(a.doc_id)) for a in allocs}
        for a in allocs:
            db.session.delete(a)
        db.session.flush()
        for doc_type, doc_id in touched:
            if doc_type in (pt.SALES, pt.PURCHASE):
                pt.recompute_doc(doc_type, doc_id)
    except Exception:                                    # pragma: no cover
        db.session.rollback()


@acct_bp.route("/")
@acct_bp.route("/dashboard")
@login_required
def dashboard():
    # Settings → Rights: only the voucher types this person may view are
    # counted and listed, and a type's card opens a new one only with create.
    from shared.rights_guard import can, viewable_voucher_types
    types = viewable_voucher_types()
    base = AccountingVoucher.query.filter(AccountingVoucher.voucher_type.in_(types))
    total = base.count()
    unapproved = base.filter_by(status="unapproved").count()
    approved = base.filter_by(status="approved").count()
    by_type = {}
    for vt in VOUCHER_LABELS:
        if vt in types:
            by_type[vt] = AccountingVoucher.query.filter_by(voucher_type=vt).count()
    recent = base.order_by(AccountingVoucher.id.desc()).limit(10).all()
    creatable = [vt for vt in VOUCHER_LABELS if can(VOUCHER_SECTION[vt], "create")]
    return render_template("accounting/dashboard.html",
                           stats={"total": total, "unapproved": unapproved,
                                  "approved": approved, "by_type": by_type},
                           recent=recent, viewable=types, creatable=creatable)
@acct_bp.route("/vouchers", methods=["GET", "POST"])
@acct_bp.route("/vouchers/<int:id>", methods=["GET", "POST"])
@login_required
def voucher_form(id=None):
    # A save or approval locks the voucher until commit, so two users
    # approving it at once post it once (the second sees it approved).
    voucher = (scoped_get(AccountingVoucher, id, for_update=request.method == "POST")
               if id else None)
    is_approved = voucher and voucher.status == "approved"
    edit_mode = request.args.get("edit") == "1" if (voucher and not is_approved) else False

    if request.method == "POST" and is_approved:
        # Approvals are terminal for the form: without this, a resubmit or a
        # crafted POST fell through to the GET render and the edit vanished
        # with a 200 that looked normal.
        flash("That voucher is approved and cannot be edited. "
              "Unapprove it first to make changes.", "error")
        return redirect(url_for("accounting.voucher_form", id=voucher.id))

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
        label_ids = request.form.getlist("label_id[]")

        def _valid_aid(i):
            """Row i names a real account — the same test the saver below
            applies, so the totals that follow can never count a line the
            saver skips (an amount with no account, or a crafted non-integer
            id, used to inflate the cash/bank total and slip an unbalanced
            voucher into the books)."""
            if i >= len(accounts):
                return False
            aid = accounts[i].strip()
            if not aid:
                return False
            try:
                int(aid)
            except ValueError:
                return False
            return True

        # The company default is assigned automatically to any line saved
        # without an explicit pick. The header label (cash/bank vouchers post
        # it as "label_id"; a JV sends no such field) tags the bank/cash line
        # itself, and item lines inherit it unless an explicit pick overrides.
        default_label = InventorySettings.get().default_voucher_label()
        default_lid = default_label.id if default_label else None

        header_raw = request.form.get("label_id")
        if header_raw and header_raw.strip():
            try:
                _h = scoped_get(ProjectLabel, int(header_raw))
                header_lid = _h.id if _h is not None else default_lid
            except (TypeError, ValueError):
                header_lid = default_lid
        elif header_raw is not None:
            header_lid = default_lid
        else:
            header_lid = None
        voucher.label_id = header_lid

        def _valid_label(lid):
            if lid and str(lid).strip():
                try:
                    label = scoped_get(ProjectLabel, int(lid))
                except (TypeError, ValueError):
                    label = None
                return label.id if label is not None else (header_lid or default_lid)
            return header_lid or default_lid

        has_lines = False
        for i in range(len(accounts)):
            if not _valid_aid(i):
                continue
            aid = accounts[i].strip()
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
            # A negative amount is the other side written with a minus: it
            # printed as a negative debit in every ledger and, on a cash/bank
            # voucher, reduced the money line's total.
            if d < 0 or c < 0:
                db.session.rollback()
                flash(f"Line {i + 1}: amounts cannot be negative — put the amount in the "
                      "other column instead.", "error")
                return redirect(request.url)
            if voucher.voucher_type == "JV" and d > 0 and c > 0:
                db.session.rollback()
                flash(f"Line {i + 1}: a line is either a debit or a credit, not both.", "error")
                return redirect(request.url)
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
            except ValueError:  # unreachable: _valid_aid filtered these rows
                continue
            line = AccountingVoucherLine(
                voucher_id=voucher.id,
                line_no=i + 1,
                account_id=acct_id,
                description=descs[i] if i < len(descs) else "",
                debit=d,
                credit=c,
                label_id=_valid_label(label_ids[i]) if i < len(label_ids) else None,
            )
            db.session.add(line)
            has_lines = True

        _err_ctx = {"accounts": ChartOfAccount.query.filter_by(is_active=True)
                     .order_by(ChartOfAccount.code).all(),
                     "labels": VOUCHER_LABELS, "initial_type": "",
                     "edit_mode": edit_mode,
                     "project_labels": ProjectLabel.query.order_by(ProjectLabel.name).all()}

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
                (_safe_dec(d) for i, d in enumerate(debits)
                 if d.strip() and _valid_aid(i)),
                Decimal("0"),
            ) + sum(
                (_safe_dec(c) for i, c in enumerate(credits)
                 if c.strip() and _valid_aid(i)),
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
                # The header label is the project of the money channel: the
                # bank/cash line carries it, item lines inherit it.
                label_id=voucher.label_id,
            )
            db.session.add(cb_line)

        if voucher.voucher_type == "JV":
            total_d = sum(
                (
                    _safe_dec(d)
                    for i, d in enumerate(debits)
                    if d.strip() and _valid_aid(i)
                ),
                Decimal("0"),
            )
            total_c = sum(
                (
                    _safe_dec(c)
                    for i, c in enumerate(credits)
                    if c.strip() and _valid_aid(i)
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
            # Posting can refuse: closed period, unbalanced dust, a line on an
            # aggregating account. Those used to propagate as a 500 (and burn
            # the pre-allocated voucher number); save as unapproved instead so
            # the draft survives and the reason is shown. The savepoint keeps
            # a mid-write failure from taking the voucher and its lines down
            # with the journal.
            try:
                with db.session.begin_nested():
                    errors = _approve_voucher(voucher)
            except Exception as exc:  # noqa: BLE001 — any posting refusal
                errors = (str(exc) or "Could not approve the voucher.")
            if errors:
                # Commit the draft: rendering without a commit let teardown
                # roll it back, so a refused approval threw the typed-in
                # voucher away.
                voucher.status = "unapproved"
                voucher.approved_by = None
                voucher.approved_at = None
                db.session.commit()
                flash(f"{VOUCHER_LABELS[voucher.voucher_type]} "
                      f"{voucher.voucher_number} saved as unapproved — "
                      f"could not approve: {errors}", "error")
                return redirect(url_for("accounting.voucher_form",
                                        id=voucher.id))

        # Only a literal "approve" approves — and that path already ran
        # _approve_voucher (journal posting + approved_by/at) or bailed with
        # errors above. Any other action value, including a crafted one,
        # saves the voucher unapproved, so an approved-but-never-posted
        # voucher is impossible.
        voucher.status = "approved" if action == "approve" else "unapproved"
        db.session.commit()
        # Editing a voucher can move money out from under an assignment that
        # was already made against it. The tracker derives everything else
        # live; this refreshes the paid_amount cached on those invoices so the
        # invoice list agrees with it. Writes no journal.
        _reconcile_tracking(voucher.id)
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
        project_labels=ProjectLabel.query.order_by(ProjectLabel.name).all(),
        active_labels=ProjectLabel.query.filter_by(is_active=True).order_by(ProjectLabel.name).all(),
        default_label=InventorySettings.get().default_voucher_label(),
        per_line_labeling=InventorySettings.get().per_line_labeling_voucher,
    )


def _approve_voucher(v):
    lines = []
    for line in v.lines.all():
        lines.append({
            "account_id": line.account_id,
            "debit": float(line.debit),
            "credit": float(line.credit),
            "description": line.description,
            "label_id": line.label_id,
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
    # Settings → Rights: only the types this person may view.
    from shared.rights_guard import viewable_voucher_types
    q = q.filter(AccountingVoucher.voucher_type.in_(viewable_voucher_types()))

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
        show_labels=False,
    )


@acct_bp.route("/vouchers/<int:id>/approve", methods=["POST"])
@login_required
def approve_voucher(id):
    v = scoped_get_404(AccountingVoucher, id, for_update=True)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "approve"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status == "approved":
        flash("Already approved.", "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    try:
        err = _approve_voucher(v)
    except Exception as exc:  # noqa: BLE001 — closed period etc. must flash
        db.session.rollback()
        flash(f"Could not approve: {exc}", "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    if err:
        flash(err, "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    v.status = "approved"
    db.session.commit()
    _reconcile_tracking(v.id)
    flash(f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number} approved.", "success")
    return redirect(url_for("accounting.voucher_form", id=v.id))


@acct_bp.route("/vouchers/<int:id>/unapprove", methods=["POST"])
@login_required
def unapprove_voucher(id):
    v = scoped_get_404(AccountingVoucher, id, for_update=True)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "approve"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status != "approved":
        flash("Voucher is not approved.", "error")
        return redirect(url_for("accounting.voucher_list"))
    try:
        reverse_journal_entry(v.voucher_type, v.id, created_by=current_user.id)
    except Exception as exc:  # noqa: BLE001 — closed period must flash
        db.session.rollback()
        flash(f"Could not unapprove: {exc}", "error")
        return redirect(url_for("accounting.voucher_form", id=v.id))
    v.status = "unapproved"
    v.approved_by = None
    v.approved_at = None
    db.session.commit()
    # Unapproved money settles nothing: any invoice this voucher was assigned
    # to drops back to unpaid or partially paid.
    _reconcile_tracking(v.id)
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
        acct = line.account
        rows.append([i, acct.code if acct else "", acct.name if acct else "",
                     line.description or "",
                     float(line.debit) or 0, float(line.credit) or 0])
    rows.append(["", "", "", "Total", total_debit, total_credit])

    from shared import report_export as rx
    title = f"{VOUCHER_LABELS[v.voucher_type]} {v.voucher_number}"
    when = v.voucher_date.strftime("%d %b %Y") if v.voucher_date else ""
    status = "Approved" if getattr(v, "status", "") == "approved" else \
        (getattr(v, "status", "") or "").title()
    period = " · ".join(x for x in (when, status) if x)
    narration = (v.notes or "").strip()
    return rx.export_table(fmt, title, headers, rows, period=period,
                           filters=[narration] if narration else None,
                           row_kinds=["plain"] * (len(rows) - 1) + ["grand"],
                           col_formats={0: "0"}, mono_col=1,
                           file_period=None)

def _voucher_narration_lines(v, vlines):
    """Register Description from pre-fetched lines (chunk-friendly)."""
    if (v.notes or "").strip():
        return v.notes.strip()
    ordered = sorted(vlines, key=lambda l: (l.line_no or 0))
    line = next((l for l in ordered if (l.line_no or 0) != 0
                 and (l.description or "").strip()), None)
    if line is None:
        line = next((l for l in ordered if (l.description or "").strip()),
                    None)
    if line is not None:
        return line.description.strip()
    for l in ordered:
        if l.account is not None:
            return f"{l.account.code} {l.account.name}"
    return "-"


def _voucher_narration(v):
    """The register's Description: what the voucher is actually about.

    The voucher's own notes first. Otherwise the first user-entered line —
    never the auto-generated cash/bank line (line_no=0, "Cash Payment
    Voucher - Payment"), which describes the money channel rather than the
    transaction and is what made every register line read the same.
    """
    vlines = v.lines.order_by(AccountingVoucherLine.line_no).all()
    return _voucher_narration_lines(v, vlines)


@acct_bp.route("/registers/vouchers")
@login_required
def voucher_register():
    """Bulk voucher book: filter by month / financial year / custom range.

    Opening the page loads nothing — the register is fetched only when View
    is pressed (chunked JSON with progress + cancel), or server-rendered
    when ``view=1`` (no-JS fallback). Export/print use the same filters.
    """
    from shared.registers import resolve_register_filter
    vtype = (request.args.get("vtype") or "").strip().upper()
    if vtype not in VOUCHER_LABELS:
        vtype = ""
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    rows, total_dr, total_cr = [], 0.0, 0.0
    loaded = request.args.get("view") == "1"
    if loaded:
        rows, total_dr, total_cr, _total = _voucher_register_rows(
            vtype, status, from_date, to_date)
    return render_template(
        "accounting/voucher_register.html", rows=rows,
        total_debit=total_dr, total_credit=total_cr,
        vtype=vtype, status=status, labels=VOUCHER_LABELS,
        from_date=from_date, to_date=to_date, f=fctx,
        periods=fctx["periods"], loaded=loaded,
        title="Voucher Register",
    )


def _voucher_register_query(vtype, status, from_date, to_date):
    from shared.registers import apply_date_filter
    # Same exclusion as the voucher list: auto-generated reversals are not
    # vouchers anyone raised, and counting them doubles every reversed entry.
    query = AccountingVoucher.query.filter(
        ~AccountingVoucher.voucher_number.like("%-REV"))
    # Settings → Rights: only the types this person may view.
    from shared.rights_guard import viewable_voucher_types
    query = query.filter(AccountingVoucher.voucher_type.in_(viewable_voucher_types()))
    if vtype:
        query = query.filter_by(voucher_type=vtype)
    if status:
        query = query.filter_by(status=status)
    return apply_date_filter(query, AccountingVoucher.voucher_date,
                             from_date, to_date)


def _voucher_register_rows(vtype, status, from_date, to_date,
                           limit=None, offset=0):
    """(rows, total_debit, total_credit, total_count) for the register.

    Totals and the count always cover the whole filtered set; ``rows`` is
    the requested chunk (or everything when ``limit`` is None).
    """
    from sqlalchemy import func
    base = _voucher_register_query(vtype, status, from_date, to_date)
    total_count = base.count()
    # Totals over exactly the rows the register lists — the same filtered
    # query, not a re-statement of its filters that can drift from it.
    sdr, scr = (db.session.query(
        func.coalesce(func.sum(AccountingVoucherLine.debit), 0),
        func.coalesce(func.sum(AccountingVoucherLine.credit), 0))
        .filter(AccountingVoucherLine.voucher_id.in_(
            base.with_entities(AccountingVoucher.id)))
        .first()) or (0, 0)
    total_dr, total_cr = float(sdr or 0), float(scr or 0)
    ordered = base.order_by(AccountingVoucher.voucher_date.asc(),
                            AccountingVoucher.id.asc())
    if limit is not None:
        ordered = ordered.offset(offset).limit(limit)
    vouchers = ordered.all()
    line_map = {}
    if vouchers:
        ids = [v.id for v in vouchers]
        for ln in AccountingVoucherLine.query.filter(
                AccountingVoucherLine.voucher_id.in_(ids)).all():
            line_map.setdefault(ln.voucher_id, []).append(ln)
    rows = []
    for v in vouchers:
        vlines = line_map.get(v.id, [])
        dr = sum(float(l.debit or 0) for l in vlines)
        cr = sum(float(l.credit or 0) for l in vlines)
        rows.append({
            "id": v.id,
            "date": v.voucher_date,
            "number": v.voucher_number,
            "description": _voucher_narration_lines(
                v, sorted(vlines, key=lambda l: l.line_no)),
            "debit": dr,
            "credit": cr,
        })
    return rows, total_dr, total_cr, total_count


@acct_bp.route("/registers/vouchers/export")
@login_required
def voucher_register_export():
    from shared.registers import resolve_register_filter
    from shared import report_export as rx
    fmt = (request.args.get("fmt") or "pdf").strip().lower()
    if fmt not in ("excel", "pdf", "csv"):
        abort(404)
    vtype = (request.args.get("vtype") or "").strip().upper()
    if vtype not in VOUCHER_LABELS:
        vtype = ""
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    vouchers = (_voucher_register_query(vtype, status, from_date, to_date)
                .order_by(AccountingVoucher.voucher_date.asc(),
                          AccountingVoucher.id.asc()).all())
    headers = ["Date", "Voucher #", "Description", "Debit", "Credit"]
    data, kinds = [], []
    total_dr, total_cr = 0.0, 0.0
    for v in vouchers:
        dr = round(sum(float(l.debit or 0) for l in v.lines), 2)
        cr = round(sum(float(l.credit or 0) for l in v.lines), 2)
        total_dr += dr
        total_cr += cr
        data.append([v.voucher_date, v.voucher_number, _voucher_narration(v),
                     dr, cr])
        kinds.append("account")
    data.append(["", "", "TOTAL", round(total_dr, 2), round(total_cr, 2)])
    kinds.append("grand")
    subtitle = fctx["label"]
    if vtype:
        subtitle += f" · {VOUCHER_LABELS[vtype]}"
    if status:
        subtitle += f" · {status.title()}"
    return rx.export_table(fmt, "Voucher Register", headers, data,
                           period=subtitle, row_kinds=kinds, mono_col=1,
                           filters=[f"{len(vouchers)} vouchers"],
                           file_period=fctx["label"])


@acct_bp.route("/registers/vouchers/data")
@login_required
def voucher_register_data():
    """One chunk of the register as JSON.

    Table mode (default): {total, rows, debit, credit, debit_label,
    credit_label, html} — totals cover the whole filtered set, ``html`` is
    the table body for this chunk through the shared partial.
    Full mode (``full=1``): {total, rows, docs} — complete print-ready
    voucher documents for the bulk print page. Each chunk is an independent
    GET, so cancelling is just dropping the loop.
    """
    vtype = (request.args.get("vtype") or "").strip().upper()
    if vtype not in VOUCHER_LABELS:
        vtype = ""
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    full = request.args.get("full") == "1"
    try:
        limit = max(1, min(int(request.args.get("limit", 200)),
                           20 if full else 500))
    except (TypeError, ValueError):
        limit = 20 if full else 200
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    from shared.registers import resolve_register_filter
    from_date, to_date, _fctx = resolve_register_filter(request.args)
    rows, total_dr, total_cr, total = _voucher_register_rows(
        vtype, status, from_date, to_date, limit=limit, offset=offset)
    if full:
        docs = []
        for r in rows:
            v = scoped_get(AccountingVoucher, r["id"])
            if v is None:
                continue
            lines = v.lines.order_by(AccountingVoucherLine.line_no).all()
            docs.append(render_template(
                "accounting/_voucher_doc.html", voucher=v, lines=lines,
                total_debit=sum(float(l.debit or 0) for l in lines),
                total_credit=sum(float(l.credit or 0) for l in lines),
                labels=VOUCHER_LABELS))
        return jsonify({"total": total, "rows": len(rows),
                        "offset": offset, "limit": limit, "docs": docs})
    html = render_template("accounting/_voucher_table_body.html",
                           rows=rows)
    tfoot = render_template("accounting/_voucher_table_totals.html",
                            total_debit=total_dr, total_credit=total_cr)
    from shared.formatting import format_amount
    return jsonify({"total": total, "rows": len(rows),
                    "debit": total_dr, "credit": total_cr,
                    "debit_label": format_amount(total_dr, 0),
                    "credit_label": format_amount(total_cr, 0),
                    "offset": offset, "limit": limit, "html": html,
                    "tfoot": tfoot})


@acct_bp.route("/registers/vouchers/documents")
@login_required
def voucher_register_documents():
    """Bulk print: every filtered voucher as a full document, one per page.

    Each voucher renders through the same partial as the single-voucher
    preview, so the bulk print and the individual print can never diverge.
    Print All uses the browser (PDF via print-to-PDF); Excel comes from the
    register export carrying the same filters.
    """
    from shared.registers import resolve_register_filter
    vtype = (request.args.get("vtype") or "").strip().upper()
    if vtype not in VOUCHER_LABELS:
        vtype = ""
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    docs = None
    if request.args.get("view") == "1":
        # No-JS fallback: render every document server-side. Without it the
        # page fetches chunks itself, so nothing is loaded here.
        vouchers = (_voucher_register_query(vtype, status, from_date, to_date)
                    .order_by(AccountingVoucher.voucher_date.asc(),
                              AccountingVoucher.id.asc()).all())
        docs = []
        for v in vouchers:
            lines = v.lines.order_by(AccountingVoucherLine.line_no).all()
            docs.append({
                "voucher": v,
                "lines": lines,
                "total_debit": sum(float(l.debit or 0) for l in lines),
                "total_credit": sum(float(l.credit or 0) for l in lines),
            })
    label = fctx["label"]
    if vtype:
        label += f" · {VOUCHER_LABELS[vtype]}"
    if status:
        label += f" · {status.title()}"
    return render_template(
        "accounting/voucher_documents.html", docs=docs, label=label,
        labels=VOUCHER_LABELS, f=fctx,
    )


@acct_bp.route("/vouchers/<int:id>/delete", methods=["POST"])
@login_required
def delete_voucher(id):
    v = scoped_get_404(AccountingVoucher, id, for_update=True)
    if deny_page(VOUCHER_SECTION.get(v.voucher_type, "journal_vouchers"), "delete"):
        return redirect(url_for("accounting.voucher_list"))
    if v.status == "approved":
        flash("Cannot delete an approved voucher. Unapprove it first.", "error")
        return redirect(url_for("accounting.voucher_list"))
    # Assignments against a voucher that no longer exists are meaningless, and
    # they carry a foreign key to it — drop them first, then refresh the
    # invoices they used to settle.
    _drop_tracking_for_voucher(v.id)
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


@acct_bp.route("/gl-entry")
@login_required
def gl_entry():
    """The journal(s) a document posted, for the floating "Accounting entry"
    panel on every form that hits the general ledger (shared/gl_entry.py)."""
    from shared.gl_entry import entries_for
    vtype = (request.args.get("type") or "").strip()
    vid = request.args.get("id", type=int)
    if not vtype or len(vtype) > 20:
        return jsonify({"ok": False, "error": "Unknown document type"}), 400
    if not vid:
        return jsonify({"ok": True, "posted": [], "history": [], "saved": False})
    also = []
    for part in (request.args.get("also") or "").split(","):
        t, _, i = part.strip().rpartition(":")
        if t and i.isdigit() and len(t) <= 20:
            also.append((t.upper(), int(i)))
    data = entries_for(vtype, vid, also=also[:200])
    return jsonify({"ok": True, "saved": True,
                    "ledger_url": url_for("finance.ledger"), **data})


@acct_bp.route("/api/account-balance")
@login_required
def api_account_balance():
    """Posted balance of one account (debit minus credit), for the voucher
    form's "balance now → after this voucher" line under the cash/bank
    account."""
    acc_id = request.args.get("id", type=int)
    acc = scoped_get(ChartOfAccount, acc_id) if acc_id else None
    if acc is None:
        return jsonify({"ok": False}), 404
    from shared.models.gl_balance import GLDailyBalance as G
    dr, cr = (db.session.query(
        db.func.coalesce(db.func.sum(G.debit), 0),
        db.func.coalesce(db.func.sum(G.credit), 0))
        .filter(G.account_id == acc.id).one())
    return jsonify({"ok": True, "id": acc.id, "code": acc.code, "name": acc.name,
                    "balance": float(Decimal(str(dr)) - Decimal(str(cr)))})
