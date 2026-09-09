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
    "whatsapp-india-addresses": ["Code"],
}

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
  .topbar { background:#fff; border-bottom:1px solid #e2e5ea; padding:16px 24px; }
  .topbar h1 { font-size:19px; margin:0; color:#1f2430; }
  .content { padding:20px 24px; }
  .nav { margin-bottom:18px; display:flex; gap:6px; }
  .nav a { color:#5b6473; text-decoration:none; padding:6px 16px; border-radius:6px; font-size:13px; border:1px solid #dde1e7; background:#fff; }
  .nav a.active { background:#2f6fed; border-color:#2f6fed; color:#fff; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:14px; }
  .chips { margin-bottom:16px; }
  .chip { display:inline-block; background:#fff; border:1px solid #dde1e7; border-radius:999px; padding:4px 12px; margin:0 8px 8px 0; font-size:13px; color:#374151; }
  .toolbar { display:flex; justify-content:flex-end; gap:8px; margin-bottom:14px; }
  form.filters { margin-bottom:16px; display:flex; gap:8px; flex-wrap:wrap; }
  input, select, textarea { background:#fff; border:1px solid #d3d8e0; color:#1f2430; border-radius:6px; padding:6px 10px; font-size:13px; font-family:inherit; }
  button, .btn { background:#fff; border:1px solid #d3d8e0; color:#374151; border-radius:6px; padding:7px 16px; font-size:13px; cursor:pointer; text-decoration:none; display:inline-block; }
  .btn.primary, button.primary { background:#22a55e; border-color:#22a55e; color:#fff; }
  .btn.danger, button.danger { color:#d64545; border-color:#f3c9c9; }
  .btn.disabled, button:disabled { opacity:.4; cursor:not-allowed; pointer-events:none; }
  table { border-collapse:collapse; width:100%; font-size:13px; background:#fff; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid #edf0f3; vertical-align:top; }
  th { color:#5b6473; font-weight:600; background:#f5f7f5; border-bottom:1px solid #dde1e7; white-space:nowrap; }
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
  .wrap { overflow-x:auto; border:1px solid #dde1e7; border-radius:8px; }
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
<style>{PAGE_STYLE}</style>
</head>
<body>
  <div class="topbar"><h1>WhatsApp Label Verification</h1></div>
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
  <div class="sub">{len(items)} spec(s) in the catalog. Click a row to select it, then Edit or Delete.</div>
  {error_html}

  <div class="toolbar">
    <a class="btn primary" href="?token={token_esc}&view=catalog&edit=__new__">+ New</a>
    <a id="editBtn" class="btn disabled" href="#" data-href-base="?token={token_esc}&view=catalog&edit=">Edit</a>
    <button id="deleteBtn" class="btn danger" disabled type="button">Delete</button>
  </div>

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
      <tr><th>Spec Code</th><th>Description</th><th>PDF</th><th>Actions</th></tr>
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


def render_table_row(key_attrs, item, columns):
    row_key = {k: item.get(k) for k in key_attrs}
    encoded = encode_key(row_key)
    cells = "".join(f"<td>{html_escape(item.get(c, ''))}</td>" for c in columns)
    return f"<tr class='selectable' data-code='{html_escape(encoded)}'>{cells}<td></td></tr>"


def render_table_edit_row(token, table_name, key_attrs, item, columns, is_new):
    cells = []
    for col in columns:
        val = html_escape(item.get(col, '')) if item else ''
        name = f"col::{html_escape(col)}"
        if col in key_attrs:
            if is_new:
                cells.append(f"<td><input type='text' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}' required></td>")
            else:
                cells.append(f"<td>{val}<input type='hidden' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}'></td>")
        else:
            cells.append(f"<td><input type='text' name='{name}' form='{TABLE_EDIT_FORM_ID}' value='{val}'></td>")

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


def render_tables(token, table_name, key_attrs, items, columns, edit_item, adding_new, next_key, error):
    edit_key = {k: edit_item.get(k) for k in key_attrs} if edit_item else None

    rows = []
    if adding_new:
        rows.append(render_table_edit_row(token, table_name, key_attrs, item=None, columns=columns, is_new=True))
    for item in items:
        item_key = {k: item.get(k) for k in key_attrs}
        if edit_key and item_key == edit_key:
            rows.append(render_table_edit_row(token, table_name, key_attrs, item=item, columns=columns, is_new=False))
        else:
            rows.append(render_table_row(key_attrs, item, columns))

    header_cells = "".join(f"<th>{html_escape(c)}</th>" for c in columns) + "<th>Extra field</th><th>Actions</th>"
    filter_cells = "".join(f"<td><input type='text' data-col='{i}' placeholder='Filter...'></td>" for i in range(len(columns))) + "<td></td><td></td>"

    error_html = f"<div class='error'>{html_escape(error)}</div>" if error else ""
    token_esc = html_escape(token)
    table_esc = html_escape(table_name)

    body = f"""
  {render_table_subnav(token, table_name)}
  <div class="sub">{len(items)} row(s) shown from <b>{table_esc}</b> (page size {PAGE_SIZE}). Click a row to select it, then Edit or Delete.</div>
  {error_html}

  <div class="toolbar">
    <a class="btn primary" href="?token={token_esc}&view=tables&table={table_esc}&edit=__new__">+ New</a>
    <a id="editBtn" class="btn disabled" href="#" data-href-base="?token={token_esc}&view=tables&table={table_esc}&edit=">Edit</a>
    <button id="deleteBtn" class="btn danger" disabled type="button">Delete</button>
  </div>

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
      {''.join(rows) if rows else f'<tr><td colspan="{len(columns) + 2}">No rows.</td></tr>'}
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

    html = render_tables(token, table_name, key_attrs, items, columns, edit_item, adding_new, next_key, error=None)
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


def render_spec_selector(token, groups, active_spec, descriptions):
    token_esc = html_escape(token)
    chips = "".join(
        f"<a href='?token={token_esc}&view=files&spec={html_escape(code)}' "
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


def render_files(token, groups, spec_code, edit_key, edit_error, edit_content_override, descriptions, page_error=None):
    token_esc = html_escape(token)
    selector = render_spec_selector(token, groups, spec_code, descriptions)
    spec_files = sorted(groups.get(spec_code, []), key=lambda f: f['filename'])

    page_error_html = f"<div class='error'>{html_escape(page_error)}</div>" if page_error else ""

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

    upload_form = f"""
  <form method="post" style="display:flex;gap:8px;align-items:center;margin-bottom:16px">
    <input type="hidden" name="token" value="{token_esc}">
    <input type="hidden" name="view" value="files">
    <input type="hidden" name="action" value="upload_new">
    <input type="text" name="spec" value="{html_escape(spec_code)}" placeholder="Spec code" style="width:90px">
    <input type="text" name="filename" placeholder="e.g. 17D_label.png (include spec code prefix)" style="width:280px">
    <input type="hidden" name="content_b64" class="b64target">
    <input type="file" class="fileInput" style="display:none">
    <button type="button" class="btn replaceBtn">Choose file &amp; upload</button>
  </form>
"""

    body = f"""
  {selector}
  {page_error_html}
  {description_banner}
  <div class="toolbar">{new_config_link}</div>
  {upload_form}
  <div class="wrap">
  <table>
    <thead><tr><th>File</th><th>Preview</th><th>Size</th><th>Modified</th><th>Actions</th></tr></thead>
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
    spec_code = query_params.get('spec', '').strip()
    if not spec_code and groups:
        spec_code = sorted(groups.keys(), key=spec_code_sort_key)[0]

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
