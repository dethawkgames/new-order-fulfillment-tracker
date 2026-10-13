"""Google Sheets + Shopify I/O for the tracker (stdlib + pyjwt only)."""
import base64
import json
import os
import time
import urllib.parse
import urllib.request
import jwt

WEEKLY_ORDERS_SHEET_ID = os.environ.get('WEEKLY_ORDERS_SHEET_ID', '1wHisaKXPcRpgXH9HXw6sdvTQi25QZoLj4cHPxSYlFrQ')
PRODUCT_SKUS_SHEET_ID = os.environ.get('PRODUCT_SKUS_SHEET_ID', '1yC-oZ-0hD5ReTcOA9iTjTGC6mONbDUCpfbZZA9GrQtI')
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

_google = {'token': None, 'exp': 0}
_shopify = {'token': None, 'exp': 0}


def _google_token():
    if _google['token'] and time.time() < _google['exp'] - 60:
        return _google['token']
    email = os.environ['GOOGLE_SA_EMAIL']
    if os.environ.get('GOOGLE_SA_PRIVATE_KEY_B64'):
        key = base64.b64decode(os.environ['GOOGLE_SA_PRIVATE_KEY_B64']).decode('utf-8')
    else:
        key = os.environ['GOOGLE_SA_PRIVATE_KEY'].replace('\\n', '\n')
    now = int(time.time())
    assertion = jwt.encode({'iss': email, 'scope': 'https://www.googleapis.com/auth/spreadsheets',
                            'aud': 'https://oauth2.googleapis.com/token', 'exp': now + 3600, 'iat': now}, key, algorithm='RS256')
    data = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode()
    with urllib.request.urlopen(urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')) as r:
        _google['token'] = json.loads(r.read())['access_token']
    _google['exp'] = now + 3500
    return _google['token']


def _sheets(method, path, body=None):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{path}'
    req = urllib.request.Request(url, method=method, headers={'Authorization': f'Bearer {_google_token()}', 'Content-Type': 'application/json'},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b'{}')


def read_range(sheet_id, rng):
    return _sheets('GET', f'{sheet_id}/values/{urllib.parse.quote(rng)}').get('values', [])


class SheetsStore:
    """Tab-level access to the Weekly Orders spreadsheet."""

    def __init__(self, sheet_id=WEEKLY_ORDERS_SHEET_ID):
        self.id = sheet_id

    def get(self, tab):
        return read_range(self.id, f"'{tab}'!A1:Z")

    def append(self, tab, rows):
        if rows:
            _sheets('POST', f"{self.id}/values/{urllib.parse.quote(f'{chr(39)}{tab}{chr(39)}!A1')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                    {'values': rows})

    def replace_body(self, tab, rows):
        """Write the new body first, then blank whatever is left below it, so an
        interruption can only leave stale rows, never lose data."""
        old = len(self.get(tab)) - 1
        if rows:
            _sheets('PUT', f"{self.id}/values/{urllib.parse.quote(f'{chr(39)}{tab}{chr(39)}!A2')}?valueInputOption=RAW", {'values': rows})
        if old > len(rows):
            _sheets('POST', f"{self.id}/values/{urllib.parse.quote(f'{chr(39)}{tab}{chr(39)}!A{len(rows) + 2}:Z{old + 1}')}:clear", {})


class Shopify:
    def _token(self):
        if _shopify['token'] and time.time() < _shopify['exp'] - 60:
            return _shopify['token']
        data = urllib.parse.urlencode({'grant_type': 'client_credentials', 'client_id': os.environ['SHOPIFY_CLIENT_ID'],
                                       'client_secret': os.environ['SHOPIFY_CLIENT_SECRET']}).encode()
        with urllib.request.urlopen(urllib.request.Request(f'https://{SHOPIFY_SHOP}/admin/oauth/access_token', data=data, method='POST')) as r:
            body = json.loads(r.read())
        _shopify['token'] = body['access_token']
        _shopify['exp'] = time.time() + int(body.get('expires_in', 3600))
        return _shopify['token']

    def gql(self, query, variables=None):
        req = urllib.request.Request(f'https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json', method='POST',
                                     data=json.dumps({'query': query, 'variables': variables or {}}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Shopify-Access-Token': self._token()})
        with urllib.request.urlopen(req) as r:
            res = json.loads(r.read())
        if res.get('errors'):
            raise RuntimeError(f"Shopify GraphQL errors: {res['errors']}")
        return res['data']

    def get_orders(self, names):
        out = {}
        nums = [str(n).lstrip('#') for n in names]
        for i in range(0, len(nums), 25):
            q = ' OR '.join(f'name:{n}' for n in nums[i:i + 25])
            d = self.gql('''query($q: String!) { orders(first: 50, query: $q) { edges { node {
                id name tags displayFulfillmentStatus cancelledAt
                lineItems(first: 50) { edges { node { sku currentQuantity product { tags } } } } } } } }''', {'q': q})
            for e in d['orders']['edges']:
                n = e['node']
                out[n['name']] = {'id': n['id'], 'tags': n['tags'], 'fulfillment': n['displayFulfillmentStatus'],
                                  'cancelled': bool(n['cancelledAt']), 'lines': [x['node'] for x in n['lineItems']['edges']]}
        return out

    def tags_add(self, order_id, tags):
        self._mutate('tagsAdd', order_id, tags)

    def tags_remove(self, order_id, tags):
        self._mutate('tagsRemove', order_id, tags)

    def _mutate(self, op, order_id, tags):
        d = self.gql(f'mutation($id: ID!, $tags: [String!]!) {{ {op}(id: $id, tags: $tags) {{ userErrors {{ field message }} }} }}',
                     {'id': order_id, 'tags': tags})
        errs = d[op]['userErrors']
        if errs:
            raise RuntimeError(f'{op} failed: {errs}')

    def barcodes_for_skus(self, skus):
        out = {}
        skus = sorted({s for s in skus if s})
        for i in range(0, len(skus), 40):
            q = ' OR '.join('sku:"%s"' % s.replace('"', '').replace('\\', '') for s in skus[i:i + 40])
            d = self.gql('query($q: String!) { productVariants(first: 100, query: $q) { edges { node { sku barcode } } } }', {'q': q})
            for e in d['productVariants']['edges']:
                if e['node']['sku'] and e['node']['barcode']:
                    out[e['node']['sku']] = str(e['node']['barcode']).strip()
        return out
