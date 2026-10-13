"""Property, plant & equipment movement schedule (the IAS 16 note).

For a period, per category:

    Cost:          opening + additions - disposals = closing
    Depreciation:  opening + charge    - disposals = closing
    Net book value opening / closing

Built from the asset subledger, not the GL: additions are acquisitions and
capitalisations dated in the period, disposals are assets derecognised in it
(sold / scrapped through FA-DISP, or moved back to stock through FA-TRF), and
depreciation is the live charges by date. Every figure therefore traces to
an asset, and the closing columns agree with the register as at the end date.
"""
from collections import OrderedDict

from shared.extensions import db


def _derecognition_dates(asset_ids):
    """asset id -> date it left the books (disposal or transfer to stock)."""
    from shared.models.ledger import JournalEntry
    from shared.models.asset_transfer import AssetTransfer
    out = {}
    if not asset_ids:
        return out
    for vid, d in (db.session.query(JournalEntry.voucher_id, JournalEntry.entry_date)
                   .filter(JournalEntry.voucher_type == "FA-DISP",
                           JournalEntry.is_posted == True,
                           JournalEntry.voucher_id.in_(asset_ids))):
        out[vid] = d.date() if hasattr(d, "date") else d
    for aid, d in (db.session.query(AssetTransfer.asset_id, AssetTransfer.transfer_date)
                   .filter(AssetTransfer.direction == "to_inventory",
                           AssetTransfer.status == "approved",
                           AssetTransfer.asset_id.in_(asset_ids))):
        if d:
            out[aid] = d
    return out


def _live_charges(asset_ids):
    """asset id -> [(date, amount)] of depreciation still backed by a posted journal."""
    from shared.models.ledger import JournalEntry
    from .models.asset import AssetDepreciation
    out = {}
    if not asset_ids:
        return out
    rows = (db.session.query(AssetDepreciation.asset_id, AssetDepreciation.entry_date,
                             AssetDepreciation.amount)
            .outerjoin(JournalEntry, AssetDepreciation.journal_entry_id == JournalEntry.id)
            .filter(AssetDepreciation.asset_id.in_(asset_ids))
            .filter(db.or_(AssetDepreciation.journal_entry_id.is_(None),
                           JournalEntry.is_posted == True)))
    for aid, d, amt in rows:
        out.setdefault(aid, []).append((d, float(amt or 0)))
    return out


COLUMNS = ("cost_open", "additions", "cost_disposals", "cost_close",
           "dep_open", "charge", "dep_disposals", "dep_close", "nbv_open", "nbv_close")


def movement(date_from, date_to):
    """{"rows": [{category, <COLUMNS>}], "total": {<COLUMNS>}, "assets": [...]}."""
    from .models.asset import FixedAsset, AssetCategory
    assets = (FixedAsset.query
              .filter(FixedAsset.status.notin_(("draft", "cancelled")))
              .filter(FixedAsset.purchase_cost > 0)
              .all())
    ids = [a.id for a in assets]
    gone = _derecognition_dates(ids)
    charges = _live_charges(ids)
    cats = {c.id: c.name for c in AssetCategory.query.all()}
    by_cat = OrderedDict()
    per_asset = []
    for a in sorted(assets, key=lambda x: (cats.get(x.category_id, ""), x.asset_code)):
        cost = float(a.purchase_cost)
        acq = a.purchase_date
        out_d = gone.get(a.id)
        if acq is None or acq > date_to:
            continue
        held_open = acq < date_from and not (out_d and out_d < date_from)
        if not held_open and acq < date_from:
            continue              # left the books before the period
        dep = charges.get(a.id, [])
        before = sum(v for d, v in dep if d < date_from)
        during = sum(v for d, v in dep if date_from <= d <= date_to)
        left_in_period = bool(out_d and date_from <= out_d <= date_to)
        r = {
            "cost_open": cost if held_open else 0.0,
            "additions": cost if acq >= date_from else 0.0,
            "cost_disposals": cost if left_in_period else 0.0,
            "dep_open": before if held_open else 0.0,
            "charge": during,
            "dep_disposals": (before + during) if left_in_period else 0.0,
        }
        r["cost_close"] = r["cost_open"] + r["additions"] - r["cost_disposals"]
        r["dep_close"] = r["dep_open"] + r["charge"] - r["dep_disposals"]
        r["nbv_open"] = r["cost_open"] - r["dep_open"]
        r["nbv_close"] = r["cost_close"] - r["dep_close"]
        name = cats.get(a.category_id, "Uncategorised")
        agg = by_cat.setdefault(name, {k: 0.0 for k in COLUMNS})
        for k in COLUMNS:
            agg[k] += r[k]
        per_asset.append(dict(r, asset=a, category=name))
    rows = [dict(v, category=k) for k, v in by_cat.items()]
    total = {k: round(sum(r[k] for r in rows), 2) for k in COLUMNS}
    for r in rows:
        for k in COLUMNS:
            r[k] = round(r[k], 2)
    return {"rows": rows, "total": total, "assets": per_asset}
