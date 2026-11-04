# store/news_email.py
"""
NFP / CPI / FOMC prediction emails, designed like the News Scanner page.

  preview   a few hours before the release: countdown time, forecast vs previous, beat / miss odds, and the lean
            for each market (BUY / SELL, chance, usual move in pips)
  result    right after the number is out: BEAT / MISS, the locked call for each market and the expected pips

The picture at the top is drawn here (the reactor: glowing core with the best odds, orbit rings, one dot per
market: higher = more likely to rise) and attached inline, so it shows in Gmail, Outlook and phone apps.

Sending: every active user with a real email who did not switch these emails off (Notification settings, or the
one-click unsubscribe link in each email). The background scanner calls safe_tick(); each tick sends a small batch
over one SMTP connection, and NewsEmailLog makes sure nobody gets the same email twice, even after a restart.
Gmail allows about 500 emails a day from one account: NEWS_EMAIL_DAILY_CAP (default 450) keeps under it, most
recently active users first.

    python manage.py news_email --test you@gmail.com      # send the current one to yourself now
    python manage.py news_email --html out.html           # save the email as a file to look at
"""
import io
import logging
import math
from datetime import datetime, timedelta, timezone as dt_tz
from email.mime.image import MIMEImage

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.template.loader import render_to_string
from django.utils import timezone

from . import emails, news_history as nh

logger = logging.getLogger(__name__)
SALT = 'news-email-off'
PREVIEW_HOURS = 3            # preview goes out in the last 3 hours before the release
RESULT_HOURS = 2             # result goes out up to 2 hours after
BATCH = 25                   # emails per scanner pass


def _cfg(name, default):
    return getattr(settings, name, None) or default


def enabled():
    return str(_cfg('NEWS_EMAIL_ENABLED', 'True')).lower() in ('1', 'true', 'yes')


def keys():
    return [k.strip().upper() for k in str(_cfg('NEWS_EMAIL_KEYS', 'NFP,CPI,FOMC')).split(',') if k.strip()]


def daily_cap():
    try:
        return int(_cfg('NEWS_EMAIL_DAILY_CAP', 450))
    except (TypeError, ValueError):
        return 450


# ═══════════════════════════════════════════════════════════════
# WHICH RELEASE, WHICH STAGE
# ═══════════════════════════════════════════════════════════════
def due(board=None):
    """→ (row, stage) for the release that should be emailed now, or (None, None)."""
    from . import news_live
    board = board or news_live.board()
    for r in board.get('events') or []:
        if r['key'] not in keys() or not r.get('markets'):
            continue
        if r['state'] == 'upcoming' and 5 * 60 < r['seconds'] <= PREVIEW_HOURS * 3600:
            return r, 'preview'
        if r['state'] == 'released' and -RESULT_HOURS * 3600 <= r['seconds'] <= 0:
            return r, 'result'
    return None, None


def ref_of(row, stage):
    return f"{row['id']}:{stage}"[:60]


# ═══════════════════════════════════════════════════════════════
# THE PICTURE (reactor, like the page)
# ═══════════════════════════════════════════════════════════════
def reactor_png(row, stage):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    from .poster import font
    W, H = 1120, 600
    img = Image.new('RGB', (W, H), (5, 7, 12))
    glow = Image.new('RGB', (W, H), (0, 0, 0))
    g, d = ImageDraw.Draw(glow), ImageDraw.Draw(img)
    cx, cy = W // 2, H // 2 + 10
    released = stage == 'result'
    res = row.get('result')
    core = (52, 211, 153) if released and res == 'BEAT' else (244, 63, 94) if released and res == 'MISS' else (245, 179, 1)
    # backdrop: dotted grid + sweep
    for x in range(0, W, 28):
        for y in range(0, H, 28):
            d.point((x, y), fill=(22, 34, 44))
    for a in range(0, 50):
        ang = math.radians(-40 + a * .7)
        g.line([(cx, cy), (cx + math.cos(ang) * 520, cy + math.sin(ang) * 230)], fill=(8, 60 - a // 2, 44 - a // 3), width=3)
    # orbit rings (ellipses = tilted circles)
    for i, (rx, col) in enumerate([(210, (245, 179, 1)), (300, (52, 211, 153)), (390, (56, 189, 248))]):
        ry = int(rx * .36)
        g.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=tuple(int(c * .55) for c in col), width=3)
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=tuple(int(c * .35) for c in col), width=1)
    # core glow
    for r, k in ((150, .18), (110, .3), (82, .55)):
        g.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(int(c * k) for c in core))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(26)))     # soft glow under the crisp drawing
    d = ImageDraw.Draw(img)
    # core sphere + wireframe hint
    d.ellipse([cx - 86, cy - 86, cx + 86, cy + 86], fill=tuple(int(c * .35) for c in core), outline=core, width=4)
    for k in range(6):
        ang = math.radians(k * 30)
        d.ellipse([cx - 86 * abs(math.cos(ang)), cy - 86, cx + 86 * abs(math.cos(ang)), cy + 86], outline=tuple(int(c * .6) for c in core), width=1)
    best = max([m['prob'] for m in row['markets']] or [50])
    t = f'{best}%'
    f1 = font(64)
    tw = d.textlength(t, font=f1)
    d.text((cx - tw / 2, cy - 50), t, font=f1, fill=(255, 255, 255))
    lab = (res or 'LOCKED') if released else 'BEST ODDS'
    f2 = font(20)
    d.text((cx - d.textlength(lab, font=f2) / 2, cy + 24), lab, font=f2, fill=(253, 230, 138))
    # satellites: one per market, higher = more likely to rise. Dots first, then labels placed where they fit.
    ms = row['markets']
    taken = [(cx - 100, cy - 90, cx + 100, cy + 90)]                 # the core and its number
    dots = []
    for i, m in enumerate(ms):
        up = m['prob'] if m['call'] == 'BUY' else (100 - m['prob'] if m['call'] == 'SELL' else 50)
        ang = math.radians(165 + i * (230 / max(1, len(ms) - 1)) if len(ms) > 1 else 270)
        rx = 300 + 70 * (i % 2)
        x = cx + math.cos(ang) * rx
        y = cy + 20 + math.sin(ang) * rx * .36 - (up - 50) * 3.0
        x, y = max(60, min(W - 60, x)), max(70, min(H - 70, y))
        col = (52, 211, 153) if m['call'] == 'BUY' else (251, 113, 133) if m['call'] == 'SELL' else (148, 163, 184)
        for rr, k in ((34, .25), (24, .45)):
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=tuple(int(c * k) for c in col))
        d.ellipse([x - 15, y - 15, x + 15, y + 15], fill=col)
        taken.append((x - 18, y - 18, x + 18, y + 18))
        dots.append((x, y, col, m))
    f3 = font(30)

    def free(r):
        return all(r[2] < t[0] or r[0] > t[2] or r[3] < t[1] or r[1] > t[3] for t in taken)
    for x, y, col, m in dots:
        call = m['call'] if released else ('▲' if m['call'] == 'BUY' else '▼' if m['call'] == 'SELL' else '•')
        label = f"{m['symbol']}  {call} {m['prob']}%"
        bw, bh = d.textlength(label, font=f3) + 28, 48
        spots = [(x - bw / 2, y - 30 - bh), (x - bw / 2, y + 30), (x + 30, y - bh / 2), (x - 30 - bw, y - bh / 2),
                 (x - bw / 2, y - 90 - bh), (x - bw / 2, y + 90)]
        box = None
        for lx, ly in spots:
            lx, ly = max(8, min(W - bw - 8, lx)), max(6, min(H - bh - 40, ly))
            r = (lx, ly, lx + bw, ly + bh)
            if free(r):
                box = r
                break
        box = box or (max(8, min(W - bw - 8, x - bw / 2)), max(6, y - 30 - bh), 0, 0)
        lx, ly = box[0], box[1]
        taken.append((lx, ly, lx + bw, ly + bh))
        d.rounded_rectangle([lx, ly, lx + bw, ly + bh], radius=12, fill=(6, 10, 20), outline=col, width=3)
        d.text((lx + 14, ly + 8), label, font=f3, fill=(255, 255, 255))
    # caption
    f4 = font(21, bold=False)
    cap = 'Higher dot = more likely to rise   ·   green BUY   ·   red SELL'
    d.text((cx - d.textlength(cap, font=f4) / 2, H - 30), cap, font=f4, fill=(120, 130, 150))
    out = io.BytesIO()
    img.save(out, 'PNG', optimize=True)
    return out.getvalue()


# ═══════════════════════════════════════════════════════════════
# THE EMAIL
# ═══════════════════════════════════════════════════════════════
def unsubscribe_url(user):
    return f"{emails.site_url()}/email/news-off/{signing.dumps(user.pk, salt=SALT)}/"


def context(row, stage, name=''):
    when = datetime.fromisoformat(row['when'])
    released = stage == 'result'
    ms = []
    for m in row['markets']:
        price = m.get('price')
        ms.append(dict(m, price_txt=(f'{price:,.5f}'.rstrip('0').rstrip('.') if isinstance(price, (int, float)) else ''), color='#16C784' if m['call'] == 'BUY' else '#EA3943' if m['call'] == 'SELL' else '#94A3B8',
                       bar=max(4, min(100, m['prob'])),
                       arrow='▲' if m['call'] == 'BUY' else '▼' if m['call'] == 'SELL' else '•'))
    short = row['label'].split(' (')[0]
    if released:
        subject = f"{row['key'].replace('_', ' ')} is out: {row.get('result') or ''} · signals locked"
        headline = f"{short}: {row.get('result', '')}"
    else:
        mins = max(1, round(row['seconds'] / 60))
        left = f'{mins} min' if mins < 60 else (f'{mins // 60} h' + (f' {mins % 60:02d} min' if mins % 60 else ''))
        subject = f"{row['key'].replace('_', ' ')} in {left}: the AI scanner's calls"
        headline = f"{short}: the scanner's calls"
    return {
        'subject': subject, 'headline': headline, 'row': row, 'stage': stage, 'released': released, 'markets': ms,
        'when_utc': when.astimezone(dt_tz.utc).strftime('%A %d %b, %H:%M GMT'),
        'when_eat': when.astimezone(dt_tz(timedelta(hours=3))).strftime('%H:%M EAT'),
        'beat': row['p_beat'], 'miss': row['p_miss'], 'name': name, 'site': emails.site_url(),
        'scanner_url': emails.site_url() + '/news-impact/', 'history_url': f"{emails.site_url()}/news/{row['key'].lower().replace('_', '-')}/",
    }


def build(row, stage, to, user=None):
    from django.core.mail import EmailMultiAlternatives
    ctx = context(row, stage, name=(user.first_name or user.username) if user else '')
    ctx['unsubscribe'] = unsubscribe_url(user) if user else ctx['site'] + '/notifications/#settings'
    html = render_to_string('emails/news_scanner.html', ctx)
    text = render_to_string('emails/news_scanner.txt', ctx)
    msg = EmailMultiAlternatives(ctx['subject'], text, emails.from_address(), [to],
                                 headers={'List-Unsubscribe': f"<{ctx['unsubscribe']}>"})
    msg.attach_alternative(html, 'text/html')
    msg.mixed_subtype = 'related'
    for cid, data in (('logo', emails.LOGO.read_bytes() if emails.LOGO.exists() else None), ('reactor', reactor_png(row, stage))):
        if not data:
            continue
        img = MIMEImage(data, 'png')
        img.add_header('Content-ID', f'<{cid}>')
        img.add_header('Content-Disposition', 'inline', filename=f'{cid}.png')
        msg.attach(img)
    return msg


# ═══════════════════════════════════════════════════════════════
# WHO GETS IT
# ═══════════════════════════════════════════════════════════════
def wants(user):
    from . import notif_prefs as NP
    p = NP.get(user)
    return p.get('email', True) and p.get('news_email', True)


def recipients(ref, limit):
    """Active users with a real email who haven't had this one, most recently active first."""
    from django.contrib.auth.models import User
    from .models import NewsEmailLog
    done = NewsEmailLog.objects.filter(ref=ref).values_list('user_id', flat=True)
    out = []
    qs = (User.objects.filter(is_active=True).exclude(email='').exclude(email__iendswith='.invalid')
          .exclude(pk__in=done).order_by('-last_login', '-date_joined'))
    for u in qs.iterator():
        if emails.deliverable(u.email) and wants(u):
            out.append(u)
            if len(out) >= limit:
                break
    return out


def sent_today():
    from .models import NewsEmailLog
    return NewsEmailLog.objects.filter(sent_at__date=timezone.now().date()).count()


def send_batch(row, stage, limit=BATCH):
    """Send the next batch over one SMTP connection. Returns how many were sent."""
    from django.core.mail import get_connection
    from .models import NewsEmailLog
    room = daily_cap() - sent_today()
    if room <= 0:
        return 0
    ref = ref_of(row, stage)
    users = recipients(ref, min(limit, room))
    if not users:
        return 0
    sent = 0
    conn = get_connection(fail_silently=False)
    try:
        conn.open()
        for u in users:
            try:
                conn.send_messages([build(row, stage, u.email, u)])
                NewsEmailLog.objects.get_or_create(user=u, ref=ref)
                sent += 1
            except Exception as e:
                logger.warning(f'news email {ref} to {u.email}: {e}')
                if 'Authentication' in type(e).__name__ or 'Disconnected' in type(e).__name__:
                    break                              # Gmail stopped us: try again on a later pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return sent


def tick():
    if not enabled():
        return 0
    row, stage = due()
    if not row:
        return 0
    return send_batch(row, stage)


def safe_tick():
    """Called by the background scanner on every pass; works at most once a minute."""
    if not cache.add('news_email:tick', 1, 60):
        return 0
    try:
        return tick()
    except Exception as e:
        logger.warning(f'news email tick failed: {e}')
        return 0
