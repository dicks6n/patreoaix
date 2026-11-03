# store/account_adapter.py
"""allauth emails (password reset and the rest) in the PATREOAIX branded template (store/emails.py)."""
import re

from allauth.account.adapter import DefaultAccountAdapter
from django.template.loader import render_to_string


class PatreoaixAccountAdapter(DefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
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
