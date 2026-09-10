import base64
import difflib
import hashlib
import hmac
import itertools
import json
import logging
import math
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import cv2
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get('WHATSAPP_AWS_REGION', 'us-east-2')
BUCKET_NAME = os.environ.get('SPEC_BUCKET_NAME', 'dole-pallet-specs-2026-677513501349-us-east-2-an')
DEDUP_TABLE_NAME = os.environ.get('DEDUP_TABLE_NAME', 'whatsapp-processed-messages')
DEDUP_TTL_SECONDS = 24 * 60 * 60
AUDIT_TABLE_NAME = os.environ.get('AUDIT_TABLE_NAME', 'whatsapp-audit-log')
SPEC_CATALOG_TABLE_NAME = os.environ.get('SPEC_CATALOG_TABLE_NAME', 'whatsapp-spec-catalog')

SPEC_CODE_PATTERN = re.compile(r'^[A-Z0-9_-]{1,32}$')
REFERENCE_MATCH_THRESHOLD = 0.6
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-3.5-flash-lite"

# Known secondary label types a spec can opt into (e.g. a punnet/retail-pack
# label printed alongside the carton label for the same shipment). A spec
# opts in purely by uploading specs/{spec_code}_config_{suffix}.json and
# specs/{spec_code}_label_{suffix}.png -- a spec with neither file behaves
# exactly as before.
LABEL_VARIANT_SUFFIXES = ["punnet", "punnet_mix", "2", "3", "4", "5", "pallet"]

ssm = boto3.client('ssm', region_name=AWS_REGION)
s3_client = boto3.client('s3', region_name=AWS_REGION)
rekognition = boto3.client('rekognition', region_name=AWS_REGION)
textract = boto3.client('textract', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)


def get_ssm_param(param_name):
    try:
        res = ssm.get_parameter(Name=param_name, WithDecryption=True)
        return res['Parameter']['Value']
    except Exception as e:
        logger.error(f"Failed to fetch SSM param {param_name}: {str(e)}")
        return None


def verify_webhook_signature(event, raw_body_bytes, app_secret):
    headers = event.get('headers') or {}
    signature_header = None
    for key, value in headers.items():
        if key.lower() == 'x-hub-signature-256':
            signature_header = value
            break

    if not signature_header:
        logger.error("Missing X-Hub-Signature-256 header on webhook POST")
        return False

    expected_signature = 'sha256=' + hmac.new(
        app_secret.encode('utf-8'), raw_body_bytes, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


def mark_message_processed(message_id):
    """
    Returns True if this message should be processed (first time seen or dedup
    store unavailable), False if it's a duplicate delivery that should be skipped.
    Fails open on DynamoDB errors so a dedup-store outage doesn't drop messages.
    """
    table = dynamodb.Table(DEDUP_TABLE_NAME)
    try:
        table.put_item(
            Item={
                'message_id': message_id,
                'expires_at': int(time.time()) + DEDUP_TTL_SECONDS
            },
            ConditionExpression='attribute_not_exists(message_id)'
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False
        logger.error(f"Dedup check failed for message {message_id}: {str(e)}")
        return True


def write_audit_record(sender, message_id, msg_type, spec_code, result, detail="", image_key=None, content_key=None, sender_name=None):
    """
    Best-effort audit log of every processed request for compliance/reporting.
    Never raises - a logging failure must not block the WhatsApp reply.
    """
    table = dynamodb.Table(AUDIT_TABLE_NAME)
    item = {
        'sender': sender or 'unknown',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'record_type': 'REQUEST',
        'message_id': message_id or '',
        'msg_type': msg_type or '',
        'spec_code': spec_code or '',
        'result': result,
        'detail': (detail or '')[:1000],
    }
    if image_key:
        item['image_key'] = image_key
    if content_key:
        item['content_key'] = content_key
    if sender_name:
        item['sender_name'] = sender_name
    try:
        table.put_item(Item=item)
    except Exception as e:
        logger.error(f"Failed to write audit record for message {message_id}: {str(e)}")


WELCOME_MESSAGE = (
    "👋 Welcome to Dole's *Label Verification* service!\n\n"
    "• Send a *Spec Code* (e.g. *9A*) to receive the reference spec sheet and label example.\n"
    "• Send a *photo of a label* with the Spec Code as the caption to verify it against the spec.\n"
    "• If Spec not available or uploaded validation failed, please contact your *Area Manager* for manual validation."
)


def is_first_time_sender(sender):
    """
    Checks the audit log for any prior record from this sender. Fails closed
    (treats as not-first-time) on error, so a lookup failure never spams a
    repeat user with the welcome message.
    """
    if not sender:
        return False
    table = dynamodb.Table(AUDIT_TABLE_NAME)
    try:
        response = table.query(
            KeyConditionExpression=Key('sender').eq(sender),
            Limit=1
        )
        return len(response.get('Items', [])) == 0
    except Exception as e:
        logger.error(f"Failed to check first-time sender status for {sender}: {str(e)}")
        return False


def send_whatsapp_message(recipient_id, text_body, access_token, phone_number_id):
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "text",
        "text": {"body": text_body}
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        logger.error(f"META REJECTED TEXT: Status {e.code} - Response: {e.read().decode('utf-8')}")
        return None


def send_whatsapp_image_bytes(recipient_id, image_bytes, mime_type, caption, access_token, phone_number_id):
    upload_url = f"https://graph.facebook.com/v21.0/{phone_number_id}/media"
    boundary = "BoundaryString334455"

    timestamp = int(time.time())
    filename = f"spec_image_{timestamp}.png" if "png" in mime_type else f"spec_image_{timestamp}.jpg"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="messaging_product"\r\n\r\n'
        f"whatsapp\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode('utf-8') + image_bytes + f"\r\n--{boundary}--\r\n".encode('utf-8')

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }

    try:
        req_upload = urllib.request.Request(upload_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req_upload) as resp:
            response_data = json.loads(resp.read().decode('utf-8'))
            media_id = response_data.get('id')
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
        logger.error(f"META MEDIA UPLOAD FAILED ({caption}): Status {e.code} - Response: {err_body}")
        return None
    except Exception as e:
        logger.error(f"META MEDIA UPLOAD EXCEPTION ({caption}): {str(e)}")
        return None

    if not media_id:
        logger.error(f"Failed to obtain media ID from Meta for {caption}")
        return None

    msg_url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    msg_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": caption
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req_msg = urllib.request.Request(msg_url, data=data, headers=msg_headers, method='POST')
    try:
        with urllib.request.urlopen(req_msg) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        logger.error(f"META REJECTED IMAGE MESSAGE ({caption}): Status {e.code} - Response: {e.read().decode('utf-8')}")
        return None


def send_whatsapp_document_bytes(recipient_id, doc_bytes, filename, caption, access_token, phone_number_id):
    upload_url = f"https://graph.facebook.com/v21.0/{phone_number_id}/media"
    boundary = "BoundaryString334455"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="messaging_product"\r\n\r\n'
        f"whatsapp\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode('utf-8') + doc_bytes + f"\r\n--{boundary}--\r\n".encode('utf-8')

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }

    try:
        req_upload = urllib.request.Request(upload_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req_upload) as resp:
            response_data = json.loads(resp.read().decode('utf-8'))
            media_id = response_data.get('id')
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
        logger.error(f"META DOCUMENT UPLOAD FAILED ({caption}): Status {e.code} - Response: {err_body}")
        return None
    except Exception as e:
        logger.error(f"META DOCUMENT UPLOAD EXCEPTION ({caption}): {str(e)}")
        return None

    if not media_id:
        logger.error(f"Failed to obtain media ID from Meta for document {caption}")
        return None

    msg_url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    msg_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "document",
        "document": {
            "id": media_id,
            "caption": caption,
            "filename": filename
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req_msg = urllib.request.Request(msg_url, data=data, headers=msg_headers, method='POST')
    try:
        with urllib.request.urlopen(req_msg) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        logger.error(f"META REJECTED DOCUMENT MESSAGE ({caption}): Status {e.code} - Response: {e.read().decode('utf-8')}")
        return None


def get_spec_pdf(spec_code):
    """Looks up the cached spec-sheet PDF for a spec code in the spec catalog table."""
    table = dynamodb.Table(SPEC_CATALOG_TABLE_NAME)
    try:
        res = table.get_item(Key={'spec_code': spec_code})
        item = res.get('Item')
        if not item or not item.get('pdf_s3_key'):
            return None
        return item['pdf_s3_key']
    except ClientError as e:
        logger.error(f"Spec catalog lookup failed for '{spec_code}': {str(e)}")
        return None


def get_spec_images(spec_code):
    allowed_spec_key = f"specs/{spec_code}_allowed.png"
    label_example_key = f"specs/{spec_code}_label.png"
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=allowed_spec_key)
        s3_client.head_object(Bucket=BUCKET_NAME, Key=label_example_key)
        return {
            "allowed_key": allowed_spec_key,
            "label_key": label_example_key
        }
    except ClientError as e:
        logger.info(f"Spec '{spec_code}' images not found in S3: {str(e)}")
        return None


def download_whatsapp_media(media_id, access_token):
    meta_url = f"https://graph.facebook.com/v21.0/{media_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    req_meta = urllib.request.Request(meta_url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req_meta) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            download_url = res_data.get('url')
            mime_type = res_data.get('mime_type', 'image/jpeg')
    except urllib.error.HTTPError as e:
        logger.error(f"Failed to fetch media URL for ID {media_id}: Status {e.code}")
        return None, None

    if not download_url:
        return None, None

    req_download = urllib.request.Request(download_url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req_download) as response:
            return response.read(), mime_type
    except urllib.error.HTTPError as e:
        logger.error(f"Failed to download media bytes for ID {media_id}: Status {e.code}")
        return None, None


def correct_text_rotation(image_bytes, min_lines=3, min_angle_degrees=3.0):
    """
    Detects the dominant angle of the label's text lines via Rekognition and
    rotates the whole photo so text runs horizontal, before ORB alignment.
    Corrects sideways/tilted captures (e.g. phone held in portrait against
    a landscape label) that can't be counted on to be fixed by
    align_zone_locally, since that step's ORB feature matching may not
    find enough correspondences to compute a homography on a badly
    misoriented photo.
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes

        h, w, _ = img.shape
        response = rekognition.detect_text(Image={'Bytes': image_bytes})
        lines = [d for d in response.get('TextDetections', []) if d.get('Type') == 'LINE']
        if len(lines) < min_lines:
            return image_bytes

        sin_sum, cos_sum, count = 0.0, 0.0, 0
        for line in lines:
            polygon = line.get('Geometry', {}).get('Polygon', [])
            if len(polygon) < 2:
                continue
            dx = (polygon[1]['X'] - polygon[0]['X']) * w
            dy = (polygon[1]['Y'] - polygon[0]['Y']) * h
            if dx == 0 and dy == 0:
                continue
            angle = math.atan2(dy, dx)
            sin_sum += math.sin(angle)
            cos_sum += math.cos(angle)
            count += 1

        if count == 0:
            return image_bytes

        # Circular mean (not a plain average) so angles near the +/-180 wraparound don't cancel out.
        angle_degrees = math.degrees(math.atan2(sin_sum, cos_sum))
        if abs(angle_degrees) < min_angle_degrees:
            return image_bytes

        center = (w / 2, h / 2)
        matrix = cv2.getRotationMatrix2D(center, angle_degrees, 1.0)

        cos_a = abs(matrix[0, 0])
        sin_a = abs(matrix[0, 1])
        new_w = int((h * sin_a) + (w * cos_a))
        new_h = int((h * cos_a) + (w * sin_a))
        matrix[0, 2] += (new_w / 2) - center[0]
        matrix[1, 2] += (new_h / 2) - center[1]

        rotated = cv2.warpAffine(img, matrix, (new_w, new_h), borderMode=cv2.BORDER_REPLICATE)
        success, encoded = cv2.imencode('.jpg', rotated)
        if success:
            logger.info(f"Corrected label rotation by {angle_degrees:.1f} degrees")
            return encoded.tobytes()

    except Exception as e:
        logger.warning(f"Text rotation correction skipped: {str(e)}")

    return image_bytes


def crop_to_label_boundary(image_bytes, margin=0.05, min_area_fraction=0.15):
    """
    Crops to the physical label's boundary, found via a plain brightness
    threshold + bounding box of the resulting foreground pixels -- not a
    text-detection API. A prior version used Rekognition's DetectText to
    find the label's text bounding box, but that silently truncated a
    dense label (DetectText hard-caps at 100 words per image, and this
    label's two 23-language translation blocks alone can exceed that) no
    matter how generous the margin was, since no margin can recover content
    that was never in the detection results to begin with. Thresholding on
    brightness has no equivalent hidden limit to hit.

    Uses the bounding box of ALL foreground pixels rather than the largest
    single contour, so a label visually split into multiple regions by an
    uneven shadow (or a lifted/torn corner) doesn't lose part of itself to
    a connectivity requirement -- worst case that makes the crop less tight,
    never smaller than the real label. Falls back to the unmodified image
    if the detected region doesn't plausibly look like the label at all
    (too small a fraction of the frame), same reasoning as the DetectText
    version: a failed detection returning less than the real label is far
    worse than leaving extra background in frame.
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes

        h, w, _ = img.shape
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Printed labels are on bright white/light stock; Otsu's threshold
        # separates that from most backgrounds (wood, fabric, hands, table)
        # without needing per-photo brightness tuning.
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        foreground_pixels = cv2.findNonZero(thresh)
        if foreground_pixels is None:
            return image_bytes

        x, y, box_w, box_h = cv2.boundingRect(foreground_pixels)
        if (box_w * box_h) / (w * h) < min_area_fraction:
            logger.warning("crop_to_label_boundary: detected region too small to plausibly be the label; skipping crop")
            return image_bytes

        pad_x = int(box_w * margin)
        pad_y = int(box_h * margin)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(w, x + box_w + pad_x)
        y2 = min(h, y + box_h + pad_y)

        cropped = img[y1:y2, x1:x2]
        if cropped.size == 0:
            return image_bytes

        success, encoded = cv2.imencode('.jpg', cropped)
        return encoded.tobytes() if success else image_bytes
    except Exception as e:
        logger.warning(f"crop_to_label_boundary fallback triggered: {str(e)}")
        return image_bytes


def extract_label_automatically(uploaded_bytes):
    """
    Rotates the label upright (see correct_text_rotation), then crops to
    its physical boundary (see crop_to_label_boundary) to trim background
    clutter before alignment.
    """
    rotated = correct_text_rotation(uploaded_bytes)
    return crop_to_label_boundary(rotated)


def build_orb_matcher(ref_image_bytes, user_image_bytes):
    """
    Runs ORB feature detection/matching between the reference label and the
    user's photo once, and fits a whole-image homography to use as a
    fallback for zones that don't have enough of their own nearby matches
    (see align_zone_locally). Keeping this as a single shared pass means ORB
    runs once per photo rather than once per zone.

    A thin-plate-spline whole-image warp was tried in place of this (to
    handle a physically lifted/torn/wrinkled label without assuming a flat
    plane), but real photos here only yield ~100-150 usable ORB matches --
    far too sparse to constrain a flexible non-rigid warp across the whole
    label, even after fixing several real bugs in that implementation. It
    kept landing worse (2-3/8 zones passing) than this per-zone local-fit
    approach (6/8), so it was reverted. Splitting into fixed regions
    (whole-image, then top/bottom halves) had already been tried and also
    failed, because a rectangular split still lumps well-behaved points
    together with points near the damage. Per-zone local fitting (see
    align_zone_locally) sidesteps that by not assuming any particular
    macro-shape for where the label deviates from a flat plane.
    """
    try:
        ref_arr = np.frombuffer(ref_image_bytes, np.uint8)
        ref_img = cv2.imdecode(ref_arr, cv2.IMREAD_COLOR)

        user_arr = np.frombuffer(user_image_bytes, np.uint8)
        user_img = cv2.imdecode(user_arr, cv2.IMREAD_COLOR)

        if ref_img is None or user_img is None:
            return None

        gray_ref = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
        gray_user = cv2.cvtColor(user_img, cv2.COLOR_BGR2GRAY)

        orb = cv2.ORB_create(5000)
        kp1, des1 = orb.detectAndCompute(gray_ref, None)
        kp2, des2 = orb.detectAndCompute(gray_user, None)

        if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
            logger.warning("build_orb_matcher: insufficient keypoints for alignment")
            return {"ref_img": ref_img, "user_img": user_img, "kp1": [], "kp2": [], "matches": [], "global_matrix": None}

        # Lowe's ratio test instead of plain crossCheck nearest-neighbor matching:
        # repetitive printed patterns (a barcode's parallel bars, a long digit
        # string like a GGN with repeated digit glyphs) give ORB many
        # look-alike candidates, and crossCheck alone can't tell the one true
        # match from a look-alike -- both sides can agree on a wrong match if
        # the pattern repeats symmetrically. The ratio test rejects a match
        # unless it's clearly better than the next-best alternative for that
        # keypoint, which is exactly the ambiguity repetitive patterns create.
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        knn_matches = bf.knnMatch(des1, des2, k=2)
        matches = [pair[0] for pair in knn_matches if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance]

        global_matrix = None
        if len(matches) >= 4:
            ranked = sorted(matches, key=lambda x: x.distance)
            good = ranked[:max(40, int(len(ranked) * 0.15))]
            src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            global_matrix, _ = cv2.findHomography(dst, src, cv2.RANSAC, 5.0)

        logger.info(
            f"build_orb_matcher: keypoints ref={len(kp1)} user={len(kp2)} matches={len(matches)} "
            f"global_fit={'ok' if global_matrix is not None else 'failed'}"
        )

        return {
            "ref_img": ref_img,
            "user_img": user_img,
            "kp1": kp1,
            "kp2": kp2,
            "matches": matches,
            "global_matrix": global_matrix,
        }
    except Exception as e:
        logger.error(f"build_orb_matcher failed: {str(e)}")
        return None


def _expand_box(box, pad):
    """Expands a Left/Top/Width/Height fractional box by pad on every side, clamped to [0, 1]."""
    left = max(0.0, box.get("Left", 0) - pad)
    top = max(0.0, box.get("Top", 0) - pad)
    right = min(1.0, box.get("Left", 0) + box.get("Width", 1) + pad)
    bottom = min(1.0, box.get("Top", 0) + box.get("Height", 1) + pad)
    return {"Left": left, "Top": top, "Width": right - left, "Height": bottom - top}


def align_zone_locally(matcher, box, zone_name="", upscale=2, use_vision_llm=False):
    """
    Returns a list of (crop_bytes, crop_box) candidates for this zone, most
    trustworthy first, so the caller can fall through to the next one if
    OCR finds nothing in the first. crop_box is the actual fractional
    region each crop was drawn from (which may be padded well beyond the
    nominal box), so callers doing a reference comparison can crop the
    same region on the reference side instead of assuming the nominal box.
    Fit from ORB matches found only in a
    padded window around the zone -- not the whole label or a fixed half of
    it -- so it only requires that one neighborhood be locally flat, making
    it robust to damage (a lifted corner, a wrinkle) located anywhere else
    on the label. Falls back to the whole-image homography from
    build_orb_matcher, and finally to an unwarped crop, if the zone doesn't
    have enough of its own matches.

    The search window starts tight and only widens if there aren't enough
    matches yet, rather than using one fixed padding for every zone: a
    small pad is plenty (and most accurate) for content-dense zones, but a
    fixed generous pad applied to an already-large zone pulls in unrelated
    neighboring content and dilutes the fit, while that same pad can still
    be too tight for a small, sparsely-textured zone. Widening only as
    needed lets each zone find its own right-sized neighborhood.
    """
    ref_img = matcher["ref_img"]
    user_img = matcher["user_img"]
    kp1 = matcher["kp1"]
    kp2 = matcher["kp2"]

    ref_h, ref_w, _ = ref_img.shape
    left = box.get("Left", 0)
    top = box.get("Top", 0)
    width = box.get("Width", 1)
    height = box.get("Height", 1)

    MIN_TARGET_MATCHES = 20
    local_matches = []
    pad_used = None
    for pad in (0.03, 0.06, 0.12, 0.20, 0.30):
        x1 = max(0.0, left - pad)
        y1 = max(0.0, top - pad)
        x2 = min(1.0, left + width + pad)
        y2 = min(1.0, top + height + pad)
        px1, py1, px2, py2 = x1 * ref_w, y1 * ref_h, x2 * ref_w, y2 * ref_h

        local_matches = [
            m for m in matcher["matches"]
            if px1 <= kp1[m.queryIdx].pt[0] <= px2 and py1 <= kp1[m.queryIdx].pt[1] <= py2
        ] if kp1 else []
        pad_used = pad

        if len(local_matches) >= MIN_TARGET_MATCHES:
            break

    # A homography fit from only a handful of inliers is barely constrained --
    # its accuracy degrades fast outside the small cluster of points that
    # anchored it. Trust a local fit only once it clears a real confidence
    # bar; below that, prefer the whole-image global homography, which is
    # less locally precise but doesn't blow up the way an under-constrained
    # local fit does when the crop is widened.
    LOCAL_TRUST_THRESHOLD = 12

    matrix = matcher["global_matrix"]
    fit_source = "global" if matrix is not None else "none"

    if len(local_matches) >= 4:
        src = np.float32([kp1[m.queryIdx].pt for m in local_matches]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in local_matches]).reshape(-1, 1, 2)
        local_matrix, mask = cv2.findHomography(dst, src, cv2.RANSAC, 5.0)
        inliers = int(mask.ravel().sum()) if mask is not None else 0
        if local_matrix is not None and inliers >= LOCAL_TRUST_THRESHOLD:
            matrix = local_matrix
            fit_source = f"local (pad={pad_used} matches={len(local_matches)} inliers={inliers})"

    # Only widen the crop for whichever transform we actually decided to
    # trust (local-and-confident gets a tight crop; global-fallback gets a
    # wider margin) -- never widen a crop drawn from a rejected, shaky local
    # fit, since that just exposes more of the region the fit was never
    # valid for.
    crop_pad = 0.0 if fit_source.startswith("local") else (0.20 if matrix is not None else 0.22)

    logger.info(f"align_zone_locally: zone='{zone_name}' fit={fit_source} crop_pad={crop_pad}")

    crop_box = _expand_box(box, crop_pad) if crop_pad else box

    candidates = []
    if matrix is not None:
        warped = cv2.warpPerspective(user_img, matrix, (ref_w, ref_h))
        crop = crop_zone_png(warped, crop_box, upscale)
        if crop:
            candidates.append((crop, crop_box))

    # A whole-image homography can be inaccurate deep into a region far from
    # whatever matches anchored it -- not just mispositioned but genuinely
    # distorted, so widening the crop just shows more of the same bad warp.
    # When we're not using a confident local fit, offer a second candidate
    # cropped straight from the unwarped, rotation-corrected photo (no
    # perspective correction at all), which can beat a poorly-extrapolated
    # warp for a photo that isn't drastically tilted to begin with. The
    # caller tries candidates in order and stops at the first that yields
    # any detected text.
    #
    # Vision-LLM zones get this raw candidate even when the local fit *is*
    # trusted: a local homography can clear the inlier-count bar yet still
    # introduce visible skew/rotation into the crop (small angular errors in
    # a handful of matched points translate into a noticeably tilted warp),
    # and dense multi-language transcription is far more sensitive to that
    # skew than Rekognition/Textract's line detection is -- a tilted crop
    # measurably undercounts translations even though every entry is still
    # present in it. The already rotation-corrected extraction is usually
    # cleaner for this than a re-warped crop, so it's worth trying too.
    if not fit_source.startswith("local") or use_vision_llm:
        raw_crop = crop_zone_png(user_img, crop_box, upscale)
        if raw_crop:
            candidates.append((raw_crop, crop_box))

    if not candidates:
        fallback = crop_zone_png(user_img, box, upscale)
        if fallback:
            candidates.append((fallback, box))

    return candidates


def normalize_zone_text(lines):
    return re.sub(r'\s+', ' ', " ".join(lines).strip().upper())


ZONE_TOKEN_SPLIT_PATTERN = re.compile(r'\s*/\s*|\s+-\s+')


def split_zone_tokens(lines):
    """
    Splits a zone's OCR'd text into individual entries (e.g. one per language
    translation), on '/' or on '-' that has surrounding whitespace. The
    whitespace requirement on '-' avoids breaking compound words that
    legitimately contain a hyphen (e.g. "Zuid-Afrika"), since printed
    translation lists consistently pad their separators with spaces while
    compound words don't.
    """
    text = normalize_zone_text(lines)
    return [t.strip() for t in ZONE_TOKEN_SPLIT_PATTERN.split(text) if t.strip()]


def content_matches(expected_value, actual_text, fuzzy_threshold=None):
    """
    Compares a canonical reference value against extracted/zone text. A
    short exact code (GGN, LidlProductNumber) is checked by substring --
    fine even against text that's picked up extra neighboring content,
    since the code either appears verbatim or it doesn't. A long free-text
    block (23 language translations) needs fuzzy_threshold instead: the
    source text can carry bleed-over as a trailing suffix, which dilutes a
    whole-string similarity ratio even when the real content is a perfect
    match, so the ratio is computed only against the correspondingly-sized
    leading portion. And unlike a short code, even a highly-accurate vision
    LLM occasionally drops a diacritic on one word in a long block -- real
    content, not a defect -- so this needs a similarity threshold rather
    than exact/substring equality.
    """
    if not expected_value:
        return True
    if fuzzy_threshold:
        comparison_text = actual_text[:len(expected_value)]
        similarity = difflib.SequenceMatcher(None, expected_value, comparison_text).ratio()
        return similarity >= fuzzy_threshold
    return expected_value in actual_text


def content_matches_with_retry(expected_value, fuzzy_threshold, get_actual_text, refresh):
    """
    A vision-LLM transcription of a long, dense multi-script block (e.g. a
    23-language translation list) has enough call-to-call variance that an
    otherwise-correct label can occasionally dip just under a fuzzy
    threshold on a single unlucky call -- confirmed directly: re-running
    extraction on the same photo that failed live scored 0.96-0.998 on the
    next four attempts. Only worth retrying when fuzzy_threshold is actually
    in play: an exact/substring check failing means the content is
    genuinely wrong, not noisy, and retrying would just mask a real defect.
    get_actual_text/refresh are callables so this can serve both a
    text_blocks-sourced comparison and an extracted_fields-sourced one.
    """
    actual_text = get_actual_text()
    if content_matches(expected_value, actual_text, fuzzy_threshold):
        return True, actual_text
    if not fuzzy_threshold:
        return False, actual_text
    refresh()
    actual_text = get_actual_text()
    return content_matches(expected_value, actual_text, fuzzy_threshold), actual_text


DIGIT_CONFUSION_PAIRS = (('5', '6'),)


def generate_digit_confusion_variants(value, pairs=DIGIT_CONFUSION_PAIRS):
    """
    A short printed code (PUC, GGN) needs an exact reference-table match --
    unlike a dense text block, there's no fuzzy_threshold to smooth over
    vision-LLM noise here. But a specific, narrow kind of noise is worth
    compensating for anyway: on a low-resolution photo, a printed '5' and
    '6' can look genuinely identical (confirmed on a real live label: PUC
    "D0415"/GGN "...485296" both came back with a 6 where the label prints
    a 5). Yields every combination of swapping each ambiguous digit in the
    value, original first (so the common, already-correct case costs
    nothing extra) -- e.g. "D0416" yields "D0416" then "D0415" (one
    ambiguous digit); a value with N ambiguous digits yields 2**N variants,
    trivial at this code length. Only ever tried as a fallback after the
    literal extracted value has already failed to match/resolve, so this
    never masks a genuinely different, non-digit-confusion error.
    """
    swap = {ch: other for pair in pairs for ch, other in (pair, pair[::-1])}
    ambiguous_positions = [i for i, ch in enumerate(value) if ch in swap]
    if not ambiguous_positions:
        yield value
        return
    seen = set()
    for bitmask in range(2 ** len(ambiguous_positions)):
        chars = list(value)
        for bit_i, pos in enumerate(ambiguous_positions):
            if (bitmask >> bit_i) & 1:
                chars[pos] = swap[chars[pos]]
        variant = ''.join(chars)
        if variant not in seen:
            seen.add(variant)
            yield variant


# Rule 9's Size Prompt: the heading text that must precede the size value,
# fixed per Commodity Code -- a stable, universal Dole business rule, not
# per-shipment data, so it's kept as code rather than table-driven per the
# 2026-09-03 decision. Commodity codes not covered by name fall to "Size:".
# Corrected 2026-09-10 against the user's authoritative list after a real
# 17A (Malaysia) orange label printed "Size Ref/Count:", not the previously
# assumed "Size Ref/Size:" -- also fixed "BB" (not a real commodity code in
# whatsapp-commodity) to "BI" (Blueberries) and added Strawberries (SB).
# Each commodity maps to a list since some accept more than one valid
# wording (e.g. Avocados' "Size Code:" or "Code:") -- a check passes if ANY
# listed wording is found.
#
# Known deferred exception, not implemented: Oranges/Grapefruit/Lemons
# should use "Size Ref" + "Diameter" instead when the pack is a bin AND
# class is P -- expected_size_prompt() only receives Commodity today, not
# pack type or class, so this conditional isn't wired up. Revisit if a real
# bin/class-P label is seen.
SIZE_PROMPT_BY_COMMODITY = {
    "OR": ["Size Ref/Count:"], "GF": ["Size Ref/Count:"], "LE": ["Size Ref/Count:"],
    "SC": ["Size Ref:"],
    "GR": ["Berry Size:"],
    "PL": ["Size/Diameter:"],
    "NE": ["Count/Size/Diameter:"], "PE": ["Count/Size/Diameter:"],
    "AC": ["Size/Diameter:"],
    "BI": ["Size:"], "SB": ["Size:"],
    "AV": ["Size Code:", "Code:"],
}


def expected_size_prompt(commodity):
    return SIZE_PROMPT_BY_COMMODITY.get((commodity or "").strip().upper(), ["Size:"])


def get_compare_text(source_name, zone_content, extracted_fields, text_blocks):
    """
    Resolves a lookup_rules "compare_zone" name against whichever source
    actually has it: a legacy pixel-zone crop (zone_content, a list of OCR
    lines -- still how 8B and 9A's remaining zone-based checks work), a
    layout-agnostic extracted field (a single value), or an extracted
    free-text block (a single verbatim string). This is what lets the same
    lookup_rules config work unchanged whether "Grower GGN" or "Variety
    Group languages" comes from a calibrated box or from Gemini finding it
    by content on a label with a different physical layout.
    """
    if source_name in zone_content:
        return normalize_zone_text(zone_content[source_name])
    if source_name in text_blocks:
        return normalize_zone_text([str(text_blocks[source_name])])
    if source_name in extracted_fields:
        return normalize_zone_text([str(extracted_fields[source_name])])
    return ""


def zone_matches_reference(ref_img, box, user_lines):
    """
    For static zones (e.g. a fixed multi-language origin block), compares the
    user's OCR'd text against the same zone cropped from the spec's
    reference label, instead of a hand-typed pattern. Uses a fuzzy ratio to
    tolerate normal OCR noise between two separately-photographed/scanned
    images.
    """
    try:
        crop_bytes = crop_zone_png(ref_img, box)
        if not crop_bytes:
            logger.warning("compare_to_reference zone crop failed; skipping strict check")
            return True

        ref_analysis = rekognition.detect_text(Image={'Bytes': crop_bytes})
        ref_lines = [b['DetectedText'].strip() for b in ref_analysis.get('TextDetections', []) if b['Type'] == 'LINE']
        if not ref_lines:
            logger.warning("compare_to_reference zone has no detectable text on reference label; skipping strict check")
            return True

        ref_text = normalize_zone_text(ref_lines)
        user_text = normalize_zone_text(user_lines)
        similarity = difflib.SequenceMatcher(None, ref_text, user_text).ratio()
        logger.info(
            f"compare_to_reference similarity={similarity:.3f} "
            f"ref_text='{ref_text[:400]}' user_text='{user_text[:400]}'"
        )
        return similarity >= REFERENCE_MATCH_THRESHOLD
    except Exception as e:
        logger.error(f"Reference zone comparison failed: {str(e)}")
        return True


def crop_zone_png(img, box, upscale=2):
    """
    Crops just the zone out of an already-decoded image (box is
    Left/Top/Width/Height as fractions of the full image) and optionally
    upscales it, returning PNG bytes. Takes a decoded array rather than
    bytes since callers (validate_label_layout, zone_matches_reference) each
    already have the relevant image decoded and may crop it many times
    (once per zone) -- re-decoding per crop would be wasted work.
    """
    if img is None:
        return None

    h, w, _ = img.shape
    x1 = max(0, int(box.get("Left", 0) * w))
    y1 = max(0, int(box.get("Top", 0) * h))
    x2 = min(w, int((box.get("Left", 0) + box.get("Width", 1)) * w))
    y2 = min(h, int((box.get("Top", 0) + box.get("Height", 1)) * h))

    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    if upscale and upscale > 1:
        crop = cv2.resize(crop, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)

    success, encoded = cv2.imencode('.png', crop)
    return encoded.tobytes() if success else None


def detect_zone_lines_textract(crop_bytes):
    """
    Runs Amazon Textract's DetectDocumentText on an already-aligned,
    already-cropped zone image (see align_zone_locally). Textract is built
    for dense/fine-print document text and catches small multi-line blocks
    that Rekognition's photo-oriented OCR tends to miss.
    """
    try:
        if not crop_bytes:
            return []

        response = textract.detect_document_text(Document={'Bytes': crop_bytes})
        return [b['Text'].strip() for b in response.get('Blocks', []) if b.get('BlockType') == 'LINE']
    except Exception as e:
        logger.error(f"Textract zone detection failed: {str(e)}")
        return []


VISION_ZONE_PROMPT = (
    "Transcribe all text visible in this image exactly as printed, preserving "
    "every separator character (such as '/' or '-'), punctuation, diacritics, "
    "and special characters precisely as shown — do not paraphrase, translate, "
    "summarize, or add/remove any separators. Respond with ONLY the "
    "transcribed text, nothing else — no commentary, no markdown, no quotes "
    "around it."
)


def _call_gemini(payload, api_key):
    data = json.dumps(payload).encode('utf-8')
    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Google AI Studio's free tier has no latency SLA -- a timeout or a 5xx
    # (e.g. 503 "model currently experiencing high demand") is usually
    # transient server-side overload, not a real failure, so retry with a
    # short backoff before giving up. Observed live: an immediate retry with
    # no delay at all still hit 503 both times during a real overload spike,
    # so a brief pause between attempts matters, not just the retry itself.
    # A 4xx HTTPError means the API rejected the request itself (bad auth,
    # bad payload) -- a retry can't fix that and would just double the wait
    # for the same outcome, so those fail immediately, no backoff.
    max_attempts = 3
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as e:
            logger.error(f"Gemini vision API error: Status {e.code} - {e.read().decode('utf-8')}")
            if e.code >= 500 and attempt < max_attempts - 1:
                time.sleep(3)
                continue
            return None
        except Exception as e:
            logger.error(f"Gemini vision API call failed (attempt {attempt + 1}/{max_attempts}): {str(e)}")
            if attempt == max_attempts - 1:
                return None
            time.sleep(3)


def call_gemini_vision(image_bytes, prompt, api_key, mime_type='image/png'):
    b64_image = base64.b64encode(image_bytes).decode('utf-8')
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": b64_image}}
                ]
            }
        ],
        "generationConfig": {"temperature": 0}
    }
    return _call_gemini(payload, api_key)


def classify_label_variant(uploaded_bytes, reference_images, api_key):
    """
    Some spec families print two physically different labels for the same
    shipment -- e.g. the carton label and a small punnet/retail-pack label
    -- each needing its own validation rules. Rather than hand-write what
    distinguishes them (which would need updating per spec family), show
    Gemini the spec's own reference photos (the same ones already kept in
    S3 for the /spec info command) side by side with the uploaded photo and
    ask which one it resembles. reference_images maps a name (e.g.
    "primary", "punnet") to that variant's reference photo bytes.
    """
    if not api_key or len(reference_images) < 2:
        return None

    parts = [{"text": (
        "Each reference image below shows a different physical type of "
        "produce label, each preceded by a text line naming it. After all "
        "references, one final image preceded by the line 'UPLOADED:' is "
        "given. Decide which reference the UPLOADED image is the SAME "
        "DESIGN as -- as if checking two photos are of the same printed "
        "template, not just 'both produce labels' or 'both small retail "
        "labels'. Compare concretely: background color and pattern, "
        "whether a large brand logo/graphic is present versus a plain "
        "text-only layout, overall color scheme, and layout density -- not "
        "just coarse size/shape category. Two labels can both be small "
        "individual punnet/retail-pack labels while being completely "
        "different designs (e.g. one a colorful branded box graphic, the "
        "other a plain white text-only layout) -- that is NOT a match. "
        "Ignore the specific printed values (variety name, codes, "
        "barcodes), which differ between any two real labels regardless of "
        "design. If the UPLOADED image is not the same design as any "
        "reference, do not force a match to whichever is merely the "
        "closest of a bad set of options -- respond with exactly 'NONE'."
    )}]
    for name, img_bytes in reference_images.items():
        parts.append({"text": f"Reference '{name}':"})
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img_bytes).decode('utf-8')}})
    parts.append({"text": "UPLOADED:"})
    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(uploaded_bytes).decode('utf-8')}})
    parts.append({"text": (
        "Respond with ONLY the exact reference name (as given above, e.g. "
        "'primary') that the UPLOADED image most closely matches, or "
        "'NONE' if it doesn't clearly match any of them -- nothing else, "
        "no commentary, no punctuation."
    )})

    payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0}}
    response_text = _call_gemini(payload, api_key)
    if not response_text:
        return None

    cleaned = response_text.strip().strip('`').strip().lower()
    if cleaned == "none":
        return "NONE"
    # Check for an exact match first, then substring matches -- and among
    # those, the longest name, since a shorter variant name can be a
    # substring of a longer one (e.g. "punnet" inside "punnet_mix"), which
    # previously made a correct "punnet_mix" answer get misfiled as
    # "punnet" purely because of dict iteration order.
    for name in reference_images:
        if name.lower() == cleaned:
            return name
    substring_matches = [name for name in reference_images if name.lower() in cleaned]
    if substring_matches:
        return max(substring_matches, key=len)
    logger.info(f"Label variant classification returned unrecognized value: {response_text!r}")
    return None


def detect_zone_lines_vision_llm(crop_bytes, api_key):
    """
    Asks Gemini to transcribe an already-aligned, already-cropped zone image
    (see align_zone_locally) exactly as printed (including separators like
    '/' or '-'), rather than asking it to also split the result into a JSON
    array — that double-duty (accurate transcription + correct
    restructuring) proved unreliable, with Gemini often merging everything
    into one string despite explicit instructions. Splitting is instead
    handled by the existing, deterministic split_zone_tokens() on the
    transcribed text, same as for Textract's output. A vision LLM's edge is
    that it reads dense/multi-script text (accents, Greek, Cyrillic) far
    more accurately than character-level OCR, which is what actually
    matters here.
    """
    try:
        if not crop_bytes or not api_key:
            return []

        response_text = call_gemini_vision(crop_bytes, VISION_ZONE_PROMPT, api_key)
        if not response_text:
            return []

        return [line.strip() for line in response_text.strip().splitlines() if line.strip()]
    except Exception as e:
        logger.error(f"Vision LLM zone detection failed: {str(e)}")
        return []


def call_gemini_for_json(image_bytes, prompt, api_key):
    """
    Shared plumbing for extract_label_fields/extract_label_text_blocks: calls
    Gemini, strips the markdown code-fence wrapping some responses add
    despite being asked not to, and parses the result as a JSON object of
    string values.
    """
    response_text = call_gemini_vision(image_bytes, prompt, api_key, mime_type='image/jpeg')
    if not response_text:
        return {}

    cleaned = response_text.strip().strip('`')
    if cleaned.lower().startswith('json'):
        cleaned = cleaned[4:].strip()

    parsed = json.loads(cleaned)
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in parsed.items()}


EXTRACT_FIELDS_PROMPT_TEMPLATE = (
    "This image is a produce pallet label. Find the printed value for each of "
    "the following fields and transcribe it exactly as printed -- no "
    "paraphrasing, no translation, no reformatting:\n{field_list}\n"
    "If any part of a value is obscured, smudged, damaged, or otherwise not "
    "clearly legible in the image, do NOT guess or infer what it probably "
    "says based on similar codes or patterns you know of -- transcribe only "
    "the characters you can actually see, using '?' for each character you "
    "cannot make out (e.g. 'D75?0' if one digit is unreadable). Getting this "
    "wrong by inventing a plausible-looking value is worse than admitting "
    "uncertainty, since it would hide a real printing defect. "
    "Respond with ONLY a JSON object mapping each field name (using exactly "
    "the names given above) to its printed value as a string, nothing else "
    "-- no commentary, no markdown fences. If a field is not present "
    "anywhere on the label, map it to null."
)


def extract_label_fields(image_bytes, field_specs, api_key):
    """
    Asks Gemini to find and transcribe a set of named fields from the whole
    label image in one call -- an alternative to calibrating a dedicated
    pixel zone box for every new field that needs cross-table lookup, since
    that doesn't scale to the growing list of reference-table checks. Same
    transcribe-don't-restructure split as detect_zone_lines_vision_llm: the
    model's only job is to find and copy each value, not interpret it.

    field_specs maps each field name to an optional hint describing where/
    what it is, or None when the field name alone already matches text
    printed on the label closely enough (e.g. "PUC", "Variety" -- both are
    printed as their own heading). A hint is needed for a value with no
    literal on-label heading: e.g. IANNumber and LidlProductNumber are both
    bare, similarly-shaped codes with nothing printed labeling them as such,
    and without a distinguishing hint Gemini has been observed to swap the
    two (put the IAN number under the LidlProductNumber key and vice versa).
    """
    try:
        if not field_specs or not api_key:
            return {}

        field_list = "\n".join(
            f'- "{name}"' + (f": {hint}" if hint else "")
            for name, hint in field_specs.items()
        )
        prompt = EXTRACT_FIELDS_PROMPT_TEMPLATE.format(field_list=field_list)
        return call_gemini_for_json(image_bytes, prompt, api_key)
    except Exception as e:
        logger.error(f"Vision LLM field extraction failed: {str(e)}")
        return {}


LOGO_CHECK_PROMPT_TEMPLATE = (
    "This image is a produce pallet label. For each of the following named "
    "logos/graphics, look at the label and determine two things: (1) is it "
    "present anywhere on the label, and (2) is it printed in actual color "
    "(multiple distinct hues, not just black, white, or grayscale/halftone) "
    "-- a logo rendered only as black ink outlines/fill is NOT in color, "
    "even if other parts of the label around it are colored.\n{logo_list}\n"
    "Respond with ONLY a JSON object mapping each logo name (using exactly "
    "the names given above) to an object with two boolean fields, "
    "\"present\" and \"color\", nothing else -- no commentary, no markdown "
    "fences. If a logo is not present anywhere on the label, still include "
    "it with \"present\": false and \"color\": false."
)


def check_label_logos(image_bytes, logo_specs, api_key):
    """
    Asks Gemini a direct visual judgment about specific graphic elements
    (logos) -- whether each is printed at all, and whether it's printed in
    color versus black-and-white. Unlike extract_label_fields (transcribe
    text verbatim, no interpretation), this is a genuinely visual judgment
    call -- the same category of ask as classify_label_variant's design
    comparison -- since there's no text to transcribe for "is this graphic
    in color".
    """
    try:
        if not logo_specs or not api_key:
            return {}

        logo_list = "\n".join(f'- "{name}": {desc}' for name, desc in logo_specs.items())
        prompt = LOGO_CHECK_PROMPT_TEMPLATE.format(logo_list=logo_list)
        return call_gemini_for_json(image_bytes, prompt, api_key)
    except Exception as e:
        logger.error(f"Vision LLM logo check failed: {str(e)}")
        return {}


TEXT_BLOCK_PROMPT_TEMPLATE = (
    "This image is a produce pallet label. For each of the following named "
    "blocks of text, find it on the label and transcribe it exactly as "
    "printed, preserving every separator character (such as '/' or '-'), "
    "punctuation, diacritics, and special characters precisely as shown -- "
    "do not paraphrase, translate, summarize, or add/remove any separators. "
    "If any part of a block is obscured, smudged, damaged, or otherwise not "
    "clearly legible, do NOT guess what it probably says based on similar "
    "text you know of -- transcribe only what you can actually see, using "
    "'?' for each character you cannot make out. Inventing a plausible-"
    "looking value is worse than admitting uncertainty, since it would hide "
    "a real printing defect. "
    "The blocks to find:\n{block_list}\n"
    "Respond with ONLY a JSON object mapping each block name (using exactly "
    "the names given below) to its transcribed text as a string, nothing "
    "else -- no commentary, no markdown fences. If a block is not present "
    "anywhere on the label, map it to null."
)


def extract_label_text_blocks(image_bytes, block_prompts, api_key):
    """
    Like extract_label_fields(), but for dense free-text blocks (e.g. the
    list of ~23 language translations of the product name) rather than
    short discrete values -- found by a semantic description of what to
    look for (block_prompts maps a block name to that description) instead
    of a pixel zone box, and transcribed verbatim so the result can still be
    compared against exact reference content afterward. This is what lets a
    label whose physical layout doesn't match any calibrated zone still get
    this block validated: Gemini locates it by content, not position.
    """
    try:
        if not block_prompts or not api_key:
            return {}

        block_list = "\n".join(f'- "{name}": {desc}' for name, desc in block_prompts.items())
        prompt = TEXT_BLOCK_PROMPT_TEMPLATE.format(block_list=block_list)
        return call_gemini_for_json(image_bytes, prompt, api_key)
    except Exception as e:
        logger.error(f"Vision LLM text block extraction failed: {str(e)}")
        return {}


def resolve_lookup_value(value_spec, extracted_fields, config_data, rule_results):
    """
    Resolves one component of a lookup_rules "key" dict to an actual value.
    A value_spec is a small prefixed reference rather than a literal, so a
    rule's key can be built from whatever combination of sources a real
    multi-table chain needs:
      "field:X"  -> extracted_fields[X] (a value the vision LLM read off the
                    label this request, e.g. "field:Variety")
      "config:X" -> config_data[X] (a fixed per-spec value, e.g.
                    "config:commodity" for a single-commodity spec)
      "result:R.A" -> rule_results[R][A], the attribute A of whatever row an
                    earlier rule (with matching "store_as": "R") found --
                    this is what makes chained lookups possible (e.g. look up
                    Variety to find its Commodity, then use that Commodity to
                    look up the Commodity table).
    Anything without a recognized prefix is treated as a literal value.
    """
    if value_spec.startswith("field:"):
        return extracted_fields.get(value_spec[len("field:"):])
    if value_spec.startswith("config:"):
        return config_data.get(value_spec[len("config:"):])
    if value_spec.startswith("result:"):
        rule_name, _, attribute = value_spec[len("result:"):].partition(".")
        item = rule_results.get(rule_name)
        return item.get(attribute) if item else None
    return value_spec


def evaluate_zone(user_lines, expected_pattern, compare_to_reference, expected_token_count, token_count_tolerance, ref_img, box):
    """
    Checks OCR'd lines for a zone against its configured requirements.
    Returns None if the zone passes, or a failure-reason string if not.
    """
    user_texts = {line.upper() for line in user_lines}
    zone_tokens = split_zone_tokens(user_lines)

    if not user_texts:
        return "Missing / Empty"
    if expected_pattern and not re.search(expected_pattern, " ".join(user_lines), re.IGNORECASE):
        return "Content Mismatch"
    if compare_to_reference and not zone_matches_reference(ref_img, box, user_lines):
        return "Content Mismatch"
    if expected_token_count and (expected_token_count - len(zone_tokens)) > token_count_tolerance:
        return f"Expected {expected_token_count} translations, found {len(zone_tokens)}"
    return None


def validate_label_layout(uploaded_bytes, spec_code, debug_key_prefix=None):
    # Some specs print two physically different labels for the same
    # shipment (e.g. a carton label and a punnet/retail-pack label). A spec
    # opts into this by having a specs/{spec_code}_config_{suffix}.json
    # alongside a specs/{spec_code}_label_{suffix}.png reference photo for
    # one or more of LABEL_VARIANT_SUFFIXES; a spec with neither behaves
    # exactly as before, at the cost of one cheap head_object check per
    # known suffix.
    variant_suffix = None
    candidate_suffixes = []
    for suffix in LABEL_VARIANT_SUFFIXES:
        try:
            s3_client.head_object(Bucket=BUCKET_NAME, Key=f"specs/{spec_code}_config_{suffix}.json")
            candidate_suffixes.append(suffix)
        except ClientError:
            continue

    if candidate_suffixes:
        reference_images = {}
        try:
            primary_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=f"specs/{spec_code}_label.png")
            reference_images["primary"] = primary_obj['Body'].read()
        except ClientError:
            pass
        for suffix in candidate_suffixes:
            try:
                variant_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=f"specs/{spec_code}_label_{suffix}.png")
                reference_images[suffix] = variant_obj['Body'].read()
            except ClientError:
                continue

        if len(reference_images) > 1:
            classify_api_key = get_ssm_param('/whatsapp/gemini_api_key')
            chosen = classify_label_variant(uploaded_bytes, reference_images, classify_api_key)
            logger.info(f"Spec '{spec_code}': label variant classification -> {chosen or 'primary'}")

            # A photo that doesn't clearly match ANY of this spec's own
            # reference photos (carton or punnet) is a strong signal the
            # wrong spec code was used -- e.g. a genuinely different
            # customer's label submitted under this spec's code. Content
            # checks alone can't catch this: 9B's and 9C's punnet rules
            # are both generic (GGN/PUC/Variety/Lot, backed by Dole-global
            # reference tables), so a real product's data from the WRONG
            # spec's label satisfies the RIGHT spec's rules just fine --
            # confirmed live (2026-09-07): a 9B photo sent as "9C" passed
            # 9C's punnet checks outright, since nothing checked whether
            # the photo actually looked like a 9C label at all. Failing
            # fast here, before even loading a config, closes that gap.
            if chosen == "NONE":
                return "ERROR", (
                    f"❌ *Layout Verification FAILED*\n\n"
                    f"• Spec Reference: *{spec_code}*\n"
                    f"• Reason: This photo doesn't match either the carton or "
                    f"punnet label design on file for spec *{spec_code}*. "
                    f"Please check you used the correct spec code."
                ), {}

            if chosen and chosen != "primary":
                variant_suffix = chosen

    config_suffix = f"_{variant_suffix}" if variant_suffix else ""
    config_key = f"specs/{spec_code}_config{config_suffix}.json"

    config_data = {}
    config_found = False
    try:
        config_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=config_key)
        config_data = json.loads(config_obj['Body'].read().decode('utf-8'))
        config_found = True
    except ClientError:
        logger.info(f"No custom config found for {spec_code}.")

    if not config_found:
        # No config at all (as opposed to a config that's just sparse) used
        # to silently fall through to a single presence-only "Full Label"
        # check and report a real PASS -- meaning any spec with a catalog
        # entry/reference image but no actual validation rules yet looked
        # exactly like a fully-validated one. Report plainly instead of
        # letting that trivial check masquerade as a real result.
        return "ERROR", (
            f"⚠️ *No Validation Rules Configured*\n\n"
            f"• Spec Reference: *{spec_code}*\n"
            f"• Reason: This spec code has no label validation rules set up yet -- "
            f"nothing was actually checked. Contact your administrator to configure it."
        ), {}

    # A "layout_agnostic" spec skips the reference-photo/ORB-alignment
    # system entirely -- that system assumes the uploaded photo is (once
    # rotated/cropped) geometrically close to the one reference photo on
    # file, which by design can't hold for a label a different 3rd-party
    # printer laid out differently. Such a spec is checked purely on
    # extracted field/text-block content instead, found by Gemini wherever
    # it actually sits rather than by pixel position.
    layout_agnostic = config_data.get('layout_agnostic', False)

    extracted_bytes = extract_label_automatically(uploaded_bytes)

    if debug_key_prefix:
        try:
            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=f"debug/{debug_key_prefix}_extracted.jpg",
                Body=extracted_bytes,
                ContentType='image/jpeg'
            )
        except Exception as ex:
            logger.error(f"Failed to save debug extracted image: {str(ex)}")

    matcher = None
    target_regions = []
    if not layout_agnostic:
        ref_label_key = f"specs/{spec_code}_label{config_suffix}.png"
        try:
            ref_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=ref_label_key)
            ref_image_bytes = ref_obj['Body'].read()
        except ClientError:
            return "ERROR", f"❌ *Layout Verification FAILED*\n\n• Spec Reference: *{spec_code}*\n• Reason: Reference layout not found in S3.", {}

        matcher = build_orb_matcher(ref_image_bytes, extracted_bytes)
        if matcher is None:
            return "ERROR", f"❌ *Layout Verification FAILED*\n\n• Spec Reference: *{spec_code}*\n• Reason: Could not process uploaded image.", {}

        target_regions = config_data.get('target_regions', [
            {"name": "Full Label", "box": {"Left": 0.0, "Top": 0.0, "Width": 1.0, "Height": 1.0}}
        ])

    full_label_checks = config_data.get('full_label_checks', [])
    extract_field_specs = config_data.get('extract_fields', {})
    if isinstance(extract_field_specs, list):
        # Older configs list bare field names with no hint; normalize to the
        # {name: hint} shape extract_label_fields() expects.
        extract_field_specs = {name: None for name in extract_field_specs}
    lookup_rules = config_data.get('lookup_rules', [])
    field_checks = config_data.get('field_checks', [])
    text_block_prompts = config_data.get('text_blocks', {})
    text_block_checks = config_data.get('text_block_checks', [])
    size_prompt_checks = config_data.get('size_prompt_checks', [])
    color_tag_checks = config_data.get('color_tag_checks', [])
    allowed_variety_group_checks = config_data.get('allowed_variety_group_checks', [])
    date_comparison_checks = config_data.get('date_comparison_checks', [])
    date_freshness_checks = config_data.get('date_freshness_checks', [])
    arithmetic_checks = config_data.get('arithmetic_checks', [])
    field_equality_checks = config_data.get('field_equality_checks', [])
    week_day_code_checks = config_data.get('week_day_code_checks', [])
    logo_checks = config_data.get('logo_checks', [])
    warning_checks = config_data.get('warning_checks', [])

    vision_llm_api_key = None
    if any(zone.get("use_vision_llm") for zone in target_regions) or extract_field_specs or text_block_prompts or logo_checks:
        vision_llm_api_key = get_ssm_param('/whatsapp/gemini_api_key')

    try:
        failed_zones = []
        passed_zones = 0
        passed_zone_names = []
        warnings = []
        zone_content = {}

        for zone in target_regions:
            zone_name = zone.get("name", "Unnamed Block")
            box = zone.get("box", zone)
            expected_pattern = zone.get("expected_pattern")
            compare_to_reference = zone.get("compare_to_reference", False)
            expected_token_count = zone.get("expected_token_count")
            token_count_tolerance = zone.get("token_count_tolerance", 0)
            use_textract = zone.get("use_textract", False)
            use_vision_llm = zone.get("use_vision_llm", False)

            crop_candidates = align_zone_locally(matcher, box, zone_name=zone_name, use_vision_llm=use_vision_llm)

            # Try each candidate crop in order, but only stop at one that
            # actually satisfies the zone's requirements -- not merely one
            # that returned some text. A badly-extrapolated homography can
            # produce a crop full of garbled/mixed-in text from unrelated
            # parts of the label, which is "non-empty" but wrong; accepting
            # the first non-empty result defeats the point of having a
            # fallback candidate at all.
            user_lines = []
            failure_reason = "Missing / Empty"
            for candidate_bytes, candidate_box in crop_candidates:
                if use_vision_llm:
                    candidate_lines = detect_zone_lines_vision_llm(candidate_bytes, vision_llm_api_key)
                elif use_textract:
                    candidate_lines = detect_zone_lines_textract(candidate_bytes)
                else:
                    user_analysis = rekognition.detect_text(Image={'Bytes': candidate_bytes})
                    candidate_lines = [b['DetectedText'].strip() for b in user_analysis.get('TextDetections', []) if b['Type'] == 'LINE']

                user_lines = candidate_lines
                # Compare against a reference crop from the same region the
                # candidate actually came from (candidate_box), not the
                # zone's nominal box -- a fallback candidate is often cropped
                # from a padded region well beyond the nominal box, and
                # comparing that to a tight reference crop guarantees a
                # false mismatch regardless of actual content.
                failure_reason = evaluate_zone(
                    candidate_lines, expected_pattern, compare_to_reference,
                    expected_token_count, token_count_tolerance, matcher["ref_img"], candidate_box
                )
                if failure_reason is None:
                    break

            if failure_reason and debug_key_prefix:
                safe_zone_name = re.sub(r'[^A-Za-z0-9_-]+', '_', zone_name)
                for i, (candidate_bytes, _candidate_box) in enumerate(crop_candidates):
                    try:
                        s3_client.put_object(
                            Bucket=BUCKET_NAME,
                            Key=f"debug/{debug_key_prefix}_zone_{safe_zone_name}_candidate{i}.png",
                            Body=candidate_bytes,
                            ContentType='image/png'
                        )
                    except Exception as ex:
                        logger.error(f"Failed to save debug crop for zone '{zone_name}' candidate {i}: {str(ex)}")

            zone_content[zone_name] = user_lines
            zone_tokens = split_zone_tokens(user_lines)

            if expected_token_count:
                logger.info(f"Zone '{zone_name}' token count: expected={expected_token_count} actual={len(zone_tokens)} tokens={zone_tokens}")

            if failure_reason:
                failed_zones.append(f"{zone_name} ({failure_reason})")
            else:
                passed_zones += 1
                passed_zone_names.append(zone_name)

        full_label_text = ""
        if full_label_checks or size_prompt_checks:
            # Rekognition's detect_text on a full, dense multi-section label only
            # picks up the most prominent text (the two 23-language blocks) and
            # silently drops smaller print (Supplier/PHC/footer/Importer lines) --
            # the same dense-document limitation that required switching the
            # language zones to Textract. Use Textract here too so these
            # full-label checks see the whole label, not just its biggest text.
            full_label_lines = detect_zone_lines_textract(extracted_bytes)
            full_label_text = " ".join(full_label_lines)

        if full_label_checks:
            for check in full_label_checks:
                check_name = check.get("name", "Unnamed Check")
                pattern = check.get("pattern")
                if pattern and not re.search(pattern, full_label_text, re.IGNORECASE):
                    failed_zones.append(f"{check_name} (Missing required text)")
                else:
                    passed_zones += 1
                    passed_zone_names.append(check_name)

        extracted_fields = {}
        if extract_field_specs:
            extracted_fields = extract_label_fields(extracted_bytes, extract_field_specs, vision_llm_api_key)
            logger.info(f"Extracted label fields: {extracted_fields}")

        # Layout-agnostic replacement for what evaluate_zone's
        # expected_pattern did against a zone crop -- same regex check, but
        # against a value Gemini found by name rather than by pixel box.
        for check in field_checks:
            field_name = check.get("field")
            check_name = check.get("name", field_name)
            pattern = check.get("expected_pattern")
            value = extracted_fields.get(field_name)

            if not value:
                failed_zones.append(f"{check_name} (Missing / Empty)")
            elif pattern and not re.search(pattern, str(value), re.IGNORECASE):
                failed_zones.append(f"{check_name} (Content Mismatch)")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # Soft checks: worth flagging for a human to double-check, but never
        # enough on their own to fail the whole verification -- e.g. a
        # second GGN printed in the Packhouse block that's expected to match
        # the Grower GGN but isn't itself a hard requirement either way.
        # Excluded from total_regions/passed_zones entirely, so they can
        # never affect the overall PASS/FAIL outcome.
        for check in warning_checks:
            check_name = check.get("name", "Warning")
            field_name = check.get("field")
            compare_field = check.get("compare_field")
            value = extracted_fields.get(field_name)
            compare_value = extracted_fields.get(compare_field)

            if not value:
                warnings.append(f"{check_name}: could not find {field_name} on the label to check")
            elif compare_value and normalize_zone_text([str(value)]) != normalize_zone_text([str(compare_value)]):
                warnings.append(
                    f"{check_name}: {field_name} ('{value}') differs from {compare_field} ('{compare_value}') -- please double-check"
                )

        text_blocks = {}
        if text_block_prompts:
            text_blocks = extract_label_text_blocks(extracted_bytes, text_block_prompts, vision_llm_api_key)
            logger.info(f"Extracted text blocks: { {k: len(v) if isinstance(v, str) else v for k, v in text_blocks.items()} }")

        # Layout-agnostic replacement for expected_token_count -- same
        # split_zone_tokens()-based counting, but against a block Gemini
        # located by content instead of a zone crop, and always exact (no
        # tolerance): a genuinely correct label was proven able to hit an
        # exact count once the underlying crop-skew bug was fixed, so an
        # undercount here means a real missing translation, not noise.
        for check in text_block_checks:
            block_name = check.get("block")
            check_name = check.get("name", block_name)
            expected_token_count = check.get("expected_token_count")
            expected_content = check.get("expected_content")
            fuzzy_threshold = check.get("fuzzy_threshold")
            block_text = text_blocks.get(block_name)
            block_tokens = split_zone_tokens([block_text]) if block_text else []

            def get_block_actual_text(name=block_name):
                bt = text_blocks.get(name)
                return normalize_zone_text([bt]) if bt else ""

            def refresh_text_blocks():
                text_blocks.update(extract_label_text_blocks(extracted_bytes, text_block_prompts, vision_llm_api_key))

            if not block_text:
                failed_zones.append(f"{check_name} (Missing / Empty)")
            elif expected_token_count and len(block_tokens) < expected_token_count:
                failed_zones.append(
                    f"{check_name} (Expected {expected_token_count} translations, found {len(block_tokens)})"
                )
            elif expected_content:
                matched, _ = content_matches_with_retry(
                    expected_content.strip().upper(), fuzzy_threshold, get_block_actual_text, refresh_text_blocks
                )
                if matched:
                    passed_zones += 1
                    passed_zone_names.append(check_name)
                else:
                    failed_zones.append(f"{check_name} (Content Mismatch)")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        rule_results = {}
        for rule in lookup_rules:
            rule_name = rule.get("name", "Unnamed Rule")
            key_spec = rule.get("key", {})
            compare_attribute = rule.get("compare_attribute")
            compare_zone = rule.get("compare_zone")

            query_mode = rule.get("query", False)
            disambiguate_by = rule.get("disambiguate_by")
            scan_match = rule.get("scan_match")

            items = []
            key = {}
            last_unresolved_attr = None
            lookup_failed = False

            if scan_match:
                # No printed code identifies which reference row applies (e.g.
                # 17C's Importer Name/Address has no on-label code to key off
                # of) -- instead of an exact-key GetItem, scan the whole
                # table (small, tens of rows) and fuzzy-match the printed
                # text against every candidate row. match_fields lists one or
                # more independent {source, attribute, fuzzy_min_score}
                # comparisons (e.g. Name vs Address1, Address vs
                # AddressLines) -- a candidate only qualifies if it clears
                # EVERY field's own threshold. This matters because a single
                # combined-text score would let a long, genuinely-correct
                # address paper over a short, wrong/corrupted name (most of
                # the text still matches, diluting the one part that
                # shouldn't).
                match_fields = scan_match.get("match_fields", [])
                resolved_fields = []
                unresolved = None
                for mf in match_fields:
                    value = resolve_lookup_value(mf.get("source"), extracted_fields, config_data, rule_results)
                    if not value:
                        unresolved = mf.get("source")
                        break
                    resolved_fields.append({
                        "attribute": mf.get("attribute"),
                        "min_score": mf.get("fuzzy_min_score", 0.6),
                        "value_norm": normalize_zone_text([str(value)]).upper(),
                    })

                if unresolved:
                    last_unresolved_attr = unresolved
                else:
                    try:
                        table = dynamodb.Table(rule.get("table"))
                        candidates = table.scan().get('Items', [])
                    except Exception as e:
                        logger.error(f"Lookup rule '{rule_name}' table scan failed: {str(e)}")
                        lookup_failed = True
                        candidates = []

                    best_item, best_avg_score, best_scores = None, -1, None
                    for candidate in candidates:
                        scores = []
                        qualifies = True
                        for rf in resolved_fields:
                            candidate_text = str(candidate.get(rf["attribute"], "")).strip().upper()
                            score = difflib.SequenceMatcher(None, candidate_text, rf["value_norm"]).ratio() if candidate_text else 0.0
                            scores.append(score)
                            if score < rf["min_score"]:
                                qualifies = False
                        if not qualifies:
                            continue
                        avg_score = sum(scores) / len(scores)
                        if avg_score > best_avg_score:
                            best_item, best_avg_score, best_scores = candidate, avg_score, scores

                    if best_item is not None:
                        items = [best_item]
                        key = {"scan_match": [rf["value_norm"] for rf in resolved_fields]}
                        logger.info(f"Lookup rule '{rule_name}' scan_match -> {best_item} (scores={best_scores})")
                    elif not lookup_failed:
                        failed_zones.append(f"{rule_name} (No matching reference entry found)")
                        continue
            else:
                # Most rules have just one key shape, but a table can have an
                # exception needing an extra key attribute -- e.g. the REWE
                # combinations table is keyed on Pack+VarietyGroup for almost
                # every row, except the one VarietyGroup+InvCode='RH' exception,
                # which needs Pack+VarietyGroup+InvCode. Rather than teach the
                # engine REWE-specific bucketing logic, a rule can declare a
                # "fallback_key" (same shape as "key") tried only if the primary
                # key's exact match isn't found -- the reference table itself
                # encodes which rows need the more specific key.
                key_specs_to_try = [ks for ks in [key_spec, rule.get("fallback_key")] if ks]

                for candidate_key_spec in key_specs_to_try:
                    candidate_key = {}
                    unresolved_attr = None
                    for key_attribute, value_spec in candidate_key_spec.items():
                        # Most keys resolve from a single source, but a table keyed
                        # on more than 2 real-world attributes (DynamoDB only allows
                        # a hash+range pair) needs several sources concatenated into
                        # one stored key -- e.g. IAN Numbers' Commodity+VarietyGroup+
                        # Pack+InventoryCode -- so a value_spec can be a list of
                        # sources to join with "#", the same separator used when the
                        # table was built, instead of just one.
                        part_specs = value_spec if isinstance(value_spec, list) else [value_spec]
                        parts = []
                        for part_spec in part_specs:
                            part_value = resolve_lookup_value(part_spec, extracted_fields, config_data, rule_results)
                            if not part_value:
                                parts = None
                                break
                            # Values transcribed off the label can carry line-wrap
                            # newlines where the reference table has a plain space
                            # (e.g. a Variety name printed across two lines) --
                            # collapse all whitespace the same way zone text already
                            # is, instead of just trimming the ends, or an
                            # exact-match key lookup fails on a label that's
                            # otherwise completely correct.
                            parts.append(normalize_zone_text([str(part_value)]))

                        if parts is None:
                            unresolved_attr = key_attribute
                            break
                        candidate_key[key_attribute] = "#".join(parts)

                    if unresolved_attr:
                        last_unresolved_attr = unresolved_attr
                        continue

                    key = candidate_key
                    try:
                        table = dynamodb.Table(rule.get("table"))
                        if query_mode:
                            # `key` is expected to hold just the partition key here --
                            # e.g. Variety table's VarietyName alone, letting every
                            # commodity sharing that name come back to be
                            # disambiguated below, rather than requiring the caller
                            # to already know which commodity applies (the whole
                            # point: nothing upstream has to hardcode a per-spec
                            # commodity just to look up a Variety).
                            partition_attr, partition_value = next(iter(key.items()))
                            found_items = table.query(KeyConditionExpression=Key(partition_attr).eq(partition_value)).get('Items', [])
                        else:
                            single_item = table.get_item(Key=key).get('Item')
                            found_items = [single_item] if single_item else []

                            if not found_items:
                                # A vision-extracted digit can occasionally be
                                # a 5/6 confusion on a low-resolution photo
                                # (see generate_digit_confusion_variants) --
                                # retry the exact-match lookup against every
                                # plausible correction before giving up. The
                                # original key already failed, so it's
                                # skipped rather than re-tried. Always on
                                # (not a per-rule opt-in): it only ever fires
                                # after the literal value has already failed
                                # to resolve, so it can't turn a genuinely
                                # correct rejection into a false pass.
                                attr_variants = {
                                    attr: list(generate_digit_confusion_variants(val))
                                    for attr, val in key.items()
                                }
                                for combo in itertools.product(*attr_variants.values()):
                                    variant_key = dict(zip(attr_variants.keys(), combo))
                                    if variant_key == key:
                                        continue
                                    variant_item = table.get_item(Key=variant_key).get('Item')
                                    if variant_item:
                                        logger.info(
                                            f"Lookup rule '{rule_name}' resolved via digit-confusion retry: "
                                            f"{key} -> {variant_key}"
                                        )
                                        warnings.append(
                                            f"{rule_name}: only matched after correcting a likely 5/6 misread "
                                            f"({key} -> {variant_key}) -- consider a sharper photo"
                                        )
                                        found_items = [variant_item]
                                        key = variant_key
                                        break
                    except Exception as e:
                        logger.error(f"Lookup rule '{rule_name}' table query failed: {str(e)}")
                        lookup_failed = True
                        break

                    if found_items:
                        items = found_items
                        break

            if lookup_failed:
                failed_zones.append(f"{rule_name} (Lookup failed)")
                continue

            if not items:
                if not key:
                    failed_zones.append(f"{rule_name} (Could not resolve {last_unresolved_attr} for lookup)")
                else:
                    failed_zones.append(f"{rule_name} ({key} not found in reference table)")
                continue

            if len(items) == 1:
                item = items[0]
            elif disambiguate_by:
                # A handful of Variety names span more than one commodity
                # (e.g. "EUREKA" is both a Blueberry and a Lemon variety) --
                # pick whichever candidate's own reference text (e.g. its
                # Variety Group's canonical language block) best matches
                # what's actually printed on this label, instead of
                # requiring the caller to pre-select a commodity.
                candidate_attr = disambiguate_by.get("candidate_attribute")
                ref_table = dynamodb.Table(disambiguate_by.get("reference_table"))
                ref_key_attr = disambiguate_by.get("reference_key_attribute")
                ref_compare_attr = disambiguate_by.get("reference_compare_attribute")
                actual_text_for_dis = get_compare_text(
                    disambiguate_by.get("compare_zone"), zone_content, extracted_fields, text_blocks
                )

                item, best_score = None, -1
                for candidate in items:
                    candidate_key_value = candidate.get(candidate_attr)
                    if not candidate_key_value:
                        continue
                    ref_item = ref_table.get_item(Key={ref_key_attr: candidate_key_value}).get('Item')
                    ref_text = str(ref_item.get(ref_compare_attr, "")).strip().upper() if ref_item else ""
                    if not ref_text:
                        continue
                    score = difflib.SequenceMatcher(None, ref_text, actual_text_for_dis[:len(ref_text)]).ratio()
                    if score > best_score:
                        item, best_score = candidate, score

                if item is None:
                    failed_zones.append(f"{rule_name} ({len(items)} matches for {key}, could not disambiguate)")
                    continue
                logger.info(f"Lookup rule '{rule_name}' disambiguated {len(items)} candidates -> {item} (score={best_score:.3f})")
            else:
                failed_zones.append(f"{rule_name} ({len(items)} matches for {key}, ambiguous)")
                continue

            if rule.get("store_as"):
                rule_results[rule["store_as"]] = item

            if not compare_attribute or not compare_zone:
                # A pure resolution step (e.g. Variety -> Commodity) that
                # only feeds a later rule via "result:" -- no content of its
                # own to compare, so finding the row is the whole check.
                passed_zones += 1
                passed_zone_names.append(rule_name)
                continue

            expected_value = str(item.get(compare_attribute, "")).strip().upper()
            fuzzy_threshold = rule.get("fuzzy_threshold")

            def get_rule_actual_text(zone=compare_zone):
                return get_compare_text(zone, zone_content, extracted_fields, text_blocks)

            def refresh_rule_source(zone=compare_zone):
                # compare_zone can be sourced from either extraction call
                # depending on config -- refresh whichever one actually
                # produced it, not both, to avoid a needless extra Gemini call.
                if zone in text_block_prompts:
                    text_blocks.update(extract_label_text_blocks(extracted_bytes, text_block_prompts, vision_llm_api_key))
                elif zone in extract_field_specs:
                    extracted_fields.update(extract_label_fields(extracted_bytes, extract_field_specs, vision_llm_api_key))

            if expected_value:
                matched, actual_text = content_matches_with_retry(
                    expected_value, fuzzy_threshold, get_rule_actual_text, refresh_rule_source
                )
                if not matched and not fuzzy_threshold:
                    # Same 5/6-confusion tolerance as the key lookup above,
                    # applied here to the compared value (e.g. a GGN) rather
                    # than the lookup key -- either side of a PUC/GGN
                    # cross-check can be the one a low-res photo garbled.
                    # Scoped to exact-match comparisons only (no
                    # fuzzy_threshold): a dense fuzzy text block already gets
                    # its own re-extraction retry above, and could contain
                    # enough incidental 5s/6s to make this expensive for no
                    # benefit.
                    for variant in generate_digit_confusion_variants(actual_text):
                        if content_matches(expected_value, variant, fuzzy_threshold):
                            logger.info(f"Lookup rule '{rule_name}' matched via digit-confusion retry: '{variant}'")
                            warnings.append(
                                f"{rule_name}: only matched after correcting a likely 5/6 misread "
                                f"('{actual_text}' -> '{variant}') -- consider a sharper photo"
                            )
                            matched, actual_text = True, variant
                            break
                if matched:
                    passed_zones += 1
                    passed_zone_names.append(rule_name)
                else:
                    failed_zones.append(
                        f"{rule_name} (Expected {compare_attribute} '{expected_value}', label shows '{actual_text}')"
                    )
            else:
                passed_zones += 1
                passed_zone_names.append(rule_name)

        # Rule 9's Size Prompt: the commodity that decides which heading is
        # expected is only known once the Variety lookup above has resolved
        # it (via a prior rule's "store_as"), so this runs after lookup_rules
        # rather than alongside field_checks/text_block_checks.
        for check in size_prompt_checks:
            check_name = check.get("name", "Size Prompt")
            commodity_value = resolve_lookup_value(
                check.get("commodity_source"), extracted_fields, config_data, rule_results
            )
            if not commodity_value:
                failed_zones.append(f"{check_name} (Could not resolve commodity)")
                continue

            # The trailing ":" is dropped often enough by Textract (a thin,
            # easily-missed glyph, same class of loss as other punctuation
            # already tolerated elsewhere in this file) that requiring it
            # literally produced a false failure on a real, correctly-
            # printed "Size Ref/Count" heading -- only the heading text
            # itself needs to be present, not its trailing punctuation.
            expected_prompts = expected_size_prompt(commodity_value)
            if not any(p.upper().rstrip(':') in full_label_text.upper() for p in expected_prompts):
                options = " or ".join(f"'{p}'" for p in expected_prompts)
                failed_zones.append(f"{check_name} (Expected prompt {options}, not found on label)")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # Some punnet designs are printed on pre-made packaging stock that
        # already carries a fixed color/style tag (e.g. "Hell & Kernlos") --
        # a real defect if the wrong stock is used for a batch of a
        # different-colored variety (e.g. Crimson Seedless, a Red Seedless
        # variety, printed on White-Seedless-tagged packaging). The
        # mapping is declared per-check in config, not hardcoded, since
        # which variety groups are even allowed -- let alone their exact
        # tag wording -- is spec-specific (e.g. 8D only allows White/Red,
        # not Black); a variety group missing from the map fails naturally,
        # which also enforces "not an allowed variety for this spec".
        for check in color_tag_checks:
            check_name = check.get("name", "Packaging color tag")
            variety_group_code = resolve_lookup_value(
                check.get("variety_group_source"), extracted_fields, config_data, rule_results
            )
            if not variety_group_code:
                failed_zones.append(f"{check_name} (Could not resolve variety group)")
                continue

            expected_tag = check.get("tag_by_variety_group", {}).get(variety_group_code.upper())
            if not expected_tag:
                failed_zones.append(f"{check_name} (Variety group '{variety_group_code}' not allowed for this spec)")
                continue

            field_name = check.get("field")
            actual_value = extracted_fields.get(field_name)
            actual_text = normalize_zone_text([str(actual_value)]).upper() if actual_value else ""
            if not content_matches(expected_tag.upper(), actual_text):
                failed_zones.append(f"{check_name} (Expected '{expected_tag}', label shows '{actual_value}')")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # A simpler sibling of color_tag_checks for specs with no printed
        # packaging tag to cross-check against -- just restricts which
        # variety color groups are allowed at all (e.g. "White and Red
        # Seedless only, no Black"), resolved the same way via the existing
        # Variety lookup's result.
        for check in allowed_variety_group_checks:
            check_name = check.get("name", "Allowed variety group")
            variety_group_code = resolve_lookup_value(
                check.get("variety_group_source"), extracted_fields, config_data, rule_results
            )
            allowed = [v.upper() for v in check.get("allowed", [])]
            if not variety_group_code:
                failed_zones.append(f"{check_name} (Could not resolve variety group)")
            elif variety_group_code.upper() not in allowed:
                failed_zones.append(f"{check_name} (Variety group '{variety_group_code}' not allowed)")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # Chronological ordering between two printed dates (e.g. an expiry
        # date must fall after the production date) -- plain string
        # comparison doesn't work for dd.mm.yyyy text, so this actually
        # parses both as dates. strptime tolerates the single-digit
        # day/month forms seen on real labels (e.g. "7.09.2026").
        for check in date_comparison_checks:
            check_name = check.get("name", "Date comparison")
            date_format = check.get("date_format", "%d.%m.%Y")
            earlier_value = extracted_fields.get(check.get("earlier_field"))
            later_value = extracted_fields.get(check.get("later_field"))
            if not earlier_value or not later_value:
                failed_zones.append(f"{check_name} (Missing / Empty)")
                continue
            try:
                earlier_date = datetime.strptime(str(earlier_value).strip(), date_format)
                later_date = datetime.strptime(str(later_value).strip(), date_format)
            except ValueError:
                failed_zones.append(f"{check_name} (Could not parse '{earlier_value}' / '{later_value}' as dates)")
                continue
            if later_date <= earlier_date:
                failed_zones.append(f"{check_name} ('{later_value}' is not after '{earlier_value}')")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # A printed date (e.g. Дата изготовления/production date) should be
        # recent -- within a configured number of days of today, in either
        # direction -- rather than compared against another printed field.
        # Meant to flag a stale/reused label rather than one freshly printed
        # for this actual shipment. Deliberately a soft warning, not a fail
        # (per user decision): unlike a format/lookup check, "how many days
        # old is too old" is a judgment call worth a human glance rather
        # than an automatic rejection. "Today" is UTC (matching the audit
        # log's own timestamp convention elsewhere in this file); the
        # tolerance is generous enough to absorb any UTC/local timezone
        # difference near a day boundary, so this doesn't need its own
        # timezone handling.
        for check in date_freshness_checks:
            check_name = check.get("name", "Date freshness check")
            date_format = check.get("date_format", "%d.%m.%Y")
            max_days_diff = check.get("max_days_diff", 2)
            value = extracted_fields.get(check.get("field"))
            if not value:
                warnings.append(f"{check_name}: could not find a date on the label to check")
                continue
            try:
                parsed_date = datetime.strptime(str(value).strip(), date_format).date()
            except ValueError:
                warnings.append(f"{check_name}: could not parse '{value}' as a date")
                continue
            days_diff = abs((datetime.now(timezone.utc).date() - parsed_date).days)
            if days_diff > max_days_diff:
                warnings.append(
                    f"{check_name}: '{value}' is {days_diff} day(s) from today (expected within {max_days_diff}) -- please double-check"
                )

        # Numeric cross-check between printed weights/quantities (e.g. net +
        # tare + pallet base weight must equal the printed gross weight) --
        # a real Dole label field, not just a formatting check. Values are
        # parsed as plain numbers; a small tolerance absorbs rounding on
        # printed weights.
        for check in arithmetic_checks:
            check_name = check.get("name", "Arithmetic check")
            operand_fields = check.get("operands", [])
            target_field = check.get("target")
            tolerance = check.get("tolerance", 0.01)
            raw_values = [extracted_fields.get(f) for f in operand_fields] + [extracted_fields.get(target_field)]
            if any(v is None or str(v).strip() == "" for v in raw_values):
                failed_zones.append(f"{check_name} (Missing / Empty)")
                continue
            try:
                operand_values = [float(re.sub(r"[^\d.\-]", "", str(extracted_fields[f]))) for f in operand_fields]
                target_value = float(re.sub(r"[^\d.\-]", "", str(extracted_fields[target_field])))
            except ValueError:
                failed_zones.append(f"{check_name} (Could not parse numeric values)")
                continue
            computed = sum(operand_values)
            if abs(computed - target_value) > tolerance:
                failed_zones.append(
                    f"{check_name} ({' + '.join(operand_fields)} = {computed:g}, expected {target_field} = {target_value:g})"
                )
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # A hard (pass/fail-affecting) equality between two extracted
        # fields -- e.g. a printed order number must match the barcode's
        # own human-readable number. Distinct from warning_checks, which
        # are deliberately soft/non-blocking for a different kind of
        # cross-field mismatch.
        for check in field_equality_checks:
            check_name = check.get("name", "Field equality")
            value = extracted_fields.get(check.get("field"))
            compare_value = extracted_fields.get(check.get("compare_field"))
            if not value or not compare_value:
                failed_zones.append(f"{check_name} (Missing / Empty)")
            elif normalize_zone_text([str(value)]) != normalize_zone_text([str(compare_value)]):
                failed_zones.append(f"{check_name} ('{value}' does not match '{compare_value}')")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # A printed code whose digits, once reordered per-config, encode a
        # week number (01-53) and an ISO day-of-week (1-7) -- e.g. a Pack
        # Ref like "6403" where the last 2 digits move in front of the
        # first 2 ("0364"), the leading digit is discarded, and what's left
        # ("364") reads as week 36 / day 4. Config supplies the digit
        # positions (0-indexed into the raw extracted digit string) that
        # make up the week and the day, so this stays generic rather than
        # hardcoding this one spec's specific shuffle.
        for check in week_day_code_checks:
            check_name = check.get("name", "Week/day code check")
            value = extracted_fields.get(check.get("field"))
            expected_length = check.get("length")
            week_indices = check.get("week_digit_indices", [])
            day_indices = check.get("day_digit_indices", [])
            if not value:
                failed_zones.append(f"{check_name} (Missing / Empty)")
                continue
            digits = re.sub(r"\D", "", str(value))
            if expected_length and len(digits) != expected_length:
                failed_zones.append(f"{check_name} (Expected {expected_length} digits, got '{value}')")
                continue
            try:
                week_str = "".join(digits[i] for i in week_indices)
                day_str = "".join(digits[i] for i in day_indices)
                week_num = int(week_str)
                day_num = int(day_str)
            except (IndexError, ValueError):
                failed_zones.append(f"{check_name} (Could not parse '{value}')")
                continue
            if not (1 <= week_num <= 53) or not (1 <= day_num <= 7):
                failed_zones.append(f"{check_name} ('{value}' -> week {week_str} / day {day_str} not valid)")
            else:
                passed_zones += 1
                passed_zone_names.append(check_name)

        # Visual judgment on specific graphics (e.g. a 100% Vegetarian logo
        # that must print in actual color, versus an FSSAI logo that's
        # allowed to be black-and-white) -- one Gemini call covers every
        # configured logo, mirroring extract_label_fields' one-call-for-
        # every-field batching.
        if logo_checks:
            logo_specs = {c["name"]: c.get("description", c["name"]) for c in logo_checks}
            logo_results = check_label_logos(extracted_bytes, logo_specs, vision_llm_api_key)
            for check in logo_checks:
                check_name = check["name"]
                require_color = check.get("require_color", False)
                result = logo_results.get(check_name, {})
                if not result.get("present"):
                    failed_zones.append(f"{check_name} (Missing from label)")
                elif require_color and not result.get("color"):
                    failed_zones.append(f"{check_name} (Must be printed in color, appears black-and-white)")
                else:
                    passed_zones += 1
                    passed_zone_names.append(check_name)

        # Fold extracted fields/text blocks into the same dict the old
        # zone-crop system populates, purely so the audit trail (uploaded to
        # S3 as _content.json by the caller) still captures what was found
        # on a layout-agnostic label, not just an empty zone_content.
        for name, value in {**extracted_fields, **text_blocks}.items():
            zone_content.setdefault(name, [str(value)] if value is not None else [])

        total_regions = (
            len(target_regions) + len(full_label_checks) + len(lookup_rules)
            + len(field_checks) + len(text_block_checks) + len(size_prompt_checks)
            + len(color_tag_checks) + len(allowed_variety_group_checks)
            + len(date_comparison_checks) + len(arithmetic_checks) + len(field_equality_checks)
            + len(week_day_code_checks) + len(logo_checks)
        )
        passed_list_str = "\n".join([f"  - ✅ {pz}" for pz in passed_zone_names]) if passed_zone_names else "  - None"
        warnings_block = (
            f"• Warnings:\n" + "\n".join([f"  - ⚠️ {w}" for w in warnings]) + "\n"
        ) if warnings else ""
        label_type_line = f"• Label Type: *{variant_suffix.capitalize()}*\n" if variant_suffix else ""

        if not failed_zones and passed_zones == total_regions:
            return "PASS", (
                f"✅ *Layout Verification PASSED*\n\n"
                f"• Spec Reference: *{spec_code}*\n"
                f"{label_type_line}"
                f"• Target Zones Checked: *{total_regions} blocks*\n"
                f"• Passed Blocks:\n{passed_list_str}\n"
                f"{warnings_block}\n"
                f"_All required zones matched their expected content._"
            ), zone_content
        else:
            failed_list = "\n".join([f"  - ❌ {fz}" for fz in failed_zones])
            return "FAIL", (
                f"❌ *Layout Verification FAILED*\n\n"
                f"• Spec Reference: *{spec_code}*\n"
                f"{label_type_line}"
                f"• Passed Blocks:\n{passed_list_str}\n"
                f"• Failed Blocks:\n{failed_list}\n"
                f"{warnings_block}\n"
                f"_Please fill in the missing sections on the label._"
            ), zone_content

    except Exception as e:
        logger.error(f"Targeted layout content comparison failed: {str(e)}")
        return "ERROR", f"❌ *Layout Verification FAILED*\n\n• Spec Reference: *{spec_code}*\n• Reason: Error performing block layout comparison.", {}


def handle_text_message(sender, msg, access_token, phone_number_id, sender_name=None):
    message_id = msg.get('id')
    raw_text = msg.get('text', {}).get('body', '').strip().upper()
    spec_code = raw_text if SPEC_CODE_PATTERN.match(raw_text) else None
    spec_images = get_spec_images(spec_code) if spec_code else None

    if spec_images:
        send_whatsapp_message(sender, f"Fetching spec sheets for *{spec_code}*...", access_token, phone_number_id)

        cache_buster = int(time.time())

        try:
            allowed_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=spec_images['allowed_key'])
            allowed_bytes = allowed_obj['Body'].read()
            send_whatsapp_image_bytes(sender, allowed_bytes, "image/png", f"📋 {spec_code} - Allowed Spec [{cache_buster}]", access_token, phone_number_id)
        except Exception as ex:
            logger.error(f"Failed to send allowed spec image: {str(ex)}")

        try:
            label_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=spec_images['label_key'])
            label_bytes = label_obj['Body'].read()
            send_whatsapp_image_bytes(sender, label_bytes, "image/png", f"🏷️ {spec_code} - Label Example [{cache_buster}]", access_token, phone_number_id)
        except Exception as ex:
            logger.error(f"Failed to send label example image: {str(ex)}")

        pdf_key = get_spec_pdf(spec_code)
        if pdf_key:
            try:
                pdf_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=pdf_key)
                pdf_bytes = pdf_obj['Body'].read()
                send_whatsapp_document_bytes(
                    sender, pdf_bytes, f"{spec_code}_specsheet.pdf",
                    f"📄 {spec_code} - Full Pack Specification", access_token, phone_number_id
                )
            except Exception as ex:
                logger.error(f"Failed to send spec sheet PDF: {str(ex)}")

        send_whatsapp_message(sender, f"✅ All available information for spec *{spec_code}* has been sent.", access_token, phone_number_id)
        write_audit_record(sender, message_id, 'text', spec_code, 'SPEC_SENT', sender_name=sender_name)
    else:
        display_code = raw_text[:32] if raw_text else ''
        send_whatsapp_message(sender, f"❌ Could not find spec *{display_code}*. Please check the code and try again.", access_token, phone_number_id)
        write_audit_record(sender, message_id, 'text', display_code or None, 'SPEC_NOT_FOUND', sender_name=sender_name)


def handle_image_message(sender, msg, access_token, phone_number_id, sender_name=None):
    message_id = msg.get('id')
    image_data = msg.get('image', {})
    image_id = image_data.get('id')
    raw_caption = image_data.get('caption', '').strip().upper()
    spec_code = raw_caption if SPEC_CODE_PATTERN.match(raw_caption) else None

    if not spec_code:
        send_whatsapp_message(
            sender,
            "⚠️ Please include a valid Spec Code (e.g., *9A*) in the photo caption when uploading a label.",
            access_token,
            phone_number_id
        )
        write_audit_record(sender, message_id, 'image', raw_caption[:32] if raw_caption else None, 'NO_SPEC_CODE', sender_name=sender_name)
        return

    send_whatsapp_message(
        sender,
        f"🔍 Analyzing label layout against Spec *{spec_code}*...",
        access_token,
        phone_number_id
    )

    image_bytes, mime_type = download_whatsapp_media(image_id, access_token)

    if image_bytes:
        file_ext = 'png' if 'png' in mime_type else 'jpg'
        s3_key = f"incoming/{sender}_{spec_code}_{image_id}.{file_ext}"
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=image_bytes,
            ContentType=mime_type
        )

        status, layout_report, zone_content = validate_label_layout(
            image_bytes, spec_code, debug_key_prefix=f"{sender}_{spec_code}_{image_id}"
        )

        content_key = f"incoming/{sender}_{spec_code}_{image_id}_content.json"
        try:
            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=content_key,
                Body=json.dumps(zone_content).encode('utf-8'),
                ContentType='application/json'
            )
        except Exception as ex:
            logger.error(f"Failed to store extracted content JSON for {message_id}: {str(ex)}")
            content_key = None

        send_whatsapp_message(sender, layout_report, access_token, phone_number_id)
        write_audit_record(sender, message_id, 'image', spec_code, status, layout_report, image_key=s3_key, content_key=content_key, sender_name=sender_name)
    else:
        send_whatsapp_message(
            sender,
            "❌ Failed to download image from Meta servers. Please try again.",
            access_token,
            phone_number_id
        )
        write_audit_record(sender, message_id, 'image', spec_code, 'DOWNLOAD_FAILED', sender_name=sender_name)


def lambda_handler(event, context):
    http_method = event.get('requestContext', {}).get('http', {}).get('method') or event.get('httpMethod')

    if http_method == 'GET':
        query_params = event.get('queryStringParameters') or {}
        verify_token = get_ssm_param('/whatsapp/verify_token')
        if (verify_token
                and query_params.get('hub.mode') == 'subscribe'
                and hmac.compare_digest(query_params.get('hub.verify_token', ''), verify_token)):
            return {'statusCode': 200, 'body': query_params.get('hub.challenge', '')}
        logger.error("Webhook GET verification failed: mode/token mismatch")
        return {'statusCode': 403, 'body': 'Verification failed'}

    raw_body = event.get('body', '{}')
    if event.get('isBase64Encoded'):
        raw_body_bytes = base64.b64decode(raw_body)
    else:
        raw_body_bytes = raw_body.encode('utf-8') if isinstance(raw_body, str) else raw_body

    app_secret = get_ssm_param('/whatsapp/app_secret')
    if not app_secret or not verify_webhook_signature(event, raw_body_bytes, app_secret):
        logger.error("Webhook POST signature verification failed; rejecting request")
        return {'statusCode': 403, 'body': 'Invalid signature'}

    body = json.loads(raw_body_bytes.decode('utf-8'))

    if 'entry' not in body:
        return {'statusCode': 200, 'body': 'OK'}

    access_token = get_ssm_param('/whatsapp/access_token')
    phone_number_id = get_ssm_param('/whatsapp/phone_number_id')

    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})

                if 'statuses' in value and 'messages' not in value:
                    continue

                messages = value.get('messages', [])
                if not messages:
                    continue

                msg = messages[0]
                sender = msg.get('from')
                msg_type = msg.get('type')
                msg_id = msg.get('id')

                contacts = value.get('contacts', [])
                sender_name = contacts[0].get('profile', {}).get('name') if contacts else None

                logger.info(f"Processing message type '{msg_type}' from {sender} ({sender_name})")

                if msg_id and not mark_message_processed(msg_id):
                    logger.info(f"Duplicate delivery for message {msg_id} from {sender}, skipping")
                    continue

                if is_first_time_sender(sender):
                    send_whatsapp_message(sender, WELCOME_MESSAGE, access_token, phone_number_id)

                try:
                    if msg_type == 'text':
                        handle_text_message(sender, msg, access_token, phone_number_id, sender_name=sender_name)
                    elif msg_type == 'image':
                        handle_image_message(sender, msg, access_token, phone_number_id, sender_name=sender_name)
                    else:
                        send_whatsapp_message(
                            sender,
                            "🤖 I can only process text spec codes or label photos right now.",
                            access_token,
                            phone_number_id
                        )
                        write_audit_record(sender, msg_id, msg_type, None, 'UNSUPPORTED_TYPE', sender_name=sender_name)
                except Exception as ex:
                    logger.error(f"Error processing message {msg_id} from {sender}: {str(ex)}", exc_info=True)
                    send_whatsapp_message(
                        sender,
                        "⚠️ Something went wrong processing your request. Please try again.",
                        access_token,
                        phone_number_id
                    )
                    write_audit_record(sender, msg_id, msg_type, None, 'ERROR', str(ex), sender_name=sender_name)

        return {'statusCode': 200, 'body': 'EVENT_RECEIVED'}

    except Exception as e:
        logger.error(f"FATAL ERROR in lambda_handler: {str(e)}", exc_info=True)
        return {'statusCode': 200, 'body': 'ERROR_LOGGED'}
