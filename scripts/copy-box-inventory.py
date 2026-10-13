"""One-time: copy the 'Box Inventory' tab from the ORIGINAL tracker sheet into the Weekly
Orders sheet. Reads the original; writes only to the new sheet.
Needs GOOGLE_SA_EMAIL + GOOGLE_SA_PRIVATE_KEY (or _B64) in the environment."""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
import _tio as _io  # noqa: E402

OLD = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
NEW = _io.WEEKLY_ORDERS_SHEET_ID
TAB = 'Box Inventory'

rows = _io.read_range(OLD, f"'{TAB}'!A1:Z200")
if not rows:
    sys.exit('Box Inventory is empty or missing in the original sheet.')
have = {s['properties']['title'] for s in _io._sheets('GET', f'{NEW}?fields=sheets.properties.title')['sheets']}
if TAB not in have:
    _io._sheets('POST', f'{NEW}:batchUpdate', {'requests': [{'addSheet': {'properties': {'title': TAB}}}]})
if len(_io.read_range(NEW, f"'{TAB}'!A1:Z200")) > 1:
    sys.exit(f'"{TAB}" in the new sheet already has data; leaving it alone.')
_io._sheets('PUT', f"{NEW}/values/{urllib.parse.quote(f'{chr(39)}{TAB}{chr(39)}!A1')}?valueInputOption=RAW", {'values': rows})
print(f'Copied {len(rows) - 1} box rows to the new sheet.')
