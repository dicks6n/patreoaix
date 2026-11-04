"""NFP / CPI / FOMC prediction emails (store/news_email.py).

    python manage.py news_email --test you@gmail.com        # send the current release's email to one address now
    python manage.py news_email --test you@gmail.com --stage result
    python manage.py news_email --html scanner.html         # save the email (+ reactor.png) to look at
    python manage.py news_email --status                    # which release, and how many people would get it
    python manage.py news_email --send-now                  # send it to every registered user now (normally automatic)
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from store import news_email as ne, news_live


class Command(BaseCommand):
    help = 'Send or preview the NFP / CPI / FOMC prediction email.'

    def add_arguments(self, parser):
        parser.add_argument('--test', metavar='EMAIL')
        parser.add_argument('--html', metavar='FILE')
        parser.add_argument('--stage', choices=['preview', 'result'])
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--send-now', action='store_true')

    def pick(self, stage=None):
        board = news_live.board()
        row, st = ne.due(board)
        if row and (not stage or stage == st):
            return row, st
        # nothing due right now: use the next tracked release (or the latest released one for a result email)
        rows = [r for r in board.get('events') or [] if r['key'] in ne.keys() and r.get('markets')]
        if stage == 'result':
            rows = [r for r in rows if r['state'] == 'released'] or rows
        else:
            rows = [r for r in rows if r['state'] != 'released'] or rows
        if not rows:
            raise CommandError('No NFP / CPI / FOMC release in the calendar right now (the calendar feed may be down).')
        r = rows[0]
        return r, stage or ('result' if r['state'] == 'released' else 'preview')

    def handle(self, *a, test=None, html=None, stage=None, status=False, send_now=False, **kw):
        if status or send_now:
            row, st = ne.due()
            if not row:
                row, st = self.pick(stage)             # nothing in its automatic window: the next release
                when = 'next release, sent early by hand'
            else:
                when = 'due now'
            ref = ne.ref_of(row, st)
            people = ne.recipients(ref, 100000)
            room = max(0, ne.daily_cap() - ne.sent_today())
            self.stdout.write(f"{row['label']} · {row['when'][:16].replace('T', ' ')} UTC · {st} email ({when})")
            self.stdout.write(f"Still to get it: {len(people)} people · sent today {ne.sent_today()} / daily cap {ne.daily_cap()}"
                              f" → this run can send {min(room, len(people))}")
            if not send_now:
                return
            if not people:
                self.stdout.write('Everybody already has this one.')
                return
            if not room:
                raise CommandError('Daily cap reached (Gmail allows about 500 a day). The rest go out tomorrow, or raise NEWS_EMAIL_DAILY_CAP in .env.')
            n = ne.send_batch(row, st, limit=room)
            self.stdout.write(self.style.SUCCESS(f'Sent {n} emails.') if n else self.style.ERROR(
                'Sent 0. Email is not working: run  python manage.py email_doctor  and fix what it says.'))
            if n and n < min(room, len(people)):
                self.stdout.write(f'Stopped after {n}: Gmail refused more for now. Run --send-now again later; nobody gets it twice.')
            return
        row, st = self.pick(stage)
        if html:
            msg = ne.build(row, st, 'preview@example.com')
            body = msg.alternatives[0][0].replace('cid:reactor', 'reactor.png').replace('cid:logo', 'logo.png')
            Path(html).write_text(body)
            Path(html).with_name('reactor.png').write_bytes(ne.reactor_png(row, st))
            from store import emails
            if emails.LOGO.exists():
                Path(html).with_name('logo.png').write_bytes(emails.LOGO.read_bytes())
            self.stdout.write(self.style.SUCCESS(f'Saved {html} ({row["label"]}, {st}). Subject: {msg.subject}'))
        if test:
            from django.contrib.auth.models import User
            user = User.objects.filter(email__iexact=test).first()
            ne.build(row, st, test, user).send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f'Sent the {st} email for {row["label"]} to {test}.'))
        if not html and not test:
            self.stdout.write('Use --test EMAIL, --html FILE, --status or --send-now.')
