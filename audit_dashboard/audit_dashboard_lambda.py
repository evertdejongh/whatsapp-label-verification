import base64
import json
import logging
import os
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


def render_nav(token, active):
    token_qs = html_escape(token)
    return f"""<div class="nav">
    <a href="?token={token_qs}&view=audit" class="{'active' if active == 'audit' else ''}">Audit Log</a>
    <a href="?token={token_qs}&view=catalog" class="{'active' if active == 'catalog' else ''}">Spec Catalog</a>
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
    return sorted(items, key=lambda i: i.get('spec_code', ''))


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
        else:
            return handle_audit_log(token, query_params)
    except Exception as e:
        logger.error(f"Unhandled error in dashboard: {str(e)}", exc_info=True)
        return text_response(500, f'Unexpected error: {str(e)}')
