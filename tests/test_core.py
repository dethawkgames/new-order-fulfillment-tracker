import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
import _core as c  # noqa: E402
import _handlers as h  # noqa: E402

H14 = ['Lock Date', 'Last Order Submitted', 'Supplier', 'SKU', 'ACDD SKU', 'Quantity', 'Unit', 'Title', 'Order Names', 'In Bins']
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.t = {c.TAB_PLACED: [H14], c.TAB_SHIPPED: [H14 + ['Shipped On', 'Invoice']],
                  c.TAB_RECEIVED: [H14 + ['Shipped On', 'Invoice', 'Received On', 'Arrival Invoice']],
                  c.TAB_LINES: [['Lock Date', 'Last Order Submitted', 'Order', 'Supplier', 'SKU', 'Qty', 'Ordered', 'Shipped', 'Received']],
                  c.TAB_INVOICES: [['Processed At', 'Stage', 'Supplier', 'File', 'SHA-256', 'Units Moved']]}

    def get(self, tab): return [list(r) for r in self.t[tab]]
    def append(self, tab, rows): self.t[tab] += [list(r) for r in rows]
    def replace_body(self, tab, rows): self.t[tab] = [self.t[tab][0]] + [list(r) for r in rows]
    def body(self, tab): return self.t[tab][1:]


class Shop:
    def __init__(self, orders): self.orders = orders; self.calls = []
    def get_orders(self, names): return {n: self.orders[n] for n in names if n in self.orders}
    def tags_add(self, i, t): self.calls.append(('add', i, t)); self._o(i)['tags'] += t
    def tags_remove(self, i, t): self.calls.append(('rm', i, t)); self._o(i)['tags'] = [x for x in self._o(i)['tags'] if x not in t]
    def _o(self, i): return next(o for o in self.orders.values() if o['id'] == i)


def order(name, lines, tags=None, cancelled=False, fulfillment='UNFULFILLED'):
    return {'id': 'gid://' + name, 'tags': list(tags or ['dhg-status-order-placed', 'dhg-status-order-supplier']),
            'fulfillment': fulfillment, 'cancelled': cancelled,
            'lines': [{'sku': s, 'currentQuantity': q, 'product': {'tags': t}} for s, q, t in lines]}


def placed(lock, last, sup, sku, qty, orders, acdd=''):
    return [lock, last, sup, sku, acdd, qty, 'Each', sku + ' title', orders, '']


def line(lock, last, name, sup, sku, qty, ordered=None):
    return [lock, last, name, sup, sku, qty, qty if ordered is None else ordered, 0, 0]


ASM = lambda sku, acdd: ({sku}, set())  # noqa: E731


def items(*pairs):
    return [{'key1': s, 'key2': '', 'quantity': q, 'description': s, 'sku': s, 'barcode': ''} for s, q in pairs]


def seed(store):
    store.t[c.TAB_PLACED] += [
        placed('2026-10-06', 6007, 'Asmodee', 'A1', 2, '#6001'),
        placed('2026-10-13', 6020, 'Asmodee', 'A1', 3, '#6010, #6011'),
        placed('2026-10-13', 6020, 'Asmodee', 'A2', 1, '#6010'),
        placed('2026-10-13', 6020, 'Universal Dist', 'U1', 2, '#6010'),
        placed('2026-10-13', 6020, 'Asmodee', 'HAND', 4, ''),
    ]
    store.t[c.TAB_LINES] += [
        line('2026-10-06', 6007, '#6001', 'Asmodee', 'A1', 2),
        line('2026-10-13', 6020, '#6010', 'Asmodee', 'A1', 2),
        line('2026-10-13', 6020, '#6011', 'Asmodee', 'A1', 1),
        line('2026-10-13', 6020, '#6010', 'Asmodee', 'A2', 1),
        line('2026-10-13', 6020, '#6010', 'Universal Dist', 'U1', 2),
    ]


def run(store, shop, stage, sup, its, keys=ASM, name='inv.pdf', sha='h1', dry=False, bc=False):
    return c.process_upload(store, shop, stage=stage, supplier=sup, items=its, group_keys=keys,
                            files=[(name, sha)], dry_run=dry, now=NOW, has_barcode_column=bc)


class Flow(unittest.TestCase):
    def setUp(self):
        self.s = Store(); seed(self.s)
        self.shop = Shop({
            '#6001': order('#6001', [('A1', 2, [])]),
            '#6010': order('#6010', [('A1', 2, []), ('A2', 1, []), ('U1', 2, [])]),
            '#6011': order('#6011', [('A1', 1, [])]),
        })

    def test_oldest_cycle_first_partial_and_tagging(self):
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 3), ('A2', 1)))
        self.assertEqual(r['unitsAdvancedToShipped'], 4)
        placed_a1 = [x for x in self.s.body(c.TAB_PLACED) if x[3] == 'A1']
        self.assertEqual([(x[1], x[5]) for x in placed_a1], [(6020, 2)])        # 6007 row gone, 6020 row 3 -> 2
        shipped = self.s.body(c.TAB_SHIPPED)
        self.assertEqual(sorted((x[3], x[5], x[10], x[11]) for x in shipped),
                         [('A1', 1, '2026-10-14', 'inv.pdf'), ('A1', 2, '2026-10-14', 'inv.pdf'), ('A2', 1, '2026-10-14', 'inv.pdf')])
        self.assertEqual(r['ordersFullyShippedAndTagged'], ['#6001'])           # 6010 still owes A1 x1 and U1
        ln = {(x[2], x[4]): x for x in self.s.body(c.TAB_LINES)}
        self.assertEqual(ln[('#6001', 'A1')][7], 2)
        self.assertEqual(ln[('#6010', 'A1')][7], 1)       # oldest order in the row first
        self.assertEqual(ln[('#6011', 'A1')][7], 0)

    def test_order_spanning_two_suppliers_tagged_only_when_all_shipped(self):
        run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 5), ('A2', 1)), sha='a')
        self.assertNotIn('dhg-shipped-from-supplier', self.shop.orders['#6010']['tags'])
        self.assertIn('dhg-shipped-from-supplier', self.shop.orders['#6001']['tags'])
        self.assertNotIn('dhg-status-order-supplier', self.shop.orders['#6001']['tags'])      # old stage tag swapped
        self.assertIn('dhg-status-order-placed', self.shop.orders['#6001']['tags'])           # lifecycle tag untouched
        r = run(self.s, self.shop, 'shipped', 'Universal Dist',
                [{'key1': 'U1', 'key2': '', 'quantity': 2, 'description': '', 'sku': 'U1', 'barcode': ''}],
                keys=lambda sku, a: ({sku}, set()), sha='u')
        self.assertEqual(r['ordersFullyShippedAndTagged'], ['#6010'])
        self.assertIn('dhg-shipped-from-supplier', self.shop.orders['#6010']['tags'])

    def test_duplicate_file_is_refused_and_changes_nothing(self):
        run(self.s, self.shop, 'shipped', 'Asmodee', items(('A2', 1)), sha='same')
        before = {k: [list(r) for r in v] for k, v in self.s.t.items()}
        with self.assertRaises(c.DuplicateInvoice):
            run(self.s, self.shop, 'shipped', 'Asmodee', items(('A2', 1)), sha='same')
        self.assertEqual(before, self.s.t)
        # the same file at the OTHER stage is fine
        run(self.s, self.shop, 'arrived', 'Asmodee', items(('A2', 1)), sha='same')

    def test_dry_run_writes_nothing(self):
        before = {k: [list(r) for r in v] for k, v in self.s.t.items()}
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 5), ('A2', 1)), dry=True)
        self.assertEqual(before, self.s.t)
        self.assertEqual(self.shop.calls, [])
        self.assertEqual(r['ordersThatWouldBeTagged'], ['#6001', '#6011'])

    def test_more_than_outstanding_moves_only_outstanding(self):
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('A2', 5)))
        self.assertEqual(r['unitsAdvancedToShipped'], 1)
        row = [x for x in r['results'] if x[0] == 'A2'][0]
        self.assertEqual(row[4], c.ST_MORE)

    def test_hand_added_row_moves_without_crediting_orders(self):
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('HAND', 4)))
        self.assertEqual(r['unitsAdvancedToShipped'], 4)
        self.assertTrue(all(x[7] == 0 for x in self.s.body(c.TAB_LINES)))
        self.assertEqual(r['ordersFullyShippedAndTagged'], [])

    def test_short_ordered_order_never_completes(self):
        self.s.t[c.TAB_LINES][3] = line('2026-10-13', 6020, '#6011', 'Asmodee', 'A1', 1, ordered=0)   # was held short
        run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 5)))
        self.assertNotIn('dhg-shipped-from-supplier', self.shop.orders['#6011']['tags'])

    def test_refunded_unit_does_not_hold_order_back_and_cancelled_is_skipped(self):
        self.shop.orders['#6011'] = order('#6011', [('A1', 0, [])])          # refunded since
        self.shop.orders['#6001'] = order('#6001', [('A1', 2, [])], cancelled=True)
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 5), ('A2', 1)))
        self.assertIn('#6001', r['removedFulfilledOrCancelled'])
        self.assertEqual(r['ordersFullyShippedAndTagged'], [])               # 6011 has nothing live -> not tagged

    def test_inventory_queued_left_alone(self):
        self.shop.orders['#6001']['tags'] = ['dhg-status-inventory-queued']
        r = run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 2)))
        self.assertEqual(r['skippedAlreadyInventoryQueued'], ['#6001'])
        self.assertEqual(self.shop.calls, [])

    def test_arrival_moves_shipped_to_received_and_tags(self):
        run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 2)), sha='s')
        r = run(self.s, self.shop, 'arrived', 'Asmodee', items(('A1', 2)), sha='a')
        self.assertEqual(r['unitsAdvancedToArrived'], 2)
        self.assertEqual(r['ordersFullyArrivedAndTagged'], [{'order': '#6001', 'tag': 'dhg-status-order-received'}])
        rec = self.s.body(c.TAB_RECEIVED)
        self.assertEqual((rec[0][3], rec[0][5], rec[0][12], rec[0][13]), ('A1', 2, '2026-10-14', 'inv.pdf'))
        self.assertEqual([x for x in self.s.body(c.TAB_SHIPPED) if x[3] == 'A1'], [])

    def test_arrival_cannot_exceed_what_was_shipped(self):
        r = run(self.s, self.shop, 'arrived', 'Asmodee', items(('A1', 2)))
        self.assertEqual(r['unitsAdvancedToArrived'], 0)
        self.assertEqual(r['results'][0][4], c.ST_EXTRA)

    def test_arrived_preorder_variant(self):
        self.shop.orders['#6001'] = order('#6001', [('A1', 2, ['preorder', 'release-date-2027-01-01'])])
        run(self.s, self.shop, 'shipped', 'Asmodee', items(('A1', 2)), sha='s')
        r = run(self.s, self.shop, 'arrived', 'Asmodee', items(('A1', 2)), sha='a')
        self.assertEqual(r['ordersFullyArrivedAndTagged'][0]['tag'], 'dhg-status-order-received-preorder')

    def test_acdd_matches_by_upc_then_shopify_barcode(self):
        s = Store()
        s.t[c.TAB_PLACED] += [placed('2026-10-13', 6020, 'ACDD', 'PSALAD', 1, '', acdd='AEG7059'),
                              placed('2026-10-13', 6020, 'ACDD', 'NOACDD', 2, '', acdd='')]
        upc_by_item = {'AEG7059': '729220070593'}
        barcodes = {'NOACDD': '841333138462'}
        keys = lambda sku, acdd: ({upc_by_item.get(acdd, '')}, {barcodes.get(sku, '')})  # noqa: E731
        its = [{'key1': u, 'key2': u, 'quantity': q, 'description': d, 'barcode': u, 'sku': ''}
               for u, q, d in [('729220070593', 1, 'Point Salad'), ('841333138462', 2, 'B2')]]
        r = run(s, None, 'shipped', 'ACDD', its, keys=keys, bc=True)
        self.assertEqual(r['unitsAdvancedToShipped'], 3)
        self.assertEqual(s.body(c.TAB_PLACED), [])


class Quote(unittest.TestCase):
    def test_verify_against_latest_lock_including_shipped_rows(self):
        s = Store(); seed(s)
        run(s, None, 'shipped', 'Asmodee', items(('A2', 1)))
        q = [{'sku': 'A1', 'quantity': 3}, {'sku': 'A2', 'quantity': 1}, {'sku': 'HAND', 'quantity': 3}, {'sku': 'ZZ', 'quantity': 1}]
        out = c.verify_quote(s, q)
        self.assertEqual(sorted((m['sku'], m['issue']) for m in out['mismatches']),
                         [('HAND', 'Quantity mismatch'), ('ZZ', 'In quote but not locked')])
        self.assertIn('6020', out['comparedAgainstSupplierOrderId'])


class OcrQuote(unittest.TestCase):
    def _text(self):
        import pathlib
        return (pathlib.Path(__file__).parent / 'fixtures' / 'asmodee_quote_ocr.txt').read_text()

    def test_parse_ocr_text_of_real_image_only_quote(self):
        import _parsers
        got = _parsers.parse_ocr_quote_text(self._text())
        self.assertEqual([(i['sku'], i['quantity']) for i in got],
                         [('SWQ96', 1), ('MC65EN', 1), ('SWQ20', 1), ('SWQ13', 1), ('MCP08', 1), ('CP170', 1),
                          ('CP129', 1), ('SWGO04EN', 1), ('DOW910251', 1), ('ODGTTS0101EN', 1), ('SWQ28', 1)])

    def test_snap_fixes_ocr_misreads_but_not_real_neighbours(self):
        locked = {'SWG04EN': {}, 'SWQ20': {}, 'SWQ28': {}, 'ODGTTS0101EN': {}}
        quoted = {'SWGO04EN': 1, 'SWQ20': 1, 'SWQ28': 1, 'ODGTTS0101EN': 1}
        out, snapped = c.snap_ocr_skus(quoted, locked)
        self.assertEqual(snapped, [{'read': 'SWGO04EN', 'adopted': 'SWG04EN'}])
        self.assertEqual(sorted(out), ['ODGTTS0101EN', 'SWG04EN', 'SWQ20', 'SWQ28'])

    def test_image_only_pdf_asks_for_ocr_instead_of_false_mismatches(self):
        import _parsers
        from unittest import mock
        with mock.patch.object(_parsers, 'pdf_has_text', return_value=False), mock.patch.dict('sys.modules', {'_tio': mock.MagicMock()}):
            status, data = h.run_verify([('q.pdf', b'x')], {})
        self.assertEqual((status, data), (200, {'needsOcr': True}))


class Http(unittest.TestCase):
    def test_multipart(self):
        b = 'XX'
        body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\nContent-Type: application/pdf\r\n\r\nPDFDATA\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="b.xlsx"\r\n\r\nSECOND\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="dry_run"\r\n\r\ntrue\r\n--{b}--\r\n').encode()
        files, fields = h.parse_multipart(f'multipart/form-data; boundary={b}', body)
        self.assertEqual(files, [('a.pdf', b'PDFDATA'), ('b.xlsx', b'SECOND')])
        self.assertEqual(fields, {'dry_run': 'true'})

    SAMPLE = '/root/.claude/uploads/c8c3a056-3566-5c03-92e4-1064f8a3c36c/d0b8575e-ACDInvoice.pdf'

    @unittest.skipUnless(os.path.exists(SAMPLE), 'sample ACDD invoice not available')
    def test_acdd_parser_on_real_invoice(self):
        import _parsers
        no, got = _parsers.parse_acdd_invoice(open(self.SAMPLE, 'rb').read())
        self.assertEqual(no, 'INV0001898754')
        self.assertEqual([(g['upc'], g['quantity']) for g in got],
                         [('729220070593', 1), ('841333138462', 2), ('841333133443', 2), ('759751002657', 1), ('787416368749', 1)])


if __name__ == '__main__':
    unittest.main()


class EndToEnd(unittest.TestCase):
    """Real HTTP handler + real ACDD PDF; only Google/Shopify are faked."""
    SAMPLE = Http.SAMPLE

    @unittest.skipUnless(os.path.exists(SAMPLE), 'sample ACDD invoice not available')
    def test_acdd_upload_over_http(self):
        import json
        import threading
        import urllib.request
        from http.server import HTTPServer
        import _tio as _io
        store = Store()
        store.t[c.TAB_PLACED] += [placed('2026-10-13', 6020, 'ACDD', 'PSALAD', 1, '#6010', acdd='AEG7059'),
                                  placed('2026-10-13', 6020, 'ACDD', 'SWL1', 2, '#6011', acdd='AMGSWQ154')]
        store.t[c.TAB_LINES] += [line('2026-10-13', 6020, '#6010', 'ACDD', 'PSALAD', 1), line('2026-10-13', 6020, '#6011', 'ACDD', 'SWL1', 2)]
        shop = Shop({'#6010': order('#6010', [('PSALAD', 1, [])]), '#6011': order('#6011', [('SWL1', 2, [])])})
        garland = [['ItemID', 'ShortDesc', 'MSRP', 'Product Type', 'Status', 'Street Date', 'UPC'],
                   ['AEG7059', 'Point Salad', '', '', '', '', '729220070593'], ['AMGSWQ154', 'B2', '', '', '', '', '841333138462']]
        saved = (_io.SheetsStore, _io.Shopify, _io.read_range)
        _io.SheetsStore, _io.Shopify, _io.read_range = (lambda: store), (lambda: shop), (lambda *a, **k: garland)
        shop.barcodes_for_skus = lambda skus: {}
        srv = HTTPServer(('127.0.0.1', 0), h.make_handler('acdd', 'shipped'))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            pdf = open(self.SAMPLE, 'rb').read()
            b = 'BOUND'
            body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="inv.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() + pdf + \
                   f'\r\n--{b}\r\nContent-Disposition: form-data; name="dry_run"\r\n\r\nfalse\r\n--{b}--\r\n'.encode()
            req = urllib.request.Request(f'http://127.0.0.1:{srv.server_port}/', data=body, method='POST',
                                         headers={'Content-Type': f'multipart/form-data; boundary={b}'})
            data = json.loads(urllib.request.urlopen(req).read())
            self.assertEqual(data['unitsAdvancedToShipped'], 3)
            self.assertEqual(sorted(data['ordersFullyShippedAndTagged']), ['#6010', '#6011'])
            self.assertEqual(store.body(c.TAB_PLACED), [])
            self.assertEqual(len(store.body(c.TAB_SHIPPED)), 2)
            # same file again is refused
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 409)
        finally:
            srv.shutdown()
            _io.SheetsStore, _io.Shopify, _io.read_range = saved
