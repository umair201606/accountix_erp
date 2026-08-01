from datetime import datetime
from shared.models.fbr import FBRConfig
from shared.invoice_totals import sales_totals
from inventory_app.models.invoice import InvInvoice
from inventory_app.models.customer import InvCustomer
from shared.tenancy import scoped_get_404


def build_fbr_invoice(invoice_id):
    invoice = scoped_get_404(InvInvoice, invoice_id)
    config = FBRConfig.get()
    customer = invoice.customer
    items = invoice.items.all()

    invoice_type = "S"
    if invoice.invoice_number and "DN" in invoice.invoice_number.upper():
        invoice_type = "DN"
    elif invoice.invoice_number and "CN" in invoice.invoice_number.upper():
        invoice_type = "CN"

    # Take the money from the same §8 chain the journal was posted from, so what
    # is filed with FBR always equals what was booked. Deriving it here from a
    # couple of legacy fields silently dropped every additional charge, the
    # further tax and the withholding.
    totals = sales_totals(invoice)
    net_total = totals["sales_tax_base"]
    total_tax = totals["sales_tax"]
    total_amount = totals["net_receivable"]

    line_items = []
    for item in items:
        product = item.product
        unit_value = item.unit_price or 0
        qty = item.quantity or 1
        line_total = unit_value * qty
        disc_amount = item.discount_amount or 0
        tax_rate = item.sales_tax_pct or 0
        tax_charged = 0
        if tax_rate > 0:
            base_for_tax = line_total - disc_amount
            tax_charged = round(base_for_tax * tax_rate / 100, 2)

        line_items.append({
            "itemCode": product.sku if product else "",
            "itemName": item.description or (product.name if product else ""),
            "quantity": qty,
            "unitOfMeasurement": item.unit or "pcs",
            "unitValue": unit_value,
            "totalAmount": round(line_total, 2),
            "discountAmount": round(disc_amount, 2),
            "taxRate": tax_rate,
            "taxCharged": tax_charged,
            "hSCode": getattr(product, "hs_code", "") if product else "",
            "otherCharges": round((item.delivery or 0) + (item.installation or 0), 2),
        })

    invoice_date = invoice.invoice_date or datetime.utcnow()
    due_date = invoice.due_date or invoice_date

    payload = {
        "fbrInvoice": {
            "sellerNTN": config.seller_ntn or "",
            "sellerSTRN": config.seller_strn or "",
            "sellerCNIC": config.seller_cnic or "",
            "sellerName": config.seller_name or "",
            "sellerBusinessName": config.seller_business_name or "",
            "sellerAddress": config.seller_address or "",
            "sellerPhoneNumber": config.seller_phone or "",
            "sellerEmail": config.seller_email or "",
            "buyerNTN": getattr(customer, "tax_id", "") or "",
            "buyerSTRN": "",
            "buyerCNIC": "",
            "buyerName": customer.name if customer else "",
            "buyerAddress": getattr(customer, "address", "") or "",
            "buyerPhoneNumber": getattr(customer, "phone", "") or "",
            "buyerEmail": getattr(customer, "email", "") or "",
            "invoiceNumber": invoice.invoice_number or "",
            "invoiceDate": invoice_date.strftime("%Y-%m-%d %H:%M:%S"),
            "invoiceType": invoice_type,
            "totalSaleValue": round(net_total, 2),
            "totalTaxCharged": round(total_tax, 2),
            "totalBillAmount": round(total_amount, 2),
            "discount": round(invoice.total_discount or 0, 2),
            # Further tax is filed on its own line even though it goes on the
            # same return as sales tax; withholding is what the buyer deducts.
            "furtherTax": totals["further_tax"],
            "withholdingTax": totals["wht"],
            "totalOtherCharges": totals["billed"],
            "otherCharges": [
                {"chargeType": row.description or "Charge",
                 "amount": round(float(row.amount), 2),
                 "inSalesTaxBase": bool(row.st_taxable)}
                for row in totals["pools"]["billed_rows"]
            ],
            # Legacy single-purpose fields, kept for the older sandbox schema.
            "deliveryCharges": round(invoice.global_delivery or 0, 2),
            "installationCharges": round(invoice.global_installation or 0, 2),
            "paymentInfo": {
                "paymentMode": "C" if getattr(invoice, "payment_status", "unpaid") == "paid" else "O",
                "paymentStatus": getattr(invoice, "payment_status", "unpaid").upper(),
            },
            "items": line_items,
        }
    }

    return payload
