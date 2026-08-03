import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get('WHATSAPP_AWS_REGION', 'us-east-2')
AUDIT_TABLE_NAME = os.environ.get('AUDIT_TABLE_NAME', 'whatsapp-audit-log')
GSI_NAME = os.environ.get('AUDIT_GSI_NAME', 'record_type-timestamp-index')
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


def get_image_url(image_key):
    if not image_key:
        return None
    try:
        return s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': SPEC_BUCKET_NAME, 'Key': image_key},
            ExpiresIn=IMAGE_URL_TTL_SECONDS
        )
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {image_key}: {str(e)}")
        return None


def html_escape(value):
    return (
        str(value)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def render_dashboard(items, counts, filters, next_key):
    rows = []
    for item in items:
        result = item.get('result', '')
        color, icon = RESULT_STYLES.get(result, ('#6b7280', '•'))
        image_url = get_image_url(item.get('image_key'))
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
        qs = f"?token={html_escape(filters['token'])}&last_key={html_escape(encode_key(next_key))}"
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

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp Label Verification - Audit Log</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0f1115; color:#e5e7eb; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .sub {{ color:#9ca3af; font-size:13px; margin-bottom:20px; }}
  .chips {{ margin-bottom:16px; }}
  .chip {{ display:inline-block; background:#1f2430; border:1px solid #2d3340; border-radius:999px; padding:4px 12px; margin:0 8px 8px 0; font-size:13px; }}
  form.filters {{ margin-bottom:16px; display:flex; gap:8px; flex-wrap:wrap; }}
  input, select {{ background:#1f2430; border:1px solid #2d3340; color:#e5e7eb; border-radius:6px; padding:6px 10px; font-size:13px; }}
  button {{ background:#3b82f6; border:none; color:white; border-radius:6px; padding:6px 14px; font-size:13px; cursor:pointer; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #232833; vertical-align:top; }}
  th {{ color:#9ca3af; font-weight:600; position:sticky; top:0; background:#0f1115; }}
  td.detail {{ color:#9ca3af; max-width:420px; }}
  .thumb {{ height:48px; width:auto; border-radius:4px; border:1px solid #2d3340; display:block; }}
  .more {{ display:inline-block; margin-top:16px; color:#60a5fa; text-decoration:none; }}
  .wrap {{ overflow-x:auto; }}
</style>
</head>
<body>
  <h1>WhatsApp Label Verification &mdash; Audit Log</h1>
  <div class="sub">Showing up to {PAGE_SIZE} most recent records{' (filtered)' if (sender_val or result_val) else ''}</div>
  <div class="chips">{count_chips}</div>
  <form class="filters" method="get">
    <input type="hidden" name="token" value="{html_escape(filters['token'])}">
    <input type="text" name="sender" placeholder="Filter by sender phone" value="{sender_val}">
    <select name="result">
      <option value="">All results</option>
      {result_options}
    </select>
    <button type="submit">Filter</button>
  </form>
  <div class="wrap">
  <table>
    <thead><tr><th>Timestamp (SAST)</th><th>Sender</th><th>Name</th><th>Type</th><th>Spec</th><th>Result</th><th>Detail</th><th>Photo</th></tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="7">No records found.</td></tr>'}
    </tbody>
  </table>
  </div>
  {next_link}
</body>
</html>
"""


def lambda_handler(event, context):
    query_params = event.get('queryStringParameters') or {}
    token = query_params.get('token', '')

    dashboard_token = get_ssm_param('/whatsapp/dashboard_token')
    if not dashboard_token or token != dashboard_token:
        return {
            'statusCode': 401,
            'headers': {'Content-Type': 'text/plain'},
            'body': 'Unauthorized. Append ?token=<your dashboard token> to the URL.'
        }

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
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'text/plain'},
            'body': f'Error querying audit log: {str(e)}'
        }

    counts = {}
    for item in items:
        r = item.get('result', 'UNKNOWN')
        counts[r] = counts.get(r, 0) + 1

    html = render_dashboard(
        items,
        counts,
        {'token': token, 'sender': sender_filter, 'result': result_filter},
        next_key
    )

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/html; charset=utf-8'},
        'body': html
    }
