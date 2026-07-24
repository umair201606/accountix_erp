# Accounting Policy Document

## General Ledger Transaction Reference — All Modules

**ERP:** Solarkon ERP  
**Date:** July 2026  
**Basis:** Accrual accounting, double-entry, periodic inventory (historic cost)

---

## 1. Chart of Accounts Structure

The COA follows a fixed five-level segmented scheme enforced by the application.

### 1.1. Level Hierarchy

| Level | Format | Example | Purpose |
|-------|--------|---------|---------|
| 1 | `1` | `1` — Assets | Account class |
| 2 | `1-01` | `1-01` — Current Assets | Sub-group |
| 3 | `1-01-01` | `1-01-01` — Cash & Bank | Parent head |
| 4 | `1-01-01-01` | `1-01-01-01` — Cash in Hand | Control account |
| 5 | `1-01-01-01-0001` | `1-01-01-01-0001` — Main Cash | **Operational account (only posting level)** |

### 1.2. Account Classes

| Code | Class |
|------|-------|
| `1` | Assets |
| `2` | Liabilities |
| `3` | Equity |
| `4` | Revenue |
| `5` | Expenses |

### 1.3. Seeded Level-5 Operational Accounts

| Code | Name | Type |
|------|------|------|
| `1-01-01-01-0001` | Main Cash | Asset |
| `1-01-02-01-0001` | Trade Debtors — General | Asset |
| `1-01-03-01-0001` | Employee Loans & Advances | Asset |
| `1-01-04-01-0001` | Stock — General | Asset |
| `1-01-05-01-0001` | Input Sales Tax | Asset |
| `1-02-01-01-0001` | Fixed Assets — General | Asset |
| `2-01-01-01-0001` | Trade Creditors — General | Liability |
| `2-01-02-01-0001` | Employee Payables | Liability |
| `2-01-02-02-0001` | Salary Payable | Liability |
| `2-01-02-02-0002` | PF Payable | Liability |
| `2-01-02-02-0003` | Loan Deductions Clearing | Liability |
| `2-01-03-01-0001` | Output Sales Tax | Liability |
| `2-01-03-03-0001` | WHT Payable | Liability |
| `4-01-01-01-0001` | Sales — General | Revenue |
| `4-02-01-01-0001` | Sales Returns — General | Revenue (contra) |
| `5-01-01-01-0001` | Cost of Goods Sold | Expense |
| `5-01-02-01-0004` | Inventory Cost Variance | Expense |
| `5-02-01-01-0001` | Salary Expense | Expense |

---

## 2. Core Posting Infrastructure

### 2.1. `post_journal_entry(accounting_voucher.py)`

All journal entries in the system pass through this single function at `shared/ledger_utils.py:46`. It:

- Accepts a voucher type code, voucher ID & number, description, entry date, created-by user, and a list of `{account_id, debit, credit, description}` lines.
- **Validates balance:** total debits must equal total credits (within 0.01 tolerance), else the entry is rejected.
- **Enforces posting level:** every line's account must have `level >= 5`. Aggregating accounts (levels 1–4) are refused.
- **Enforces period lock:** calls `require_open_period()` to verify the entry date does not fall in a closed accounting period.
- Sets `is_posted = True` on the resulting `JournalEntry`.

### 2.2. `reverse_journal_entry`

- Finds all `JournalEntry` rows matching `voucher_type + voucher_id` and flips `is_posted = False`.
- Does **not** create counter-entries or delete rows.
- Also enforces period lock (cannot unpost from a closed period).

### 2.3. `post_variance_journal`

- Creates a `"VAR"`-type journal entry for inventory cost variance arising when a purchase invoice is reversed but its stock was already consumed.
- Debits/credits the difference to `Inventory Cost Variance` vs `Stock — General`.

### 2.4. Entity Subledger Accounts

Every customer, supplier, product, employee, and loan advance gets its own level-5 account under the relevant control account. These are created on-demand by `create_entity_account()`:

| Entity Type | Parent Control Account | Code Pattern |
|-------------|----------------------|--------------|
| Customer | `1-01-02-01` — Trade Debtors | `1-01-02-01-01<ID>` |
| Supplier | `2-01-01-01` — Trade Creditors | `2-01-01-01-01<ID>` |
| Product | `1-01-04-01` — Trading Goods Stock | `1-01-04-01-01<ID>` |
| Employee | `2-01-02-01` — Employee Payables | `2-01-02-01-01<ID>` |
| Loan | `1-01-03-01` — Employee Loans & Advances | `1-01-03-01-01<ID>` |

Seeded defaults use codes `0001–0099`; entity subledgers start at `0100` (i.e., `1-01-02-01-0105` for customer ID 5).

---

## 3. All Voucher Types and Their Journal Entries

### 3.1. Sales Invoice — `SI`

**Module:** Invoicing  
**Trigger:** User approves a sales invoice  
**Source file:** `invoicing_app/routes/invoices.py:429–437`

| Account | Debit | Credit | Condition |
|---------|-------|--------|-----------|
| Trade Debtors — [Customer] (entity subledger) | Gross invoice total | — | Always |
| Sales — General | — | Gross total − output tax | Always |
| Output Sales Tax | — | Total tax charged | If `total_tax > 0` |
| Cost of Goods Sold | Total COGS (historic cost) | — | If `total_cogs > 0` |
| Stock — General | — | Total COGS | If `total_cogs > 0` |

**Amount calculation:**
- `total` = `inv.total_amount` (the invoice's net receivable)
- `output_tax` = `inv.total_tax`
- `revenue` = `total − output_tax` (revenue stated net of sales tax)
- `total_cogs` = sum of `record_out()` per line (historic cost at issue time)

**Policy:**
- Revenue is always booked **net** of sales tax. Output tax is a separate liability.
- COGS is booked at **historic cost** computed by the costing engine (weighted average / FIFO across all purchase layers), not at the selling price or a user-entered cost.
- The AR account resolves via `party_account("customer", ...)`:
  1. Explicit `party_account_id` override on the invoice, if set.
  2. Customer's own subledger account (created on first use).
  3. Fallback to `Trade Debtors — General`.

**Reverse:** `reverse_journal_entry("SI", invoice.id)` on unapprove.

---

### 3.2. Sales Payment — `PMT`

**Module:** Invoicing  
**Trigger:** User records a cash/cheque payment against a sales invoice  
**Source file:** `invoicing_app/routes/invoices.py:523–536`

| Account | Debit | Credit |
|---------|-------|--------|
| Main Cash | Payment amount | — |
| Trade Debtors — [Customer] (entity subledger) | — | Payment amount |

**Amount calculation:** `amount` = user-entered payment value from form.

**Policy:** Payments reduce the customer's AR subledger balance. No revenue recognition (revenue was recognised at approval time).

**Reverse:** No reverse mechanism. Payments are one-way.

---

### 3.3. Purchase Invoice — `PI`

**Module:** Invoicing  
**Trigger:** User approves a purchase invoice  
**Source file:** `invoicing_app/routes/purchase_invoice.py:436–444`

| Account | Debit | Credit | Condition |
|---------|-------|--------|-----------|
| Stock — General | Goods value (subtotal − discount + expenses) | — | Always |
| Input Sales Tax | Total input tax | — | If `total_tax > 0` |
| WHT Payable | — | WHT amount | If `wht > 0.005` |
| Trade Creditors — [Supplier] (entity subledger) | — | Net payable | Always |

**Amount calculation:**
- `goods` = `subtotal − total_discount + total_charges` (the landed cost of goods)
- `input_tax` = `total_tax` (input sales tax recoverable)
- `net_payable` = invoice net payable from form
- `wht` = `goods + input_tax − net_payable` (balancing figure for WHT deducted)

**Policy:**
- Stock is debited at **landed cost** (invoice value net of trade discount, plus charges).
- Input tax is booked as an **asset** (receivable from tax authority).
- WHT deducted at source is booked as a **liability** payable to tax authority.
- The AP account resolves via `party_account("supplier", ...)`.

**Reverse:** `reverse_journal_entry("PI", invoice.id)` on unapprove.

---

### 3.4. Sales Return (Credit Note) — `SR`

**Module:** Invoicing  
**Trigger:** User approves a sales return  
**Source file:** `invoicing_app/routes/sales_return.py:300–309`

| Account | Debit | Credit | Condition |
|---------|-------|--------|-----------|
| Sales Returns — General | Net return (gross − tax) | — | Always |
| Output Sales Tax | Total tax on return | — | If `total_tax > 0` |
| Stock — General | Cost of returned goods | — | If `total_cost_returned > 0` |
| Trade Debtors — [Customer] (entity subledger) | — | Gross return amount (net + tax) | Always |
| Cost of Goods Sold | — | Cost of returned goods | If `total_cost_returned > 0` |

**Amount calculation:**
- `gross` = `net_return_amount` (the total value of returned items)
- `tax` = `total_tax` on the return
- `net_of_tax` = `gross − tax`
- `total_cost_returned` = sum of `cost_basis × qty` per line, where `cost_basis` is the `original_issue_cost` from the costing layer that was consumed when the goods were sold.

**Policy:**
- Sales returns **reverse** the original revenue and COGS entries.
- Returned stock is brought back into inventory at **the same historic cost** it left at (the cost layer that was consumed at sale time).
- Output tax on the return is reversed (Dr Output Tax).

**Reverse:** `reverse_journal_entry("SR", return.id)` on unapprove.

---

### 3.5. Purchase Return (Debit Note) — `PR`

**Module:** Invoicing  
**Trigger:** User approves a purchase return  
**Source file:** `invoicing_app/routes/purchase_return.py:259–272`

| Account | Debit | Credit |
|---------|-------|--------|
| Trade Creditors — [Supplier] (entity subledger) | Net return amount | — |
| Stock — General | — | Net return amount |

**Policy:** Returns the stock value to the supplier and reduces the AP subledger. Input tax and WHT are NOT reversed automatically (the user must create a separate adjustment if needed).

**Reverse:** `reverse_journal_entry("PR", return.id)` on unapprove.

---

### 3.6. Consumption Voucher — `CONS`

**Module:** Inventory  
**Trigger:** User approves a consumption voucher (stock issued for internal use)  
**Source file:** `inventory_app/routes/vouchers.py:124–131`

| Account | Debit | Credit |
|---------|-------|--------|
| Consumption Expense (or user-selected charge account) | Total value at historic cost | — |
| Stock — General | — | Total value at historic cost |

**Amount calculation:** `total_value` = sum of `qty × unit_cost` across all items, where `unit_cost` comes from `cost_of_issue()` (historic cost from costing engine).

**Policy:** Consumption is an **expense** — the stock is consumed internally and not expected to generate revenue. The charge account defaults to `Consumption Expense (5700)` but the user may select any postable expense account.

**Reverse:** `reverse_journal_entry("CONS", voucher.id)` on unapprove.

---

### 3.7. Scrap Voucher — `SCRAP`

**Module:** Inventory  
**Trigger:** User approves a scrap voucher (stock written off)  
**Source file:** `inventory_app/routes/vouchers.py:251–258`

| Account | Debit | Credit |
|---------|-------|--------|
| Scrap/Write-off Expense (or user-selected charge account) | Total value at historic cost | — |
| Stock — General | — | Total value at historic cost |

**Amount calculation:** Identical to consumption — historic cost from costing engine.

**Policy:** Scrap is an **expense** — the stock is written off with no expectation of recovery. Defaults to `Scrap/Write-off (5800)`.

**Reverse:** `reverse_journal_entry("SCRAP", voucher.id)` on unapprove.

---

### 3.8. Stock Adjustment Voucher — `ADJ`

**Module:** Inventory  
**Trigger:** User approves a stock adjustment (physical vs system difference)  
**Source file:** `inventory_app/routes/vouchers.py:369–398`

| Scenario | Account | Debit | Credit |
|----------|---------|-------|--------|
| **Excess** (physical > system) | Stock — General | Difference value | — |
| | Inventory Adjustment | — | Difference value |
| **Shortage** (physical < system) | Inventory Adjustment | Difference value | — |
| | Stock — General | — | Difference value |

**Amount calculation:** `val` = `item.total_cost` at current valuation cost. For excess, uses `current_unit_cost(product_id)`. For shortage, uses `record_out()` (historic cost at issue).

**Policy:** Adjustments normalise the stock ledger to physical counts. The offset goes to `Inventory Adjustment (5900)` — an expense account.

**Reverse:** `reverse_journal_entry("ADJ", voucher.id)` on unapprove.

---

### 3.9. Stock Take — `ST`

**Module:** Inventory  
**Trigger:** User approves a stock take (creates an underlying adjustment voucher)  
**Source file:** `inventory_app/routes/vouchers.py:519–560`

Identical posting logic to Stock Adjustment (`ADJ`) above, because a stock take automatically creates an Adjustment Voucher at approval time.

| Scenario | Account | Debit | Credit |
|----------|---------|-------|--------|
| Excess | Stock — General | Difference | — |
| | Inventory Adjustment | — | Difference |
| Shortage | Inventory Adjustment | Difference | — |
| | Stock — General | — | Difference |

**Reverse:** The underlying Adjustment Voucher is reversed.

---

### 3.10. Payroll Run — `PRL` (Two Entries)

**Module:** HR / Compensation  
**Trigger:** User processes a monthly payroll run  
**Source file:** `hr_app/routes/compensation.py:404–419`

#### Entry 1 — Salary and Deductions (lines 404–408)

| Account | Debit | Credit | Condition |
|---------|-------|--------|-----------|
| Salary Expense (`5121`) | Total gross pay | — | Always |
| Income Tax Payable (`2122`) | — | Total income tax deducted | If `total_tax > 0` |
| PF Payable (`2123`) | — | Total PF employee contribution | If `total_pf_ee > 0` |
| Loan Deductions Clearing (`2124`) | — | Total loan repayments | If `total_loan > 0` |
| Salary Payable (`2121`) | — | Total custom deductions | If `total_custom > 0` |
| Salary Payable (`2121`) | — | Total net pay | Always |

**Amount calculation:**
- `total_gross` = sum of all employees' gross pay
- `total_tax` = sum of income tax deducted
- `total_pf_ee` = sum of PF employee contributions
- `total_loan` = sum of loan repayment deductions
- `total_custom` = sum of custom (miscellaneous) deductions
- `total_net` = sum of net pay after all deductions

#### Entry 2 — PF Employer Contribution (lines 415–419)

| Account | Debit | Credit | Condition |
|---------|-------|--------|-----------|
| PF Employer Expense (`5122`) | Total employer PF | — | If `total_pf_er > 0` |
| PF Payable (`2123`) | — | Total employer PF | If `total_pf_er > 0` |

**Amount calculation:** `total_pf_er` = sum of `gross × employer_contribution_pct ÷ 100` across all employees.

**Policy:**
- Salary expense is recognised in full (gross) at run time.
- Deductions at source (tax, PF, loan) are booked as **liabilities** until remitted.
- Net pay is a **current liability** (`Salary Payable`) until disbursed.
- Employer PF contribution is a separate expense line.

**Reverse:** Not implemented. Payroll runs are not un-approved.

---

### 3.11. Accounting Vouchers — `CPV` / `CRV` / `BPV` / `BRV` / `JV`

**Module:** Finance / Accounting  
**Trigger:** User approves any accounting voucher  
**Source file:** `finance_app/routes/accounting.py:252–263`

| Voucher Type | Name | Behaviour |
|-------------|------|-----------|
| `CPV` | Cash Payment Voucher | User picks lines + cash account. Cash is **credited** automatically. |
| `CRV` | Cash Receipt Voucher | User picks lines + cash account. Cash is **debited** automatically. |
| `BPV` | Bank Payment Voucher | User picks lines + bank account. Bank is **credited** automatically. |
| `BRV` | Bank Receipt Voucher | User picks lines + bank account. Bank is **debited** automatically. |
| `JV` | Journal Voucher | User picks all lines freely. Must balance. |

**Policy:**
- Cash/bank vouchers (CPV/CRV/BPV/BRV): the cash/bank line is auto-inserted on the opposite side of the user-entered counter-lines. User selects the cash/bank account from the COA.
- JV: fully flexible — any postable accounts, user must balance debits and credits.
- All lines are validated for posting level (level 5) and period lock.

**Reverse:** `reverse_journal_entry(voucher_type, voucher.id)` on unapprove.

---

### 3.12. Inventory Cost Variance — `VAR`

**Module:** Shared (auto-generated)  
**Trigger:** Purchase invoice unapprove when consumed stock exists  
**Source file:** `shared/ledger_utils.py:154–168`

| Scenario | Account | Debit | Credit |
|----------|---------|-------|--------|
| Net variance positive (write-off) | Inventory Cost Variance | `abs(total)` | — |
| | Stock — General | — | `abs(total)` |
| Net variance negative (recovery) | Stock — General | `abs(total)` | — |
| | Inventory Cost Variance | — | `abs(total)` |

**Amount calculation:** `total` = sum of all product-level variances (the difference between the original purchase cost and the replacement cost when stock was already consumed). `amount` = `abs(total)`. If `total > 0`, it is a write-off; if `total < 0`, it is a recovery.

**Policy:** When a purchase invoice is reversed but its stock was already sold or consumed, the original cost layers cannot be unwound. The variance is booked to `Inventory Cost Variance` — an expense (or contra-expense) — so the stock ledger and general ledger remain in balance.

---

---

## 4. Inventory Costing Engine — General Ledger Effects

The costing engine (`shared/costing.py`) is the single source of truth for stock valuation. It does **not** post directly to the general ledger — instead it provides the **historic cost values** that every stock-moving voucher posts to the GL through `post_journal_entry`. The voucher always uses the value returned by the engine, never a user-entered price.

### 4.1. StockLedger — The Link Between Costing and GL

Every stock movement (purchase in, sale out, consumption, scrap, adjustment) writes a row to `StockLedger` with a frozen `unit_cost` and `total_cost`. These frozen figures are what the voucher posts to the GL, and they **never change** — not on reversal, revaluation, or retroactive adjustment.

Each `StockLedger` row also carries a `running_qty` and `running_cost` (recomputed on every mutation) so the system can verify at any time that:

```
sum(all StockLayer.qty_remaining × unit_cost)  ==  StockLedger.running_cost
```

This invariant is the tie between the physical layers and the GL inventory control account.

### 4.2. `record_in` — Stock Receipt

**Called by:** Purchase Invoice (approve), Purchase Return (reverse), Opening Balance seed

| Effect | Value |
|--------|-------|
| **Layer action** | FIFO: opens a new layer at landed unit cost. WA: merges into the single open pool, re-averaging. |
| **GL posting** | **Not posted here.** The calling voucher posts the GL entry (Dr Stock, Cr AP for purchases; or the reverse for returns). |
| **Frozen on row** | `unit_cost` = landed cost from the purchase invoice. |

**Key invariant:** `record_in` value must equal the landed cost debited to Stock in the GL. Currently this holds because the purchase invoice route passes the same `goods / qty` as `record_in`'s `unit_cost`.

### 4.3. `record_out` — Stock Issue

**Called by:** Sales Invoice (approve), Consumption (approve), Scrap (approve), Stock Adjustment — shortage side, Purchase Return (the return of goods), Sales Return (reverse)

| Effect | Value |
|--------|-------|
| **Layer action** | Consumes from oldest open layers first, recording each consumption in `LayerConsumption`. Returns `(unit_cost, total_cost)` to the caller. |
| **GL posting** | **Not posted here.** The calling voucher posts the GL entry using the returned `total_cost`: |
| | — Sales: Dr COGS, Cr Stock |
| | — Consumption: Dr Expense, Cr Stock |
| | — Scrap: Dr Scrap Write-off, Cr Stock |
| | — Adjustment shortage: Dr Adjustment Expense, Cr Stock |
| | — Purchase return: Cr Stock (goods returned to supplier) |
| **Frozen on row** | `unit_cost` = the blended cost of the layers consumed (oldest-first). |

**Negative stock protection:** `record_out` raises `NegativeStockError` if there is insufficient stock on hand and `allow_negative_stock` is disabled — preventing the engine from inventing a cost for units that were never purchased.

### 4.4. `cost_of_issue` / `current_unit_cost` — Valuation Queries

Used by stock adjustments and stock takes to compute the valuation cost for excess/shortage without mutating layers. The adjustment voucher then calls either `record_out` (shortage) or passes the value through `_write_row` + a separate layer action.

### 4.5. Voucher Reversal — `reverse_voucher_stock`

When a stock-moving voucher is unapproved:

1. **Layer restoration:** `LayerConsumption` rows are reversed — quantities are returned to the layers they were drawn from.
2. **Layer withdrawal:** For FIFO, layers that were opened by the reversed receipt are deleted. For WA, the pool is resynced to the ledger balance via `_resync_pool`.
3. **Variance detection:** If the receipt's stock was already consumed downstream (`allow_variance=True`), `_reconcile_to_variance` writes a value-only VAR row to StockLedger (quantity 0, carrying the unbacked cost).
4. **GL effect:** The caller posts `post_variance_journal()` to book the variance:

| Scenario | Account | Debit | Credit |
|----------|---------|-------|--------|
| Net variance positive (write-off) | Inventory Cost Variance (`5-01-02-01-0004`) | `abs(variance)` | — |
| | Stock — General (`1-01-04-01-0001`) | — | `abs(variance)` |
| Net variance negative (recovery) | Stock — General (`1-01-04-01-0001`) | `abs(variance)` | — |
| | Inventory Cost Variance (`5-01-02-01-0004`) | — | `abs(variance)` |

If `allow_variance=False` (the default), `reverse_voucher_stock` raises `ConsumedLayerError` and refuses the reversal — the downstream issues must be reversed first.

### 4.6. Valuation Method Switch — Revaluation

When the admin toggles between FIFO and Weighted Average via Inventory Settings:

- **FIFO → WA:** Open layers are collapsed into one at their total book value. The single layer carries forward as the weighted average pool.
- **WA → FIFO:** The single open layer stays as-is. Future receipts open new layers.
- **No GL entry is created.** Book value is identical before and after — no cost that was already posted ever changes. The switch is purely prospective.

### 4.7. Cost Invariant Assertion

The engine can verify internal consistency at any point with `assert_invariant(product_id)`:

```
abs(sum(StockLayer.qty_remaining × unit_cost) - StockLedger.running_cost) <= 0.01
```

A violation means the GL inventory control account has drifted from the physical stock value — every posted cost must be traceable to a real purchase layer.

### 4.8. GL Diagram: Costing Engine Data Flow

```
Purchase Invoice (approve)
  │  record_in(product, qty, landed_cost)     ← opens layer
  │  post_journal_entry("PI", ...)            ← Dr Stock, Cr AP (at landed_cost)
  ▼
StockLayer + StockLedger (IN row, unit_cost frozen)
  │
  │  Sales Invoice (approve)
  │    │  record_out(product, qty)            ← consumes layers, returns (unit_cost, total_cost)
  │    │  post_journal_entry("SI", ...)       ← Dr COGS, Cr Stock (at returned total_cost)
  │    ▼
  │  StockLedger (OUT row, unit_cost frozen)
  │
  │  Sales Invoice (unapprove)
  │    │  reverse_voucher_stock("SI", id)      ← restores layer qty
  │    │  reverse_journal_entry("SI", id)      ← flips is_posted=False on original GL entry
  │    ▼
  │  Layers restored, GL reversed
  │
  Purchase Invoice (unapprove — stock not yet sold)
    │  reverse_voucher_stock("PI", id)         ← deletes opened layer(s), removes IN row
    │  reverse_journal_entry("PI", id)         ← flips is_posted=False
    ▼
  Clean reversal

Purchase Invoice (unapprove — stock already sold)
    │  reverse_voucher_stock("PI", id, allow_variance=True)
    │    ├── restores downstream layer qty from surviving layers
    │    ├── writes VAR row (qty=0, cost=variance) to StockLedger
    │    └── returns variance amount
    │  post_variance_journal()                  ← Dr/Cr Inventory Cost Variance vs Stock
    │  reverse_journal_entry("PI", id)          ← flips is_posted=False
    ▼
  Original PI reversed, variance booked to P&L
```

### 4.9. Products Not Tracked by the Costing Engine

Products with zero `current_stock` and no `StockLedger` history are not tracked by the engine. Selling or consuming them will raise `NegativeStockError` unless `allow_negative_stock` is enabled. No GL effect occurs for products the engine does not know about.

---

## 5. Complete Voucher Type Index

| Code | Description | Module | Posting Function | Reverse Function |
|------|-------------|--------|------------------|------------------|
| `SI` | Sales Invoice | Invoicing | `post_journal_entry` | `reverse_journal_entry` |
| `PMT` | Sales Payment Received | Invoicing | `post_journal_entry` | Not implemented |
| `PI` | Purchase Invoice | Invoicing | `post_journal_entry` | `reverse_journal_entry` |
| `SR` | Sales Return (Credit Note) | Invoicing | `post_journal_entry` | `reverse_journal_entry` |
| `PR` | Purchase Return (Debit Note) | Invoicing | `post_journal_entry` | `reverse_journal_entry` |
| `CONS` | Consumption Voucher | Inventory | `post_journal_entry` | `reverse_journal_entry` |
| `SCRAP` | Scrap Voucher | Inventory | `post_journal_entry` | `reverse_journal_entry` |
| `ADJ` | Stock Adjustment Voucher | Inventory | `post_journal_entry` | `reverse_journal_entry` |
| `ST` | Stock Take | Inventory | `post_journal_entry` (via ADJ) | Reverse underlying ADJ |
| `PRL` | Payroll Run | HR | `post_journal_entry` (×2) | Not implemented |
| `VAR` | Inventory Cost Variance | Shared | `post_variance_journal` | Not implemented |
| `CPV` | Cash Payment Voucher | Finance | `post_journal_entry` | `reverse_journal_entry` |
| `CRV` | Cash Receipt Voucher | Finance | `post_journal_entry` | `reverse_journal_entry` |
| `BPV` | Bank Payment Voucher | Finance | `post_journal_entry` | `reverse_journal_entry` |
| `BRV` | Bank Receipt Voucher | Finance | `post_journal_entry` | `reverse_journal_entry` |
| `JV` | Journal Voucher | Finance | `post_journal_entry` | `reverse_journal_entry` |

---

## 6. Accounting Policies Enforced

1. **Double-entry balance:** Every journal entry must have equal total debits and total credits (enforced by `post_journal_entry`; tolerance 0.01).

2. **Posting level restriction:** Only level-5 (operational) accounts may carry journal lines. Levels 1–4 are aggregating controls and cannot be posted to directly.

3. **Period locking:** Transactions cannot be posted to or unposted from a closed accounting period. The system checks `require_open_period()` before any posting or reversal.

4. **Entity subledgers:** Every customer and supplier gets its own level-5 account under the Trade Debtors (Assets) or Trade Creditors (Liabilities) control account. This enables per-entity ageing and ledger reports.

5. **Revenue net of tax:** Sales invoices credit revenue net of output sales tax. The output tax is debited to the customer (as part of the gross receivable) and credited to Output Sales Tax (liability).

6. **Historic cost for inventory:** All inventory movements affecting COGS, consumption, scrap, and adjustments are valued at **historic cost** computed by the costing engine (weighted average / FIFO across purchase layers), never at a user-entered price or selling price.

7. **Landed cost for purchases:** Purchase invoices debit Stock at the full landed cost (subtotal − discount + charges). Input tax and WHT are separated into their own accounts.

8. **Unposting by flag:** `reverse_journal_entry` does not delete rows or create contra-entries. It flips `is_posted = False` on the original entry, which is treated as reversed in all ledger reports and financial statements.

9. **Payroll gross-up:** Salary expense is recognised at gross pay. All statutory deductions (income tax, PF) and voluntary deductions (loans) are booked as liabilities until remitted. Employer PF contribution is a separate expense.

10. **No auto-FBR posting:** The FBR Digital Invoicing module does not create general ledger entries. It operates purely as a compliance reporting layer.

---

*End of document.*
