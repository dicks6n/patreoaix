"""
Find out exactly why the site can't send email, step by step, then send a real test email.

    python manage.py email_doctor                      # check everything
    python manage.py email_doctor --send you@gmail.com # check, then send the branded test email

Never prints the password (only its length).
"""
import os
import smtplib
import socket
import ssl

from django.conf import settings
from django.core.management.base import BaseCommand

GOOD, BAD, INFO = '✅', '❌', '•'


class Command(BaseCommand):
    help = 'Check the email settings and the connection to Gmail step by step, then optionally send a test email.'

    def add_arguments(self, parser):
        parser.add_argument('--send', metavar='EMAIL', help='Send the branded test email to this address when the checks pass')

    def say(self, icon, text):
        self.stdout.write(f'{icon} {text}')

    def handle(self, *args, send=None, **kw):
        host = getattr(settings, 'EMAIL_HOST', 'smtp.gmail.com')
        user = getattr(settings, 'EMAIL_HOST_USER', '')
        pw = (getattr(settings, 'EMAIL_HOST_PASSWORD', '') or '').replace(' ', '')
        self.stdout.write('\n── 1. Settings ──')
        self.say(INFO, f'backend  {settings.EMAIL_BACKEND}')
        self.say(INFO, f'server   {host}  port {settings.EMAIL_PORT}  ssl={settings.EMAIL_USE_SSL} tls={settings.EMAIL_USE_TLS}')
        self.say(INFO, f'user     {user}')
        self.say(INFO, f'from     {settings.DEFAULT_FROM_EMAIL}')
        if 'console' in settings.EMAIL_BACKEND or 'locmem' in settings.EMAIL_BACKEND:
            self.say(BAD, 'The site is not set up to really send email (console backend). Check that .env is loaded.')
            return
        if len(pw) != 16 or not pw.isalpha():
            self.say(BAD, f'EMAIL_HOST_PASSWORD is {len(pw)} characters. A Gmail App Password is 16 letters '
                          '(spaces are ignored). Put it in .env as EMAIL_HOST_PASSWORD=abcdabcdabcdabcd and reload.')
        else:
            self.say(GOOD, 'App password looks right (16 letters).')

        self.stdout.write('\n── 2. This server ──')
        proxy = {k: v for k, v in os.environ.items() if k.lower() in ('http_proxy', 'https_proxy')}
        if proxy:
            self.say(BAD, 'This server sends web traffic through a proxy (' + ', '.join(f'{k}={v}' for k, v in proxy.items()) +
                     '). On PythonAnywhere that means a FREE account: outgoing email connections are blocked or cut off. '
                     'Upgrade the account that runs the site (Account → upgrade) and reload; the paid plan has open internet.')
        else:
            self.say(GOOD, 'No web proxy: open internet access (paid account).')

        self.stdout.write('\n── 3. Reaching Gmail ──')
        try:
            addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 587, proto=socket.IPPROTO_TCP)})
            self.say(GOOD, f'{host} found: {", ".join(addrs[:4])}')
        except Exception as e:
            self.say(BAD, f'Could not look up {host}: {e}')
            return

        results = {}
        for port in (587, 465):
            results[port] = self.try_port(host, port, user, pw)

        self.stdout.write('\n── Verdict ──')
        ok = [p for p, r in results.items() if r == 'ok']
        if ok:
            best = ok[0]
            if best != settings.EMAIL_PORT:
                self.say(BAD, f'Port {best} works but the site uses port {settings.EMAIL_PORT}. Put EMAIL_PORT={best} in .env and reload.')
            else:
                self.say(GOOD, f'Gmail accepted the login on port {best}. Email works.')
            if send:
                self.send_test(send)
            return
        if all(r == 'auth' for r in results.values()):
            self.say(BAD, 'Gmail is reachable but says the app password is wrong for ' + user + '.\n'
                          '   (App passwords stop working when the Google account password is changed, or if it was made on another Google account.)\n'
                          '   Make a NEW App Password, logged in as ' + user + ':\n'
                          '   myaccount.google.com → Security → 2-Step Verification (must be ON) → App passwords → create "PATREOAIX"\n'
                          '   copy the 16 letters into .env as EMAIL_HOST_PASSWORD, then reload the web app.')
        elif 'busy' in results.values():
            self.say(BAD, 'Gmail says too many login attempts from this server. Wait 1 to 2 hours without trying, then run this again.\n'
                          '   Also open the patreoaix@gmail.com inbox: if there is a "Sign-in attempt blocked" / "Critical security alert"\n'
                          '   email, open it and confirm it was you.')
        elif 'cut' in results.values():
            self.say(BAD, 'Gmail talks to this server but hangs up at login. Usually Google is blocking sign-ins from this server:\n'
                          '   open patreoaix@gmail.com → any "Critical security alert / sign-in blocked" email → "Yes, it was me",\n'
                          '   and check myaccount.google.com → Security → Recent security activity. Then make a NEW app password\n'
                          '   (myaccount.google.com/apppasswords), put it in .env, reload, and run this again.')
        elif all(r in ('closed', 'timeout', 'refused') for r in results.values()):
            self.say(BAD, 'This server cannot hold a connection to Gmail at all (it is cut before Gmail even says hello),\n'
                          '   so no password can work. The network of this server blocks email.\n'
                          '   On PythonAnywhere: make sure the account that RUNS the site is on a paid plan (Account page),\n'
                          '   then reload the web app and run this again. If it is paid and still blocked, ask PythonAnywhere\n'
                          '   (Help button on their site) to allow outgoing email to smtp.gmail.com.')
        else:
            self.say(BAD, 'Mixed results above: use the port marked ✅ if any, otherwise follow the hint on each line.')

    def try_port(self, host, port, user, pw):
        label = f'port {port} ({"SSL" if port == 465 else "STARTTLS"})'
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(host, port, timeout=20)
        except socket.timeout:
            self.say(BAD, f'{label}: no answer (timeout): outgoing email is blocked here.')
            return 'timeout'
        except ConnectionRefusedError:
            self.say(BAD, f'{label}: connection refused: outgoing email is blocked here.')
            return 'refused'
        except smtplib.SMTPServerDisconnected:
            self.say(BAD, f'{label}: cut off before Gmail said hello: something between this server and Gmail closes the line.')
            return 'closed'
        except Exception as e:
            self.say(BAD, f'{label}: {type(e).__name__}: {e}')
            return 'closed'
        try:
            banner = (s.ehlo()[1] or b'').decode(errors='ignore').splitlines()[0][:60]
            self.say(GOOD, f'{label}: Gmail answered ({banner})')
            if port == 587:
                s.starttls(context=ssl.create_default_context()); s.ehlo()
                self.say(GOOD, f'{label}: encryption on')
            # One login method only. smtplib's login() retries with a second method after a refusal, Gmail has
            # already hung up by then, and the real answer (535 wrong password) turns into "connection closed".
            s.user, s.password = user, pw
            code, resp = s.auth('PLAIN', s.auth_plain, initial_response_ok=True)
            reply = (resp or b'').decode(errors='ignore').replace('\n', ' ')[:160]
            if code in (235, 503):
                self.say(GOOD, f'{label}: LOGIN OK')
                s.quit()
                return 'ok'
            self.say(BAD, f'{label}: Gmail said {code}: {reply}')
            return 'auth' if code == 535 else ('busy' if code == 454 else 'other')
        except smtplib.SMTPAuthenticationError as e:
            reply = (e.smtp_error or b'').decode(errors='ignore').replace('\n', ' ')[:160]
            self.say(BAD, f'{label}: Gmail said {e.smtp_code}: {reply}')
            return 'auth' if e.smtp_code == 535 else ('busy' if e.smtp_code == 454 else 'other')
        except smtplib.SMTPServerDisconnected:
            self.say(BAD, f'{label}: Gmail answered, then hung up at the login step without a reason.')
            return 'cut'
        except Exception as e:
            self.say(BAD, f'{label}: {type(e).__name__}: {e}')
            return 'other'

    def send_test(self, to):
        from store import emails
        try:
            emails.send(to, 'PATREOAIX test email', 'Email is working', background=False,
                        intro='If you can read this, the site can send emails: OTP codes, deposits, withdrawals and password resets.',
                        button=('Open PATREOAIX', '/'), tone='good')
            self.say(GOOD, f'Test email sent to {to}. Check the inbox (and the spam folder the first time).')
        except Exception as e:
            self.say(BAD, f'Sending failed: {type(e).__name__}: {e}')
