"""
whatsapp-config-sanity-check -- scheduled (weekly, EventBridge) + on-demand
(invoked by the audit dashboard's "Run Now" button) Lambda that re-runs the
same check as tools/check_allowed_vs_config.py, but read-only and against
the LIVE configs/PDFs in S3 rather than local git files -- catches a spec
config's field_checks expected_pattern drifting out of sync with the actual
allowed value printed on the LAST PAGE of that spec's own catalog spec-sheet
PDF (specs/{code}_specsheet.pdf), even if nobody remembers to run the local
tool before/after an edit.

The source of truth is deliberately the catalog PDF, not a hand-made
"_allowed.png" summary image: 13A's InventoryCode config bug (found
2026-11-10) was real ("^RD$" when the PDF's actual allowed value is "PN"),
but a parallel-looking "bug" found in 9C turned out to be the hand-made
image itself having a transcription error ("PM" when the PDF -- both its
summary table AND its illustrative example label -- clearly says "P5");
checking against the image would have "fixed" working config into a new
bug. The PDF's last page is rendered to an image (PyMuPDF) and read with
the same vision-LLM approach used elsewhere in this project, rather than
parsed as PDF text, since the embedded text layer's extraction order
doesn't follow the table's visual layout. The prompt explicitly tells the
model to use ONLY the dark-blue/green-and-white allowed-values summary
table near the bottom of the page and to ignore any illustrative example
label graphic elsewhere on the page, which can show different sample
values (plausibly how 13A's original config bug happened, if whoever wrote
it read the mockup instead of the summary table).

Deliberately detection-only: writes every run's result to DynamoDB
(whatsapp-config-sanity-results) for the dashboard's history view, and
sends a WhatsApp alert to an admin number ONLY when a mismatch is found
(silent on a clean run). Never auto-fixes -- an unattended job silently
rewriting live validation rules off a vision-LLM transcription, with no
human reviewing the diff first, is exactly the kind of risk this system
has hit before with OCR/vision misreads on real labels (and exactly what
would have made the 9C false-positive above a real, deployed bug instead
of a caught-in-review mistake).
"""
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3
import pymupdf

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
BUCKET_NAME = "dole-pallet-specs-2026-677513501349-us-east-2-an"
RESULTS_TABLE = "whatsapp-config-sanity-results"

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-3.5-flash-lite"

# Matches a base spec's config key only -- variant configs (e.g.
# "specs/13A_config_punnet.json") have no catalog spec-sheet PDF of their own.
SPEC_CONFIG_RE = re.compile(r'^specs/([A-Za-z0-9]+)_config\.json$')

s3 = boto3.client('s3', region_name=REGION)
ssm = boto3.client('ssm', region_name=REGION)
dynamodb = boto3.client('dynamodb', region_name=REGION)


def get_ssm_param(name):
    res = ssm.get_parameter(Name=name, WithDecryption=True)
    return res['Parameter']['Value']


def call_gemini(image_bytes, prompt, api_key):
    b64_image = base64.b64encode(image_bytes).decode('utf-8')
    payload = {
        "contents": [
            {"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": b64_image}}
            ]}
        ],
        "generationConfig": {"temperature": 0}
    }
    data = json.dumps(payload).encode('utf-8')
    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    max_attempts = 3
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            logger.error(f"Gemini vision API error: Status {e.code} - {body}")
            if e.code >= 500 and attempt < max_attempts - 1:
                time.sleep(3)
                continue
            return None
        except Exception as e:
            logger.error(f"Gemini vision API call failed (attempt {attempt + 1}/{max_attempts}): {str(e)}")
            if attempt == max_attempts - 1:
                return None
            time.sleep(3)
    return None


def parse_json_response(text):
    cleaned = text.strip().strip('`')
    if cleaned.lower().startswith('json'):
        cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def find_base_specs():
    specs = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix='specs/'):
        for obj in page.get('Contents', []):
            m = SPEC_CONFIG_RE.match(obj['Key'])
            if m:
                specs.append(m.group(1))
    return sorted(specs)


def render_last_page(pdf_bytes, dpi=200):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[doc.page_count - 1]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()


def fetch_allowed_values(code, field_checks, api_key):
    """Returns a dict mapping each field_checks field name to the list of
    allowed candidate value(s) read off the last page of that spec's live
    specs/{code}_specsheet.pdf in S3, or None if that spec has no catalog
    PDF at all."""
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key=f"specs/{code}_specsheet.pdf")
        pdf_bytes = obj['Body'].read()
    except s3.exceptions.NoSuchKey:
        return None

    image_bytes = render_last_page(pdf_bytes)

    field_list = "\n".join(
        f'- "{fc["field"]}" (printed on the image under a heading meaning "{fc["name"]}")'
        for fc in field_checks
    )
    prompt = (
        "This image is the last page of a produce pallet label specification "
        "document. Near the bottom of the page there is a dark-colored "
        "summary table listing ALLOWED values (rows like \"Carton/Pack code\", "
        "\"Inventory code\", \"Carton trade mark code\", \"Berry size\", "
        "\"Target Region code\", \"Target Country code\", \"Target Market code\", "
        "\"Agent code\"). Use ONLY that summary table as your source -- ignore "
        "any example/illustrative label graphic shown elsewhere on the page, "
        "which may show different sample values that are NOT the authoritative "
        "allowed values.\n"
        "For each of the following fields, find its value in that summary "
        "table and transcribe it exactly as printed. If a field is not "
        "present in that table, map it to null -- do not guess or infer a "
        "value.\n"
        f"{field_list}\n"
        "Respond with ONLY a JSON object mapping each field name (exactly "
        "as given above) to its value or null, nothing else -- no "
        "commentary, no markdown fences."
    )

    values = None
    last_error = None
    for attempt in range(2):
        raw = call_gemini(image_bytes, prompt, api_key)
        if raw is None:
            last_error = "Gemini call failed"
            continue
        try:
            values = parse_json_response(raw)
            break
        except json.JSONDecodeError as e:
            last_error = str(e)
            continue
    if values is None:
        raise RuntimeError(f"Gemini returned no usable response for {code}: {last_error}")

    result = {}
    for fc in field_checks:
        value = values.get(fc['field'])
        if value is None or (isinstance(value, str) and not value.strip()):
            result[fc['field']] = None
            continue
        result[fc['field']] = [v.strip() for v in str(value).split('/') if v.strip()]
    return result


def check_spec(code, api_key):
    """Returns (mismatches, status) where status is 'no_pdf', 'no_checks',
    or 'ok'. mismatches is a list of dicts with field/name/value/pattern."""
    config_obj = s3.get_object(Bucket=BUCKET_NAME, Key=f"specs/{code}_config.json")
    config = json.loads(config_obj['Body'].read().decode('utf-8'))

    field_checks = config.get('field_checks', [])
    if not field_checks:
        return [], 'no_checks'

    allowed_values = fetch_allowed_values(code, field_checks, api_key)
    if allowed_values is None:
        return [], 'no_pdf'

    mismatches = []
    for fc in field_checks:
        field = fc['field']
        pattern = fc.get('expected_pattern')
        if not pattern:
            continue
        candidates = allowed_values.get(field)
        if not candidates:
            continue  # not shown in this spec's spec-sheet PDF -- skip

        bad_candidates = [v for v in candidates if not re.fullmatch(pattern, v)]
        if bad_candidates:
            shown_value = candidates[0] if len(candidates) == 1 else f"{'/'.join(candidates)} (failing part(s): {', '.join(bad_candidates)})"
            mismatches.append({
                'field': field,
                'name': fc['name'],
                'value': shown_value,
                'pattern': pattern,
            })

    return mismatches, 'ok'


def send_whatsapp_alert(mismatches_by_spec):
    access_token = get_ssm_param('/whatsapp/access_token')
    phone_number_id = get_ssm_param('/whatsapp/phone_number_id')
    admin_number = get_ssm_param('/whatsapp/admin_alert_number')

    lines = ["*Config Sanity Check: mismatch(es) found*", ""]
    for code, mismatches in mismatches_by_spec.items():
        lines.append(f"*{code}*:")
        for m in mismatches:
            lines.append(f"  - {m['name']} ({m['field']}): spec-sheet PDF shows {m['value']!r}, config expects {m['pattern']!r}")
        lines.append("")
    lines.append("Run tools/check_allowed_vs_config.py --fix locally, review the diff, then re-upload to S3.")
    text = "\n".join(lines)

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": admin_number,
        "type": "text",
        "text": {"body": text}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        logger.error(f"Failed to send WhatsApp sanity-check alert: Status {e.code} - {e.read().decode('utf-8')}")
    except Exception as e:
        logger.error(f"Failed to send WhatsApp sanity-check alert: {str(e)}")


def lambda_handler(event, context):
    triggered_by = (event or {}).get('trigger', 'schedule')

    api_key = get_ssm_param('/whatsapp/gemini_api_key')
    specs = find_base_specs()

    mismatches_by_spec = {}
    no_pdf, no_checks, errored = [], [], []

    for code in specs:
        try:
            mismatches, status = check_spec(code, api_key)
        except Exception as e:
            logger.error(f"Error checking {code}: {str(e)}")
            errored.append(code)
            continue

        if status == 'no_pdf':
            no_pdf.append(code)
        elif status == 'no_checks':
            no_checks.append(code)
        elif mismatches:
            mismatches_by_spec[code] = mismatches

        time.sleep(1)  # stay polite to Gemini's free-tier rate limits

    run_id = datetime.now(timezone.utc).isoformat()
    status = 'MISMATCHES_FOUND' if mismatches_by_spec else 'CLEAN'

    dynamodb.put_item(
        TableName=RESULTS_TABLE,
        Item={
            'pk': {'S': 'RUN'},
            'run_id': {'S': run_id},
            'triggered_by': {'S': triggered_by},
            'status': {'S': status},
            'specs_checked': {'N': str(len(specs))},
            'mismatches': {'S': json.dumps(mismatches_by_spec)},
            'no_pdf_specs': {'S': json.dumps(no_pdf)},
            'no_checks_specs': {'S': json.dumps(no_checks)},
            'errored_specs': {'S': json.dumps(errored)},
        }
    )

    if mismatches_by_spec:
        send_whatsapp_alert(mismatches_by_spec)

    return {
        'statusCode': 200,
        'body': json.dumps({'run_id': run_id, 'status': status, 'mismatches': mismatches_by_spec})
    }
