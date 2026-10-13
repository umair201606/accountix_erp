"""The cash side of payroll in the general ledger.

The payroll run posts the salary expense and what is owed (Salary Payable,
tax, PF, EOBI, loan recoveries). What used to stop there and leave the books
waiting for a manual voucher:

* paying the salaries        Dr Salary Payable          Cr Cash/Bank
* paying out a staff loan    Dr Employee loan account   Cr Cash/Bank
* PF withdrawal / PF loan /  Dr PF Payable              Cr Cash/Bank
  final PF settlement
* PF profit credited         Dr PF Profit Expense       Cr PF Payable

Without them a recovered loan credited a receivable that was never debited,
and every PF withdrawal or settlement moved the members' PF ledger while PF
Payable in the GL stayed put. Each posting is keyed (voucher type, document
id) so un-doing it is one ``reverse_journal_entry``.
"""
from shared.extensions import db
from shared.ledger_utils import (create_entity_account, get_or_create_account,
                                 post_journal_entry, reverse_journal_entry)


def salary_payable():
    return get_or_create_account("2121", "Salary Payable", "liability", parent_code="212")


def pf_payable():
    return get_or_create_account("2123", "PF Payable", "liability", parent_code="212")


def pf_profit_expense():
    return get_or_create_account("5126", "PF Profit Credited to Members", "expense",
                                 parent_code="512")


def loan_account(loan):
    return create_entity_account(
        "loan", loan.id, f"{loan.request_type.title()} #{loan.id} - {loan.user.full_name}")


def record_salary_payment(run, account, when, user_id):
    """Pay a run's net salaries from ``account`` on ``when``."""
    from datetime import datetime
    if run.paid_at:
        raise ValueError("This payroll has already been paid.")
    net = round(float(run.total_net or 0), 2)
    if net <= 0:
        raise ValueError("Nothing to pay on this payroll.")
    post_journal_entry(
        voucher_type="PRL-PAY", voucher_id=run.id,
        voucher_number=f"PRL-PAY-{run.year}{run.month:02d}",
        description=f"Salaries paid {run.month}/{run.year}",
        entry_date=when, created_by=user_id,
        lines=[{"account_id": salary_payable().id, "debit": net, "credit": 0,
                "description": f"Net salaries {run.month}/{run.year}"},
               {"account_id": account.id, "debit": 0, "credit": net,
                "description": f"Salaries paid {run.month}/{run.year}"}])
    run.paid_at = datetime.utcnow()
    run.status = "paid"


def undo_salary_payment(run):
    reverse_journal_entry("PRL-PAY", run.id)
    run.paid_at = None
    run.status = "completed"


def disburse_loan(loan, account, when, user_id):
    """Pay an approved staff loan / advance out of ``account``."""
    from datetime import datetime, time
    if loan.status != "approved":
        raise ValueError("Only an approved loan can be paid out.")
    if loan.disbursed_at:
        raise ValueError("This loan has already been paid out.")
    amount = round(float(loan.amount or 0), 2)
    post_journal_entry(
        voucher_type="LOAN-DSB", voucher_id=loan.id,
        voucher_number=f"LOAN-DSB-{loan.id}",
        description=f"{loan.request_type.title()} paid to {loan.user.full_name}",
        entry_date=when, created_by=user_id,
        lines=[{"account_id": loan_account(loan).id, "debit": amount, "credit": 0,
                "description": f"{loan.request_type.title()} #{loan.id}"},
               {"account_id": account.id, "debit": 0, "credit": amount,
                "description": f"Paid to {loan.user.full_name}"}])
    loan.disbursed_at = datetime.combine(when, time(12, 0))


def undo_disbursement(loan):
    from .models.loan import LoanRepayment
    if LoanRepayment.query.filter_by(loan_id=loan.id).first():
        raise ValueError("Instalments have already been recovered on this loan.")
    reverse_journal_entry("LOAN-DSB", loan.id)
    loan.disbursed_at = None


def pf_cash_out(vtype, doc_id, number, amount, account, when, user_id, description):
    """Money paid out of the fund: Dr PF Payable, Cr cash/bank."""
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        return None
    return post_journal_entry(
        voucher_type=vtype, voucher_id=doc_id, voucher_number=number,
        description=description, entry_date=when, created_by=user_id,
        lines=[{"account_id": pf_payable().id, "debit": amount, "credit": 0,
                "description": description},
               {"account_id": account.id, "debit": 0, "credit": amount,
                "description": description}])


def pf_profit(dist, when, user_id):
    amount = round(float(dist.total_profit or 0), 2)
    return post_journal_entry(
        voucher_type="PF-PROFIT", voucher_id=dist.id,
        voucher_number=f"PF-PROFIT-{dist.year}{dist.month:02d}-{dist.id}",
        description=f"PF profit credited {dist.month}/{dist.year}",
        entry_date=when, created_by=user_id,
        lines=[{"account_id": pf_profit_expense().id, "debit": amount, "credit": 0,
                "description": f"PF profit {dist.month}/{dist.year}"},
               {"account_id": pf_payable().id, "debit": 0, "credit": amount,
                "description": "Profit credited to members"}])
