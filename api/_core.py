"""Core engine for the Weekly Orders tracker.

Moves units between three tabs of the "Weekly Orders" spreadsheet as supplier
documents are uploaded:

    Order placed  --(shipping invoice)-->  Shipped from supplier  --(arrival invoice)-->  Order received

and keeps the per-order `Order Lines` tab in step so Shopify orders can be tagged
once EVERY unit they need has reached a stage.

Everything here is pure Python over a small store interface (get/append/replace_body)
so it can be tested without Google or Shopify. The HTTP handlers in the other api files
only parse the upload and call process_upload().
"""
import hashlib
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta

TAB_PLACED = 'Order placed'
TAB_SHIPPED = 'Shipped from supplier'
TAB_RECEIVED = 'Order received'
TAB_LINES = 'Order Lines'
TAB_INVOICES = 'Processed Invoices'

# Order placed / Shipped / Received share these leading columns.
C_LOCK, C_LAST, C_SUP, C_SKU, C_ACDD, C_QTY, C_UNIT, C_TITLE, C_ORDERS, C_BINS = range(10)
C_SHIPPED_ON, C_INVOICE, C_RECEIVED_ON, C_ARRIVAL_INVOICE = 10, 11, 12, 13
# On Order placed ONLY, column K holds the date a line was marked backorder/preorder (header
# "Backorder Since"). On Shipped / Received the same column is "Shipped On".
C_BACKORDER = 10
BACKORDER_HEADER = 'Backorder Since'
KIND_PREORDER, KIND_BACKORDER = 'Preorder', 'Backorder'

# Order Lines columns
L_LOCK, L_LAST, L_ORDER, L_SUP, L_SKU, L_QTY, L_ORDERED, L_SHIPPED, L_RECEIVED = range(9)

SHIPPED_TAG = 'dhg-shipped-from-supplier'
EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}
RELEASE_DATE_TAG_RE = re.compile(r'^release-date-(\d{4}-\d{2}-\d{2})$')
HOLD_WINDOW_DAYS = 3

ST_MATCH = 'Match'
ST_MORE = 'More than submitted (likely preorder/backorder)'
ST_LESS = 'Less than submitted (partial shipment?)'
ST_MISSING = 'Missing from invoice entirely - needs review'
ST_EXTRA = 'In invoice but not submitted - needs review'

STAGES = {
    'shipped': {'source': TAB_PLACED, 'dest': TAB_SHIPPED, 'line_from': L_ORDERED, 'line_to': L_SHIPPED},
    'arrived': {'source': TAB_SHIPPED, 'dest': TAB_RECEIVED, 'line_from': L_SHIPPED, 'line_to': L_RECEIVED},
}


class DuplicateInvoice(Exception):
    pass


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def _pad(row, n):
    row = list(row)
    return row + [''] * (n - len(row))


def today_et(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

def newest_lock(store):
    """Highest 'Last Order Submitted' across the three stage tabs (= the most recent lock)."""
    last = 0
    for tab in (TAB_PLACED, TAB_SHIPPED, TAB_RECEIVED):
        for r in store.get(tab)[1:]:
            last = max(last, _int(_pad(r, 2)[C_LAST]))
    return last


def order_number(name):
    m = re.search(r'(\d+)', str(name))
    return int(m.group(1)) if m else 0


def parse_order_names(text):
    return [t.strip() for t in re.split(r'[,\s]+', str(text or '')) if t.strip().lstrip('#').isdigit()]


# ── Duplicate protection ─────────────────────────────────────────────────────

def check_duplicates(store, stage, files):
    """files: [(filename, sha)]. Raises if this exact file already went through this stage."""
    seen = {}
    for r in store.get(TAB_INVOICES)[1:]:
        r = _pad(r, 6)
        seen[(r[1], r[4])] = r[0]
    for name, digest in files:
        when = seen.get((stage, digest))
        if when:
            raise DuplicateInvoice(f'"{name}" was already processed for this step on {when}. '
                                   'Nothing was changed. (Re-running the same file would move other outstanding units.)')


def record_invoices(store, stage, supplier, files, units_moved, now):
    store.append(TAB_INVOICES, [[now, stage, supplier, name, digest, units_moved if i == 0 else 0]
                                for i, (name, digest) in enumerate(files)])


# ── Matching ─────────────────────────────────────────────────────────────────

def _norm(v):
    return str(v or '').strip().upper()


def build_groups(rows, group_keys):
    """Group source rows by (supplier, SKU). `rows` are (row_index, row) pairs.
    group_keys(sku, acdd_sku) -> (keys1:set, keys2:set)."""
    groups = {}
    for idx, row in rows:
        row = _pad(row, 14)
        sku = str(row[C_SKU]).strip()
        if not sku or _int(row[C_QTY]) <= 0:
            continue
        g = groups.get(sku)
        if g is None:
            k1, k2 = group_keys(sku, str(row[C_ACDD]).strip())
            g = groups[sku] = {'sku': sku, 'rows': [], 'qty': 0, 'title': '', 'orders': [], 'k1': set(k1), 'k2': set(k2)}
        g['rows'].append(idx)
        g['qty'] += _int(row[C_QTY])
        g['title'] = g['title'] or str(row[C_TITLE])
        for o in parse_order_names(row[C_ORDERS]):
            if o not in g['orders']:
                g['orders'].append(o)
    return groups


def match_items(groups, items):
    """items: [{'key1','key2','quantity','description', ...}] (already aggregated).
    Pass 1 matches key1<->group.k1 (barcode/UPC/SKU), pass 2 matches key2<->group.k2.
    Returns (pairs [(group, item)], unmatched_groups, unmatched_items)."""
    gl = list(groups.values())
    g_done = [False] * len(gl)
    i_done = [False] * len(items)
    pairs = []
    for key_item, key_group in (('key1', 'k1'), ('key2', 'k2')):
        index = {}
        for gi, g in enumerate(gl):
            if g_done[gi]:
                continue
            for k in g[key_group]:
                if k:
                    index.setdefault(_norm(k), gi)
        for ii, it in enumerate(items):
            if i_done[ii] or not it.get(key_item):
                continue
            gi = index.get(_norm(it[key_item]))
            if gi is not None and not g_done[gi]:
                pairs.append((gl[gi], it))
                g_done[gi] = True
                i_done[ii] = True
    return (pairs,
            [g for gi, g in enumerate(gl) if not g_done[gi]],
            [it for ii, it in enumerate(items) if not i_done[ii]])


def aggregate_items(items):
    out = {}
    for it in items:
        k = (it.get('key1') or '') + '|' + (it.get('key2') or '')
        if k not in out:
            out[k] = dict(it, quantity=0)
        out[k]['quantity'] += _int(it.get('quantity'))
    return list(out.values())


def status_for(outstanding, invoice_qty):
    if outstanding == invoice_qty:
        return ST_MATCH
    return ST_MORE if invoice_qty > outstanding else ST_LESS


# ── The move ─────────────────────────────────────────────────────────────────

def process_upload(store, shopify, *, stage, supplier, items, group_keys, files, dry_run,
                   now=None, has_barcode_column=False, force=False):
    """Apply a supplier document to the sheets.

    stage:  'shipped' (Order placed -> Shipped) or 'arrived' (Shipped -> Received)
    items:  parsed document lines, each {'key1','key2','quantity','description', 'barcode'?}
    files:  [(filename, sha256)] for the duplicate guard and the audit tab
    Returns the dict the page renders."""
    cfg = STAGES[stage]
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
    if not force:
        check_duplicates(store, stage, files)

    src_tab = store.get(cfg['source'])
    src_body = [_pad(r, 14) for r in src_tab[1:]]
    candidates = [(i, r) for i, r in enumerate(src_body) if str(r[C_SUP]) == supplier]
    groups = build_groups(candidates, group_keys)
    items = aggregate_items(items)
    pairs, unmatched_groups, unmatched_items = match_items(groups, items)

    ref = ', '.join(sorted({i.get('invoice_no') for i in items if i.get('invoice_no')})) or ', '.join(f for f, _ in files)

    # 1. Decide which units move from which source rows (oldest locked cycle first).
    moves = []   # (row_index, qty)
    results = []
    for g, it in pairs:
        take = min(g['qty'], it['quantity'])
        remaining = take
        for idx in sorted(g['rows'], key=lambda i: (_int(src_body[i][C_LAST]), i)):
            if remaining <= 0:
                break
            t = min(_int(src_body[idx][C_QTY]), remaining)
            if t > 0:
                moves.append((idx, t))
                remaining -= t
        results.append(_result_row(g, it, has_barcode_column))
    for g in unmatched_groups:
        results.append(_result_row(g, None, has_barcode_column))
    for it in unmatched_items:
        results.append(_result_row(None, it, has_barcode_column))
    results.sort(key=lambda r: (str(r[0] or ''), str(r[1] or '')))
    units = sum(t for _, t in moves)
    filled = backorders_filled(store, src_body, moves, supplier, stage)

    # 2. Per-order allocation (Order Lines) and which orders are now complete.
    lines_tab = store.get(TAB_LINES)
    lines = [_pad(r, 9) for r in lines_tab[1:]]
    touched = _allocate(lines, src_body, moves, supplier, cfg)

    # 3. Sheet writes: dest first (a crash then duplicates rather than loses), then source, then lines.
    dest_rows = []
    for idx, t in moves:
        r = list(src_body[idx])
        r[C_QTY] = t
        if stage == 'shipped':
            dest_rows.append(r[:10] + [today, ref])
        else:
            dest_rows.append(r[:12] + [today, ref])
    new_src = []
    cut = {}
    for idx, t in moves:
        cut[idx] = cut.get(idx, 0) + t
    for i, r in enumerate(src_body):
        q = _int(r[C_QTY]) - cut.get(i, 0)
        if i in cut and q <= 0:
            continue
        r = list(r)
        if i in cut:
            r[C_QTY] = q
        new_src.append(r)

    if not dry_run and moves:
        store.append(cfg['dest'], dest_rows)
        store.replace_body(cfg['source'], new_src)
        store.replace_body(TAB_LINES, lines)
    if not dry_run:
        record_invoices(store, stage, supplier, files, units, now.isoformat(timespec='seconds'))

    # Tags last: sheet state is the source of truth, tagging problems are reported not fatal.
    tag_info = tag_orders(shopify, lines, touched, stage, dry_run) if touched else _empty_tags()
    out = {
        'success': True, 'dryRun': dry_run, 'skusCompared': len(results), 'results': results,
        'tagErrors': tag_info['errors'],
        'skippedAlreadyInventoryQueued': tag_info['queued'],
        'blockedByCancellationOrRefund': [],
        'removedFulfilledOrCancelled': tag_info['gone'],
        'unitsMoved': units,
        'backordersFilled': filled,
    }
    if stage == 'shipped':
        out['unitsAdvancedToShipped'] = units
        out['ordersFullyShippedAndTagged'] = [t['order'] for t in tag_info['tagged']]
        out['ordersThatWouldBeTagged'] = [t['order'] for t in tag_info['planned']]
        out['note'] = ('DRY RUN: nothing was written.' if dry_run else
                       'Units moved from "Order placed" to "Shipped from supplier". No Shopify inventory was adjusted.')
    else:
        out[('unitsThatWouldAdvanceToArrived' if dry_run else 'unitsAdvancedToArrived')] = units
        out[('ordersThatWouldBeFullyArrivedAndTagged' if dry_run else 'ordersFullyArrivedAndTagged')] = (
            tag_info['planned'] if dry_run else tag_info['tagged'])
        out['note'] = ('DRY RUN: no sheets or Shopify tags were changed.' if dry_run else
                       'Units moved from "Shipped from supplier" to "Order received". No Shopify inventory was adjusted.')
    return out


def _result_row(g, it, has_barcode_column):
    if g and it:
        row = [g['sku'], it.get('barcode', ''), g['title'] or it.get('description', ''), g['qty'], it['quantity'],
               status_for(g['qty'], it['quantity']), ', '.join(g['orders'])]
    elif g:
        row = [g['sku'], '', g['title'], g['qty'], 0, ST_MISSING, ', '.join(g['orders'])]
    else:
        row = [it.get('sku', '') or it.get('key1', ''), it.get('barcode', ''), it.get('description', ''), 0,
               it['quantity'], ST_EXTRA, '']
    return row if has_barcode_column else [row[0], row[2], row[3], row[4], row[5], row[6]]


def _allocate(lines, src_body, moves, supplier, cfg):
    """Credit moved units to the orders on the row they came from, oldest order first.
    A row only credits orders named on it, and only up to what each order still has
    outstanding at this stage, so hand-added units never get assigned to an order.
    Mutates `lines`. Returns the set of order names touched."""
    touched = set()
    for idx, t in moves:
        row = src_body[idx]
        orders = set(parse_order_names(row[C_ORDERS]))
        if not orders:
            continue
        cand = [l for l in lines
                if str(l[L_SUP]) == supplier and str(l[L_SKU]).strip() == str(row[C_SKU]).strip()
                and str(l[L_LAST]) == str(row[C_LAST]) and str(l[L_LOCK]) == str(row[C_LOCK])
                and str(l[L_ORDER]) in orders]
        cand.sort(key=lambda l: order_number(l[L_ORDER]))
        left = t
        for l in cand:
            cap = _int(l[cfg['line_from']]) - _int(l[cfg['line_to']])
            if cap <= 0 or left <= 0:
                continue
            a = min(cap, left)
            l[cfg['line_to']] = _int(l[cfg['line_to']]) + a
            left -= a
            touched.add(str(l[L_ORDER]))
    return touched


# ── Shopify tagging ──────────────────────────────────────────────────────────

def _empty_tags():
    return {'tagged': [], 'planned': [], 'errors': [], 'queued': [], 'gone': []}


def determine_arrived_tag(line_items, now=None):
    """Same preorder-hold rule as the original tracker."""
    now = now or datetime.now(timezone.utc)
    latest = None
    for item in line_items:
        tags = (item.get('product') or {}).get('tags') or []
        if 'preorder' not in [t.lower() for t in tags]:
            continue
        for t in tags:
            m = RELEASE_DATE_TAG_RE.match(t.strip())
            if m:
                try:
                    d = datetime.strptime(m.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if latest is None or d > latest:
                    latest = d
    if latest is not None and latest - now > timedelta(days=HOLD_WINDOW_DAYS):
        return 'order-received-preorder'
    return 'order-received'


def order_is_complete(order_lines, stage, shopify_qty):
    """Every line the order owes has reached `stage`. Quantities are capped by what
    Shopify says the order still contains, so refunded units don't hold an order back.
    An order with nothing live (or no recorded lines) is never complete."""
    col = STAGES[stage]['line_to']
    live = False
    for l in order_lines:
        need = min(_int(l[L_QTY]), shopify_qty.get(str(l[L_SKU]).strip(), 0))
        if need <= 0:
            continue
        live = True
        if _int(l[col]) < need:
            return False
    return live


def tag_orders(shopify, lines, touched, stage, dry_run):
    res = _empty_tags()
    if shopify is None:
        return res
    names = sorted(touched, key=order_number)
    by_order = {}
    for l in lines:
        by_order.setdefault(str(l[L_ORDER]), []).append(l)
    try:
        details = shopify.get_orders(names)
    except Exception as e:
        res['errors'].append({'order': '(all)', 'error': f'Shopify lookup failed: {e}'})
        return res
    for name in names:
        d = details.get(name)
        if d is None:
            res['errors'].append({'order': name, 'error': 'Order not found in Shopify'})
            continue
        if d['cancelled'] or d['fulfillment'] == 'FULFILLED':
            res['gone'].append(name)
            continue
        qty = {}
        for li in d['lines']:
            sku = str(li.get('sku') or '').strip()
            qty[sku] = qty.get(sku, 0) + (li.get('currentQuantity') or 0)
        if not order_is_complete(by_order.get(name, []), stage, qty):
            continue
        current = next((t for t in d['tags'] if t.startswith('dhg-status-') and t not in EMAIL_LIFECYCLE_TAGS), None)
        status = current.replace('dhg-status-', '') if current else None
        if status == 'inventory-queued':
            res['queued'].append(name)
            continue
        if stage == 'shipped':
            if status and status.startswith('order-received'):
                continue                           # never move an order backwards
            if SHIPPED_TAG in d['tags']:
                continue
            tag = SHIPPED_TAG
        else:
            tag = f"dhg-status-{determine_arrived_tag(d['lines'])}"
            if tag in d['tags']:
                continue
        try:
            if dry_run:
                res['planned'].append({'order': name, 'wouldRemove': current, 'wouldApply': tag})
            else:
                if current:
                    shopify.tags_remove(d['id'], [current])
                shopify.tags_add(d['id'], [tag])
                res['tagged'].append({'order': name, 'tag': tag})
        except Exception as e:
            res['errors'].append({'order': name, 'error': str(e)})
    return res


# ── OCR SKU helpers (image-only quote PDFs) ──────────────────────────────────

_OCR_CONFUSE = str.maketrans({'O': '0', 'I': '1', 'L': '1', 'S': '5', 'B': '8', 'Z': '2'})


def _ocr_key(sku):
    import re as _re
    return _re.sub(r'(.)\1+', r'\1', str(sku).upper().translate(_OCR_CONFUSE))


def snap_ocr_skus(quoted, locked):
    """OCR reads 0 as O, drops/duplicates a character, etc. For a quoted SKU that is not locked,
    adopt the locked SKU whose OCR-normalised form is identical, but only when exactly one
    locked SKU fits and the quote doesn't already list that SKU. -> (quoted, [(read, adopted)])"""
    out, snapped = dict(quoted), []
    for sku in sorted(quoted):
        if sku in locked:
            continue
        cands = [l for l in locked if l not in quoted and _ocr_key(l) == _ocr_key(sku)]
        if len(cands) == 1:
            out[cands[0]] = out.get(cands[0], 0) + out.pop(sku)
            snapped.append({'read': sku, 'adopted': cands[0]})
    return out, snapped


def backorders_filled(store, src_body, moves, supplier, stage):
    """Units this document moves that belong to an EARLIER lock than the newest one, or that were marked
    backorder/preorder: i.e. a backorder or preorder being filled. Reported, no extra writes (the normal
    oldest-lock-first move already takes them out of Order placed and into the shipment's rows)."""
    if not moves:
        return []
    newest = newest_lock(store)
    out = {}
    for idx, t in moves:
        r = src_body[idx]
        marked = stage == 'shipped' and bool(str(r[C_BACKORDER]).strip())
        if _int(r[C_LAST]) >= newest and not marked:
            continue
        sku = str(r[C_SKU]).strip()
        g = out.setdefault(sku, {'sku': sku, 'title': str(r[C_TITLE]), 'units': 0, 'locks': [], 'orders': []})
        g['units'] += t
        if str(r[C_LOCK]) not in g['locks']:
            g['locks'].append(str(r[C_LOCK]))
        for o in parse_order_names(r[C_ORDERS]):
            if o not in g['orders']:
                g['orders'].append(o)
    return sorted(out.values(), key=lambda g: g['sku'])


# ── Backorder / preorder list ────────────────────────────────────────────────

def backorder_list(store, shopify=None, supplier=None):
    """Units customers have ordered that the supplier has not shipped, for orders that are still open.

    Derived, never stored: an Order Lines row (Ordered - Shipped > 0) is listed when it belongs to a lock
    older than the newest one (still unshipped a cycle later) or its Order placed row was marked at a quote
    check (column K). Live Shopify then drops cancelled and fulfilled orders and caps each line at what the
    order still has unfulfilled and un-refunded."""
    placed = [_pad(r, 14) for r in store.get(TAB_PLACED)[1:]]
    lines = [_pad(r, 9) for r in store.get(TAB_LINES)[1:]]
    newest = max([_int(r[C_LAST]) for r in placed] + [_int(l[L_LAST]) for l in lines] + [0])
    marked, titles = {}, {}
    for r in placed:
        sku = str(r[C_SKU]).strip()
        titles.setdefault((str(r[C_SUP]), sku), str(r[C_TITLE]))
        if str(r[C_BACKORDER]).strip():
            marked[(_int(r[C_LAST]), str(r[C_SUP]), sku)] = str(r[C_BACKORDER]).strip()
    cand = []
    for l in lines:
        sup, sku = str(l[L_SUP]), str(l[L_SKU]).strip()
        if supplier and sup != supplier:
            continue
        owed = _int(l[L_ORDERED]) - _int(l[L_SHIPPED])
        last = _int(l[L_LAST])
        mk = marked.get((last, sup, sku), '')
        if owed <= 0 or (last >= newest and not mk):
            continue
        cand.append({'line': l, 'owed': owed, 'last': last, 'marked': mk, 'carried': last < newest})

    shop, checked = {}, False
    if shopify is not None and cand:
        try:
            shop = shopify.get_orders(sorted({str(c['line'][L_ORDER]) for c in cand}))
            checked = True
        except Exception:
            shop = {}
    unverified, groups = set(), {}
    for c in cand:
        l, owed = c['line'], c['owed']
        order, sup, sku = str(l[L_ORDER]), str(l[L_SUP]), str(l[L_SKU]).strip()
        kind, release, stitle = KIND_BACKORDER, '', ''
        if checked:
            d = shop.get(order)
            if d is None:
                unverified.add(order)
            elif d['cancelled'] or d['fulfillment'] == 'FULFILLED':
                continue
            else:
                cap, found = 0, False
                for li in d['lines']:
                    if str(li.get('sku') or '').strip() != sku:
                        continue
                    found = True
                    q = li.get('currentQuantity') or 0
                    u = li.get('unfulfilledQuantity')
                    cap += q if u is None else min(q, u)
                    stitle = stitle or str(li.get('title') or '')
                    tags = (li.get('product') or {}).get('tags') or []
                    if 'preorder' in [t.lower() for t in tags]:
                        kind = KIND_PREORDER
                    for t in tags:
                        m = RELEASE_DATE_TAG_RE.match(t.strip())
                        if m and m.group(1) > release:
                            release = m.group(1)
                if not found:
                    continue
                owed = min(owed, cap)
        if owed <= 0:
            continue
        g = groups.setdefault((sup, sku), {
            'supplier': sup, 'sku': sku, 'title': '', 'qty': 0, 'carried': 0, 'markedNow': 0, 'orders': [],
            'since': str(l[L_LOCK]), '_since': c['last'], 'markedOn': '', 'kind': KIND_BACKORDER, 'releaseDate': ''})
        g['qty'] += owed
        g['carried' if c['carried'] else 'markedNow'] += owed
        g['orders'].append({'order': order, 'qty': owed})
        g['title'] = g['title'] or titles.get((sup, sku), '') or stitle
        if c['last'] < g['_since']:
            g['_since'], g['since'] = c['last'], str(l[L_LOCK])
        if c['marked'] and c['marked'] > g['markedOn']:
            g['markedOn'] = c['marked']
        if kind == KIND_PREORDER:
            g['kind'] = KIND_PREORDER
        if release > g['releaseDate']:
            g['releaseDate'] = release
    items = []
    for g in groups.values():
        g.pop('_since')
        g['orders'].sort(key=lambda o: order_number(o['order']))
        g['orderNames'] = ', '.join(o['order'] for o in g['orders'])
        g['origin'] = ' + '.join(x for x, on in (('Carried over from an earlier lock', g['carried']),
                                                 ('Marked at quote check', g['markedNow'])) if on)
        items.append(g)
    items.sort(key=lambda g: (g['supplier'], g['releaseDate'] or '9999', g['sku']))
    return {'success': True, 'items': items, 'skus': len(items), 'unitsOwed': sum(g['qty'] for g in items),
            'ordersAffected': len({o['order'] for g in items for o in g['orders']}),
            'newestLock': newest, 'shopifyChecked': checked, 'unverifiedOrders': sorted(unverified)}


def mark_backorders(store, *, skus, clear=(), supplier='Asmodee', today=None):
    """Set / clear the 'Backorder Since' marker (Order placed column K) on the newest lock's rows for these
    SKUs. Targeted cell writes only, so rows written meanwhile by close-cycle are never overwritten.
    A row already marked keeps its original date."""
    skus, clear = {str(x).strip() for x in skus}, {str(x).strip() for x in clear}
    tab = store.get(TAB_PLACED)
    header = _pad(tab[0] if tab else [], 14)
    body = [_pad(r, 14) for r in tab[1:]]
    latest = 0
    for t in (TAB_PLACED, TAB_SHIPPED, TAB_RECEIVED):
        for r in store.get(t)[1:]:
            r = _pad(r, 3)
            if str(r[C_SUP]) == supplier:
                latest = max(latest, _int(r[C_LAST]))
    if not latest:
        return {'warning': 'No locked order was found. Close a cycle first.'}
    today = today or today_et()
    updates, marked, cleared, seen = {}, [], [], set()
    for i, r in enumerate(body):
        sku = str(r[C_SKU]).strip()
        if str(r[C_SUP]) != supplier or _int(r[C_LAST]) != latest:
            continue
        cell = f'K{i + 2}'
        if sku in skus and not str(r[C_BACKORDER]).strip():
            updates[cell] = today
            marked.append(sku)
        elif sku in clear and str(r[C_BACKORDER]).strip():
            updates[cell] = ''
            cleared.append(sku)
        if sku in skus or sku in clear:
            seen.add(sku)
    if updates and not str(header[C_BACKORDER]).strip():
        updates['K1'] = BACKORDER_HEADER
    if updates:
        store.update_cells(TAB_PLACED, updates)
    return {'success': True, 'marked': sorted(set(marked)), 'cleared': sorted(set(cleared)),
            'notOnOrderPlaced': sorted((skus | clear) - seen), 'comparedAgainstLock': latest}


# ── Asmodee quote check (read only) ──────────────────────────────────────────

ST_Q_MATCH = 'Match'
ST_Q_RELEASED = 'Backorder released'
ST_Q_MARKED = 'Not quoted (marked backorder/preorder)'
ST_Q_MISSING = 'Locked but missing from quote'
ST_Q_EXTRA = 'In quote but not locked'
ST_Q_QTY = 'Quantity mismatch'
_Q_REVIEW = (ST_Q_QTY, ST_Q_MISSING, ST_Q_EXTRA)
_Q_ORDER = {ST_Q_QTY: 0, ST_Q_MISSING: 1, ST_Q_EXTRA: 2, ST_Q_RELEASED: 3, ST_Q_MARKED: 4, ST_Q_MATCH: 5}


def verify_quote(store, quote_items, ocr=False, backorders=None):
    """Compare a Sales Quote with the most recent locked Asmodee cycle. Looks at all three tabs, so it
    still works after part of the order has shipped. `backorders` is the backorder_list() items: units
    still owed from earlier locks explain a quote that is larger than the lock (a released backorder),
    and a locked item marked backorder/preorder that the quote omits is expected, not a problem."""
    rows = []
    for tab in (TAB_PLACED, TAB_SHIPPED, TAB_RECEIVED):
        rows += [(tab, _pad(r, 14)) for r in store.get(tab)[1:] if str(_pad(r, 3)[C_SUP]) == 'Asmodee']
    if not rows:
        return {'warning': 'No locked Asmodee order was found. Close a cycle first.'}
    latest = max(_int(r[C_LAST]) for _, r in rows)
    block = [(t, r) for t, r in rows if _int(r[C_LAST]) == latest]
    lock_date = block[0][1][C_LOCK]
    locked, flagged, titles = {}, set(), {}
    for tab, r in block:
        sku = str(r[C_SKU]).strip()
        d = locked.setdefault(sku, {'qty': 0, 'orders': []})
        d['qty'] += _int(r[C_QTY])
        titles.setdefault(sku, str(r[C_TITLE]))
        if tab == TAB_PLACED and str(r[C_BACKORDER]).strip():
            flagged.add(sku)
        for o in parse_order_names(r[C_ORDERS]):
            if o not in d['orders']:
                d['orders'].append(o)
    carried = {b['sku']: b['carried'] for b in (backorders or [])
               if b.get('supplier') == 'Asmodee' and b.get('carried', 0) > 0}
    owed_titles = {b['sku']: b.get('title', '') for b in (backorders or []) if b.get('supplier') == 'Asmodee'}
    quoted = {}
    for it in quote_items:
        sku = str(it.get('sku') or '').strip()
        quoted[sku] = quoted.get(sku, 0) + _int(it.get('quantity'))
    qdesc = {}
    for it in quote_items:
        qdesc.setdefault(str(it.get('sku') or '').strip(), it.get('description', ''))
    snapped = []
    if ocr:
        quoted, snapped = snap_ocr_skus(quoted, locked)
    lines, to_mark, to_clear = [], [], []
    for sku in sorted(set(locked) | set(quoted)):
        l = locked.get(sku, {}).get('qty', 0)
        q = quoted.get(sku, 0)
        c = carried.get(sku, 0)
        f = sku in flagged
        if q == l:
            status = ST_Q_MATCH
        elif q == 0:
            status = ST_Q_MARKED if f else ST_Q_MISSING
        elif q > l and q - l <= c:
            status = ST_Q_RELEASED
        elif l == 0:
            status = ST_Q_EXTRA
        else:
            status = ST_Q_QTY
        if status == ST_Q_MISSING:
            to_mark.append(sku)
        if f and l > 0 and q >= l:
            to_clear.append(sku)
        lines.append({'sku': sku, 'issue': status, 'status': status, 'lockedQty': l, 'quotedQty': q,
                      'backorderOwed': c, 'marked': f,
                      'title': titles.get(sku) or qdesc.get(sku, '') or owed_titles.get(sku, ''),
                      'orderNames': ', '.join(locked.get(sku, {}).get('orders', []))})
    lines.sort(key=lambda x: (_Q_ORDER[x['status']], x['sku']))
    review = [x for x in lines if x['status'] in _Q_REVIEW]
    return {
        'success': True, 'itemsInQuote': len(quote_items), 'skusLocked': len(locked), 'lines': lines,
        'matchCount': sum(1 for x in lines if x['status'] == ST_Q_MATCH),
        'releasedCount': sum(1 for x in lines if x['status'] == ST_Q_RELEASED),
        'markedCount': sum(1 for x in lines if x['status'] == ST_Q_MARKED),
        'mismatchCount': len(review), 'clean': not review, 'mismatches': review,
        'toMark': to_mark, 'toClear': to_clear, 'backorderListChecked': backorders is not None,
        'comparedAgainstSupplierOrderId': f'the {lock_date} lock (through order #{latest})',
        'source': 'ocr' if ocr else 'text', 'ocrSnapped': snapped,
    }
