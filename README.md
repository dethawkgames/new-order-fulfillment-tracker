# Weekly Orders Tracker

A copy of the Order Fulfillment Tracker page, rewired to the **Weekly Orders** sheet
(`1wHisaKXPcRpgXH9HXw6sdvTQi25QZoLj4cHPxSYlFrQ`) written by
[aggregateallorders](https://github.com/dethawkgames/aggregateallorders). The original
tracker and its Order Needs sheet are not touched by anything in this project.

## The flow

```
close-cycle ──► Order placed ──(shipping invoice)──► Shipped from supplier ──(arrival invoice)──► Order received
```

| Upload | Moves units | Notes |
|---|---|---|
| Verify Asmodee Quote | nothing (read only; optional one-click backorder marking) | compares the quote with the most recent locked Asmodee cycle (all three tabs) |
| Asmodee Invoice (PDF) | Order placed → Shipped | matched by SKU; a Sales Quote PDF is auto-detected too |
| Asmodee Arrival Invoice | Shipped → Order received | |
| Universal Dist Invoice(s) (.xlsx, several allowed) | Order placed → Shipped | barcode first, then SKU |
| Universal Dist Arrival Invoice(s) | Shipped → Order received | |
| ACDD Invoice(s) (PDF) | Order placed → Shipped | UPC under each line → ACDD catalog ("Garland" tab) → ACDD SKU; falls back to the Shopify barcode when a row has no ACDD SKU |
| ACDD Arrival Invoice(s) | Shipped → Order received | ACDD sends no arrival document: upload the same invoice again |

Rules that apply to every upload:

- A matched SKU moves `min(invoice qty, outstanding qty)`, **oldest locked cycle first**.
  A partly shipped row keeps its remainder; invoice quantity above what is outstanding is
  reported (“More than submitted”) and not moved. Items not on the invoice stay put.
- **Dry run** (checkbox) previews everything and writes nothing. It is on by default for arrivals.
- **Duplicate guard:** every processed file's SHA-256 is recorded in `Processed Invoices`.
  The same file at the same step is refused (HTTP 409) with nothing changed. (Re-running a
  file would otherwise move units that belong to a later cycle.) The same file at the *other*
  step is fine, which is how ACDD works.
- No Shopify inventory is ever adjusted here.

## Tagging orders

`Order Lines` (written by close-cycle: one row per order + SKU with Qty / Ordered / Shipped /
Received) lets the tracker know which *orders* the moved units belong to. Units credit the
orders named on the row they came from, oldest order first, only up to what each order
still has outstanding at that step; hand-added rows credit no order.

After the sheets are written, each order that received units is checked against live Shopify.
When **every** unit it needs has reached the step:

- shipped → `dhg-shipped-from-supplier`
- arrived → `dhg-status-order-received` (or `-preorder` when a preorder item's release date is
  more than 3 days away, same rule as before)

The order's current `dhg-status-*` stage tag is removed first; email-lifecycle tags are never
touched; `dhg-status-inventory-queued` orders are skipped and listed; an order that already has
a later status is never moved backwards. Quantities are capped at what Shopify says is still on
the order, so refunded units don't hold an order back, and cancelled/fulfilled orders are skipped.
An order with no live lines is never tagged (the old `all([])` problem). A units row that was
reduced by hand before close-cycle leaves an order “under-ordered”, so it is never tagged. Tagging
problems are reported on the page; the sheets are still updated.

Needs the Shopify app scopes `read_orders` and `write_orders`.

## Page

Same layout as the original. Kept: Verify Quote, Asmodee/UD invoices and arrivals, **new** ACDD
invoice and arrival cards, Box Size, Picking Lists, Order Check. Removed: Record Quantity Received,
Identify Surplus, Order Pipeline. Picking Lists' "backorder" flag and Order Check read the
`Order Lines` tab instead of Order Needs; Order Check only flags a mismatch for SKUs that have a
recorded line (in-stock and untagged items have none).

## Backorders & preorders

The **Backorders** card lists what customers have ordered that the supplier hasn't shipped, for orders that
are still open. It is **derived each time** from `Order placed`, `Order Lines` and live Shopify, so there is no
second list to keep in sync.

A unit is listed when `Ordered - Shipped > 0` on its `Order Lines` row and either

- its lock is **older than the newest lock** (still unshipped a cycle later), or
- its `Order placed` row was **marked** at a quote check (column K, header `Backorder Since`).

Shopify then removes cancelled and fulfilled orders and caps each line at what the order still has
unfulfilled and un-refunded. Product tags give the type (`preorder` tag = Preorder) and the release date
(`release-date-YYYY-MM-DD`).

**Marking:** after a quote check, if locked items are missing from the quote, a button marks them (writes only the
`Backorder Since` cell on that lock's `Order placed` rows; an existing date is kept). Items that later appear on
a quote are cleared the same way. The quote check itself stays read-only.

**Quote check statuses:** `Match`; `Backorder released` (quote is larger than the lock by units still owed from
earlier locks); `Not quoted (marked backorder/preorder)` (expected); and the three that need review:
`Locked but missing from quote`, `In quote but not locked`, `Quantity mismatch`.

**Mark Shipped / Received:** unchanged mechanics (units move oldest lock first, so a released backorder leaves
`Order placed` and lands in `Shipped from supplier` with this invoice and date). The result now also lists
**Filled backorders/preorders**: units that came from an earlier lock or a marked row. Quantities are never
moved twice, and marks on rows that ship simply go with the row.

## Deploy

1. New GitHub repo and a **new** Vercel project (do not reuse the original's).
2. Environment variables: `GOOGLE_SA_EMAIL`, `GOOGLE_SA_PRIVATE_KEY` (or `_B64`),
   `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, `SHOPIFY_SHOP` (optional),
   `SITE_PASSWORD` (and optionally `AUTH_SECRET`). `WEEKLY_ORDERS_SHEET_ID` and
   `PRODUCT_SKUS_SHEET_ID` default to the current sheets.
3. The `Box Inventory` tab was copied into the new sheet once (`scripts/copy-box-inventory.py`);
   Box Size now edits the copy.

## Tests

`python3 -m unittest discover -s tests` — engine (partial moves, cycle order, cross-supplier
tagging, duplicates, dry run, refunds/cancellations, arrival), the quote check, multipart parsing,
the ACDD parser on a real invoice, and a full HTTP round trip for ACDD.
