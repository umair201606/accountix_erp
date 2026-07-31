from collections import defaultdict
from decimal import Decimal
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.tenancy import scoped_get
from shared.models.stock_ledger import StockLedger
from ..models.product import InvProduct
from ..models.stock_movement import InvStockMovement

inv_reports_bp = Blueprint("inv_reports", __name__, url_prefix="/inventory/reports")


@inv_reports_bp.route("/stock-ledger", methods=["GET"])
@login_required
def stock_ledger_report():
    product_id = request.args.get("product_id", type=int)
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()

    if product_id:
        entries = StockLedger.query.filter_by(product_id=product_id).order_by(StockLedger.id).all()
        product = scoped_get(InvProduct, product_id)
    else:
        entries = []
        product = None

    return render_template("reports/stock_ledger.html",
                           products=products, product=product,
                           entries=entries, selected_id=product_id)


@inv_reports_bp.route("/valuation", methods=["GET"])
@login_required
def valuation_report():
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    rows = []
    for p in products:
        bal = StockLedger.get_running_balance(p.id)
        qty = round(bal[0], 2)
        avg_cost = round(bal[2], 4) if bal[0] else 0
        value = round(qty * avg_cost, 2)
        rows.append({
            "id": p.id, "sku": p.sku, "name": p.name,
            "qty": qty, "avg_cost": avg_cost, "value": value,
            "unit": p.unit
        })
    total_val = sum(r["value"] for r in rows)
    return render_template("reports/valuation.html", rows=rows, total_val=total_val)


@inv_reports_bp.route("/low-stock", methods=["GET"])
@login_required
def low_stock_report():
    products = InvProduct.query.filter(
        InvProduct.is_active == True,
        InvProduct.current_stock <= InvProduct.reorder_level
    ).order_by(InvProduct.current_stock).all()
    return render_template("reports/low_stock.html", products=products)


@inv_reports_bp.route("/api/stock-ledger-json")
@login_required
def stock_ledger_json():
    product_id = request.args.get("product_id", type=int)
    if not product_id:
        return jsonify([])
    entries = StockLedger.query.filter_by(product_id=product_id).order_by(StockLedger.id).all()
    data = []
    for e in entries:
        data.append({
            "id": e.id,
            "voucher_type": e.voucher_type,
            "voucher_number": e.voucher_number,
            "transaction_type": e.transaction_type,
            "quantity": float(e.quantity),
            "unit_cost": float(e.unit_cost),
            "running_qty": float(e.running_qty),
            "running_value": float(e.running_cost),
            "running_avg_cost": float(e.running_avg),
            "date": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
            "notes": e.notes or "",
        })
    return jsonify(data)
