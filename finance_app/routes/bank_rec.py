"""Bank reconciliation (Accounting → Bank Reconciliation).

Attached to the accounting blueprint, so it sits behind the same module
entitlement. See shared/models/bank_rec.py for the model.
"""
from datetime import datetime, time

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from shared.extensions import db
from shared.forms import FormInputError, REQUIRED, form_date, form_float
from shared.models.bank_rec import BankRecItem, BankReconciliation
from shared.models.ledger import JournalEntry, JournalLine
from shared.tenancy import scoped_get_404

from .accounting import acct_bp


def _lines_upto(account_id, upto):
    """Posted journal lines on the account dated up to ``upto`` (inclusive)."""
    end = datetime.combine(upto, time.max)
    return (db.session.query(JournalLine, JournalEntry)
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .filter(JournalLine.account_id == account_id,
                    JournalEntry.is_posted == True,  # noqa: E712
                    JournalEntry.entry_date <= end)
            .order_by(JournalEntry.entry_date, JournalEntry.id, JournalLine.id).all())


def _net(line):
    return round(float(line.debit or 0) - float(line.credit or 0), 2)


def _cleared_elsewhere(rec):
    """Line ids cleared by this account's other COMPLETED reconciliations."""
    return {i for (i,) in db.session.query(BankRecItem.journal_line_id)
            .join(BankReconciliation, BankRecItem.reconciliation_id == BankReconciliation.id)
            .filter(BankReconciliation.account_id == rec.account_id,
                    BankReconciliation.status == "completed",
                    BankReconciliation.id != rec.id)}


def reconcile(rec):
    """Everything the page and the statement show for one reconciliation."""
    rows = _lines_upto(rec.account_id, rec.statement_date)
    before = _cleared_elsewhere(rec)
    mine = {i for (i,) in db.session.query(BankRecItem.journal_line_id)
            .filter(BankRecItem.reconciliation_id == rec.id)}
    book = round(sum(_net(ln) for ln, _ in rows), 2)
    cleared_before = round(sum(_net(ln) for ln, _ in rows if ln.id in before), 2)
    open_rows = [(ln, je) for ln, je in rows if ln.id not in before]
    ticked = [(ln, je) for ln, je in open_rows if ln.id in mine]
    cleared = round(cleared_before + sum(_net(ln) for ln, _ in ticked), 2)
    unticked = [(ln, je) for ln, je in open_rows if ln.id not in mine]
    unpresented = round(sum(-_net(ln) for ln, _ in unticked if _net(ln) < 0), 2)
    in_transit = round(sum(_net(ln) for ln, _ in unticked if _net(ln) > 0), 2)
    statement = round(float(rec.statement_balance or 0), 2)
    return {
        "rows": open_rows, "mine": mine, "book": book, "cleared_before": cleared_before,
        "cleared": cleared, "statement": statement,
        "difference": round(statement - cleared, 2),
        "unpresented": unpresented, "in_transit": in_transit,
        "unticked": unticked,
        # Book balance + payments not yet on the statement - receipts not yet
        # on it = what the bank should show.
        "bank_per_books": round(book + unpresented - in_transit, 2),
    }


@acct_bp.route("/bank-reconciliation")
@login_required
def bank_rec_list():
    from shared.clock import local_today
    from shared.ledger_utils import cash_bank_accounts
    recs = (BankReconciliation.query
            .order_by(BankReconciliation.statement_date.desc(), BankReconciliation.id.desc())
            .all())
    return render_template("accounting/bank_rec_list.html", recs=recs,
                           accounts=cash_bank_accounts(), today=local_today())


@acct_bp.route("/bank-reconciliation/new", methods=["POST"])
@login_required
def bank_rec_new():
    from shared.ledger_utils import cash_bank_account
    account = cash_bank_account(request.form.get("account_id", type=int))
    if account is None:
        raise FormInputError("Choose the bank or cash account to reconcile.")
    when = form_date("statement_date", REQUIRED, label="Statement date")
    balance = form_float("statement_balance", REQUIRED, label="Statement closing balance")
    if BankReconciliation.query.filter_by(account_id=account.id, status="draft").first():
        raise FormInputError(f"{account.name} already has a reconciliation in progress — "
                             "finish or delete it first.")
    last = (BankReconciliation.query.filter_by(account_id=account.id, status="completed")
            .order_by(BankReconciliation.statement_date.desc()).first())
    if last and when <= last.statement_date:
        raise FormInputError(f"{account.name} is reconciled to "
                             f"{last.statement_date:%d %b %Y}; the next statement must be later.")
    rec = BankReconciliation(account_id=account.id, statement_date=when,
                             statement_balance=round(balance, 2), created_by=current_user.id)
    db.session.add(rec)
    db.session.commit()
    return redirect(url_for("accounting.bank_rec_view", id=rec.id))


@acct_bp.route("/bank-reconciliation/<int:id>")
@login_required
def bank_rec_view(id):
    rec = scoped_get_404(BankReconciliation, id)
    data = reconcile(rec)
    fmt = request.args.get("format")
    if fmt:
        return _export_statement(rec, data, fmt)
    return render_template("accounting/bank_rec_view.html", rec=rec, d=data)


@acct_bp.route("/bank-reconciliation/<int:id>/save", methods=["POST"])
@login_required
def bank_rec_save(id):
    rec = scoped_get_404(BankReconciliation, id)
    if rec.status != "draft":
        raise FormInputError("Reopen the reconciliation to change it.")
    if request.form.get("statement_balance") not in (None, ""):
        rec.statement_balance = round(form_float("statement_balance", REQUIRED,
                                                 label="Statement closing balance"), 2)
    allowed = {ln.id for ln, _ in reconcile(rec)["rows"]}
    wanted = {int(i) for i in request.form.getlist("line_ids[]") if str(i).isdigit()} & allowed
    BankRecItem.query.filter_by(reconciliation_id=rec.id).delete()
    for lid in sorted(wanted):
        db.session.add(BankRecItem(reconciliation_id=rec.id, journal_line_id=lid))
    db.session.flush()
    if request.form.get("action") == "complete":
        diff = reconcile(rec)["difference"]
        if abs(diff) >= 0.005:
            db.session.commit()
            flash(f"Saved. Not complete: the cleared balance differs from the statement by "
                  f"{diff:,.2f}.", "warning")
            return redirect(url_for("accounting.bank_rec_view", id=rec.id))
        rec.status = "completed"
        rec.completed_at = datetime.utcnow()
        db.session.commit()
        flash(f"{rec.account.name} reconciled to {rec.statement_date:%d %b %Y}.", "success")
        return redirect(url_for("accounting.bank_rec_view", id=rec.id))
    db.session.commit()
    flash("Reconciliation saved.", "success")
    return redirect(url_for("accounting.bank_rec_view", id=rec.id))


@acct_bp.route("/bank-reconciliation/<int:id>/reopen", methods=["POST"])
@login_required
def bank_rec_reopen(id):
    rec = scoped_get_404(BankReconciliation, id)
    later = (BankReconciliation.query.filter(BankReconciliation.account_id == rec.account_id,
                                             BankReconciliation.statement_date > rec.statement_date)
             .first())
    if rec.status != "completed" or later:
        raise FormInputError("Only the latest completed reconciliation can be reopened.")
    if BankReconciliation.query.filter_by(account_id=rec.account_id, status="draft").first():
        raise FormInputError("Finish or delete the reconciliation in progress first.")
    rec.status = "draft"
    rec.completed_at = None
    db.session.commit()
    flash("Reconciliation reopened.", "success")
    return redirect(url_for("accounting.bank_rec_view", id=rec.id))


@acct_bp.route("/bank-reconciliation/<int:id>/delete", methods=["POST"])
@login_required
def bank_rec_delete(id):
    rec = scoped_get_404(BankReconciliation, id)
    if rec.status != "draft":
        raise FormInputError("Reopen a completed reconciliation before deleting it.")
    db.session.delete(rec)
    db.session.commit()
    flash("Reconciliation deleted.", "success")
    return redirect(url_for("accounting.bank_rec_list"))


def _export_statement(rec, d, fmt):
    """The bank reconciliation statement as Excel / PDF / CSV."""
    from shared import report_export as rx
    rows = [["Balance as per books", "", d["book"]],
            ["Add: payments not yet presented", "", d["unpresented"]]]
    rows += [[f"   {je.voucher_number} {je.entry_date:%d %b %Y}", ln.description or je.description or "",
              -_net(ln)] for ln, je in d["unticked"] if _net(ln) < 0]
    rows.append(["Less: deposits in transit", "", -d["in_transit"]])
    rows += [[f"   {je.voucher_number} {je.entry_date:%d %b %Y}", ln.description or je.description or "",
              -_net(ln)] for ln, je in d["unticked"] if _net(ln) > 0]
    rows.append(["Balance as per bank (books)", "", d["bank_per_books"]])
    rows.append(["Balance as per bank statement", "", d["statement"]])
    rows.append(["Difference", "", round(d["statement"] - d["bank_per_books"], 2)])
    kinds = ["plain"] * (len(rows) - 3) + ["total", "total", "grand"]
    return rx.export_table(fmt, "Bank Reconciliation Statement", ["", "Details", "Amount"], rows,
                           period=f"{rec.account.code} {rec.account.name} — as at "
                                  f"{rec.statement_date:%d %b %Y}",
                           row_kinds=kinds, file_period=rec.statement_date)
