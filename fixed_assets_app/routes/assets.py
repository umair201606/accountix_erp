from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.models.ledger import ChartOfAccount
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, create_fixed_asset_accounts)
from ..models.asset import FixedAsset, AssetCategory, AssetDepreciation
from .depreciation import post_asset_depreciation
from shared.forms import form_date, form_float, form_int, FormInputError, REQUIRED


def account_choices():
    """The accounts each mapping may point at. Every field used to list the
    whole chart, so an asset could be "booked" to a revenue or expense
    account and the asset register stopped agreeing with the ledger."""
    from shared.ledger_utils import accounts_under, cash_bank_accounts
    cash = cash_bank_accounts()
    return {
        "fa": accounts_under("1-02-01") + accounts_under("1-02-03"),
        "accum": accounts_under("1-02-02"),
        "expense": ChartOfAccount.query.filter(
            ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
            ChartOfAccount.is_active == True,  # noqa: E712
            ChartOfAccount.type == "expense").order_by(ChartOfAccount.code).all(),
        "credit": accounts_under("2-") + cash + accounts_under("1-01-03-02"),
        "proceeds": cash + accounts_under("1-01-02") + accounts_under("1-01-03"),
    }


def _mapped(field, allowed, current=None, label="account"):
    """The account id posted in ``field`` if it is one of ``allowed``; blank
    keeps ``current``; anything else is refused."""
    aid = request.form.get(field, type=int)
    if not aid:
        return current
    if aid not in {a.id for a in allowed}:
        raise FormInputError(f"That {label} cannot be used here.")
    return aid


def _read_terms(category):
    """Cost, life, salvage and method off the form, checked the way an
    auditor would: a zero-cost or zero-life asset, or salvage at or above
    cost, produced nonsense charges (or none) for years."""
    if category is None:
        raise FormInputError("Choose a category.")
    cost = form_float("purchase_cost", 0)
    life = form_int("useful_life", category.default_useful_life or 5)
    salvage = form_float("salvage_value", 0)
    method = request.form.get("depreciation_method") or category.default_depreciation_method
    if cost <= 0:
        raise FormInputError("Purchase cost must be more than zero.")
    if not 1 <= life <= 100:
        raise FormInputError("Useful life must be between 1 and 100 years.")
    if salvage < 0 or salvage >= cost:
        raise FormInputError("Salvage value must be zero or more and less than the cost.")
    if method not in FixedAsset.METHODS:
        raise FormInputError("Choose straight line or reducing balance.")
    return round(cost, 2), life, round(salvage, 2), method


def post_acquisition(asset, created_by, credit_account_id=None):
    """Book the asset into the GL: Dr fixed asset, Cr whatever paid for it.

    Without this the subledger carried the cost and the GL carried nothing, so
    depreciation and disposal drove the fixed-asset account negative.

    Keyed ``voucher_id=asset.id`` and skipped when a posted FA-ACQ already
    exists, so re-running it cannot double-book.
    """
    from shared.models.ledger import JournalEntry
    if not asset.purchase_cost:
        return None
    existing = JournalEntry.query.filter_by(
        voucher_type="FA-ACQ", voucher_id=asset.id, is_posted=True).first()
    if existing:
        return existing
    if not asset.fixed_asset_account_id:
        fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = fa_acct.id
        db.session.flush()
    credit_id = (credit_account_id or asset.acquisition_credit_account_id
                 or posting_account("ap").id)
    asset.acquisition_credit_account_id = credit_id
    return post_journal_entry(
        voucher_type="FA-ACQ", voucher_id=asset.id,
        voucher_number=f"FA-ACQ-{asset.asset_code}",
        description=f"Acquisition of {asset.name}",
        entry_date=asset.purchase_date, created_by=created_by,
        lines=[
            {"account_id": asset.fixed_asset_account_id,
             "debit": asset.purchase_cost, "credit": 0,
             "label_id": getattr(asset, "label_id", None),
             "description": f"Fixed asset - {asset.name}"},
            {"account_id": credit_id, "debit": 0, "credit": asset.purchase_cost,
             "description": f"Acquisition of {asset.name}"},
        ],
    )

def _capitalised_from_stock(asset):
    """True when the asset entered the books through a stock capitalisation
    (FA-CAP), not an acquisition (FA-ACQ)."""
    from shared.models.asset_transfer import AssetTransfer
    return AssetTransfer.query.filter_by(asset_id=asset.id, direction="to_fixed_asset",
                                         status="approved").first() is not None


fa_assets_bp = Blueprint("fa_assets", __name__, url_prefix="/fixed-assets/assets")


@fa_assets_bp.route("/")
@login_required
def list_assets():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    category_filter = request.args.get("category_id", "")
    query = FixedAsset.query.filter_by(is_active=True)
    if q:
        query = query.filter(FixedAsset.name.ilike(f"%{q}%") | FixedAsset.asset_code.ilike(f"%{q}%"))
    if status_filter:
        query = query.filter_by(status=status_filter)
    if category_filter:
        query = query.filter_by(category_id=int(category_filter))
    assets = query.order_by(FixedAsset.created_at.desc()).all()
    categories = AssetCategory.query.filter_by(is_active=True).all()
    return render_template("fixed_assets/assets/index.html",
                           assets=assets, categories=categories,
                           q=q, status_filter=status_filter, category_filter=category_filter)


@fa_assets_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_asset():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    categories = AssetCategory.query.filter_by(is_active=True).all()
    choices = account_choices()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Asset name is required.", "error")
            return render_template("fixed_assets/assets/form.html", asset=None, categories=categories, choices=choices)
        category_id = form_int("category_id", 0)
        category = scoped_get(AssetCategory, category_id)
        purchase_date = form_date("purchase_date", REQUIRED, label="Purchase date")
        purchase_cost, useful_life, salvage_value, method = _read_terms(category)
        choices = account_choices()
        fa_acct_id = (_mapped("fixed_asset_account_id", choices["fa"], label="fixed-asset account")
                      or posting_account("fixed_assets").id)
        asset = FixedAsset(
            asset_code=FixedAsset.next_code(),
            name=name,
            description=request.form.get("description", ""),
            category_id=category_id,
            purchase_date=purchase_date,
            purchase_cost=purchase_cost,
            useful_life=useful_life,
            depreciation_method=method,
            salvage_value=salvage_value,
            current_book_value=purchase_cost,
            status="active",
            location=request.form.get("location", ""),
            assigned_to=request.form.get("assigned_to", ""),
            vendor=request.form.get("vendor", ""),
            serial_number=request.form.get("serial_number", ""),
            label_id=request.form.get("label_id", type=int),
            fixed_asset_account_id=fa_acct_id,
            accum_dep_account_id=_mapped("accum_dep_account_id", choices["accum"],
                                         label="accumulated depreciation account"),
            dep_expense_account_id=_mapped("dep_expense_account_id", choices["expense"],
                                           label="expense account"),
            notes=request.form.get("notes", ""),
            acquisition_credit_account_id=_mapped("acquisition_credit_account_id",
                                                  choices["credit"], label="credit account"),
        )
        db.session.add(asset)
        db.session.flush()
        try:
            post_acquisition(asset, current_user.id)
        except Exception as e:
            db.session.rollback()
            flash(f"Asset not created — acquisition entry failed: {e}", "error")
            return render_template("fixed_assets/assets/form.html", asset=None,
                                   categories=categories, choices=choices)
        asset.recalculate()
        db.session.commit()
        flash(f"Asset '{name}' created and booked to the ledger.", "success")
        return redirect(url_for("fa_assets.list_assets"))
    return render_template("fixed_assets/assets/form.html", asset=None, categories=categories, choices=choices)


@fa_assets_bp.route("/<int:asset_id>")
@login_required
def view_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = scoped_get_404(FixedAsset, asset_id)
    # Fresh query, not the relationship: its own order_by is appended to, so
    # .desc() through it silently yields ascending order.
    depreciation_entries = (AssetDepreciation.query
                            .filter_by(asset_id=asset.id)
                            .order_by(AssetDepreciation.entry_date.desc(),
                                      AssetDepreciation.id.desc()).all())
    choices = account_choices()
    from .depreciation import depreciation_schedule
    from shared.models.asset_transfer import AssetTransfer
    # Every journal that touched this asset, for the "Accounting entry" panel:
    # its depreciation, its disposal, and the capitalisation / transfer
    # vouchers (keyed by transfer id, so they were missing before).
    gl_also = [f"FA-DEP:{e.id}" for e in depreciation_entries] + [f"FA-DISP:{asset.id}"]
    for t in AssetTransfer.query.filter_by(asset_id=asset.id).all():
        gl_also.append(f"{'FA-CAP' if t.direction != 'to_inventory' else 'FA-TRF'}:{t.id}")
    return render_template("fixed_assets/assets/view.html", asset=asset, gl_also=gl_also,
                           depreciation_entries=depreciation_entries,
                           choices=choices, schedule=depreciation_schedule(asset))


@fa_assets_bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = scoped_get_404(FixedAsset, asset_id, for_update=request.method == "POST")
    categories = AssetCategory.query.filter_by(is_active=True).all()
    choices = account_choices()
    if request.method == "POST":
        # Descriptive fields are always editable.
        name = request.form.get("name", "").strip()
        if not name:
            raise FormInputError("Asset name is required.")
        asset.name = name
        asset.description = request.form.get("description", "")
        asset.location = request.form.get("location", "")
        asset.assigned_to = request.form.get("assigned_to", "")
        asset.vendor = request.form.get("vendor", "")
        asset.serial_number = request.form.get("serial_number", "")
        asset.notes = request.form.get("notes", "")
        if asset.status != "active":
            # A disposed / transferred asset is out of the books: restating
            # its cost re-posted the acquisition while the disposal journal
            # still credited the old figure, leaving the asset account adrift.
            # The status itself only changes through Dispose / Undo disposal
            # (the form used to offer "Disposed", which skipped the journal).
            db.session.commit()
            flash("Details updated. Cost, life and accounts are locked once an "
                  "asset is out of service.", "success")
            return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))

        category = scoped_get(AssetCategory, form_int("category_id", 0))
        purchase_date = form_date("purchase_date", REQUIRED, label="Purchase date")
        cost, life, salvage, method = _read_terms(category)
        posted = float(asset.posted_depreciation)
        first_charge = (asset.live_depreciation_query()
                        .order_by(AssetDepreciation.entry_date).first())
        capitalised = _capitalised_from_stock(asset)
        if capitalised and (round(cost, 2) != round(float(asset.purchase_cost), 2)
                            or purchase_date != asset.purchase_date):
            raise FormInputError("This asset was capitalised from stock: its cost "
                                 "and date come from that transfer. Unapprove the "
                                 "transfer to change them.")
        if first_charge and purchase_date > first_charge.entry_date:
            raise FormInputError(f"Depreciation is already posted from "
                                 f"{first_charge.entry_date:%d %b %Y}; the purchase "
                                 f"date cannot be later than that.")
        if posted and cost - salvage < posted - 0.005:
            raise FormInputError(f"{posted:,.2f} of depreciation is already posted; "
                                 f"cost less salvage cannot be below that.")
        choices = account_choices()
        # Blank = automatic: keep the account the asset already uses.
        new_accum = _mapped("accum_dep_account_id", choices["accum"], asset.accum_dep_account_id,
                            label="accumulated depreciation account")
        if posted and new_accum != asset.accum_dep_account_id:
            raise FormInputError("Accumulated depreciation is already posted to this "
                                 "asset's account; it cannot be moved to another.")

        prior = (asset.purchase_cost, asset.purchase_date,
                 asset.acquisition_credit_account_id, asset.fixed_asset_account_id,
                 asset.label_id)
        asset.category_id = category.id
        asset.purchase_date = purchase_date
        asset.purchase_cost = cost
        asset.useful_life = life
        asset.depreciation_method = method
        asset.salvage_value = salvage
        asset.fixed_asset_account_id = _mapped("fixed_asset_account_id", choices["fa"],
                                               asset.fixed_asset_account_id,
                                               label="fixed-asset account")
        asset.accum_dep_account_id = new_accum
        asset.dep_expense_account_id = _mapped("dep_expense_account_id", choices["expense"],
                                               label="expense account")
        asset.label_id = request.form.get("label_id", type=int)
        asset.acquisition_credit_account_id = _mapped(
            "acquisition_credit_account_id", choices["credit"],
            asset.acquisition_credit_account_id, label="credit account")
        # Changes in life / salvage / method are prospective: the engine spreads
        # what is left over the life that is left. Anything the acquisition
        # journal was built from (cost, date, accounts, label) re-posts it, or
        # the GL keeps carrying the old figures — the asset account included,
        # which disposal would later credit.
        after = (asset.purchase_cost, asset.purchase_date,
                 asset.acquisition_credit_account_id, asset.fixed_asset_account_id,
                 asset.label_id)
        if not capitalised and after != prior:
            try:
                reverse_journal_entry("FA-ACQ", asset.id, created_by=current_user.id)
                post_acquisition(asset, current_user.id)
            except Exception as e:
                db.session.rollback()
                flash(f"Could not restate the acquisition entry: {e}", "error")
                return redirect(url_for("fa_assets.edit_asset", asset_id=asset.id))
        asset.recalculate()
        db.session.commit()
        flash("Asset updated successfully.", "success")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    return render_template("fixed_assets/assets/form.html", asset=asset, categories=categories, choices=choices)


@fa_assets_bp.route("/<int:asset_id>/depreciate", methods=["POST"])
@login_required
def record_depreciation(asset_id):
    if not current_user.module_access("fixed_assets"):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    asset = scoped_get_404(FixedAsset, asset_id, for_update=True)
    if asset.status != "active":
        flash("Only an asset in service can be depreciated.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    entry_date = form_date("entry_date")
    amount = form_float("amount", 0)
    last = asset.live_depreciation_query().order_by(
        AssetDepreciation.entry_date.desc()).first()
    if entry_date < asset.purchase_date or (last and entry_date < last.entry_date):
        flash("A manual charge must be dated on or after the purchase date and the "
              "last posted charge.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if amount <= 0:
        flash("Depreciation amount must be positive.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if amount > asset.remaining_depreciable:
        flash(f"Only {asset.remaining_depreciable:,.2f} remains depreciable before "
              f"the salvage value of {asset.salvage_value:,.2f}.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    # One shared implementation: this route used to duplicate the posting logic
    # and drifted from it (same voucher_id for every charge, no journal link).
    impairment = request.form.get("kind") == "impairment"
    expense_id = None
    if impairment:
        # IAS 36: an impairment is its own loss, not depreciation; it reduces
        # the carrying amount through the same contra account.
        from shared.ledger_utils import get_or_create_account
        expense_id = get_or_create_account("5710", "Impairment Loss on Fixed Assets",
                                           "expense").id
    note = (request.form.get("notes") or "").strip()[:150]
    try:
        charged = post_asset_depreciation(asset, entry_date, current_user.id,
                                          amount=amount, expense_account_id=expense_id,
                                          notes=(("Impairment" + (f": {note}" if note else ""))
                                                 if impairment else note or None))
    except Exception as e:
        db.session.rollback()
        flash(f"Depreciation entry failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if not charged:
        flash("Nothing left to depreciate on this asset.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    db.session.commit()
    flash(f"Depreciation of {charged:,.2f} recorded for '{asset.name}'.", "success")
    return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))


@fa_assets_bp.route("/<int:asset_id>/dispose", methods=["POST"])
@login_required
def dispose_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = scoped_get_404(FixedAsset, asset_id, for_update=True)
    if not asset.fixed_asset_account_id:
        fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = fa_acct.id
        db.session.flush()
    if not asset.accum_dep_account_id:
        _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum_acct.id
        db.session.flush()
    if asset.status == "disposed":
        flash("This asset is already disposed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    from shared.posting_helpers import parse_doc_date
    disposal_date = parse_doc_date(request.form.get("disposal_date")).date()
    if asset.purchase_date and disposal_date < asset.purchase_date:
        flash("Disposal date cannot be before the purchase date.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if asset.status != "active":
        flash("Only an asset in service can be disposed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    later = (asset.live_depreciation_query()
             .filter(AssetDepreciation.entry_date > disposal_date).count())
    if later:
        # Charges dated after the disposal would be written off with it and
        # leave depreciation in P&L for months the asset was no longer owned.
        flash("Depreciation is posted after that date. Reverse those charges, or "
              "dispose on or after the last one.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    # Depreciate up to the disposal date first: the asset was in use until
    # then, so that charge belongs in P&L as depreciation — not hidden inside
    # the gain/loss, which is what disposing at a stale book value did.
    proceeds = form_float("proceeds", 0)
    if proceeds < 0:
        flash("Proceeds cannot be negative.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    post_asset_depreciation(asset, disposal_date, current_user.id)
    db.session.flush()
    fa_acct_id = asset.fixed_asset_account_id
    accum_dep_acct_id = asset.accum_dep_account_id
    purchase_cost = asset.purchase_cost
    # Derived, not the cached column: a reversed depreciation charge must not
    # leave disposal writing off accumulated depreciation the GL no longer holds.
    accum_dep = asset.posted_depreciation
    net_book = purchase_cost - accum_dep

    proceeds_acct_id = (_mapped("proceeds_account_id", account_choices()["proceeds"],
                                label="account for the proceeds")
                        or posting_account("cash").id)

    lines = [
        {"account_id": accum_dep_acct_id, "debit": accum_dep, "credit": 0,
         "description": f"Write-off accumulated depreciation - {asset.name}"},
        {"account_id": fa_acct_id, "debit": 0, "credit": purchase_cost,
         "description": f"Asset disposal - {asset.name}"},
    ]
    if proceeds:
        lines.append(
            {"account_id": proceeds_acct_id, "debit": proceeds, "credit": 0,
             "description": f"Proceeds on disposal - {asset.name}"})
    # Sale price against what the books still carry. Scrapping is just the
    # proceeds = 0 case, so there is no separate code path for it.
    result = round(proceeds - net_book, 2)
    if result:
        gain_loss_acct = posting_account("disposal_gain_loss")
        lines.append({
            "account_id": gain_loss_acct.id,
            "debit": abs(result) if result < 0 else 0,
            "credit": result if result > 0 else 0,
            "description": (f"{'Gain' if result > 0 else 'Loss'} on disposal - "
                            f"{asset.name}"),
        })
    asset.status = "disposed"
    asset.is_active = False
    db.session.flush()
    try:
        post_journal_entry(
            voucher_type="FA-DISP",
            voucher_id=asset.id,
            voucher_number=f"FA-DISP-{asset.asset_code}",
            description=f"Disposal of {asset.name}",
            entry_date=disposal_date,
            created_by=current_user.id,
            lines=lines,
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Disposal entry failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    db.session.commit()
    outcome = ("" if not result else
               f" {'Gain' if result > 0 else 'Loss'} of {abs(result):,.2f} booked.")
    flash(f"Asset '{asset.name}' has been disposed.{outcome}", "success")
    return redirect(url_for("fa_assets.list_assets"))


@fa_assets_bp.route("/<int:asset_id>/undo-disposal", methods=["POST"])
@login_required
def undo_disposal(asset_id):
    """Un-post a disposal and put the asset back into service.

    Disposal was a one-way door: a mis-keyed disposal left the asset written out
    of the ledger with no way back. Un-posting restores the cost and the
    accumulated depreciation together, because both were derecognised by the
    same journal.
    """
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = scoped_get_404(FixedAsset, asset_id, for_update=True)
    if asset.status != "disposed":
        flash("This asset is not disposed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    try:
        reverse_journal_entry("FA-DISP", asset.id, created_by=current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Could not reverse the disposal: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    asset.status = "active"
    asset.is_active = True
    asset.recalculate()
    db.session.commit()
    flash(f"Disposal of '{asset.name}' reversed; book value is back to "
          f"{asset.current_book_value:,.2f}.", "success")
    return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))


