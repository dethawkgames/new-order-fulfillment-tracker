"""PDF / spreadsheet parsers, copied unchanged from the original tracker's
reconcile.py (Asmodee quote + invoice) and reconcile-ud.py (Universal Dist
invoice), whose column positions were validated against real documents."""
import io
import re
import pdfplumber
import openpyxl

SKU_X = 43.7
DESC_X = 100  # was 102.8, which sat just above real word positions (~102.757)
              # for the first description word, silently dropping it
GTIN_X_MIN = 240
GTIN_X_MAX = 260
QTY_X_MIN = 320
QTY_X_MAX = 345

# ── PDF parsing (Sales Quote format - no GTIN/barcode column at all, so it
# cannot be detected the same way as the invoice. Columns verified 8/13/26
# against a real Sales Quote PDF: No. (SKU) ~43.2, Description ~101.6,
# MSRP ~360, Quantity ~420, Unit ~440 (always "Each"), Unit Price ~483,
# Line Amount ~555. SKU/description wrap onto their own continuation line
# the same way the invoice does. ──────────────────────────────────────────

QUOTE_SKU_X = 43.2
QUOTE_DESC_X_MIN = 95
QUOTE_DESC_X_MAX = 350

def parse_asmodee_sales_quote(file_bytes):
    """Parses the Asmodee Sales Quote format (pre-shipment document, no
    GTIN column). Normally this only informs the Lock stage / Latest
    Reconciliation tab for reference - the Sales Invoice is what should
    drive Shipped. Iain can choose to run a Sales Quote through the same
    Shipped-advancing flow when he has independent proof of shipment
    (e.g. a UPS notice) and the invoice hasn't arrived yet."""
    line_items = []
    hit_total = False
    header_seen = False
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if hit_total:
                break
            words = page.extract_words()
            lines = {}
            for w in words:
                top_key = round(w['top'])
                lines.setdefault(top_key, []).append(w)
            sorted_tops = sorted(lines.keys())
            current_item = None
            for top in sorted_tops:
                row_words = sorted(lines[top], key=lambda w: w['x0'])
                row_text = ' '.join(w['text'] for w in row_words)
                stripped = row_text.strip()
                if stripped.startswith('Total') and '$' in stripped:
                    hit_total = True
                    break
                if stripped.startswith('No.') and 'Description' in stripped:
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                if stripped.startswith('Home Page') or stripped.startswith('www.') or 'customerservice@' in stripped:
                    continue
                unit_word = next((w for w in row_words if w['text'] == 'Each'), None)
                sku_word = next((w for w in row_words if abs(w['x0'] - QUOTE_SKU_X) < 2), None)
                if unit_word and sku_word:
                    qty_candidates = [w for w in row_words
                                       if w['x1'] <= unit_word['x0'] and w['x0'] > 390
                                       and w['text'].replace('.', '', 1).isdigit()]
                    qty_word = qty_candidates[-1] if qty_candidates else None
                    if current_item:
                        line_items.append(current_item)
                    desc_words = [w['text'] for w in row_words
                                  if w is not sku_word and QUOTE_DESC_X_MIN <= w['x0'] < QUOTE_DESC_X_MAX]
                    qty_val = qty_word['text'] if qty_word else None
                    current_item = {
                        'sku': sku_word['text'],
                        'description': ' '.join(desc_words),
                        'quantity': int(qty_val) if qty_val and qty_val.isdigit() else qty_val,
                    }
                elif current_item is not None:
                    other_words = [w for w in row_words if w is not sku_word]
                    if sku_word and not other_words:
                        frag = sku_word['text']
                        if len(frag) <= 12:
                            current_item['sku'] = current_item['sku'] + frag
                    else:
                        desc_words = [w['text'] for w in row_words
                                      if QUOTE_DESC_X_MIN <= w['x0'] < QUOTE_DESC_X_MAX]
                        if desc_words:
                            current_item['description'] = (current_item['description'] + ' ' + ' '.join(desc_words)).strip()
            if current_item:
                line_items.append(current_item)
    return line_items

def pdf_has_text(file_bytes):
    """False for image-only PDFs (printed/scanned/outlined text) - pdfplumber sees no words."""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        return any(page.extract_words() for page in pdf.pages)


_OCR_ROW = re.compile(r'^(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]*)\s+(?P<desc>.+?)\s+(?P<msrp>\d+\.\d\d)\s+(?P<qty>\d+)\s+Each\b')
_OCR_SKU_TAIL = re.compile(r'^[A-Z0-9]{1,6}$')


def parse_ocr_quote_text(text):
    """Parses OCR text of an Asmodee Sales Quote (the browser OCRs image-only PDFs and
    sends the text). Same output shape as parse_asmodee_sales_quote. A SKU that wraps onto
    its own line (e.g. 'ODGTTS010' / '1EN') is a short alphanumeric token containing a digit;
    wrapped description words ('Pack', 'Man') have none."""
    items, current, in_table = [], None, False
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('No.') and 'Description' in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith('Total') and '$' in line:
            break
        if line.startswith('Home Page') or line.startswith('www.') or 'customerservice@' in line:
            continue
        m = _OCR_ROW.match(line)
        if m:
            current = {'sku': m.group('sku'), 'description': m.group('desc'), 'quantity': int(m.group('qty'))}
            items.append(current)
        elif current is not None:
            if _OCR_SKU_TAIL.match(line) and any(ch.isdigit() for ch in line):
                current['sku'] += line
            else:
                current['description'] = (current['description'] + ' ' + line).strip()
    return items


def _is_sales_quote_pdf(file_bytes):
    """Detect Sales Quote vs Sales Invoice from the document title on page 1."""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = (pdf.pages[0].extract_text() or '')[:200]
    return 'Sales Quote' in text

def parse_asmodee_quote(file_bytes):
    """Despite the name (kept for compatibility with the rest of this file),
    this parses the Asmodee INVOICE format by default - the shipment-
    confirmation document. Auto-detects and dispatches to the Sales Quote
    parser when a Quote PDF is uploaded instead (see parse_asmodee_sales_quote
    for why that's sometimes intentional)."""
    if _is_sales_quote_pdf(file_bytes):
        return parse_asmodee_sales_quote(file_bytes)
    line_items = []
    hit_subtotal = False
    header_seen = False  # persists across pages - the header row only appears once
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if hit_subtotal:
                break
            words = page.extract_words()
            lines = {}
            for w in words:
                top_key = round(w['top'])
                lines.setdefault(top_key, []).append(w)
            sorted_tops = sorted(lines.keys())
            current_item = None
            for top in sorted_tops:
                row_words = sorted(lines[top], key=lambda w: w['x0'])
                row_text = ' '.join(w['text'] for w in row_words)
                if row_text.strip().startswith('Subtotal'):
                    hit_subtotal = True
                    break
                if row_text.strip().startswith('No.') and 'Description' in row_text:
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                if row_text.strip().startswith('Home Page'):
                    continue
                gtin_word = next((w for w in row_words if GTIN_X_MIN <= w['x0'] <= GTIN_X_MAX), None)
                qty_word = next((w for w in row_words if QTY_X_MIN <= w['x0'] <= QTY_X_MAX), None)
                sku_word = next((w for w in row_words if abs(w['x0'] - SKU_X) < 2), None)
                if gtin_word and qty_word and sku_word:
                    if current_item:
                        line_items.append(current_item)
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < 180]
                    qty_val = qty_word['text']
                    current_item = {
                        'sku': sku_word['text'],
                        'barcode': gtin_word['text'],
                        'description': ' '.join(desc_words),
                        'quantity': int(qty_val) if qty_val.isdigit() else qty_val,
                    }
                elif not gtin_word and not qty_word and current_item is not None:
                    if sku_word:
                        fragment = sku_word['text']
                        if len(fragment) <= 6 and fragment.isalnum():
                            current_item['sku'] = current_item['sku'] + fragment
                    desc_words = [w['text'] for w in row_words
                                  if w is not sku_word and w['x0'] >= DESC_X and w['x0'] < 180]
                    if desc_words:
                        current_item['description'] = (current_item['description'] + ' ' + ' '.join(desc_words)).strip()
            if current_item:
                line_items.append(current_item)
    return line_items


NON_PRODUCT_ITEM_NOS = {'41040'}
NON_PRODUCT_DESCRIPTIONS = {'2% cash discount reversal'}

# ── Invoice parsing (unchanged from v1) ───────────────────────────────────────

def parse_ud_invoice(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h).strip() if h is not None else '' for h in rows[0]]

    def col(name):
        for i, h in enumerate(header):
            if h.lower() == name.lower():
                return i
        return None

    item_no_idx = col('Item No.')
    vendor_item_idx = col('Vendor Item No.')
    product_idx = col('Product')
    qty_idx = col('Quantity')
    if item_no_idx is None or vendor_item_idx is None or qty_idx is None:
        raise ValueError(f"Couldn't find expected columns in invoice header: {header}")

    items = []
    for row in rows[1:]:
        if row is None or all(c is None for c in row):
            continue
        item_no = str(row[item_no_idx]).strip() if row[item_no_idx] is not None else ''
        vendor_item = str(row[vendor_item_idx]).strip() if row[vendor_item_idx] is not None else ''
        product = str(row[product_idx]).strip() if (product_idx is not None and row[product_idx] is not None) else ''
        qty_raw = row[qty_idx] if qty_idx is not None else None
        if not vendor_item and not item_no:
            continue
        if item_no in NON_PRODUCT_ITEM_NOS:
            continue
        if product.strip().lower() in NON_PRODUCT_DESCRIPTIONS:
            continue
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            continue
        items.append({'barcode': item_no, 'sku': vendor_item, 'description': product, 'quantity': qty})
    return items

def aggregate_invoice_items(all_items):
    combined = {}
    for item in all_items:
        key = item['barcode'] or item['sku']
        if not key:
            continue
        if key not in combined:
            combined[key] = {'barcode': item['barcode'], 'sku': item['sku'], 'description': item['description'], 'quantity': 0}
        combined[key]['quantity'] += item['quantity']
    return combined



# ── ACD Distribution (ACDD) invoice / packing slip ───────────────────────────
# Layout verified 10/13/26 against ACD invoice INV0001898754. Each line item is
#   <product code in a barcode font> <qty shipped> <description> <msrp> <price> <pricing> <amount>
# followed on the next line by the item's 12-digit UPC. The product code column is
# barcode-font gibberish when extracted, so the UPC line is the reliable identifier
# (it matches the "UPC" column of the Garland catalog tab, whose "ItemID" is the ACDD SKU).
import re as _re

_ACDD_ITEM_RE = _re.compile(r'^(\S.*?)\s+(\d+)\s+(.+?)\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s+([A-Z]{2,4})\s+(\d+\.\d{2})$')
_ACDD_UPC_RE = _re.compile(r'^\d{8,14}$')


def parse_acdd_invoice(file_bytes):
    """Returns (invoice_number, [{'upc', 'quantity', 'description'}])."""
    items = []
    invoice_no = ''
    pending = None
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text() or '').split('\n'):
                line = raw.strip()
                m = _re.search(r'Invoice number:\s*(\S+)', line)
                if m and not invoice_no:
                    invoice_no = m.group(1)
                if line.startswith('Subtotal'):
                    pending = None
                    return invoice_no, items
                mi = _ACDD_ITEM_RE.match(line)
                if mi:
                    pending = {'upc': '', 'quantity': int(mi.group(2)), 'description': mi.group(3)}
                    items.append(pending)
                    continue
                if pending is not None and not pending['upc'] and _ACDD_UPC_RE.match(line):
                    pending['upc'] = line
                    pending = None
    return invoice_no, items
