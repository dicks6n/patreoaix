# store/account_adapter.py
"""allauth emails (password reset and the rest) in the PATREOAIX branded template (store/emails.py)."""
import logging
import re

from allauth.account.adapter import DefaultAccountAdapter
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


class PatreoaixAccountAdapter(DefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
        """Never let a mail problem crash the page: log it and tell the visitor to try again later."""
        try:
            self._send_branded(template_prefix, email, context)
        except Exception as e:
            logger.warning(f'allauth email {template_prefix} to {email} failed: {e}')
            if getattr(self, 'request', None) is not None:
                self.request._px_mail_failed = True      # no "email sent" message after this (add_message below)
            try:
                from django.contrib import messages
                if getattr(self, 'request', None) is not None:
                    messages.error(self.request, "We couldn't send the email right now. Please try again in a few minutes, "
                                                 "or contact support if it keeps happening.")
            except Exception:
                pass

    def add_message(self, request, level, message_template=None, message_context=None, extra_tags='', message=None):
        """Skip allauth's "email sent" messages when the email did not go out."""
        if getattr(request, '_px_mail_failed', False) and 'sent' in str(message_template or ''):
            return
        return super().add_message(request, level, message_template=message_template, message_context=message_context,
                                   extra_tags=extra_tags, message=message)

    def _send_branded(self, template_prefix, email, context):
        from . import emails
        if not emails.deliverable(email):
            return
        user = context.get('user')
        name = (getattr(user, 'first_name', '') or getattr(user, 'username', '')) if user else ''
        if template_prefix.endswith('password_reset_key'):
            emails.send(email, 'Reset your PATREOAIX password', 'Reset your password', background=False, name=name,
                        intro=f'Someone (hopefully you) asked to reset the password for your PATREOAIX account'
                              f'{" (" + user.username + ")" if user else ""}. Choose a new password with the button below.',
                        button=('Choose a new password', context['password_reset_url']), tone='info',
                        note="The link works once. If you didn't ask for this, ignore this email: your password stays the same.")
            return
        if 'email_confirmation' in template_prefix and context.get('activate_url'):
            emails.send(email, 'Confirm your email for PATREOAIX', 'Confirm your email', background=False, name=name,
                        intro=f'Tap the button to confirm {email} for your PATREOAIX account. Confirmed emails can reset '
                              f'passwords and receive receipts.', button=('Confirm my email', context['activate_url']),
                        note="Didn't ask for this? Ignore this email.")
            return
        # any other allauth email: its own subject and text, in the branded layout
        subject = render_to_string(f'{template_prefix}_subject.txt', context).strip() or 'PATREOAIX'
        try:
            body = render_to_string(f'{template_prefix}_message.txt', context).strip()
        except Exception:
            return super().send_mail(template_prefix, email, context)
        link = re.search(r'https?://\S+', body)
        emails.send(email, subject, subject, background=False, name=name,
                    intro=re.sub(r'https?://\S+', '', body).strip(),
                    button=('Open link', link.group(0)) if link else None)
