# store/kyc_auto.py
"""
Automatic identity verification with Didit (didit.me), API v3. The customer scans their ID and takes a selfie on
Didit's hosted page; Didit checks the document, liveness and face match and approves or declines automatically.

Flow:
  1. start_session(): our server POSTs the KYC workflow_id to https://verification.didit.me/v3/session/ with the
     x-api-key header → {url, session_id}; the customer is sent to `url` (Didit's hosted flow).
  2. Didit POSTs a signed webhook to /kyc/webhook/ → verify_webhook() checks X-Signature-V2 (HMAC-SHA256 of the
     canonical JSON: whole floats as ints, keys sorted, Unicode unescaped), then the SIGNED body `timestamp`
     (≤ 300 s old; the X-Timestamp header is not signed so it is never used), then dedupes on event_id.
     The webhook is the source of truth for the decision.
  3. When the customer comes back (callback) we also read GET /v3/session/{id}/decision/ server-side with our key,
     so the status page is up to date even before the webhook lands. The callback URL itself proves nothing.

Configuration (.env, server-side only, never committed):
    DIDIT_API_KEY=…            long-lived API key (x-api-key)
    DIDIT_WEBHOOK_SECRET=…     signing secret of the webhook destination (console → Webhooks)
The workflow is per-session config, not a secret: WORKFLOW_ID below ("Free KYC").
Webhook destination URL: https://www.patreoaix.com/kyc/webhook/  (webhook_version v3, events status.updated, data.updated)

Without DIDIT_API_KEY the site keeps the manual upload form (reviewed by staff at /admin/kyc/).
"""
import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

API = 'https://verification.didit.me/v3/'
WORKFLOW_ID = '92348e66-430e-4922-8330-ea5336dc6c33'      # "Free KYC" (ID + liveness + face match)
WEBHOOK_MAX_AGE = 300

# Didit's literal session statuses (case-sensitive) → our KYC status
APPROVED = {'Approved'}
DECLINED = {'Declined'}
WAITING = {'Not Started', 'In Progress', 'Awaiting User', 'In Review'}
RESUBMIT = {'Resubmitted'}
RESTART = {'Abandoned', 'Expired', 'Kyc Expired'}         # the customer starts a fresh session


def _cfg(name, default=''):
    return str(getattr(settings, name, '') or default)


def workflow_id():
    return _cfg('DIDIT_WORKFLOW_ID') or WORKFLOW_ID           # an older .env value still wins if set


def enabled():
    return bool(_cfg('DIDIT_API_KEY'))


def _headers():
    return {'x-api-key': _cfg('DIDIT_API_KEY'), 'Content-Type': 'application/json', 'Accept': 'application/json'}


# ═══════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════
def start_session(user, callback_url):
    """Create a verification session server-side → (session_id, url to send the customer to)."""
    import requests
    r = requests.post(API + 'session/', headers=_headers(), timeout=20, json={
        'workflow_id': workflow_id(),
        'vendor_data': str(user.pk),                          # our stable internal user id
        'callback': callback_url,                             # where Didit sends the customer back
    })
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 403:
        raise RuntimeError('Didit refused the API key (check DIDIT_API_KEY in .env)')
    if r.status_code >= 300 or not body.get('url'):
        raise RuntimeError(body.get('detail') or body.get('message') or f'verification service error ({r.status_code})')
    return str(body.get('session_id') or ''), body['url']


def fetch_decision(session_id):
    """Didit's own record of the session, read with our key (GET /v3/session/{id}/decision/)."""
    import requests
    r = requests.get(f'{API}session/{session_id}/decision/', headers=_headers(), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f'verification service error ({r.status_code})')
    return r.json()


# ═══════════════════════════════════════════════════════════════
# DECISIONS (V3 plural arrays, indexed by node)
# ═══════════════════════════════════════════════════════════════
def _items(decision, key):
    v = (decision or {}).get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _reason(decision):
    """A short, customer-safe reason for a decline."""
    labels = [('id_verifications', 'ID document'), ('liveness_checks', 'Selfie / liveness check'),
              ('face_matches', 'Face match'), ('aml_screenings', 'Screening'), ('ip_analyses', 'Device / IP check')]
    for key, label in labels:
        for item in _items(decision, key):
            if str(item.get('status', '')).lower() in ('declined', 'rejected', 'failed'):
                warn = item.get('warnings') or []
                first = warn[0] if warn and isinstance(warn[0], dict) else {}
                text = first.get('short_description') or first.get('long_description') or first.get('risk') or ''
                return f'{label}: {text}' if text else f'{label} could not be verified.'
    return 'The verification was not approved. Please try again with a clear photo of a valid ID.'


def apply_decision(kyc, payload):
    """
    Update the KYC record from a Didit status (webhook body or GET …/decision/). `payload` has `status`
    and, for Approved / Declined / In Review / Abandoned, a `decision` object (the decision endpoint returns
    the decision fields at the top level, which is handled too). Returns the new KYC status.
    """
    from .models import Notification
    status = str(payload.get('status') or '').strip()
    decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else payload
    old = kyc.status
    kyc.provider_status = status[:40]
    idv = (_items(decision, 'id_verifications') or [{}])[0]
    if idv.get('document_type'):
        kyc.document_type = str(idv['document_type'])[:50]
    if idv.get('document_number'):
        kyc.id_number = str(idv['document_number'])[:50]
    if status in APPROVED:
        kyc.status, kyc.admin_notes = 'APPROVED', ''
        kyc.verified_at = kyc.verified_at or timezone.now()
    elif status in DECLINED:
        kyc.status, kyc.admin_notes = 'REJECTED', _reason(decision)
    elif status in RESUBMIT:
        kyc.status = 'PENDING'
        kyc.admin_notes = 'Didit asks you to redo some steps. Open the verification again to continue.'
    elif status in RESTART:
        if status == 'Kyc Expired':
            kyc.verified_at = None
        kyc.status, kyc.provider_session = 'PENDING', ''
        kyc.admin_notes = ('Your verification has expired. Please verify again.' if status == 'Kyc Expired'
                           else 'The verification was not finished. Start it again when you are ready.')
    elif status in WAITING or not status:
        kyc.status = 'PENDING' if old != 'APPROVED' or status == 'In Review' else old
    else:
        logger.warning(f'Didit: unknown status {status!r} for KYC {kyc.pk}')
    kyc.save()
    if kyc.status != old and kyc.status in ('APPROVED', 'REJECTED'):
        approved = kyc.status == 'APPROVED'
        if approved:
            from .referral_rewards import safe as referral_reward
            referral_reward(kyc.user, 'verified their identity')
        try:
            Notification.objects.create(
                user=kyc.user, category='KYC', priority='success' if approved else 'error',
                title='Identity verified ✅' if approved else 'Verification not approved',
                message='Your identity has been verified automatically. Withdrawals and funded payouts are unlocked.'
                        if approved else f'{kyc.admin_notes} You can try again from the Verification page.',
                action_url='/kyc/status/', action_label='View')
        except Exception as e:
            logger.warning(f'KYC notification failed: {e}')
        try:
            from .sms_service import sms_service
            sms_service.send_kyc_status(kyc.user, kyc.status, '' if approved else kyc.admin_notes)
        except Exception:
            pass
    return kyc.status


def refresh(kyc):
    """Look the session up again (return link, or the status page while pending)."""
    if not (enabled() and kyc and kyc.provider == 'didit' and kyc.provider_session):
        return kyc.status if kyc else None
    return apply_decision(kyc, fetch_decision(kyc.provider_session))


# ═══════════════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════════════
def _shorten_floats(v):
    """Whole-number floats (1.0) → integers (1), recursively: Didit's canonicalisation."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _shorten_floats(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_shorten_floats(x) for x in v]
    return v


def canonical(body):
    """shortenFloats → sortKeys → compact JSON with unescaped Unicode (what JS JSON.stringify produces)."""
    return json.dumps(_shorten_floats(body), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def verify_webhook(raw_body, headers, now=None):
    """
    → (body, None) for a genuine, fresh webhook, or (None, reason).
    Order: parse → canonicalise → constant-time HMAC-SHA256 vs X-Signature-V2 → signed body `timestamp` ≤ 300 s.
    """
    secret = _cfg('DIDIT_WEBHOOK_SECRET')
    if not secret:
        return None, 'DIDIT_WEBHOOK_SECRET is not set'
    try:
        body = json.loads(raw_body.decode('utf-8') if isinstance(raw_body, (bytes, bytearray)) else raw_body)
    except (ValueError, UnicodeDecodeError):
        return None, 'invalid JSON'
    if not isinstance(body, dict):
        return None, 'invalid body'
    sig = str(headers.get('X-Signature-V2') or headers.get('x-signature-v2') or '').strip().lower()
    expected = hmac.new(secret.encode(), canonical(body), hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(expected, sig):
        return None, 'bad signature'
    try:
        ts = int(float(body.get('timestamp')))
    except (TypeError, ValueError):
        return None, 'no timestamp'
    if abs((now or time.time()) - ts) > WEBHOOK_MAX_AGE:
        return None, 'stale'
    return body, None


def first_delivery(body):
    """Idempotency: True the first time an event_id is seen (unique DB constraint, safe under retries)."""
    from django.db import IntegrityError, transaction
    from .models import DiditWebhookEvent
    event_id = str(body.get('event_id') or f"{body.get('session_id')}:{body.get('webhook_type')}:{body.get('timestamp')}")[:100]
    try:
        with transaction.atomic():
            DiditWebhookEvent.objects.create(event_id=event_id, session_id=str(body.get('session_id') or '')[:100],
                                             status=str(body.get('status') or '')[:40])
        return True
    except IntegrityError:
        return False


def handle_webhook(body):
    """Apply a verified webhook to the customer's KYC record."""
    from django.contrib.auth.models import User
    from .models import KYCProfile
    if str(body.get('webhook_type') or 'status.updated') not in ('status.updated', 'data.updated'):
        return None
    sid = str(body.get('session_id') or '')
    kyc = KYCProfile.objects.filter(provider='didit', provider_session=sid).first() if sid else None
    if kyc is None and str(body.get('vendor_data') or '').isdigit():       # our user id
        user = User.objects.filter(pk=int(body['vendor_data'])).first()
        if user:
            kyc, _ = KYCProfile.objects.get_or_create(user=user, defaults={'status': 'PENDING', 'provider': 'didit'})
            if not kyc.provider_session and sid:
                kyc.provider, kyc.provider_session = 'didit', sid
    if kyc is None:
        logger.info(f'Didit webhook for unknown session {sid}')
        return None
    if kyc.status == 'APPROVED' and str(body.get('status')) not in APPROVED | DECLINED | {'Kyc Expired'}:
        return kyc.status                                     # don't downgrade a verified customer on progress events
    return apply_decision(kyc, body)
