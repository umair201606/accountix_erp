# Design System — Professional Enterprise Logic

The visual standard for this ERP's forms and pages. Adapted from the reference
designs in `../templates/Designs/`:

| Reference | What it fixes |
|---|---|
| `professional_enterprise_logic/DESIGN.md` | the tokens — colour, type, spacing, elevation |
| `sales_invoice_erp_core_solutions/` | sales invoice: header strip, line grid, settings rail |
| `purchase_invoice_erp_core_solutions/` | purchase invoice: info cards, grid toolbar, summary |

Read the `screen.png` in those folders, not only the prose. A previous pass
validated arithmetic against written specs without opening the images and the
screens drifted badly from them.

The other 33 folders in `Designs/` cover dashboards, vouchers, reports and
masters. This document governs all of them; the two invoice folders are simply
the most complete worked examples.

---

## 1. The governing principle: restraint

**Clean, not over-coloured.** This is the rule the rest of the document serves.

The current forms fail it. They run a bright blue primary (`#2563eb`) plus
danger, success and warning accents scattered across chrome that carries no
meaning. Saturated blue on large areas is the specific problem: it competes with
the data instead of framing it, and once it is everywhere nothing it touches
reads as important. The replacement navy (`#002045`) is dark enough to act as
structure — a rule, a heading, a single button — rather than as a wash.

Default to neutral. A screen that looks almost monochrome, with navy structure
and one or two semantic figures in red or emerald, is the target.

Three rules follow from it:

1. **Colour is not decoration.** Navy carries structure (primary actions,
   headings, the total rule). Every other hue must earn its place by meaning
   something: red is money leaving, emerald is a settled state. A coloured
   element the user cannot act on or read a status from is a bug.
2. **Borders separate, shadows do not.** A 1px `outline-variant` stroke is the
   default divider. Shadow is reserved for things that genuinely float — cards
   lifting off the page, modals, dropdowns, popovers.
3. **Weight before size.** Hierarchy comes from font weight and generous
   whitespace, not from large type or coloured backgrounds. Most of the
   interface sits at 14px.

Density is not the enemy of calm. These are screens people work in for hours;
the goal is low cognitive load, which comes from a strict grid and predictable
placement — not from removing information.

---

## 2. Tokens

### Colour

| Token | Value | Use |
|---|---|---|
| `primary` | `#002045` | primary buttons, active nav, headings, the total rule |
| `primary-container` | `#1a365d` | hover on primary, sidebar gradient foot |
| `on-primary` | `#ffffff` | text on navy |
| `secondary` | `#545f72` | secondary controls, sub-navigation |
| `background` | `#f6f9ff` | the page behind the cards |
| `surface-container-lowest` | `#ffffff` | cards, table body |
| `surface-container-low` | `#eef4fc` | grid toolbars, table header, zebra stripe |
| `surface-variant` | `#dde3eb` | inset panels, disabled fields |
| `on-surface` | `#161c22` | body text |
| `on-surface-variant` | `#43474e` | labels, secondary text |
| `outline` | `#74777f` | icon strokes, placeholder text |
| `outline-variant` | `#c4c6cf` | **the default border** |
| `error` | `#ba1a1a` | deductions, withholding, validation |
| `error-container` | `#ffdad6` | error badge background |
| `tertiary-container` | `#003f25` | success text (Paid, Approved, Posted) |
| `tertiary-fixed` | `#9ff5c1` | success badge background |

Zebra striping uses `surface-container-low` on alternating rows. Never colour a
whole row to signal status — that is what the badge is for.

### Typography

**Inter**, with `font-variant-numeric: tabular-nums` on every numeric cell so
figures align down a column. This is not optional in a ledger.

| Role | Size / weight / leading | Use |
|---|---|---|
| `display-lg` | 32 / 700 / 40, `-0.02em` | page title (`Purchase Invoice`) |
| `headline-md` | 20 / 600 / 28 | card titles, Net Payable row |
| `body-base` | 16 / 400 / 24 | prose |
| `body-sm` | 14 / 400 / 20 | **the workhorse** — inputs, table cells |
| `label-caps` | 12 / 600 / 16, `0.05em`, uppercase | card headers, table headers, field labels |
| `data-mono` | 14 / 500 / 20, tabular | every money figure |

On mobile `display-lg` drops to 24/700/32.

### Shape, spacing, elevation

- Radius: **8px** buttons and inputs, **16px** cards and modals, **4px** for
  anything nested inside an 8px container, `9999px` for badges and pills.
- Spacing: strict **4px** base unit. 8px and 16px inside components, 24px and
  32px between sections. Gutter 24px, page max-width 1440px.
- Elevation: exactly one shadow, and only on floating things —
  `0 4px 6px -1px rgba(0,0,0,.1), 0 2px 4px -1px rgba(0,0,0,.06)`.

---

## 3. Components

### Page header

Title in `display-lg`, and beneath it the document number as a pill on
`surface-variant` followed by `•` and the status. Primary action (`Save
Document`) sits far right in solid navy; secondary actions (`Print`) are white
with an `outline-variant` border. Never two solid navy buttons side by side.

### Info cards (the header strip)

A 3-column grid (`md:grid-cols-3`, 16px gap) of white cards, 20px padding, 16px
radius, 1px `outline-variant`, one ambient shadow. Each opens with a 20px icon
in `primary` beside a `label-caps` heading — `SUPPLIER DETAILS`, `LOGISTICS &
DELIVERY`, `DOCUMENT DATES`. Inside, a field is a `label-caps` label in
`on-surface-variant` above its value in `body-sm`; the value that identifies the
document (party name, document number) is bold.

Collapse to one column below `md`.

### Grid toolbar

A `surface-container-low` strip with a bottom border, sitting directly on top of
the line grid inside the same rounded container. Scope controls read as
`LABEL:` in `label-caps` followed by the current value as a white chip with an
`outline-variant` border. Actions (`Add Item`, `Additional Charges`,
`Settings`) sit right, as ghost or bordered buttons with a leading icon.

### Card-wrapped table — the signature pattern

**This is the pattern the whole layout is built from. Prefer it everywhere a
table appears.** A table is never loose on the page; it lives inside a card that
owns it:

```
┌─ card: white, 16px radius, 1px outline-variant, one shadow, overflow hidden ─┐
│  toolbar   surface-container-low, 12px padding, bottom border               │
│            scope chips left · actions right                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│  thead     surface-container-low, label-caps, sticky, bottom border         │
│  tbody     white, zebra surface-container-low, data-mono numerics           │
└─────────────────────────────────────────────────────────────────────────────┘
```

`overflow: hidden` on the card is what lets the header and the first row square
off cleanly against the 16px radius — without it the corners tear. The toolbar
and the table are one object: no gap, no second border between them, no
free-floating heading above the card.

Header row is `label-caps` in `on-surface-variant` and **sticky** on scroll.
Body rows sit on white with a `surface-container-low` zebra stripe. Numeric
columns are right aligned and `data-mono`. The item cell carries the name in
`primary` with its SKU beneath in 12px `on-surface-variant`. Absorbed or memo
figures are parenthesised, never given a minus sign.

Row height is comfortable — 8px vertical padding minimum. Do not compress rows
to fit more on screen; scrolling is cheaper than misreading a figure.

The same wrapper serves the settings tables, the order picker and the report
grids. One pattern, applied consistently, is most of what makes the reference
screens feel calm.

### Summary panel

Bottom-right, white card, right-aligned figures in `data-mono`. Rows are plain
`body-sm` on `on-surface`, with three deliberate exceptions:

- a **derived** line (Taxable Value) carries an info affordance explaining its
  derivation;
- a **memo** line (absorbed landing cost) is italic, 12px, `on-surface-variant`
  and parenthesised — it is not part of the arithmetic;
- a **deduction** (withholding) is `error` with an explicit minus.

The final row (`Net Payable` / `Net Receivable`) is `headline-md`, separated by
a **2px `primary`** top rule, with the figure bold in `primary`. That rule is
the single strongest line on the page and nothing else may use it.

### Status badges

Soft pill: status colour at ~10% opacity behind full-opacity text. `Paid` and
`Approved` use `tertiary`; `Draft` and `Unapproved` use `surface-variant` with
`on-surface-variant`; overdue and failed use `error`. Text is `label-caps`.

### Buttons

- **Primary** — solid `primary`, white text, 8px radius. One per view.
- **Secondary** — white, `outline-variant` border, `on-surface` text. Print,
  Download, Manage Charges.
- **Ghost** — no background or border. Clear filters, remove row.
- **Destructive** — `error` text on white with an `error` border; solid red is
  reserved for a confirmed destructive action inside a modal.

### Inputs

Label always above the field in `label-caps`. 1px `outline-variant` border, 8px
radius, transitioning to `primary` on focus with no glow. Disabled fields go
`surface-variant` with no border change — a locked approved document should read
as flat, not as broken. Numeric inputs are `data-mono` and right aligned.

### Side panel / slide-over

Settings rail on the right, white, 1px left border, `headline-md` title with a
leading icon. Sections are accordions with `label-caps` headers, one open at a
time. Inset summaries within a section sit on `surface-container-low` with a 4px
radius.

---

## 4. Applying this to the existing forms

The four documents — `invoices/`, `sales/`, `purchases/`,
`purchase_invoice/form_inv.html` — each declare their own `:root`. They already
share variable **names**, so the palette moves by changing values:

| Existing variable | Was | Becomes |
|---|---|---|
| `--bg` | `#f1f5f9` | `#f6f9ff` |
| `--card` | `#fff` | `#ffffff` |
| `--border` | `#e2e8f0` | `#c4c6cf` |
| `--text` | `#1e293b` | `#161c22` |
| `--muted` | `#64748b` | `#43474e` |
| `--light` | `#94a3b8` | `#74777f` |
| `--primary` | `#2563eb` | `#002045` |
| `--primary-hover` | `#1d4ed8` | `#1a365d` |
| `--primary-light` | `#eff6ff` | `#eef4fc` |
| `--danger` | `#ef4444` | `#ba1a1a` |
| `--success` | `#22c55e` | `#003f25` |
| `--radius` | `8px` | `8px` cards → `16px` |

`--warning` should disappear rather than be remapped: nothing in these documents
warrants amber, and keeping it invites decorative use.

**Constraint — do not change the DOM contract.** The e2e suite drives these
forms through element ids, `data-col` attributes and class hooks (`#itemsBody`,
`#discountMode`, `.pill-b`, `.bsm-settings`, `[data-col="addcharges"]`,
`.chg-cell`, `.pop-man`). Restyling must not rename or restructure them. If a
mockup genuinely requires new structure, add it alongside and update the tests
in the same change — per `AGENTS.md` §2, the full suite runs before committing.

**Responsive is mandatory** (`AGENTS.md` §3). The info strip collapses to one
column, the line grid scrolls horizontally inside its own container rather than
pushing the page wide, and the summary panel goes full width beneath the grid.
320px is the floor.

---

## 5. Checklist

Before calling a screen done:

- [ ] Nothing is coloured that does not mean something.
- [ ] Every money figure is `data-mono` with tabular numerals and right aligned.
- [ ] Separation is borders; shadow only on genuinely floating elements.
- [ ] Exactly one solid navy button in view.
- [ ] Every table is wrapped in a card that owns its toolbar and header, with
      `overflow: hidden` so the corners stay square against the radius.
- [ ] Table header is sticky, `label-caps`, and rows are zebra striped.
- [ ] The 2px navy rule appears once, above the net total.
- [ ] Deductions are red with a minus; memo lines are italic and parenthesised.
- [ ] Labels sit above their fields, in `label-caps`.
- [ ] It holds together at 320px.
- [ ] Element ids and `data-col` hooks are unchanged, and the e2e suite is green.
