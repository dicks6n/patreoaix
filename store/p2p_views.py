# store/p2p_views.py
"""Pages of the P2P marketplace (logic in store/p2p.py)."""
import json
import mimetypes

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from . import p2p, p2p_desk, p2p_markets, p2p_rates, transfers
from .models import P2PAd, P2PMessage, P2POrder, P2PPaymentMethod


def _ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _common(request):
    """Shared context: mode, balances, open orders badge."""
    u = request.user
    open_n = P2POrder.objects.filter(Q(buyer=u) | Q(seller=u), status__in=('PENDING', 'PAID', 'APPEAL')).count()
    bals = p2p.balances(u)
    lvl = p2p.seller_level(u)
    return {'live': p2p.live(), 'fee_pct': p2p.fee_pct(), 'open_n': open_n, 'assets': list(p2p.ASSETS.keys()),
            'bals': [(a, p2p.fmt(a, b)) for a, b in bals.items()], 'can_trade': p2p.can_trade(u),
            'sell_level': lvl, 'sell_level_name': p2p.LEVELS[lvl], 'high_risk': sorted(p2p.HIGH_RISK)}


@login_required
def market(request):
    p2p.safe_expire()
    if request.method == 'POST':
        act = request.POST.get('action')
        try:
            if act == 'unlock_request':
                p2p.request_unlock(request.user)
                messages.success(request, 'Request sent to support. You will get a notification as soon as buying is unlocked.')
                return redirect(request.get_full_path())
            if act == 'express':
                o = p2p.express(request.user, request.POST.get('side'), (request.POST.get('asset') or 'USDT').upper(),
                                (request.POST.get('cur') or 'KES').upper(), request.POST.get('fiat'))
                return redirect('p2p_order', ref=o.ref)
            if act == 'take':
                method_id = request.POST.get('method')
                if not method_id and request.POST.get('new_method'):      # selling without a saved method: add it now
                    method_id = p2p.add_method(request.user, {
                        'method': request.POST.get('new_method'), 'account_name': request.POST.get('account_name'),
                        'account_number': request.POST.get('account_number'), 'bank_name': request.POST.get('bank_name')}).id
                o = p2p.open_order(request.user, request.POST.get('ad'), fiat=request.POST.get('fiat'),
                                   coins=request.POST.get('coins'), method_id=method_id)
                return redirect('p2p_order', ref=o.ref)
        except p2p.P2PError as e:
            messages.error(request, str(e))
        return redirect(request.get_full_path())
    side = 'sell' if request.GET.get('side') == 'sell' else 'buy'          # what the visitor wants to do
    asset = (request.GET.get('asset') or 'USDT').upper()
    asset = asset if asset in p2p.ASSETS else 'USDT'
    cur = (request.GET.get('cur') or request.COOKIES.get('p2p_cur') or 'KES').upper()
    cur = cur if cur in p2p.CURRENCIES or cur == 'ANY' else 'KES'
    every = cur == 'ANY'                             # all currencies: every trader's offers, ranked by reputation
    method = request.GET.get('method') or ''
    if not every:
        p2p_rates.refresh_pair(asset, cur)      # this pair only, cached rates: no waiting on the network
    qs = P2PAd.objects.filter(status='ACTIVE', asset=asset, demo=False, **({} if every else {'currency': cur}),
                              side='SELL' if side == 'buy' else 'BUY').exclude(user=request.user) \
        .exclude(user_id__in=p2p.blocked_ids(request.user)).select_related('user')
    ads = [a for a in qs.order_by('-updated_at' if every else ('price' if side == 'buy' else '-price'))[:300 if every else 100]
           if not method or method in a.methods]
    stock = {}
    for a in ads:                                    # hide desk sell offers the desk cannot fill even at the minimum
        if a.pool:
            k = (a.user_id, a.asset)
            if k not in stock:
                stock[k] = p2p.balance(a.user, a.asset)
            a._stock = stock[k]
    ads = [a for a in ads if not a.pool or a._stock * a.price >= a.min_fiat]
    levels = {}                                      # sell ads only while the owner's plan allows them (Pro / VIP)
    for a in ads:
        if a.side == 'SELL' and a.user_id not in levels:
            levels[a.user_id] = p2p.seller_level(a.user)
    ads = [a for a in ads if a.side != 'SELL' or levels[a.user_id] == 'vip'
           or (levels[a.user_id] == 'pro' and not any(k in p2p.HIGH_RISK for k in a.methods))]
    try:
        want = float(str(request.GET.get('amount') or '').replace(',', '')) if request.GET.get('amount') else None
    except ValueError:
        want = None
    if want and not every:                           # an amount only means something in one currency
        ads = [a for a in ads if float(a.min_fiat) <= want <= float(a.max_fiat) and want <= float(a.remaining * a.price)]
    stats = p2p.stats_map([a.user for a in ads])
    if every:                                        # best traders first: completion, feedback, then 30-day trades
        def score(a):
            st = stats[a.user_id]
            return (a.user.is_staff, (st['completion'] or 0) * .5 + (st['positive'] or 0) * .3 + min(st['trades_30d'], 100) * .2,
                    st['trades'])
        ads.sort(key=score, reverse=True)
        ads = ads[:100]
    for rank, a in enumerate(ads, 1):
        a.rank = rank
    mine = list(P2PPaymentMethod.objects.filter(user=request.user, is_active=True))
    my_lvl = p2p.seller_level(request.user)
    for a in ads:
        a.my_methods = [(m.id, f'{p2p.method_name(m.method)} · {m.account_number}') for m in mine
                        if m.method in a.methods and (my_lvl == 'vip' or (my_lvl == 'pro' and m.method not in p2p.HIGH_RISK))]
        a.risky = any(k in p2p.HIGH_RISK for k in a.methods)
        a.method_opts = [(k, p2p.method_name(k), p2p.METHODS.get(k, ('', 'Number'))[1]) for k in a.methods]
        a.st = stats[a.user_id]
        a.method_names = [p2p.method_name(m) for m in a.methods]
        if a.pool:                                   # desk sell offer: what the desk really holds right now
            a.remaining = min(a.remaining, a._stock)
        a.remaining_txt = p2p.fmt(a.asset, a.remaining)
        a.max_now = min(a.max_fiat, p2p._q(a.remaining * a.price))
        a.desk = a.user.is_staff
        a.sample = p2p.is_sample(a.user)            # sample trader from p2p_seed: labelled, not tradable
    ctx = dict(_common(request), side=side, asset=asset, cur=cur, method=method, ads=ads, amount=request.GET.get('amount', ''),
               currency_groups=p2p.currency_groups(), methods=[] if every else p2p.methods_for(cur), every=every,
               cur_name='All currencies' if every else p2p.CURRENCIES[cur][0], cur_flag='🌍' if every else p2p.CURRENCIES[cur][1],
               my_methods=list(P2PPaymentMethod.objects.filter(user=request.user, is_active=True)),
               method_names=dict((k, v[0]) for k, v in p2p.METHODS.items()), tab='market',
               market_now=None if every else p2p_rates.market_price_fast(asset, cur),
               buy_pause=p2p.buy_pause(request.user) if side == 'buy' else None, now=timezone.now())
    resp = render(request, 'store/p2p_market.html', ctx)
    resp.set_cookie('p2p_cur', cur, max_age=365 * 86400, samesite='Lax')
    return resp


@login_required
def my_ads(request):
    p2p.safe_expire()
    form_error, form_data = None, {}
    if request.method == 'POST':
        act = request.POST.get('action')
        if act == 'post':                       # errors are shown inside the ad form, with what was typed kept
            try:
                ad = p2p.post_ad(request.user, request.POST)
                messages.success(request, f'Ad posted: {ad.get_side_display()} {p2p.fmt(ad.asset, ad.total)} {ad.asset} '
                                          f'at {ad.price:,} {ad.currency}. It is live in the market now.')
                return redirect(reverse('p2p_ads') + '#myads')
            except p2p.P2PError as e:
                form_error, form_data = str(e), request.POST
        try:
            if act in ('pause', 'activate', 'close'):
                p2p.set_ad_status(request.user, request.POST.get('ad'), {'pause': 'PAUSED', 'activate': 'ACTIVE', 'close': 'CLOSED'}[act])
                messages.success(request, {'pause': 'Ad paused.', 'activate': 'Ad is live again.', 'close': 'Ad closed; locked coins returned.'}[act])
            elif act == 'add_method':
                m = p2p.add_method(request.user, request.POST)
                messages.success(request, f'{p2p.method_name(m.method)} added.')
            elif act == 'del_method':
                P2PPaymentMethod.objects.filter(user=request.user, pk=request.POST.get('id')).update(is_active=False)
                messages.info(request, 'Payment method removed.')
        except p2p.P2PError as e:
            messages.error(request, str(e))
        if not form_error:
            return redirect(reverse('p2p_ads') + ('#methods' if 'method' in (act or '') else '#myads'))
    ads = list(P2PAd.objects.filter(user=request.user).exclude(status='CLOSED').order_by('-created_at'))
    for a in ads:
        a.method_names = [p2p.method_name(m) for m in a.methods]
        a.remaining_txt, a.total_txt = p2p.fmt(a.asset, a.remaining), p2p.fmt(a.asset, a.total)
    methods = list(P2PPaymentMethod.objects.filter(user=request.user, is_active=True))
    for m in methods:
        m.label = p2p.method_name(m.method)
        m.risky = m.method in p2p.HIGH_RISK
        m.currency = 'any' if m.method in p2p_markets.UNIVERSAL else ' '.join(c for c, v in p2p.CURRENCIES.items() if m.method in v[3])
    return render(request, 'store/p2p_ads.html', dict(
        _common(request), ads=ads, methods=methods, tab='ads', time_limits=p2p.TIME_LIMITS,
        currency_groups=p2p.currency_groups(),
        all_methods={c: p2p.methods_for(c) for c in p2p.CURRENCIES}, stats=p2p.merchant_stats(request.user),
        form_error=form_error, f=form_data, f_methods=form_data.getlist('methods') if form_data else [],
        f_pay=form_data.getlist('pay_methods') if form_data else []))


@login_required
def my_orders(request):
    p2p.safe_expire()
    u = request.user
    orders = list(P2POrder.objects.filter(Q(buyer=u) | Q(seller=u)).select_related('buyer', 'seller')[:100])
    for o in orders:
        o.i_buy = o.buyer_id == u.id
        o.other = o.seller if o.i_buy else o.buyer
        o.amount_txt = p2p.fmt(o.asset, o.amount)
    return render(request, 'store/p2p_orders.html', dict(_common(request), orders=orders, tab='orders'))


@login_required
def order_page(request, ref):
    p2p.safe_expire()
    try:
        o = p2p._get(request.user, ref, lock=False, staff=request.user.is_staff)
    except p2p.P2PError:
        raise Http404
    if request.method == 'POST':
        act = request.POST.get('action')
        try:
            if act == 'paid':
                p2p.mark_paid(request.user, ref)
            elif act == 'cancel':
                p2p.cancel(request.user, ref)
            elif act == 'release':
                if request.POST.get('confirm') != 'yes':
                    raise p2p.P2PError('Tick the box to confirm you received the money in your account.')
                p2p.release(request.user, ref, password=request.POST.get('password'))
            elif act == 'block':
                other = o.seller if o.buyer_id == request.user.id else o.buyer
                p2p.block(request.user, other.username)
                messages.info(request, f'{other.username} is blocked: you will not see their ads and they cannot take yours.')
            elif act == 'appeal':
                p2p.appeal(request.user, ref, request.POST.get('reason', ''))
            elif act == 'feedback':
                p2p.feedback(request.user, ref, request.POST.get('positive') == '1', request.POST.get('comment', ''))
                messages.success(request, 'Thanks for the feedback.')
            elif act == 'ask_details':
                p2p.ask_details(request.user, ref)
                messages.success(request, 'Asked the seller for payment details. Their answer appears in the chat.')
            elif act == 'send_details':
                p2p.send_details(request.user, ref, request.POST)
                messages.success(request, 'Your payment details are on this trade now. The buyer sees them right away.')
            elif act == 'message':
                m = p2p.message(request.user, ref, request.POST.get('text', ''), request.FILES.get('image'))
                if _ajax(request):
                    return JsonResponse({'success': True, 'id': m.id})
            elif act in ('resolve_release', 'resolve_refund') and request.user.is_staff:
                note = request.POST.get('note', '')
                if act == 'resolve_release':
                    p2p.release(request.user, ref, staff=True, note=note)
                else:
                    p2p.resolve_refund(request.user, ref, note)
                messages.success(request, 'Appeal resolved.')
        except p2p.P2PError as e:
            if _ajax(request):
                return JsonResponse({'success': False, 'error': str(e)})
            messages.error(request, str(e))
        return redirect('p2p_order', ref=ref)
    me = request.user
    i_buy = o.buyer_id == me.id
    other = o.seller if i_buy else o.buyer
    left = max(0, int((o.expires_at - timezone.now()).total_seconds())) if o.status == 'PENDING' else 0
    pay_form = []
    if o.seller_id == me.id and o.status == 'PENDING':          # the seller's details form, pre-filled from saved methods
        saved = {m.method: m for m in P2PPaymentMethod.objects.filter(user=me, is_active=True)}
        current = (o.pay_to or [{}])[0]
        for k in (o.ad.methods or []):
            m, nm = saved.get(k), p2p.method_name(k)
            now_on = current.get('method') == nm
            pay_form.append({'key': k, 'name': nm, 'label': p2p.METHODS.get(k, ('', 'Number'))[1],
                             'number': current.get('number') if now_on else (m.account_number if m else ''),
                             'holder': current.get('name') if now_on else (m.account_name if m else me.get_full_name()),
                             'bank': (current.get('bank') if now_on else (m.bank_name if m else '')) or '', 'on': now_on})
        if pay_form and not any(f['on'] for f in pay_form):
            pay_form[0]['on'] = True
    import hashlib
    pay_v = hashlib.md5(json.dumps(o.pay_to or [], sort_keys=True).encode()).hexdigest()[:10]
    appeal_wait = 0                                        # buyer: minutes until the dispute button opens
    if o.status == 'PAID' and i_buy and o.paid_at:
        appeal_wait = max(0, int(((o.paid_at + timezone.timedelta(minutes=10)) - timezone.now()).total_seconds() // 60) + 1)
    can_appeal = o.status == 'PAID' and (not i_buy or (o.paid_at and timezone.now() >= o.paid_at + timezone.timedelta(minutes=10)))
    return render(request, 'store/p2p_order.html', dict(
        _common(request), o=o, i_buy=i_buy, i_sell=o.seller_id == me.id, other=other, left=left, tab='orders',
        other_stats=p2p.merchant_stats(other), amount_txt=p2p.fmt(o.asset, o.amount),
        receive_txt=p2p.fmt(o.asset, o.amount - o.fee), fee_txt=p2p.fmt(o.asset, o.fee), can_appeal=can_appeal,
        pay_form=pay_form, pay_v=pay_v, appeal_wait=appeal_wait,
        my_feedback=o.feedback.filter(from_user=me).first(), staff_view=me.is_staff and me.id not in (o.buyer_id, o.seller_id)))


@login_required
def order_poll(request, ref):
    """Chat and status for the order page (polled every few seconds)."""
    try:
        o = p2p._get(request.user, ref, lock=False, staff=request.user.is_staff)
    except p2p.P2PError:
        return JsonResponse({'success': False}, status=404)
    if o.status == 'PENDING' and timezone.now() > o.expires_at:
        p2p.safe_expire()
        o.refresh_from_db()
    try:
        after = int(request.GET.get('after') or 0)
    except ValueError:
        after = 0
    rows = []
    for m in o.messages.filter(id__gt=after).select_related('sender'):
        rows.append({'id': m.id, 'me': m.sender_id == request.user.id, 'system': m.sender_id is None,
                     'who': m.sender.username if m.sender else 'PATREOAIX', 'text': m.text,
                     'image': reverse('p2p_proof', args=[m.id]) if m.image else None,
                     'pdf': bool(m.image and str(m.image.name).endswith('.pdf')),
                     'time': timezone.localtime(m.created_at).strftime('%H:%M')})
    import hashlib
    pay_v = hashlib.md5(json.dumps(o.pay_to or [], sort_keys=True).encode()).hexdigest()[:10]
    return JsonResponse({'success': True, 'status': o.status, 'status_label': o.get_status_display(), 'messages': rows, 'pay_v': pay_v,
                         'left': max(0, int((o.expires_at - timezone.now()).total_seconds())) if o.status == 'PENDING' else 0})


@login_required
def proof(request, msg_id):
    """Chat photos are private: only the two traders and staff can open them."""
    m = P2PMessage.objects.select_related('order').filter(pk=msg_id).first()
    if not m or not m.image or not (request.user.is_staff or request.user.id in (m.order.buyer_id, m.order.seller_id)):
        raise Http404
    ctype = mimetypes.guess_type(m.image.name)[0] or 'application/octet-stream'
    return FileResponse(m.image.open('rb'), content_type=ctype)


@staff_member_required
def admin_appeals(request):
    p2p.safe_expire()
    if request.method == 'POST' and request.POST.get('action') == 'unlock':
        from django.contrib.auth.models import User
        u = User.objects.filter(pk=request.POST.get('user')).first()
        if u:
            p2p.unlock_buying(request.user, u)
            messages.success(request, f'P2P buying unlocked for {u.username}. They were notified.')
        return redirect(reverse('p2p_admin') + '#paused')
    appeals = list(P2POrder.objects.filter(status='APPEAL').select_related('buyer', 'seller', 'appeal_by').order_by('created_at'))
    recent = list(P2POrder.objects.exclude(resolution='').select_related('buyer', 'seller', 'resolved_by').order_by('-completed_at', '-cancelled_at')[:20])
    for o in appeals + recent:
        o.amount_txt = p2p.fmt(o.asset, o.amount)
    stats = {
        'open': P2POrder.objects.filter(status__in=('PENDING', 'PAID')).count(),
        'done_7d': P2POrder.objects.filter(status='COMPLETED', completed_at__gte=timezone.now() - timezone.timedelta(days=7)).count(),
        'ads': P2PAd.objects.filter(status='ACTIVE').count(),
    }
    return render(request, 'store/p2p_admin.html', dict(_common(request), appeals=appeals, recent=recent, stats=stats, tab='admin',
                                                       paused=p2p.paused_buyers()))


@login_required
def trader(request, username):
    """Public trader profile: stats, open ads and recent feedback."""
    from django.contrib.auth.models import User
    from .models import P2PBlock, P2PFeedback
    u = User.objects.filter(username=username, is_active=True).first()
    if not u:
        raise Http404
    if request.method == 'POST' and u.id != request.user.id:
        on = request.POST.get('action') == 'block'
        p2p.block(request.user, u.username, on=on)
        messages.info(request, f'{u.username} {"blocked" if on else "unblocked"}.')
        return redirect('p2p_trader', username=u.username)
    ads = list(P2PAd.objects.filter(user=u, status='ACTIVE', demo=False).order_by('asset', 'currency'))
    for a in ads:
        a.method_names = [p2p.method_name(m) for m in a.methods]
        a.remaining_txt = p2p.fmt(a.asset, a.remaining)
    return render(request, 'store/p2p_trader.html', dict(
        _common(request), trader=u, stats=p2p.merchant_stats(u), ads=ads, tab='market',
        feedback=P2PFeedback.objects.filter(to_user=u).select_related('from_user').order_by('-created_at')[:20],
        blocked=P2PBlock.objects.filter(user=request.user, blocked=u).exists(), me=u.id == request.user.id,
        kyc=transfers.kyc_ok(u), joined=u.date_joined))


@login_required
def rate(request):
    """Live market price for the ad form: ?asset=BTC&cur=KES[&margin=2]."""
    asset, cur = (request.GET.get('asset') or 'USDT').upper(), (request.GET.get('cur') or 'KES').upper()
    if asset not in p2p.ASSETS or cur not in p2p.CURRENCIES:
        return JsonResponse({'success': False})
    m = p2p_rates.market_price(asset, cur)
    if not m:
        return JsonResponse({'success': False, 'error': 'No live rate right now'})
    try:
        margin = float(request.GET.get('margin') or 0)
    except ValueError:
        margin = 0
    return JsonResponse({'success': True, 'market': float(m), 'price': float(p2p_rates.with_margin(m, margin)), 'asset': asset, 'cur': cur})


@staff_member_required
def desk(request):
    """The admin's PATREOAIX Desk: post / refresh / pause offers in every currency for USDT, BTC and ETH."""
    report = None
    if request.method == 'POST':
        act = request.POST.get('action')
        try:
            if act == 'post':
                assets = [a for a in ('USDT', 'BTC', 'ETH') if request.POST.get('asset_' + a)] or ['USDT']
                curs = request.POST.get('currencies', '').upper().replace(' ', ',').split(',')
                curs = [c for c in curs if c] or None
                opts = {k: request.POST.get(k) for k in ('buy_margin', 'sell_margin', 'min_usd', 'max_usd', 'buy_capacity_usd', 'sell_capacity_usd', 'time_limit')}
                report = p2p_desk.post_all(request.user, assets, curs, opts)
                messages.success(request, f"Desk updated: {report['created']} new offers, {report['updated']} refreshed.")
            elif act in ('pause', 'resume'):
                n = p2p_desk.set_all(request.user, 'PAUSED' if act == 'pause' else 'ACTIVE')
                messages.info(request, f"{n} desk offers {'paused' if act == 'pause' else 'live again'}.")
        except p2p.P2PError as e:
            messages.error(request, str(e))
        if report is None:
            return redirect('p2p_desk')
    from .models import P2PPaymentMethod
    methods = [(p2p.method_name(m.method), m.account_number, 'every currency' if m.method in p2p_markets.UNIVERSAL else '')
               for m in P2PPaymentMethod.objects.filter(user=request.user, is_active=True)]
    rates_ok = len(p2p_rates.fx_table()) > 1
    return render(request, 'store/p2p_desk.html', dict(
        _common(request), tab='desk', summary=p2p_desk.summary(request.user), report=report, d=p2p_desk.DEFAULTS,
        methods=methods, rates_ok=rates_ok, coin={a: p2p_rates.coin_usd(a) for a in ('USDT', 'BTC', 'ETH')}))
