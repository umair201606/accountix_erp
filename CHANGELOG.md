# ERP UI Standard Implementation — Change Log

## Date: 2026-07-24

## Overview
Full implementation of the `Sales_Purchase_Orders_Invoices_ERP_UI_Standard_v3` across all four document screens: Sales Invoice, Purchase Invoice, Sales Order, and Purchase Order.

---

## 1. Additional Charge Model (Shared)

**File:** `inventory_app/models/additional_charge.py`

| Change | Detail |
|---|---|
| **New model** | `AdditionalCharge` — polymorphic `doc_type`/`doc_id` (values: SI, PI, SO, PO) |
| Fields | `charge_account_id` (FK → ChartOfAccount), `description`, `amount`, `scope` (general/per_item), `taxable` (bool), `tax_base` (after_discount/before_discount), `created_at` |

---

## 2. Invoice Model Extensions

### InvInvoice (`inventory_app/models/invoice.py`)

| Column | Type | Default |
|---|---|---|
| `further_tax_pct` | Float | 0 |
| `apply_further_tax` | Boolean | False |
| `withholding_tax_pct` | Float | 0 |
| `apply_withholding_tax` | Boolean | False |
| `total_further_tax` | Float | 0 |
| `total_withholding_tax` | Float | 0 |
| `charges_list` | property | Returns AdditionalCharge(doc_type="SI") |

### InvPurchaseInvoice (`inventory_app/models/purchase_invoice.py`)

| Column | Type | Default |
|---|---|---|
| `further_tax_pct` | Float | 0 |
| `apply_further_tax` | Boolean | False |
| `total_further_tax` | Float | 0 |
| `total_withholding_tax` | Float | 0 |
| `total_amount` | Float | 0 |
| `paid_amount` | Float | 0 |
| `payment_status` | Varchar | 'unpaid' |
| `purchase_order_id` | Integer (FK) | null |
| `charges_list` | property | Returns AdditionalCharge(doc_type="PI") |

---

## 3. Sales Order Model Extension

**Files:** `inventory_app/models/sales_order.py`, `inventory_app/models/purchase_order.py`

### InvSalesOrder — new columns

| Column | Type | Default |
|---|---|---|
| `party_account_id` | Integer (FK) | null |
| `expected_date` | Date | null |
| `discount_mode` | Varchar(20) | 'general' |
| `charges_mode` | Varchar(20) | 'general' |
| `tax_mode` | Varchar(20) | 'general' |
| `global_discount_pct` | Float | 0 |
| `global_discount_value` | Float | 0 |
| `global_delivery` | Float | 0 |
| `global_installation` | Float | 0 |
| `global_sales_tax_pct` | Float | 0 |
| `further_tax_pct` | Float | 0 |
| `apply_further_tax` | Boolean | False |
| `withholding_tax_pct` | Float | 0 |
| `apply_withholding_tax` | Boolean | False |
| `subtotal` | Float | 0 |
| `total_discount` | Float | 0 |
| `total_charges` | Float | 0 |
| `total_tax` | Float | 0 |
| `total_further_tax` | Float | 0 |
| `total_withholding_tax` | Float | 0 |
| `total_amount` | Float | 0 |
| `approved_by` | Integer (FK) | null |
| `approved_at` | DateTime | null |
| `charges_list` | property | Returns AdditionalCharge(doc_type="SO") |

### InvSalesOrderItem — new columns

| Column | Type | Default |
|---|---|---|
| `description` | Varchar(200) | '' |
| `unit` | Varchar(20) | 'pcs' |
| `quantity` changed from Integer to Float | | |
| `discount_pct` | Float | 0 |
| `discount_amount` | Float | 0 |
| `delivery` | Float | 0 |
| `installation` | Float | 0 |
| `sales_tax_pct` | Float | 0 |
| `total_before_discount` | Float | 0 |
| `total_after_discount` | Float | 0 |

### InvPurchaseOrder — new columns

| Column | Type | Default |
|---|---|---|
| `party_account_id` | Integer (FK) | null |
| `discount_mode` | Varchar(20) | 'general' |
| `charges_mode` | Varchar(20) | 'general' |
| `tax_mode` | Varchar(20) | 'general' |
| `global_discount_pct` | Float | 0 |
| `global_discount_value` | Float | 0 |
| `global_sales_tax_pct` | Float | 0 |
| `further_tax_pct` | Float | 0 |
| `apply_further_tax` | Boolean | False |
| `withholding_tax_pct` | Float | 0 |
| `apply_withholding_tax` | Boolean | False |
| `subtotal` | Float | 0 |
| `total_discount` | Float | 0 |
| `total_charges` | Float | 0 |
| `total_tax` | Float | 0 |
| `total_further_tax` | Float | 0 |
| `total_withholding_tax` | Float | 0 |
| `total_amount` | Float | 0 |
| `driver_name` | Varchar(100) | '' |
| `driver_contact` | Varchar(50) | '' |
| `vehicle_number` | Varchar(50) | '' |
| `gate_pass` | Varchar(50) | '' |
| `approved_by` | Integer (FK) | null |
| `approved_at` | DateTime | null |
| `charges_list` | property | Returns AdditionalCharge(doc_type="PO") |

### InvPurchaseOrderItem — new columns

| Column | Type | Default |
|---|---|---|
| `description` | Varchar(200) | '' |
| `unit` | Varchar(20) | 'pcs' |
| `quantity` changed from Integer to Float | | |
| `discount_pct` | Float | 0 |
| `discount_amount` | Float | 0 |
| `sales_tax_pct` | Float | 0 |
| `total_before_discount` | Float | 0 |
| `total_after_discount` | Float | 0 |

---

## 4. InvoiceSettings Model

**File:** `shared/models/invoice_settings.py`

| Column | Type | Default |
|---|---|---|
| `default_sales_tax_pct` | Float | 0 |
| `default_further_tax_pct` | Float | 0 |
| `default_withholding_tax_pct` | Float | 0 |
| `default_discount_pct` | Float | 0 |
| `default_charges_mode` | Varchar(20) | 'general' |
| `default_discount_mode` | Varchar(20) | 'general' |
| `default_tax_mode` | Varchar(20) | 'general' |
| `show_discount_column` | Boolean | True |
| `show_charges_column` | Boolean | True |
| `show_tax_column` | Boolean | True |
| `auto_add_line` | Boolean | True |
| `require_approval` | Boolean | True |
| `allow_partial_payment` | Boolean | True |
| `default_party_mode` | Varchar(10) | 'relevant' |
| `get()` | classmethod | Returns singleton (first row or creates default) |

---

## 5. Schema Migrations

**File:** `app.py` — `_migrate_schema()` function

All columns added via idempotent ALTER TABLE (checks column existence first). Tables created with `CREATE TABLE IF NOT EXISTS`:

- `additional_charges` — polymorphic charge storage
- `invoice_settings` — admin defaults singleton

Full migration list added for:
- `inv_sales_orders` — 20 new columns
- `inv_sales_order_items` — 9 new columns
- `inv_purchase_orders` — 23 new columns
- `inv_purchase_order_items` — 8 new columns

---

## 6. Sales Invoice Form (Reference Implementation)

**File:** `invoicing_app/templates/invoices/form_inv.html`
**Route:** `invoicing_app/routes/invoices.py`

### Features implemented
| Feature | Detail |
|---|---|
| Customer autocomplete | Space to search, keyboard navigation |
| Party account override | Optional ledger account when party_mode=all |
| Pill toggles | Discount/Charges/Tax — Combined vs Per Item |
| Product autocomplete per row | Space to search products, Enter to add row |
| From Orders modal | Import items from approved SOs for selected customer |
| Additional Charges modal | COA account autocomplete, amount, scope, taxable, tax base |
| ⚙ Settings side panel | Accordion: General (Inv Date, Due Date), Tax Settings (Further Tax, WHT, Sales Tax), Print/Format |
| Further Tax | Toggle + % input, calculated on net total |
| Withholding Tax | Toggle + % input, deducted from net total |
| Summary section | Subtotal, Discount, Charges, Sales Tax, Further Tax, WHT, Net Receivable |
| GL Posting (on approve) | Dr AR, Cr Revenue, Cr Output Tax, Cr Further Tax, Dr WHT Receivable / Cr AR |
| Save / Save & Approve / Unapprove / Delete | JSON API |

---

## 7. Purchase Invoice Form

**File:** `invoicing_app/templates/purchase_invoice/form_inv.html`
**Route:** `invoicing_app/routes/purchase_invoice.py`

### Features implemented
| Feature | Detail |
|---|---|
| Supplier autocomplete | Space to search |
| Party account override | Optional |
| Driver / Logistics fields | Driver name, contact, vehicle, gate pass |
| Expenses mode pill | General (global) vs Individual (per-item commission/freight/loading) |
| Pill toggles | Discount, Expenses, Tax |
| From Orders modal | Import from approved POs for selected supplier |
| Charges modal | Same as Sales Invoice |
| Settings side panel | General, Tax Settings |
| Summary | Subtotal, Discount, Expenses, Tax, Further Tax, WHT, Net Payable |
| GL Posting (on approve) | Dr Inventory + Dr Input Tax / Cr AP + Cr WHT Payable |
| Pay integration on list | Pay form with cash/AP posting |

---

## 8. Sales Order Form

**File:** `invoicing_app/templates/sales/form_inv.html`
**Route:** `invoicing_app/routes/sales.py`

### Features implemented
| Feature | Detail |
|---|---|
| Customer autocomplete | Same as Sales Invoice |
| Party account override | Optional |
| Pill toggles | Discount, Charges, Tax |
| Product autocomplete | Same pattern |
| From Orders modal | Import from approved SOs |
| Charges modal | Full implementation |
| Settings side panel | General (Order Date, Expected Date), Tax Settings |
| Summary | Subtotal, Discount, Charges, Tax, Further Tax, WHT, Total |
| Save / Approve / Unapprove / Delete | JSON API |
| List page | Status filter, View/Edit link, Deliver, Cancel, Delete |

### Route changes from previous
- Old `@/` (list) → now `@/list`
- Old `@/create` (GET+POST form) → removed
- New `@/` and `@/<id>` for form (GET)
- New `@/save` (POST JSON)
- New `@/unapprove/<id>` (POST)
- New API: `/api/products`, `/api/customers`, `/api/accounts`, `/api/orders/<customer_id>`

---

## 9. Purchase Order Form

**File:** `invoicing_app/templates/purchases/form_inv.html`
**Route:** `invoicing_app/routes/purchases.py`

### Features implemented
| Feature | Detail |
|---|---|
| Supplier autocomplete | Same pattern |
| Party account override | Optional |
| Driver / Logistics fields | Driver name, contact, vehicle, gate pass |
| Pill toggles | Discount, Charges, Tax |
| From Orders modal | Import from approved POs |
| Charges modal | Full implementation |
| Settings side panel | General, Tax Settings |
| Summary | Subtotal, Discount, Charges, Tax, Further Tax, WHT, Total |
| Save / Approve / Unapprove / Delete | JSON API |
| List page | Status filter, View/Edit link, Receive, Cancel, Delete |

### Route changes from previous
- Old `@/` (list) → now `@/list`
- Old `@/create` (GET+POST form) → removed
- New `@/` and `@/<id>` for form (GET)
- New `@/save` (POST JSON)
- New `@/unapprove/<id>` (POST)
- New API: `/api/products`, `/api/suppliers`, `/api/accounts`, `/api/orders/<supplier_id>`

---

## 10. List Page Upgrades

### Sales Invoice (`invoicing_app/templates/invoices/list_inv.html`)
- Status filter dropdown
- Columns: Invoice #, Customer, Date, Due Date, **Subtotal, Tax, Further Tax, WHT**, Total, Paid, Status, Actions
- Payment form inline (Amount input + Pay button)
- Status badges: unapproved, approved, unpaid, partial, paid, overdue

### Purchase Invoice (`invoicing_app/templates/purchase_invoice/list_inv.html`)
- Status filter dropdown
- Columns: Invoice #, Voucher #, Supplier, Date, Due Date, **Subtotal, Tax, Further Tax, WHT**, Total, Paid, Status, Actions
- Payment form inline (Amount input + Pay button)
- Route added: `pay_invoice` with GL posting (Dr AP / Cr Cash)

### Sales Order (`invoicing_app/templates/sales/list_inv.html`)
- Status filter dropdown (unapproved, approved, delivered, cancelled)
- Columns: SO #, Customer, Date, Amount, Status, Actions
- Actions: View/Edit, Deliver, Cancel, Delete (POST)

### Purchase Order (`invoicing_app/templates/purchases/list_inv.html`)
- Status filter dropdown (unapproved, approved, received, cancelled)
- Columns: PO #, Supplier, Date, Expected, Amount, Status, Actions
- Actions: View/Edit, Receive, Cancel, Delete (POST)

---

## 11. Admin Settings

**File:** `shared/routes/settings.py` (+ `templates/settings/_invoice.html`)

- "Invoice Defaults" section added to settings
- Fields: default tax %, further tax %, WHT %, discount %, default modes (discount/charges/tax), column visibility toggles, behavior flags, default party mode
- Gated by `has_invoicing_access`
- POST handler `save_invoicing()` with `_require("invoicing")`

---

## 12. Accounting Policy

### Withholding Tax — Purchase (Supplier Side)
```
Dr Inventory          (goods value = subtotal − discount + expenses)
Dr Input Tax          (sales tax)
  Cr Accounts Payable   (net payable to supplier)
  Cr WHT Payable        (liability to FBR — we deposit to government)
```
WHT Payable is a **liability** account. We withhold tax from supplier payment and owe it to FBR.

### Withholding Tax — Sales (Customer Side)
```
Dr Accounts Receivable     (net receivable from customer)
Dr WHT Receivable          (asset — recoverable from FBR)
  Cr Revenue                 (goods value)
  Cr Output Tax              (sales tax + further tax)
```
WHT Receivable is an **asset** account. Customer withholds tax from their payment; we claim it as adjustable advance against income tax.

---

## 13. Bug Fixes

| Issue | Fix | Files affected |
|---|---|---|
| Further Tax / WHT rows hidden on load | Added DOMContentLoaded init to show/hide rows based on checkbox state | All 4 form templates |
| Dashboard links to old `create_sale`/`create_purchase` | Updated to `sale_form`/`purchase_form` | `dashboard/index_invoicing.html` |
| Sales Order list `create_sale` reference | Updated to `sale_form` | `sales/list_inv.html` |
| Purchase Order list `create_purchase` reference | Updated to `purchase_form` | `purchases/list_inv.html` |
| Purchase Invoice list missing columns | Added Subtotal, Tax, Further Tax, WHT, Total, Paid, payment form | `purchase_invoice/list_inv.html` |
| Purchase Invoice missing pay route | Added `pay_invoice` with GL posting | `purchase_invoice.py` |
| Sales Order/Purchase Order delete as GET | Changed to POST with confirmation | Both list templates |
| Syntax error in Sales Invoice form JS | Fixed extra closing paren in recalc() call | `invoices/form_inv.html` |
