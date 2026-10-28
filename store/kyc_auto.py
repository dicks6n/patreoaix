# store/kyc_auto.py
"""
Automatic identity verification (like Remitly / Wise): the customer scans their ID and takes a selfie on the
provider's secure page; the provider checks the document, liveness and face match, and the account is approved
or declined automatically. Provider: Didit (didit.me), whose core KYC is free.

Setup (once):
  1. business.didit.me → sign up → create an application.
  2. Create a verification workflow (ID verification + liveness + face match) → copy its Workflow ID.
  3. API & Webhooks: copy the API key and the webhook secret; set the webhook URL to
        https://patreoaix.com/kyc/webhook/
  4. .env:  DIDIT_API_KEY=…   DIDIT_WORKFLOW_ID=…   DIDIT_WEBHOOK_SECRET=…   then reload the site.

Without DIDIT_API_KEY the site keeps the manual upload form (reviewed by staff at /admin/kyc/).

Security: the decision is always read from Didit's API with our key. The customer's return link and the webhook
only tell us *which* session to look up, so neither can be forged to approve an account.
"""
import hashlib
import hmac
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

API = 'https://verification.didit.me/v2/'
APPROVED, DECLINED = ('approved',), ('declined', 'rejected', 'failed')
REVIEW = ('in review', 'in_review', 'review', 'resubmitted')


def _cfg(name, default=''):
    return str(getattr(settings, name, '') or default)


def enabled():
    return bool(_cfg('DIDIT_API_KEY') and _cfg('DIDIT_WORKFLOW_ID'))


def _headers():
    return {'x-api-key': _cfg('DIDIT_API_KEY'), 'Content-Type': 'application/json', 'Accept': 'application/json'}


def start_session(user, callback_url):
    """Create a verification session → (session_id, url to send the customer to)."""
    import requests
    r = requests.post(API + 'session/', headers=_headers(), timeout=20, json={
        'workflow_id': _cfg('DIDIT_WORKFLOW_ID'),
        'vendor_data': str(user.pk),
        'callback': callback_url,
    })
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 300 or not body.get('url'):
        raise RuntimeError(body.get('detail') or body.get('message') or f'verification service error ({r.status_code})')
    return str(body.get('session_id') or body.get('id') or ''), body['url']


def fetch_decision(session_id):
    """The provider's own record of the session (never trust the browser or the webhook body for this)."""
    import requests
    r = requests.get(f'{API}session/{session_id}/decision/', headers=_headers(), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f'verification service error ({r.status_code})')
    return r.json()


def _reason(decision):
    """A short, customer-safe reason for a decline."""
    for part in ('id_verification', 'liveness', 'face_match', 'aml'):
        block = decision.get(part) or {}
        if str(block.get('status', '')).lower() in DECLINED:
            warn = block.get('warnings') or []
            first = warn[0] if warn and isinstance(warn[0], dict) else {}
            text = first.get('short_description') or first.get('long_description') or first.get('risk') or ''
            label = {'id_verification': 'ID document', 'liveness': 'Selfie / liveness check',
                     'face_match': 'Face match', 'aml': 'Screening'}[part]
            return f'{label}: {text}' if text else f'{label} could not be verified.'
    return 'The verification was not approved. Please try again with a clear photo of a valid ID.'


def apply_decision(kyc, decision):
    """Update the KYC record from the provider's decision. Returns the new status."""
    from .models import Notification
    status = str(decision.get('status') or '').strip().lower()
    old = kyc.status
    kyc.provider_status = status[:40]
    idv = decision.get('id_verification') or {}
    if idv.get('document_type'):
        kyc.document_type = str(idv['document_type'])[:50]
    if idv.get('document_number'):
        kyc.id_number = str(idv['document_number'])[:50]
    if status in APPROVED:
        kyc.status, kyc.admin_notes = 'APPROVED', ''
        kyc.verified_at = kyc.verified_at or timezone.now()
    elif status in DECLINED:
        kyc.status, kyc.admin_notes = 'REJECTED', _reason(decision)
    else:                                             # in progress / in review / not finished
        kyc.status = 'PENDING'
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
    """Look the session up again (return link, webhook, or the status page while pending)."""
    if not (enabled() and kyc and kyc.provider == 'didit' and kyc.provider_session):
        return kyc.status if kyc else None
    return apply_decision(kyc, fetch_decision(kyc.provider_session))


def signature_ok(raw_body, signature):
    secret = _cfg('DIDIT_WEBHOOK_SECRET')
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature).strip().lower())
