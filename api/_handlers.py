"""HTTP glue: parse the upload, call the engine, shape the JSON the page expects."""
import json
import os
import sys
import traceback
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _core as core  # noqa: E402


def parse_multipart(content_type, body):
    """-> ([(filename, bytes)], {field: str})"""
    msg = BytesParser(policy=email_policy).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    files, fields = [], {}
    for part in msg.iter_parts():
        name = part.get_param('name', header='content-disposition')
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b''
        if filename:
            files.append((filename, payload))
        elif name:
            fields[name] = payload.decode('utf-8', 'replace')
    return files, fields


def truthy(v):
    return str(v or '').strip().lower() in ('1', 'true', 'on', 'yes')


# ── Per-supplier matching rules ──────────────────────────────────────────────

def asmodee_items(files):
    import _parsers
    items, parsed = [], []
    for name, data in files:
        got = _parsers.parse_asmodee_quote(data)      # also accepts a Sales Quote PDF
        parsed.append({'filename': name, 'lineItems': len(got)})
        items += [{'key1': i['sku'], 'key2': '', 'quantity': i['quantity'], 'description': i.get('description', ''),
                   'barcode': i.get('barcode', ''), 'sku': i['sku']} for i in got]
    return items, parsed


def ud_items(files):
    import _parsers
    items, parsed = [], []
    for name, data in files:
        got = _parsers.parse_ud_invoice(data)
        parsed.append({'filename': name, 'lineItems': len(got)})
        items += [{'key1': i['barcode'], 'key2': i['sku'], 'quantity': i['quantity'], 'description': i['description'],
                   'barcode': i['barcode'], 'sku': i['sku']} for i in got]
    return items, parsed


def acdd_items(files):
    import _parsers
    items, parsed = [], []
    for name, data in files:
        invoice_no, got = _parsers.parse_acdd_invoice(data)
        parsed.append({'filename': name, 'lineItems': len(got)})
        # Old layout: match on the UPC. Current layout: no UPC, match on the product code
        # (the sheet's SKU / ACDD SKU); key2 is the same code with a wrapped hyphen kept.
        items += [{'key1': i['upc'] or i['code'], 'key2': i['upc'] or i['code_hy'] or i['code'],
                   'quantity': i['quantity'], 'description': i['description'],
                   'barcode': i['upc'], 'sku': i['code'], 'invoice_no': invoice_no} for i in got]
    return items, parsed


def asmodee_keys(store, shopify):
    return lambda sku, acdd: ({sku}, set())


def ud_keys(store, shopify):
    import _tio as _io
    rows = _io.read_range(_io.PRODUCT_SKUS_SHEET_ID, "'Universal Dist'!A1:L")
    barcode = {}
    if rows:
        h = rows[0]
        si = h.index('Variant SKU') if 'Variant SKU' in h else None
        bi = h.index('Barcode') if 'Barcode' in h else None
        if si is not None and bi is not None:
            for r in rows[1:]:
                if len(r) > max(si, bi) and r[si] and r[bi]:
                    barcode[r[si].strip()] = r[bi].strip()
    return lambda sku, acdd: ({barcode.get(sku, '')}, {sku})


def acdd_keys(store, shopify, stage):
    """Older invoices carry only a UPC: ACDD item id -> UPC through the catalog ("Garland"
    tab); rows whose ACDD SKU is blank fall back to the Shopify variant barcode. Current
    invoices carry the product code instead, which is the row's ACDD SKU or its SKU."""
    import _tio as _io
    g = _io.read_range(_io.PRODUCT_SKUS_SHEET_ID, "'Garland'!A1:G")
    upc_by_item = {}
    if g:
        h = g[0]
        ii, ui = h.index('ItemID'), h.index('UPC')
        for r in g[1:]:
            if len(r) > max(ii, ui) and r[ii] and r[ui]:
                upc_by_item[r[ii].strip()] = r[ui].strip()
    src = store.get(core.STAGES[stage]['source'])
    blank = {str(r[core.C_SKU]).strip() for r in src[1:] if len(r) > core.C_ACDD and str(r[core.C_SUP]) == 'ACDD' and not str(r[core.C_ACDD]).strip()}
    barcodes = shopify.barcodes_for_skus(blank) if blank and shopify else {}
    return lambda sku, acdd: ({upc_by_item.get(acdd, ''), acdd, sku}, {barcodes.get(sku, ''), acdd, sku})


SUPPLIERS = {
    'asmodee': ('Asmodee', asmodee_items, False),
    'ud': ('Universal Dist', ud_items, True),
    'acdd': ('ACDD', acdd_items, True),
}


def run_upload(kind, stage, files, fields):
    import _tio as _io
    supplier, parse, has_barcode = SUPPLIERS[kind]
    if not files:
        return 400, {'error': 'No file found in upload'}
    digests = [(n, core.sha256(d)) for n, d in files]
    items, parsed = parse(files)
    unreadable = [p['filename'] for p in parsed if not p['lineItems']]
    if unreadable:
        # Never carry on with an empty parse: it would show "nothing here" and (when not a dry
        # run) record the file as processed, which then blocks the re-upload after a fix.
        return 422, {'error': 'No line items could be read from: ' + ', '.join(unreadable) +
                              '. The supplier may have changed the document layout. Nothing was changed.'}
    store, shopify = _io.SheetsStore(), _io.Shopify()
    keys = acdd_keys(store, shopify, stage) if kind == 'acdd' else (ud_keys(store, shopify) if kind == 'ud' else asmodee_keys(store, shopify))
    out = core.process_upload(
        store, shopify, stage=stage, supplier=supplier, items=items, group_keys=keys, files=digests,
        dry_run=truthy(fields.get('dry_run')), has_barcode_column=has_barcode,
        force=truthy(fields.get('force')), now=_now())
    out.update({'itemsInQuote': len(items), 'itemsInInvoice': len(items), 'itemsInInvoices': len(items), 'invoicesParsed': parsed})
    return 200, out


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _backorders_for_quote(store):
    """The backorder list for the quote check; None if it can't be built (the check still runs)."""
    import _tio as _io
    try:
        return core.backorder_list(store, _io.Shopify(), supplier='Asmodee')['items']
    except Exception:
        return None


def run_verify(files, fields):
    import _tio as _io
    import _parsers
    ocr_text = fields.get('ocr_text')
    if ocr_text:                                   # browser OCR of an image-only PDF
        quote = _parsers.parse_ocr_quote_text(ocr_text)
        if not quote:
            return 200, {'warning': 'OCR could not find any line items on this quote. Try a clearer copy, or the original PDF from Asmodee.'}
        store = _io.SheetsStore()
        return 200, core.verify_quote(store, quote, ocr=True, backorders=_backorders_for_quote(store))
    if not files:
        return 400, {'error': 'No file found in upload'}
    data = files[0][1]
    if not _parsers.pdf_has_text(data):
        return 200, {'needsOcr': True}
    quote = _parsers.parse_asmodee_quote(data)
    if not quote:
        return 200, {'warning': 'No line items could be read from this PDF, so nothing was compared. Check it is an Asmodee Sales Quote.'}
    store = _io.SheetsStore()
    return 200, core.verify_quote(store, quote, backorders=_backorders_for_quote(store))


def run_backorders(fields):
    """action=list (default): the backorder/preorder list. action=mark: mark/clear SKUs on the newest lock."""
    import _tio as _io
    store = _io.SheetsStore()
    if (fields.get('action') or 'list') == 'mark':
        skus = json.loads(fields.get('skus') or '[]')
        clear = json.loads(fields.get('clear') or '[]')
        return 200, core.mark_backorders(store, skus=skus, clear=clear)
    return 200, core.backorder_list(store, _io.Shopify(), supplier=(fields.get('supplier') or '').strip() or None)


def _cors(h):
    h.send_header('Access-Control-Allow-Origin', '*')
    h.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
    h.send_header('Access-Control-Allow-Headers', 'Content-Type')


def _send(h, status, data):
    h.send_response(status)
    _cors(h)
    h.send_header('Content-Type', 'application/json')
    h.end_headers()
    h.wfile.write(json.dumps(data).encode())


def handle_options(h):
    h.send_response(200)
    _cors(h)
    h.end_headers()


def handle_post(h, kind, stage=None):
    """Body of do_POST for every endpoint file (each file declares its own
    `class handler(BaseHTTPRequestHandler)` so Vercel can detect the entrypoint)."""
    try:
        ctype = h.headers.get('Content-Type', '')
        if 'multipart/form-data' not in ctype:
            return _send(h, 400, {'error': 'Expected multipart/form-data'})
        body = h.rfile.read(int(h.headers.get('Content-Length', 0)))
        files, fields = parse_multipart(ctype, body)
        if kind == 'verify':
            status, data = run_verify(files, fields)
        elif kind == 'backorders':
            status, data = run_backorders(fields)
        else:
            status, data = run_upload(kind, stage, files, fields)
        _send(h, status, data)
    except core.DuplicateInvoice as e:
        _send(h, 409, {'error': str(e)})
    except Exception as e:
        _send(h, 500, {'error': str(e), 'trace': traceback.format_exc()})


def make_handler(kind, stage=None):
    """Used by tests; the deployed files declare the class literally."""
    class handler(BaseHTTPRequestHandler):
        def do_POST(self): handle_post(self, kind, stage)
        def do_OPTIONS(self): handle_options(self)
    return handler
