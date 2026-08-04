import base64
import difflib
import hashlib
import hmac
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


def align_zone_locally(matcher, box, zone_name="", upscale=2):
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
    if not fit_source.startswith("local"):
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


def call_gemini_vision(image_bytes, prompt, api_key):
    b64_image = base64.b64encode(image_bytes).decode('utf-8')
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": b64_image}}
                ]
            }
        ],
        "generationConfig": {"temperature": 0}
    }
    data = json.dumps(payload).encode('utf-8')
    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['candidates'][0]['content']['parts'][0]['text']
    except urllib.error.HTTPError as e:
        logger.error(f"Gemini vision API error: Status {e.code} - {e.read().decode('utf-8')}")
        return None
    except Exception as e:
        logger.error(f"Gemini vision API call failed: {str(e)}")
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
    ref_label_key = f"specs/{spec_code}_label.png"
    config_key = f"specs/{spec_code}_config.json"

    try:
        ref_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=ref_label_key)
        ref_image_bytes = ref_obj['Body'].read()
    except ClientError:
        return "ERROR", f"❌ *Layout Verification FAILED*\n\n• Spec Reference: *{spec_code}*\n• Reason: Reference layout not found in S3.", {}

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

    matcher = build_orb_matcher(ref_image_bytes, extracted_bytes)
    if matcher is None:
        return "ERROR", f"❌ *Layout Verification FAILED*\n\n• Spec Reference: *{spec_code}*\n• Reason: Could not process uploaded image.", {}

    target_regions = [
        {"name": "Full Label", "box": {"Left": 0.0, "Top": 0.0, "Width": 1.0, "Height": 1.0}}
    ]

    try:
        config_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=config_key)
        config_data = json.loads(config_obj['Body'].read().decode('utf-8'))
        if 'target_regions' in config_data:
            target_regions = config_data['target_regions']
    except ClientError:
        logger.info(f"No custom config found for {spec_code}, falling back to full-image scan.")

    vision_llm_api_key = None
    if any(zone.get("use_vision_llm") for zone in target_regions):
        vision_llm_api_key = get_ssm_param('/whatsapp/gemini_api_key')

    try:
        failed_zones = []
        passed_zones = 0
        passed_zone_names = []
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

            crop_candidates = align_zone_locally(matcher, box, zone_name=zone_name)

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

        total_regions = len(target_regions)
        passed_list_str = "\n".join([f"  - ✅ {pz}" for pz in passed_zone_names]) if passed_zone_names else "  - None"

        if not failed_zones and passed_zones == total_regions:
            return "PASS", (
                f"✅ *Layout Verification PASSED*\n\n"
                f"• Spec Reference: *{spec_code}*\n"
                f"• Target Zones Checked: *{total_regions} blocks*\n"
                f"• Passed Blocks:\n{passed_list_str}\n\n"
                f"_All required zones matched their expected content._"
            ), zone_content
        else:
            failed_list = "\n".join([f"  - ❌ {fz}" for fz in failed_zones])
            return "FAIL", (
                f"❌ *Layout Verification FAILED*\n\n"
                f"• Spec Reference: *{spec_code}*\n"
                f"• Passed Blocks:\n{passed_list_str}\n"
                f"• Failed Blocks:\n{failed_list}\n\n"
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
