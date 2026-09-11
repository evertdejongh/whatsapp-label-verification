import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get('WHATSAPP_AWS_REGION', 'us-east-2')

# Dole leaf logo, embedded inline as base64 PNGs (this Lambda ships as a
# single source file with no separate static-asset hosting) -- pre-cropped
# and resized from the source Dole_Logo.jpg rather than shipping it full size.
DOLE_HEADER_LOGO_B64 = (
    "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"
)
DOLE_FAVICON_B64 = (
    "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"
)
AUDIT_TABLE_NAME = os.environ.get('AUDIT_TABLE_NAME', 'whatsapp-audit-log')
GSI_NAME = os.environ.get('AUDIT_GSI_NAME', 'record_type-timestamp-index')
SPEC_CATALOG_TABLE_NAME = os.environ.get('SPEC_CATALOG_TABLE_NAME', 'whatsapp-spec-catalog')
SPEC_BUCKET_NAME = os.environ.get('SPEC_BUCKET_NAME', 'dole-pallet-specs-2026-677513501349-us-east-2-an')
IMAGE_URL_TTL_SECONDS = 3600
PAGE_SIZE = 200
SAST = timezone(timedelta(hours=2))

# Reference tables the "Reference Tables" tab can browse/edit -- a fixed
# allow-list (not "any table in the account"), same defensive scoping as
# tools/attach_webhook_lookup_permissions.sh's TABLES array. Value is the
# list of key attribute names (1 for a plain hash key, 2 for hash+range).
# whatsapp-audit-log / whatsapp-spec-catalog stay on their own dedicated
# tabs, deliberately excluded here to avoid two competing UIs for the same
# data.
TABLE_REGISTRY = {
    "whatsapp-puc": ["PUC"],
    "whatsapp-variety": ["VarietyName", "Commodity"],
    "whatsapp-variety-group": ["VarietyGroupCode"],
    "whatsapp-ian-numbers": ["lookup_key"],
    "whatsapp-commodity": ["Commodity"],
    "whatsapp-rewe-combinations": ["lookup_key"],
    "whatsapp-agent-addresses": ["Code"],
}

# Columns that are the table's key but are a synthetic/derived composite
# (e.g. "GR#BS#A05D#P5" built from other attributes) rather than something
# meaningful to browse or hand-edit -- hidden from the displayed table, but
# still round-tripped as a hidden form field so existing-row Save/Delete
# keep working. Adding a brand new row with a hidden key isn't supported via
# the normal key inputs; use the "Extra field" pair to set it manually.
HIDDEN_TABLE_COLUMNS = {"lookup_key"}

ssm = boto3.client('ssm', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
s3_client = boto3.client(
    's3',
    region_name=AWS_REGION,
    endpoint_url=f'https://s3.{AWS_REGION}.amazonaws.com'
)

RESULT_STYLES = {
    'PASS': ('#1f9d55', '✅'),
    'SPEC_SENT': ('#1f9d55', '📋'),
    'FAIL': ('#d64545', '❌'),
    'SPEC_NOT_FOUND': ('#d64545', '❌'),
    'ERROR': ('#b45309', '⚠️'),
    'DOWNLOAD_FAILED': ('#b45309', '⚠️'),
    'NO_SPEC_CODE': ('#6b7280', '➖'),
    'UNSUPPORTED_TYPE': ('#6b7280', '➖'),
}

PAGE_STYLE = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; background:#f5f6f8; color:#1f2430; margin:0; padding:0; }
  .topbar { background:#fff; border-bottom:1px solid #e2e5ea; padding:12px 24px; display:flex; align-items:center; gap:12px; }
  .topbar img.logo { height:36px; width:auto; display:block; }
  .topbar h1 { font-size:19px; margin:0; color:#1f2430; }
  .content { padding:20px 24px; }
  .nav { margin-bottom:18px; display:flex; gap:6px; }
  .nav a { color:#5b6473; text-decoration:none; padding:6px 16px; border-radius:6px; font-size:13px; border:1px solid #dde1e7; background:#fff; }
  .nav a.active { background:#2f6fed; border-color:#2f6fed; color:#fff; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:14px; }
  .chips { margin-bottom:16px; }
  .chip { display:inline-block; background:#fff; border:1px solid #dde1e7; border-radius:999px; padding:4px 12px; margin:0 8px 8px 0; font-size:13px; color:#374151; }
  .toolbar { display:flex; justify-content:flex-end; gap:8px; margin-bottom:14px; }
  .toprow { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
  .toprow .sub, .toprow .toolbar { margin-bottom:0; }
  form.filters { margin-bottom:16px; display:flex; gap:8px; flex-wrap:wrap; }
  input, select, textarea { background:#fff; border:1px solid #d3d8e0; color:#1f2430; border-radius:6px; padding:6px 10px; font-size:13px; font-family:inherit; }
  button, .btn { background:#fff; border:1px solid #d3d8e0; color:#374151; border-radius:6px; padding:7px 16px; font-size:13px; cursor:pointer; text-decoration:none; display:inline-block; }
  .btn.primary, button.primary { background:#22a55e; border-color:#22a55e; color:#fff; }
  .btn.danger, button.danger { color:#d64545; border-color:#f3c9c9; }
  .btn.disabled, button:disabled { opacity:.4; cursor:not-allowed; pointer-events:none; }
  table { border-collapse:collapse; width:100%; font-size:13px; background:#fff; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid #edf0f3; vertical-align:top; }
  thead { position:sticky; top:0; z-index:2; }
  th { color:#5b6473; font-weight:600; background:#f5f7f5; border-bottom:1px solid #dde1e7; white-space:nowrap; }
  th.sortable { cursor:pointer; user-select:none; }
  th.sortable:hover { color:#2f6fed; }
  th.sort-asc:after { content:' \\25B2'; font-size:10px; }
  th.sort-desc:after { content:' \\25BC'; font-size:10px; }
  td.wraptext { max-width:340px; white-space:normal; word-break:break-word; }
  tr.filter-row td { background:#fafbfc; padding:5px 8px; }
  tr.filter-row input { width:100%; font-size:12px; padding:5px 8px; }
  tbody tr:nth-child(even) { background:#f7fbf6; }
  tbody tr.selectable:hover { background:#eef4ff; cursor:pointer; }
  tbody tr.selected { background:#dbe7fd !important; }
  tr.editing { background:#fff8e1 !important; }
  tr.editing input { width:100%; }
  td.actions { display:flex; gap:6px; }
  td.detail { color:#6b7280; max-width:420px; }
  td.desc { max-width:380px; white-space:normal; word-break:break-word; }
  .thumb { height:44px; width:auto; border-radius:4px; border:1px solid #dde1e7; display:block; }
  .more { display:inline-block; margin-top:14px; color:#2f6fed; text-decoration:none; }
  .wrap { overflow-x:auto; overflow-y:auto; max-height:70vh; border:1px solid #dde1e7; border-radius:8px; }
  .pill { display:inline-block; border-radius:999px; padding:3px 10px; font-size:12px; font-weight:600; text-decoration:none; }
  .pill.yes { background:#dcfce7; color:#166534; }
  .pill.no { background:#f3f4f6; color:#6b7280; }
  .hint { color:#8b93a3; font-size:12px; margin-top:8px; }
  .error { color:#b42318; background:#fef3f2; border:1px solid #fda29b; border-radius:6px; padding:8px 12px; font-size:13px; margin-bottom:12px; }
"""

ROW_SELECT_SCRIPT = """
<script>
(function() {
  var table = document.getElementById('catalogTable');
  if (!table) return;

  // Editing a row far down the list reloads the page (Edit is a plain
  // link), which resets the scrollable .wrap panel back to the top --
  // scroll the editing row (if any) back into view instead of leaving the
  // user to hunt for it themselves.
  var editingRow = table.querySelector('tbody tr.editing');
  if (editingRow) {
    editingRow.scrollIntoView({ block: 'center' });
  }

  var editBtn = document.getElementById('editBtn');
  var deleteBtn = document.getElementById('deleteBtn');
  var deleteForm = document.getElementById('deleteForm');
  var deleteSpecInput = document.getElementById('deleteSpecCode');
  var editHrefBase = editBtn.getAttribute('data-href-base');
  var selectedCode = null;

  table.querySelectorAll('tbody tr.selectable').forEach(function(row) {
    row.addEventListener('click', function() {
      table.querySelectorAll('tbody tr').forEach(function(r) { r.classList.remove('selected'); });
      row.classList.add('selected');
      selectedCode = row.getAttribute('data-code');
      editBtn.href = editHrefBase + encodeURIComponent(selectedCode);
      editBtn.classList.remove('disabled');
      deleteBtn.disabled = false;
    });
  });

  deleteBtn.addEventListener('click', function() {
    if (!selectedCode) return;
    if (!confirm('Delete spec ' + selectedCode + '?')) return;
    deleteSpecInput.value = selectedCode;
    deleteForm.submit();
  });

  var filterInputs = table.querySelectorAll('.filter-row [data-col]');
  filterInputs.forEach(function(input) {
    input.addEventListener('input', function() { applyFilters(); });
  });

  function applyFilters() {
    var filters = [];
    filterInputs.forEach(function(input) {
      var val = input.value.trim().toLowerCase();
      if (val) filters.push({ col: parseInt(input.getAttribute('data-col'), 10), val: val });
    });
    table.querySelectorAll('tbody tr').forEach(function(row) {
      if (row.classList.contains('editing')) return;
      var cells = row.children;
      var visible = filters.every(function(f) {
        var cell = cells[f.col];
        return !cell || cell.textContent.toLowerCase().indexOf(f.val) !== -1;
      });
      row.style.display = visible ? '' : 'none';
    });
  }

  // Click a header to sort by that column -- reuses the same data-col
  // indexing the filter row already relies on. A row mid-edit has inputs
  // instead of plain text, so its cell value is read from the input rather
  // than textContent; moving its row node during sort doesn't detach the
  // form-bound inputs (they submit via the `form` attribute, not position).
  var sortHeaders = table.querySelectorAll('thead th[data-col]');
  var sortState = { col: null, dir: 1 };

  function cellSortValue(row, col) {
    var cell = row.children[col];
    if (!cell) return '';
    var input = cell.querySelector('input');
    var raw = (input ? input.value : cell.textContent).trim();
    return raw;
  }

  sortHeaders.forEach(function(th) {
    th.addEventListener('click', function() {
      var col = parseInt(th.getAttribute('data-col'), 10);
      sortState.dir = (sortState.col === col) ? -sortState.dir : 1;
      sortState.col = col;

      sortHeaders.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
      th.classList.add(sortState.dir === 1 ? 'sort-asc' : 'sort-desc');

      var tbody = table.querySelector('tbody');
      var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.selectable, tr.editing'));
      rows.sort(function(a, b) {
        var av = cellSortValue(a, col), bv = cellSortValue(b, col);
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp;
        if (av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn) && String(an) === av && String(bn) === bv) {
          cmp = an - bn;
        } else {
          cmp = av.toLowerCase().localeCompare(bv.toLowerCase());
        }
        return cmp * sortState.dir;
      });
      rows.forEach(function(row) { tbody.appendChild(row); });
    });
  });
})();
</script>
"""


def get_ssm_param(param_name):
    try:
        res = ssm.get_parameter(Name=param_name, WithDecryption=True)
        return res['Parameter']['Value']
    except Exception as e:
        logger.error(f"Failed to fetch SSM param {param_name}: {str(e)}")
        return None


def encode_key(key):
    if not key:
        return ''
    return base64.urlsafe_b64encode(json.dumps(key, default=str).encode('utf-8')).decode('utf-8')


def decode_key(token):
    if not token:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode('utf-8')).decode('utf-8'))
    except Exception:
        return None


def format_sender(phone):
    """South African mobile numbers (country code 27) display in local 0XX XXX XXXX form; other numbers pass through unchanged."""
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith('27') and len(digits) == 11:
        local = '0' + digits[2:]
        return f"{local[0:3]} {local[3:6]} {local[6:10]}"
    return phone


def normalize_sender_filter(raw):
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if digits.startswith('0') and len(digits) == 10:
        return '27' + digits[1:]
    return digits or raw


def format_sast(timestamp_str):
    try:
        dt = datetime.fromisoformat(timestamp_str)
        return dt.astimezone(SAST).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return timestamp_str


def get_presigned_url(key):
    if not key:
        return None
    try:
        return s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': SPEC_BUCKET_NAME, 'Key': key},
            ExpiresIn=IMAGE_URL_TTL_SECONDS
        )
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {key}: {str(e)}")
        return None


def html_escape(value):
    return (
        str(value)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def spec_code_sort_key(code):
    """Natural sort for spec codes (leading digits, then trailing letters) --
    a plain string sort puts "10A" before "1A" (since "0" < "A"), which reads
    as out of order to anyone scanning the list numerically. Shared by the
    Spec Catalog and Spec Files tabs, which both list specs this way."""
    match = re.match(r'^(\d+)(.*)$', code)
    if match:
        return (0, int(match.group(1)), match.group(2))
    return (1, 0, code)


def render_nav(token, active):
    token_qs = html_escape(token)
    return f"""<div class="nav">
    <a href="?token={token_qs}&view=audit" class="{'active' if active == 'audit' else ''}">Audit Log</a>
    <a href="?token={token_qs}&view=catalog" class="{'active' if active == 'catalog' else ''}">Spec Catalog</a>
    <a href="?token={token_qs}&view=tables" class="{'active' if active == 'tables' else ''}">Reference Tables</a>
    <a href="?token={token_qs}&view=files" class="{'active' if active == 'files' else ''}">Spec Files</a>
  </div>"""


def page_shell(title, nav_html, body_html, extra_script=""):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title)}</title>
<link rel="icon" type="image/png" href="data:image/png;base64,{DOLE_FAVICON_B64}">
<style>{PAGE_STYLE}</style>
</head>
<body>
  <div class="topbar"><img class="logo" src="data:image/png;base64,{DOLE_HEADER_LOGO_B64}" alt="Dole"><h1>WhatsApp Label Verification</h1></div>
  <div class="content">
  {nav_html}
  {body_html}
  </div>
  {extra_script}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Audit Log view (read-only reporting -- keeps its existing server-side
# sender/result filters, just re-skinned to the shared light theme)
# ---------------------------------------------------------------------------

def render_audit_log(items, counts, filters, next_key):
    rows = []
    for item in items:
        result = item.get('result', '')
        color, icon = RESULT_STYLES.get(result, ('#6b7280', '•'))
        image_url = get_presigned_url(item.get('image_key'))
        image_cell = (
            f"<a href='{html_escape(image_url)}' target='_blank' rel='noopener'>"
            f"<img class='thumb' src='{html_escape(image_url)}' loading='lazy' alt='label photo'></a>"
        ) if image_url else ""
        rows.append(
            "<tr>"
            f"<td>{html_escape(format_sast(item.get('timestamp', '')))}</td>"
            f"<td>{html_escape(format_sender(item.get('sender', '')))}</td>"
            f"<td>{html_escape(item.get('sender_name', ''))}</td>"
            f"<td>{html_escape(item.get('msg_type', ''))}</td>"
            f"<td>{html_escape(item.get('spec_code', ''))}</td>"
            f"<td style='color:{color};font-weight:600'>{icon} {html_escape(result)}</td>"
            f"<td class='detail'>{html_escape(item.get('detail', ''))[:200]}</td>"
            f"<td>{image_cell}</td>"
            "</tr>"
        )

    count_chips = "".join(
        f"<span class='chip'>{html_escape(r)}: <b>{c}</b></span>"
        for r, c in sorted(counts.items())
    )

    next_link = ""
    if next_key:
        qs = f"?token={html_escape(filters['token'])}&view=audit&last_key={html_escape(encode_key(next_key))}"
        if filters.get('sender'):
            qs += f"&sender={html_escape(filters['sender'])}"
        if filters.get('result'):
            qs += f"&result={html_escape(filters['result'])}"
        next_link = f"<a class='more' href='{qs}'>Load older records &rarr;</a>"

    sender_val = html_escape(filters.get('sender', ''))
    result_val = filters.get('result', '')
    result_options = "".join(
        f"<option value='{r}' {'selected' if r == result_val else ''}>{r}</option>"
        for r in sorted(RESULT_STYLES.keys())
    )

    body = f"""
  <div class="sub">Showing up to {PAGE_SIZE} most recent records{' (filtered)' if (sender_val or result_val) else ''}</div>
  <div class="chips">{count_chips}</div>
  <form class="filters" method="get">
    <input type="hidden" name="token" value="{html_escape(filters['token'])}">
    <input type="hidden" name="view" value="audit">
    <input type="text" name="sender" placeholder="Filter by sender phone" value="{sender_val}">
    <select name="result">
      <option value="">All results</option>
      {result_options}
    </select>
    <button type="submit" class="primary">Filter</button>
  </form>
  <div class="wrap">
  <table>
    <thead><tr><th>Timestamp (SAST)</th><th>Sender</th><th>Name</th><th>Type</th><th>Spec</th><th>Result</th><th>Detail</th><th>Photo</th></tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="8">No records found.</td></tr>'}
    </tbody>
  </table>
  </div>
  {next_link}
"""
    return page_shell("WhatsApp Label Verification - Audit Log", render_nav(filters['token'], 'audit'), body)


def handle_audit_log(token, query_params):
    sender_filter = query_params.get('sender', '').strip()
    result_filter = query_params.get('result', '').strip()
    last_key = decode_key(query_params.get('last_key'))

    table = dynamodb.Table(AUDIT_TABLE_NAME)

    filter_expression = None
    expr_values = {}
    expr_names = {}
    if sender_filter:
        filter_expression = "contains(#s, :sender)"
        expr_names['#s'] = 'sender'
        expr_values[':sender'] = normalize_sender_filter(sender_filter)
    if result_filter:
        clause = "#r = :result"
        expr_names['#r'] = 'result'
        expr_values[':result'] = result_filter
        filter_expression = f"{filter_expression} AND {clause}" if filter_expression else clause

    query_kwargs = {
        'IndexName': GSI_NAME,
        'KeyConditionExpression': Key('record_type').eq('REQUEST'),
        'ScanIndexForward': False,
        'Limit': PAGE_SIZE,
    }
    if filter_expression:
        query_kwargs['FilterExpression'] = filter_expression
        query_kwargs['ExpressionAttributeNames'] = expr_names
        query_kwargs['ExpressionAttributeValues'] = expr_values
    if last_key:
        query_kwargs['ExclusiveStartKey'] = last_key

    try:
        response = table.query(**query_kwargs)
        items = response.get('Items', [])
        next_key = response.get('LastEvaluatedKey')
    except Exception as e:
        logger.error(f"Failed to query audit table: {str(e)}")
        return text_response(500, f'Error querying audit log: {str(e)}')

    counts = {}
    for item in items:
        r = item.get('result', 'UNKNOWN')
        counts[r] = counts.get(r, 0) + 1

    html = render_audit_log(
        items,
        counts,
        {'token': token, 'sender': sender_filter, 'result': result_filter},
        next_key
    )
    return html_response(html)


# ---------------------------------------------------------------------------
# Spec Catalog view: list + add/edit/delete for whatsapp-spec-catalog.
# Rules-Manager-style: click a row to select it, then Edit/Delete live in a
# top-right toolbar; editing itself still happens inline in that row (see
# render_edit_row) once triggered.
# ---------------------------------------------------------------------------

def download_pdf(url):
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def scan_spec_catalog():
    table = dynamodb.Table(SPEC_CATALOG_TABLE_NAME)
    resp = table.scan()
    items = resp.get('Items', [])
    while 'LastEvaluatedKey' in resp:
        resp = table.scan(ExclusiveStartKey=resp['LastEvaluatedKey'])
        items.extend(resp.get('Items', []))
    return sorted(items, key=lambda i: spec_code_sort_key(i.get('spec_code', '')))


EDIT_FORM_ID = "catalog-edit-form"


def render_view_row(item):
    spec_code = item.get('spec_code', '')
    pdf_key = item.get('pdf_s3_key')
    pdf_url = get_presigned_url(pdf_key)
    pdf_cell = f"<a class='pill yes' href='{html_escape(pdf_url)}' target='_blank' rel='noopener'>PDF &#10003;</a>" if pdf_url else "<span class='pill no'>No PDF</span>"
    return (
        f"<tr class='selectable' data-code='{html_escape(spec_code)}'>"
        f"<td>{html_escape(spec_code)}</td>"
        f"<td class='desc'>{html_escape(item.get('description', ''))}</td>"
        f"<td>{pdf_cell}</td>"
        f"<td></td>"
        "</tr>"
    )


def render_edit_row(token, item, is_new):
    """Renders an editable table row. Its inputs bind to a single shared
    <form> defined once elsewhere on the page (via the HTML `form=` attribute)
    since a <form> can't legally wrap just some cells of a <tr>."""
    spec_code_val = html_escape(item.get('spec_code', '')) if item else ''
    description_val = html_escape(item.get('description', '')) if item else ''
    pdf_url_val = html_escape(item.get('pdf_url', '')) if item else ''

    if is_new:
        spec_code_cell = f"<input type='text' name='spec_code' form='{EDIT_FORM_ID}' value='{spec_code_val}' placeholder='e.g. 9A' required>"
    else:
        spec_code_cell = (
            f"{spec_code_val}"
            f"<input type='hidden' name='spec_code' form='{EDIT_FORM_ID}' value='{spec_code_val}'>"
        )

    cancel_qs = f"?token={html_escape(token)}&view=catalog"
    return (
        "<tr class='editing'>"
        f"<td>{spec_code_cell}</td>"
        f"<td><input type='text' name='description' form='{EDIT_FORM_ID}' value='{description_val}' placeholder='Description'></td>"
        f"<td><input type='text' name='pdf_url' form='{EDIT_FORM_ID}' value='{pdf_url_val}' placeholder='https://... (blank = no PDF)'></td>"
        f"<td class='actions'>"
        f"<button type='submit' form='{EDIT_FORM_ID}' class='primary'>Save</button>"
        f"<a class='btn' href='{cancel_qs}'>Cancel</a>"
        f"</td>"
        "</tr>"
    )


def render_catalog(token, items, edit_item, adding_new, error):
    edit_code = edit_item.get('spec_code') if edit_item else None

    rows = []
    if adding_new:
        rows.append(render_edit_row(token, item=edit_item, is_new=True))
    for item in items:
        if edit_code and item.get('spec_code') == edit_code:
            rows.append(render_edit_row(token, item=item, is_new=False))
        else:
            rows.append(render_view_row(item))

    error_html = f"<div class='error'>{html_escape(error)}</div>" if error else ""
    token_esc = html_escape(token)

    body = f"""
  <div class="toprow">
    <div class="sub">{len(items)} spec(s) in the catalog. Click a row to select it, then Edit or Delete.</div>
    <div class="toolbar">
      <a class="btn primary" href="?token={token_esc}&view=catalog&edit=__new__">+ New</a>
      <a id="editBtn" class="btn disabled" href="#" data-href-base="?token={token_esc}&view=catalog&edit=">Edit</a>
      <button id="deleteBtn" class="btn danger" disabled type="button">Delete</button>
    </div>
  </div>
  {error_html}

  <form id="{EDIT_FORM_ID}" method="post">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="catalog">
    <input type="hidden" name="action" value="save">
  </form>

  <form id="deleteForm" method="post" style="display:none">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="catalog">
    <input type="hidden" name="action" value="delete">
    <input type="hidden" id="deleteSpecCode" name="spec_code" value="">
  </form>

  <div class="wrap">
  <table id="catalogTable">
    <thead>
      <tr><th data-col="0" class="sortable">Spec Code</th><th data-col="1" class="sortable">Description</th><th>PDF</th><th>Actions</th></tr>
      <tr class="filter-row">
        <td><input type="text" data-col="0" placeholder="Filter..."></td>
        <td><input type="text" data-col="1" placeholder="Filter..."></td>
        <td></td>
        <td></td>
      </tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="4">No specs in the catalog yet.</td></tr>'}
    </tbody>
  </table>
  </div>
  <div class="hint">Editing a PDF Source URL re-downloads and re-caches it; leaving it unchanged keeps the current PDF, clearing it removes the PDF.</div>
"""
    return page_shell("WhatsApp Label Verification - Spec Catalog", render_nav(token, 'catalog'), body, extra_script=ROW_SELECT_SCRIPT)


def handle_catalog_get(token, query_params):
    try:
        items = scan_spec_catalog()
    except Exception as e:
        logger.error(f"Failed to scan spec catalog: {str(e)}")
        return text_response(500, f'Error loading spec catalog: {str(e)}')

    edit_item = None
    adding_new = False
    edit_code = query_params.get('edit', '').strip()
    if edit_code == '__new__':
        adding_new = True
    elif edit_code:
        table = dynamodb.Table(SPEC_CATALOG_TABLE_NAME)
        try:
            edit_item = table.get_item(Key={'spec_code': edit_code.upper()}).get('Item')
        except Exception as e:
            logger.error(f"Failed to load spec {edit_code} for edit: {str(e)}")

    return html_response(render_catalog(token, items, edit_item, adding_new, error=None))


def handle_catalog_post(token, form):
    action = form.get('action', [''])[0]
    table = dynamodb.Table(SPEC_CATALOG_TABLE_NAME)

    if action == 'delete':
        spec_code = form.get('spec_code', [''])[0].strip().upper()
        if spec_code:
            try:
                existing = table.get_item(Key={'spec_code': spec_code}).get('Item') or {}
                table.delete_item(Key={'spec_code': spec_code})
                pdf_key = existing.get('pdf_s3_key')
                if pdf_key:
                    s3_client.delete_object(Bucket=SPEC_BUCKET_NAME, Key=pdf_key)
            except Exception as e:
                logger.error(f"Failed to delete spec {spec_code}: {str(e)}")
        return redirect_response(f"?token={urllib.parse.quote(token)}&view=catalog")

    if action == 'save':
        spec_code = form.get('spec_code', [''])[0].strip().upper()
        description = form.get('description', [''])[0].strip()
        pdf_url = form.get('pdf_url', [''])[0].strip()

        if not spec_code:
            items = scan_spec_catalog()
            fallback_item = {'spec_code': '', 'description': description, 'pdf_url': pdf_url}
            return html_response(render_catalog(token, items, edit_item=fallback_item, adding_new=True, error="Spec code is required."))

        try:
            existing = table.get_item(Key={'spec_code': spec_code}).get('Item') or {}
            existing_pdf_url = (existing.get('pdf_url') or '').strip()
            pdf_s3_key = existing.get('pdf_s3_key')

            if pdf_url != existing_pdf_url:
                if pdf_url:
                    pdf_bytes = download_pdf(pdf_url)
                    s3_key = f"specs/{spec_code}_specsheet.pdf"
                    s3_client.put_object(Bucket=SPEC_BUCKET_NAME, Key=s3_key, Body=pdf_bytes, ContentType='application/pdf')
                    pdf_s3_key = s3_key
                else:
                    if pdf_s3_key:
                        s3_client.delete_object(Bucket=SPEC_BUCKET_NAME, Key=pdf_s3_key)
                    pdf_s3_key = None

            item = {'spec_code': spec_code, 'description': description}
            if pdf_url:
                item['pdf_url'] = pdf_url
            if pdf_s3_key:
                item['pdf_s3_key'] = pdf_s3_key

            table.put_item(Item=item)
        except Exception as e:
            logger.error(f"Failed to save spec {spec_code}: {str(e)}")
            items = scan_spec_catalog()
            edit_item = {'spec_code': spec_code, 'description': description, 'pdf_url': pdf_url}
            was_new = not any(i.get('spec_code') == spec_code for i in items)
            return html_response(render_catalog(token, items, edit_item, adding_new=was_new, error=f"Save failed: {str(e)}"))

        return redirect_response(f"?token={urllib.parse.quote(token)}&view=catalog")

    return redirect_response(f"?token={urllib.parse.quote(token)}&view=catalog")


# ---------------------------------------------------------------------------
# Reference Tables view: generic browse/add/edit/delete for any table in
# TABLE_REGISTRY. Reuses the exact Spec Catalog select-row-then-toolbar
# pattern (ROW_SELECT_SCRIPT is fully generic already -- it just treats
# data-code as an opaque string) and the Audit Log's scan-with-pagination
# pattern (encode_key/decode_key as the continuation token), generalized
# from 1 hardcoded key field to N key fields from TABLE_REGISTRY.
# ---------------------------------------------------------------------------

TABLE_EDIT_FORM_ID = "table-edit-form"


def render_table_subnav(token, active_table):
    token_esc = html_escape(token)
    links = "".join(
        f"<a href='?token={token_esc}&view=tables&table={html_escape(t)}' "
        f"class='{'active' if t == active_table else ''}' style='font-size:12px;padding:5px 12px'>"
        f"{html_escape(t.removeprefix('whatsapp-'))}</a>"
        for t in TABLE_REGISTRY
    )
    return f"<div class='nav' style='margin-bottom:10px'>{links}</div>"


def render_table_row(key_attrs, item, display_columns):
    row_key = {k: item.get(k) for k in key_attrs}
    encoded = encode_key(row_key)
    cells = "".join(f"<td class='wraptext'>{html_escape(item.get(c, ''))}</td>" for c in display_columns)
    return f"<tr class='selectable' data-code='{html_escape(encoded)}'>{cells}<td></td></tr>"


def render_table_edit_row(token, table_name, key_attrs, item, columns, display_columns, is_new):
    cells = []
    hidden_inputs = []
    for col in columns:
        val = html_escape(item.get(col, '')) if item else ''
        name = f"col::{html_escape(col)}"
        if col not in display_columns:
            # Hidden composite-key column: skip entirely on a brand new row
            # (nothing to round-trip yet -- use the Extra field to set one by
            # hand if truly needed), otherwise carry its existing value
            # through as a hidden field so Save/Delete still see the full key.
            if not is_new:
                hidden_inputs.append(f"<input type='hidden' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}'>")
            continue
        if col in key_attrs:
            if is_new:
                cells.append(f"<td><input type='text' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}' required></td>")
            else:
                cells.append(f"<td>{val}<input type='hidden' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}'></td>")
        else:
            cells.append(f"<td><input type='text' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}'></td>")

    if hidden_inputs:
        cells.append(f"<td style='display:none'>{''.join(hidden_inputs)}</td>")

    extra_cell = (
        f"<td>"
        f"<input type='text' name='extra_name' form='{TABLE_EDIT_FORM_ID}' placeholder='field name' style='width:47%'> "
        f"<input type='text' name='extra_value' form='{TABLE_EDIT_FORM_ID}' placeholder='value' style='width:47%'>"
        f"</td>"
    )

    row_key_encoded = html_escape(encode_key({k: item.get(k) for k in key_attrs})) if item and not is_new else ''
    cancel_qs = f"?token={html_escape(token)}&view=tables&table={html_escape(table_name)}"
    actions_cell = (
        f"<td class='actions'>"
        f"<input type='hidden' name='row_key' form='{TABLE_EDIT_FORM_ID}' value='{row_key_encoded}'>"
        f"<input type='hidden' name='is_new' form='{TABLE_EDIT_FORM_ID}' value='{'1' if is_new else '0'}'>"
        f"<button type='submit' form='{TABLE_EDIT_FORM_ID}' class='primary'>Save</button>"
        f"<a class='btn' href='{cancel_qs}'>Cancel</a>"
        f"</td>"
    )
    return "<tr class='editing'>" + "".join(cells) + extra_cell + actions_cell + "</tr>"


def render_tables(token, table_name, key_attrs, items, columns, display_columns, edit_item, adding_new, next_key, error):
    edit_key = {k: edit_item.get(k) for k in key_attrs} if edit_item else None

    rows = []
    if adding_new:
        rows.append(render_table_edit_row(token, table_name, key_attrs, item=None, columns=columns, display_columns=display_columns, is_new=True))
    for item in items:
        item_key = {k: item.get(k) for k in key_attrs}
        if edit_key and item_key == edit_key:
            rows.append(render_table_edit_row(token, table_name, key_attrs, item=item, columns=columns, display_columns=display_columns, is_new=False))
        else:
            rows.append(render_table_row(key_attrs, item, display_columns))

    header_cells = "".join(f"<th data-col='{i}' class='sortable'>{html_escape(c)}</th>" for i, c in enumerate(display_columns)) + "<th>Extra field</th><th>Actions</th>"
    filter_cells = "".join(f"<td><input type='text' data-col='{i}' placeholder='Filter...'></td>" for i in range(len(display_columns))) + "<td></td><td></td>"

    error_html = f"<div class='error'>{html_escape(error)}</div>" if error else ""
    token_esc = html_escape(token)
    table_esc = html_escape(table_name)

    body = f"""
  {render_table_subnav(token, table_name)}
  <div class="toprow">
    <div class="sub">{len(items)} row(s) shown from <b>{table_esc}</b> (page size {PAGE_SIZE}). Click a row to select it, then Edit or Delete.</div>
    <div class="toolbar">
      <a class="btn primary" href="?token={token_esc}&view=tables&table={table_esc}&edit=__new__">+ New</a>
      <a id="editBtn" class="btn disabled" href="#" data-href-base="?token={token_esc}&view=tables&table={table_esc}&edit=">Edit</a>
      <button id="deleteBtn" class="btn danger" disabled type="button">Delete</button>
    </div>
  </div>
  {error_html}

  <form id="{TABLE_EDIT_FORM_ID}" method="post">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="tables">
    <input type="hidden" name="table" value="{table_esc}">
    <input type="hidden" name="action" value="save">
  </form>

  <form id="deleteForm" method="post" style="display:none">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="tables">
    <input type="hidden" name="table" value="{table_esc}">
    <input type="hidden" name="action" value="delete">
    <input type="hidden" id="deleteSpecCode" name="row_key" value="">
  </form>

  <div class="wrap">
  <table id="catalogTable">
    <thead>
      <tr>{header_cells}</tr>
      <tr class="filter-row">{filter_cells}</tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else f'<tr><td colspan="{len(display_columns) + 2}">No rows.</td></tr>'}
    </tbody>
  </table>
  </div>
"""
    if next_key:
        qs = f"?token={token_esc}&view=tables&table={table_esc}&last_key={html_escape(encode_key(next_key))}"
        body += f"<a class='more' href='{qs}'>Load more &rarr;</a>"

    return page_shell(f"WhatsApp Label Verification - {table_name}", render_nav(token, 'tables'), body, extra_script=ROW_SELECT_SCRIPT)


def handle_tables_get(token, query_params):
    table_name = query_params.get('table', '').strip()
    if table_name not in TABLE_REGISTRY:
        table_name = next(iter(TABLE_REGISTRY))
    key_attrs = TABLE_REGISTRY[table_name]
    last_key = decode_key(query_params.get('last_key'))

    table = dynamodb.Table(table_name)
    scan_kwargs = {'Limit': PAGE_SIZE}
    if last_key:
        scan_kwargs['ExclusiveStartKey'] = last_key
    try:
        response = table.scan(**scan_kwargs)
        items = response.get('Items', [])
        next_key = response.get('LastEvaluatedKey')
    except Exception as e:
        logger.error(f"Failed to scan {table_name}: {str(e)}")
        return text_response(500, f'Error loading {table_name}: {str(e)}')

    columns = list(key_attrs)
    for item in items:
        for k in item.keys():
            if k not in columns:
                columns.append(k)
    display_columns = [c for c in columns if c not in HIDDEN_TABLE_COLUMNS]

    edit_item = None
    adding_new = False
    edit_param = query_params.get('edit', '').strip()
    if edit_param == '__new__':
        adding_new = True
    elif edit_param:
        row_key = decode_key(edit_param)
        if row_key:
            try:
                edit_item = table.get_item(Key=row_key).get('Item')
            except Exception as e:
                logger.error(f"Failed to load row for edit in {table_name}: {str(e)}")

    html = render_tables(token, table_name, key_attrs, items, columns, display_columns, edit_item, adding_new, next_key, error=None)
    return html_response(html)


def handle_tables_post(token, form):
    table_name = form.get('table', [''])[0]
    if table_name not in TABLE_REGISTRY:
        return redirect_response(f"?token={urllib.parse.quote(token)}&view=tables")
    key_attrs = TABLE_REGISTRY[table_name]
    action = form.get('action', [''])[0]
    table = dynamodb.Table(table_name)
    table_qs = f"?token={urllib.parse.quote(token)}&view=tables&table={urllib.parse.quote(table_name)}"

    if action == 'delete':
        row_key = decode_key(form.get('row_key', [''])[0])
        if row_key:
            try:
                table.delete_item(Key=row_key)
            except Exception as e:
                logger.error(f"Failed to delete row from {table_name}: {str(e)}")
        return redirect_response(table_qs)

    if action == 'save':
        item = {}
        for field_name, values in form.items():
            if field_name.startswith('col::'):
                col = field_name[len('col::'):]
                val = (values[0] if values else '').strip()
                if val:
                    item[col] = val
        extra_name = form.get('extra_name', [''])[0].strip()
        extra_value = form.get('extra_value', [''])[0].strip()
        if extra_name and extra_value:
            item[extra_name] = extra_value

        # Key fields are marked `required` client-side; a submission missing
        # one (e.g. a bypassed/malformed request) is silently dropped rather
        # than saved with a partial/ambiguous key -- no data corruption, just
        # a no-op back to the list.
        if all(item.get(k) for k in key_attrs):
            try:
                table.put_item(Item=item)
            except Exception as e:
                logger.error(f"Failed to save row in {table_name}: {str(e)}")

        return redirect_response(table_qs)

    return redirect_response(table_qs)


# ---------------------------------------------------------------------------
# Spec Files view: browse/edit/upload/delete the S3 specs/ prefix (label
# images, allowed-combination images, spec sheets, and the JSON validation
# configs). Editing a .json inline (view+save via a textarea) replaces the
# manual "edit locally, then `aws s3 cp`" loop used everywhere else this
# session. File uploads are read client-side (FileReader -> base64 -> a
# hidden field on the same urlencoded form) rather than parsed server-side
# as multipart/form-data -- avoids the deprecated `cgi` module and any new
# dependency, and comfortably fits Lambda's 6MB synchronous payload limit at
# this bucket's file sizes.
# ---------------------------------------------------------------------------

FILES_SCRIPT = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
<style>
  .CodeMirror { border: 1px solid #d3d8e0; border-radius: 6px; height: auto; font-size: 12px; }
</style>
<script>
(function() {
  document.querySelectorAll('.replaceBtn').forEach(function(btn) {
    var form = btn.closest('form');
    var fileInput = form.querySelector('.fileInput');
    var b64Target = form.querySelector('.b64target');
    btn.addEventListener('click', function() { fileInput.click(); });
    fileInput.addEventListener('change', function() {
      if (!fileInput.files.length) return;
      // The "+ upload a new file" form has a filename text box the user can
      // type into ahead of time to override the name -- but leaving it
      // blank (the common case) used to silently upload nothing at all,
      // since the server has no key to put_object under. Default it from
      // the picked file's own name instead of requiring that step.
      var filenameInput = form.querySelector('input[name="filename"]');
      if (filenameInput && !filenameInput.value.trim()) {
        var pickedName = fileInput.files[0].name;
        var specInput = form.querySelector('input[name="spec"]');
        var specCode = specInput ? specInput.value.trim() : '';
        if (specCode && pickedName.toUpperCase().indexOf(specCode.toUpperCase() + '_') !== 0) {
          pickedName = specCode + '_' + pickedName;
        }
        filenameInput.value = pickedName;
      }
      var reader = new FileReader();
      reader.onload = function() {
        var result = reader.result;
        b64Target.value = result.substring(result.indexOf(',') + 1);
        form.submit();
      };
      reader.readAsDataURL(fileInput.files[0]);
    });
  });

  // Wraps the raw JSON textarea with CodeMirror (syntax highlighting, bracket
  // matching, line numbers) and validates on every keystroke instead of only
  // after Save is clicked -- CodeMirror.fromTextArea keeps the original
  // <textarea> in the DOM (just hidden) and .save() copies the editor's
  // content back into it, so the existing form/POST handling needs no change.
  document.querySelectorAll('textarea.json-editor').forEach(function(textarea) {
    if (typeof CodeMirror === 'undefined') return;
    var errorBox = textarea.parentElement.querySelector('.json-editor-error');
    var editor = CodeMirror.fromTextArea(textarea, {
      mode: 'application/json',
      lineNumbers: true,
      matchBrackets: true,
      indentUnit: 2,
      tabSize: 2,
      viewportMargin: Infinity,
    });
    editor.setSize('100%', '480px');

    function validate() {
      try {
        JSON.parse(editor.getValue());
        if (errorBox) errorBox.style.display = 'none';
        return true;
      } catch (e) {
        if (errorBox) {
          errorBox.textContent = 'Invalid JSON: ' + e.message;
          errorBox.style.display = 'block';
        }
        return false;
      }
    }
    editor.on('change', validate);
    validate();

    var form = textarea.closest('form');
    if (form) {
      form.addEventListener('submit', function(e) {
        editor.save();
        if (!validate()) { e.preventDefault(); }
      });
    }
  });

  // Click a header to sort. The all-specs view groups files under a
  // per-spec heading row (a single wide <td colspan>, not per-column data)
  // -- sorting by File/Size/Modified doesn't respect that grouping, so those
  // heading rows (and the "no files yet" placeholder, same shape) are
  // hidden rather than reordered.
  var filesTable = document.getElementById('filesTable');
  if (filesTable) {
    var fileHeaders = filesTable.querySelectorAll('thead th[data-col]');
    var fileSortState = { col: null, dir: 1 };

    function fileCellValue(row, col) {
      var cell = row.children[col];
      return cell ? cell.textContent.trim() : '';
    }

    function compareFileCells(col, av, bv) {
      if (col === 2) { return parseFloat(av) - parseFloat(bv); } // "236.7 KB" -> numeric
      return av.toLowerCase().localeCompare(bv.toLowerCase()); // filename, and "YYYY-MM-DD HH:MM" sorts correctly as text
    }

    fileHeaders.forEach(function(th) {
      th.addEventListener('click', function() {
        var col = parseInt(th.getAttribute('data-col'), 10);
        fileSortState.dir = (fileSortState.col === col) ? -fileSortState.dir : 1;
        fileSortState.col = col;
        fileHeaders.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(fileSortState.dir === 1 ? 'sort-asc' : 'sort-desc');

        var tbody = filesTable.querySelector('tbody');
        var fileRows = [];
        Array.prototype.slice.call(tbody.querySelectorAll('tr')).forEach(function(row) {
          if (row.children.length > 1) {
            fileRows.push(row);
          } else {
            row.style.display = 'none';
          }
        });
        fileRows.sort(function(a, b) {
          return compareFileCells(col, fileCellValue(a, col), fileCellValue(b, col)) * fileSortState.dir;
        });
        fileRows.forEach(function(row) { tbody.appendChild(row); });
      });
    });
  }
})();
</script>
"""


def guess_content_type(key):
    if key.endswith('.json'):
        return 'application/json'
    if key.endswith('.png'):
        return 'image/png'
    if key.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg'
    if key.endswith('.pdf'):
        return 'application/pdf'
    return 'application/octet-stream'


def list_spec_files():
    files = []
    continuation = None
    while True:
        kwargs = {'Bucket': SPEC_BUCKET_NAME, 'Prefix': 'specs/'}
        if continuation:
            kwargs['ContinuationToken'] = continuation
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            filename = key[len('specs/'):]
            if not filename:
                continue
            files.append({'key': key, 'filename': filename, 'size': obj['Size'], 'last_modified': obj['LastModified']})
        if resp.get('IsTruncated'):
            continuation = resp.get('NextContinuationToken')
        else:
            break
    return files


def group_spec_files(files):
    groups = {}
    for f in files:
        code = f['filename'].split('_', 1)[0]
        groups.setdefault(code, []).append(f)
    return groups


ALL_SPECS = "__all__"


def render_spec_selector(token, groups, active_spec, descriptions):
    token_esc = html_escape(token)
    chips = "".join(
        # Clicking the already-active chip again would otherwise just reload
        # the same single-spec view -- instead it toggles to the "all specs"
        # view, a cheap way to get a "deselect" without any client-side JS.
        f"<a href='?token={token_esc}&view=files&spec={ALL_SPECS if code == active_spec else html_escape(code)}' "
        f"class='{'active' if code == active_spec else ''}' style='font-size:12px;padding:5px 12px' "
        f"title='{html_escape(descriptions.get(code, 'No Spec Catalog description on file'))}'>"
        f"{html_escape(code)} ({len(items)})</a>"
        for code, items in sorted(groups.items(), key=lambda pair: spec_code_sort_key(pair[0]))
    )
    return f"<div class='nav' style='flex-wrap:wrap;margin-bottom:14px'>{chips}</div>"


def render_file_row(token, spec_code, f, editing, error=None, content_override=None):
    key = f['key']
    filename = f['filename']
    is_json = filename.endswith('.json')
    is_image = filename.lower().endswith(('.png', '.jpg', '.jpeg'))
    size_kb = f"{f['size'] / 1024:.1f} KB"
    modified = f['last_modified'].strftime('%Y-%m-%d %H:%M') if hasattr(f['last_modified'], 'strftime') else str(f['last_modified'])
    token_esc = html_escape(token)

    if editing and is_json:
        if content_override is not None:
            content = content_override
        else:
            try:
                content = s3_client.get_object(Bucket=SPEC_BUCKET_NAME, Key=key)['Body'].read().decode('utf-8')
            except s3_client.exceptions.NoSuchKey:
                content = '{\n  \n}\n'
            except Exception as e:
                content = f'{{\n  "_error": "Failed to load: {str(e)}"\n}}'
        cancel_qs = f"?token={token_esc}&view=files&spec={html_escape(spec_code)}"
        error_html = f"<div class='error'>{html_escape(error)}</div>" if error else ""
        return f"""
    <div class="wrap" style="margin-bottom:14px;padding:12px">
      {error_html}
      <div style="font-weight:600;margin-bottom:6px">{html_escape(filename)}</div>
      <form method="post">
        <input type="hidden" name="token" value="{token_esc}">
        <input type="hidden" name="view" value="files">
        <input type="hidden" name="action" value="save_json">
        <input type="hidden" name="key" value="{html_escape(key)}">
        <input type="hidden" name="spec" value="{html_escape(spec_code)}">
        <textarea name="content" class="json-editor" rows="20" style="width:100%;font-family:monospace;font-size:12px">{html_escape(content)}</textarea>
        <div class="error json-editor-error" style="display:none"></div>
        <div style="margin-top:8px;display:flex;gap:8px">
          <button type="submit" class="primary">Save</button>
          <a class="btn" href="{cancel_qs}">Cancel</a>
        </div>
      </form>
    </div>
"""

    thumb = ""
    if is_image:
        url = get_presigned_url(key)
        thumb = f"<a href='{html_escape(url)}' target='_blank' rel='noopener'><img class='thumb' style='height:70px' src='{html_escape(url)}' loading='lazy'></a>"
    elif filename.endswith('.pdf'):
        url = get_presigned_url(key)
        thumb = f"<a class='pill yes' href='{html_escape(url)}' target='_blank' rel='noopener'>PDF &#10003;</a>"

    actions = []
    if is_json:
        actions.append(f"<a class='btn' href='?token={token_esc}&view=files&spec={html_escape(spec_code)}&edit={urllib.parse.quote(key)}'>Edit</a>")
    actions.append(f"""
      <form method="post" style="display:inline">
        <input type="hidden" name="token" value="{token_esc}">
        <input type="hidden" name="view" value="files">
        <input type="hidden" name="action" value="upload">
        <input type="hidden" name="key" value="{html_escape(key)}">
        <input type="hidden" name="spec" value="{html_escape(spec_code)}">
        <input type="hidden" name="content_b64" class="b64target">
        <input type="file" class="fileInput" style="display:none">
        <button type="button" class="btn replaceBtn">Replace</button>
      </form>
    """)
    actions.append(f"""
      <form method="post" style="display:inline" onsubmit="return confirm('Delete {html_escape(filename)}?')">
        <input type="hidden" name="token" value="{token_esc}">
        <input type="hidden" name="view" value="files">
        <input type="hidden" name="action" value="delete">
        <input type="hidden" name="key" value="{html_escape(key)}">
        <input type="hidden" name="spec" value="{html_escape(spec_code)}">
        <button type="submit" class="btn danger">Delete</button>
      </form>
    """)

    return (
        "<tr>"
        f"<td>{html_escape(filename)}</td>"
        f"<td>{thumb}</td>"
        f"<td>{size_kb}</td>"
        f"<td>{html_escape(modified)}</td>"
        f"<td class='actions'>{''.join(actions)}</td>"
        "</tr>"
    )


def render_upload_form(token, spec_value):
    """Shared by the single-spec and all-specs views: uploads target a
    filename with an explicit spec-code prefix (e.g. "17D_label.png"), so
    which spec owns the file is decided by that filename, not by which view
    the admin happened to be browsing -- the "spec" field just controls
    which page they land back on after a successful upload. Left blank on
    the all-specs view (typing a code there re-lands on that spec's page)."""
    token_esc = html_escape(token)
    spec_esc = html_escape(spec_value)
    return f"""
  <form method="post" style="display:flex;gap:8px;align-items:center;margin-bottom:16px">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="files">
    <input type="hidden" name="action" value="upload_new">
    <input type="text" name="spec" value="{spec_esc}" placeholder="Spec code" style="width:90px">
    <input type="text" name="filename" placeholder="e.g. 17D_label.png (include spec code prefix)" style="width:280px">
    <input type="hidden" name="content_b64" class="b64target">
    <input type="file" class="fileInput" style="display:none">
    <button type="button" class="btn replaceBtn">Choose file &amp; upload</button>
  </form>
"""


def render_files(token, groups, spec_code, edit_key, edit_error, edit_content_override, descriptions, page_error=None):
    token_esc = html_escape(token)
    selector = render_spec_selector(token, groups, spec_code, descriptions)
    page_error_html = f"<div class='error'>{html_escape(page_error)}</div>" if page_error else ""

    if spec_code == ALL_SPECS:
        total_files = sum(len(v) for v in groups.values())
        description_banner = (
            f"<div class='sub' style='font-size:15px;font-weight:600;color:#1f2430'>"
            f"All specs &mdash; {len(groups)} spec(s), {total_files} file(s)</div>"
        )
        rows = []
        for code in sorted(groups.keys(), key=spec_code_sort_key):
            desc = descriptions.get(code, '')
            heading = f"{html_escape(code)}" + (f" &mdash; {html_escape(desc)}" if desc else "")
            rows.append(f"<tr><td colspan='5' style='background:#f5f7f5;font-weight:600;padding-top:14px'>{heading}</td></tr>")
            for f in sorted(groups[code], key=lambda x: x['filename']):
                editing = edit_key == f['key']
                # Every row's "spec" context is the ALL_SPECS sentinel, not
                # the file's own owning code -- that's what makes Cancel/
                # Save/Delete/Replace redirect back to this all-specs view
                # instead of jumping into that one spec's single-spec view.
                rows.append(render_file_row(token, ALL_SPECS, f, editing, edit_error if editing else None, edit_content_override if editing else None))
        new_config_link = ""  # "+ New config" builds a specs/{code}_config.json key, so it needs one specific spec picked, not meaningful here
        upload_form = render_upload_form(token, spec_value="")
    else:
        spec_files = sorted(groups.get(spec_code, []), key=lambda f: f['filename'])

        spec_description = descriptions.get(spec_code, '')
        description_banner = ""
        if spec_code:
            description_banner = (
                f"<div class='sub' style='font-size:15px;font-weight:600;color:#1f2430'>"
                f"{html_escape(spec_code)} &mdash; {html_escape(spec_description) if spec_description else '<i>no Spec Catalog description on file</i>'}"
                f"</div>"
            )

        has_config = any(f['filename'] == f"{spec_code}_config.json" for f in spec_files)
        new_config_key = f"specs/{spec_code}_config.json" if spec_code else None

        rows = []
        if spec_code and not has_config and edit_key == new_config_key:
            synthetic = {'key': new_config_key, 'filename': f"{spec_code}_config.json", 'size': 0, 'last_modified': ''}
            rows.append(render_file_row(token, spec_code, synthetic, True, edit_error, edit_content_override))
        for f in spec_files:
            editing = edit_key == f['key']
            rows.append(render_file_row(token, spec_code, f, editing, edit_error if editing else None, edit_content_override if editing else None))

        new_config_link = (
            f"<a class='btn' href='?token={token_esc}&view=files&spec={html_escape(spec_code)}&edit={urllib.parse.quote(new_config_key)}'>+ New config</a>"
            if spec_code and not has_config else ""
        )

        upload_form = render_upload_form(token, spec_value=spec_code)

    body = f"""
  {selector}
  {page_error_html}
  {description_banner}
  <div class="toolbar">{new_config_link}</div>
  {upload_form}
  <div class="wrap">
  <table id="filesTable">
    <thead><tr><th data-col="0" class="sortable">File</th><th>Preview</th><th data-col="2" class="sortable">Size</th><th data-col="3" class="sortable">Modified</th><th>Actions</th></tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="5">No files for this spec yet.</td></tr>'}
    </tbody>
  </table>
  </div>
"""
    return page_shell("WhatsApp Label Verification - Spec Files", render_nav(token, 'files'), body, extra_script=FILES_SCRIPT)


def load_spec_descriptions():
    """Reuses the Spec Catalog table's own `description` field (e.g. "BIEDRONKA
    | 4.5kg Paper Bags | 2026 | V1.0") so the Spec Files tab can show which
    retailer/product a spec code actually is, instead of just a bare code."""
    try:
        return {item.get('spec_code', ''): item.get('description', '') for item in scan_spec_catalog()}
    except Exception as e:
        logger.error(f"Failed to load spec catalog descriptions: {str(e)}")
        return {}


def handle_files_get(token, query_params):
    try:
        files = list_spec_files()
    except Exception as e:
        logger.error(f"Failed to list spec files: {str(e)}")
        return text_response(500, f'Error listing spec files: {str(e)}')

    groups = group_spec_files(files)
    # Opening the tab fresh (no ?spec= given) used to auto-select whichever
    # spec sorted first ("00") -- confusing, since that's not actually a
    # meaningful default, just an accident of sort order. Land on the
    # all-specs view instead, same place re-clicking the active chip goes.
    spec_code = query_params.get('spec', '').strip() or ALL_SPECS

    edit_key = query_params.get('edit', '').strip() or None
    descriptions = load_spec_descriptions()
    html = render_files(token, groups, spec_code, edit_key=edit_key, edit_error=None, edit_content_override=None, descriptions=descriptions)
    return html_response(html)


def handle_files_post(token, form):
    action = form.get('action', [''])[0]
    spec_code = form.get('spec', [''])[0].strip()
    files_qs = f"?token={urllib.parse.quote(token)}&view=files"
    if spec_code:
        files_qs += f"&spec={urllib.parse.quote(spec_code)}"

    if action == 'delete':
        key = form.get('key', [''])[0]
        if key:
            try:
                s3_client.delete_object(Bucket=SPEC_BUCKET_NAME, Key=key)
            except Exception as e:
                logger.error(f"Failed to delete {key}: {str(e)}")
        return redirect_response(files_qs)

    if action in ('upload', 'upload_new'):
        content_b64 = form.get('content_b64', [''])[0]
        if action == 'upload':
            key = form.get('key', [''])[0]
        else:
            filename = form.get('filename', [''])[0].strip()
            key = f"specs/{filename}" if filename else None

        upload_error = None
        if not key:
            upload_error = "Upload failed: no filename given."
        elif not content_b64:
            upload_error = "Upload failed: no file was selected."
        else:
            try:
                s3_client.put_object(
                    Bucket=SPEC_BUCKET_NAME, Key=key,
                    Body=base64.b64decode(content_b64), ContentType=guess_content_type(key)
                )
            except Exception as e:
                logger.error(f"Failed to upload {key}: {str(e)}")
                upload_error = f"Upload failed: {str(e)}"

        if upload_error:
            # A silent no-op here (the original behavior) is exactly what
            # produced the bug this replaced: a blank/failed upload just
            # redirected back to an unchanged page with no indication
            # anything went wrong.
            try:
                files = list_spec_files()
            except Exception:
                files = []
            groups = group_spec_files(files)
            html = render_files(
                token, groups, spec_code, edit_key=None, edit_error=None, edit_content_override=None,
                descriptions=load_spec_descriptions(), page_error=upload_error
            )
            return html_response(html)

        return redirect_response(files_qs)

    if action == 'save_json':
        key = form.get('key', [''])[0]
        content = form.get('content', [''])[0]
        try:
            json.loads(content)
            s3_client.put_object(Bucket=SPEC_BUCKET_NAME, Key=key, Body=content.encode('utf-8'), ContentType='application/json')
        except json.JSONDecodeError as e:
            try:
                files = list_spec_files()
            except Exception:
                files = []
            groups = group_spec_files(files)
            html = render_files(
                token, groups, spec_code, edit_key=key, edit_error=f"Invalid JSON: {str(e)}",
                edit_content_override=content, descriptions=load_spec_descriptions()
            )
            return html_response(html)
        except Exception as e:
            logger.error(f"Failed to save {key}: {str(e)}")
        return redirect_response(files_qs)

    return redirect_response(files_qs)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def html_response(html):
    return {'statusCode': 200, 'headers': {'Content-Type': 'text/html; charset=utf-8'}, 'body': html}


def text_response(status, text):
    return {'statusCode': status, 'headers': {'Content-Type': 'text/plain'}, 'body': text}


def redirect_response(location):
    return {'statusCode': 303, 'headers': {'Location': location}, 'body': ''}


def parse_form_body(event):
    body = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8')
    return urllib.parse.parse_qs(body)


def lambda_handler(event, context):
    http_method = (event.get('requestContext', {}).get('http', {}) or {}).get('method', 'GET')
    query_params = event.get('queryStringParameters') or {}

    dashboard_token = get_ssm_param('/whatsapp/dashboard_token')

    if http_method == 'POST':
        form = parse_form_body(event)
        token = form.get('token', [''])[0]
        view = form.get('view', ['audit'])[0]
    else:
        token = query_params.get('token', '')
        view = query_params.get('view', 'audit')

    if not dashboard_token or token != dashboard_token:
        return text_response(401, 'Unauthorized. Append ?token=<your dashboard token> to the URL.')

    try:
        if view == 'catalog':
            if http_method == 'POST':
                return handle_catalog_post(token, form)
            return handle_catalog_get(token, query_params)
        elif view == 'tables':
            if http_method == 'POST':
                return handle_tables_post(token, form)
            return handle_tables_get(token, query_params)
        elif view == 'files':
            if http_method == 'POST':
                return handle_files_post(token, form)
            return handle_files_get(token, query_params)
        else:
            return handle_audit_log(token, query_params)
    except Exception as e:
        logger.error(f"Unhandled error in dashboard: {str(e)}", exc_info=True)
        return text_response(500, f'Unexpected error: {str(e)}')
