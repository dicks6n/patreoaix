# Evergreen beginner articles for /blog/ (Google: "how CPI affects gold", "gold trading Kenya", "what is leverage" …).
# Written once by this migration; edit them later in the admin (Blog posts). Removing the migration never deletes them.
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

RISK = ('<p class="note"><b>Risk warning:</b> trading gold and forex with leverage can lose money quickly. '
        'Only trade money you can afford to lose. This article is education, not financial advice.</p>')

ARTICLES = [
    ('how-cpi-moves-gold', 'How CPI moves gold (XAUUSD): a simple guide for traders',
     'What the US Consumer Price Index is, when it comes out, and why hot or cool inflation numbers push gold down or up within seconds.',
     '''<p>The <b>Consumer Price Index (CPI)</b> measures how fast prices rise in the United States. It is published every month by the
US Bureau of Labor Statistics at about <b>8:30 AM New York time</b> (15:30 or 16:30 in Nairobi, depending on US daylight saving), and it is
one of the biggest movers of gold all month.</p>
<h2>Why gold reacts to inflation numbers</h2>
<p>Gold pays no interest. When inflation runs hot, traders expect the US Federal Reserve to keep interest rates higher for longer.
Higher rates and higher bond yields make holding gold less attractive, and a stronger dollar makes gold more expensive for buyers in other
currencies. The usual chain is:</p>
<ul><li><b>CPI above forecast (hot):</b> rate-hike odds up → bond yields up → US dollar stronger → <b>gold tends to fall</b>.</li>
<li><b>CPI below forecast (cooling):</b> rate-cut odds up → yields down → dollar weaker → <b>gold tends to rise</b>.</li></ul>
<p>What matters is the <b>surprise</b>: the difference between the actual number and the forecast. A high CPI that was fully expected may
barely move gold. Traders watch <b>core CPI month-on-month</b> (without food and energy) most closely.</p>
<h2>How to trade CPI days more safely</h2>
<ul><li>Check the release time on the <a href="/calendar/">economic calendar</a> the day before.</li>
<li>In the first 1 to 5 minutes price can spike both ways and spreads widen. Many traders wait for the first candle to close.</li>
<li>Use a smaller lot than usual and always place a stop loss.</li>
<li>Read past reactions on the <a href="/news/">release results</a> pages to see how gold moved on earlier CPI days.</li></ul>
<p>Want to practise first? Open a free <a href="/trading-accounts/">demo account</a> and watch the next CPI release with no money at risk.</p>'''),

    ('nfp-and-gold', 'NFP and gold: what happens at 8:30 on jobs Friday',
     'Non-Farm Payrolls explained in plain words: what the jobs report measures, why strong jobs usually weigh on gold, and how to avoid the first-minute spikes.',
     '''<p><b>Non-Farm Payrolls (NFP)</b> is the number of jobs the US economy added last month, outside farming. It comes out on the
<b>first Friday of most months at about 8:30 AM New York time</b>, together with the unemployment rate and average hourly earnings.</p>
<h2>Why jobs data moves gold</h2>
<p>Gold is priced in US dollars. The jobs report changes what traders expect the Federal Reserve to do next, so the dollar, and gold,
can jump within seconds:</p>
<ul><li><b>Strong jobs (above forecast):</b> a strong economy lets the Fed stay tight → dollar stronger → <b>gold usually falls</b>.</li>
<li><b>Weak jobs (below forecast):</b> slowdown fears and rate-cut odds rise, plus safe-haven buying → <b>gold usually rises</b>.</li></ul>
<p>Watch all three numbers. Strong jobs with weak wages can give a mixed reaction, and big revisions to last month's figure matter too.</p>
<h2>NFP survival rules</h2>
<ul><li>Spreads are widest in the first minute. Market orders can fill far from the price you saw.</li>
<li>Price often spikes one way, then reverses. Waiting 5 to 15 minutes for a clear direction avoids the worst whipsaws.</li>
<li>Risk 1% or less of your account and keep the stop loss beyond the spike, not inside it.</li></ul>
<p>The <a href="/calendar/">economic calendar</a> shows the next NFP date and the forecast; past releases are on the
<a href="/news/">release results</a> pages.</p>'''),

    ('ppi-gold-inflation-early-warning', 'PPI and gold: the inflation early warning',
     'The Producer Price Index shows what factories and businesses charge. Here is why it can move gold before CPI does, and how traders use it.',
     '''<p>The <b>Producer Price Index (PPI)</b> measures the prices businesses receive for their goods and services: what it costs to make
things before they reach the shop. It is released monthly by the US Bureau of Labor Statistics, usually at <b>8:30 AM New York time</b>,
often a day before or after CPI.</p>
<h2>Why PPI matters for gold</h2>
<p>Factory prices today often become shop prices tomorrow. So PPI is a hint about the next CPI, and gold can react before the
inflation shows up in consumer prices:</p>
<ul><li><b>PPI above forecast:</b> costs rising → higher CPI expected → dollar stronger → <b>gold tends to fall</b>.</li>
<li><b>PPI below forecast:</b> costs easing → lower CPI expected → dollar weaker → <b>gold tends to rise</b>.</li></ul>
<p>PPI usually moves gold less than CPI or NFP, but a big surprise, especially in <b>core PPI</b>, can still cause a fast move.</p>
<h2>Using PPI in your plan</h2>
<ul><li>When PPI and CPI come out the same week, a hot PPI can make traders nervous before CPI.</li>
<li>Trade smaller around the release and keep a stop loss.</li>
<li>Learn the basics of news trading in the free <a href="/academy/">PATREOAIX Academy</a>.</li></ul>'''),

    ('gold-trading-for-beginners-kenya', 'Gold trading for beginners in Kenya: start small, the safe way',
     'How gold (XAUUSD) trading works, how much you need to start, what a lot and a pip are, and the habits that keep beginners in the game.',
     '''<p>Gold (symbol <b>XAUUSD</b>) is one of the most traded markets in the world and a favourite of traders in Kenya and across Africa:
it moves a lot every day and reacts clearly to big US news. Here is how to start without burning your account.</p>
<h2>How much do you need?</h2>
<p>Gold is traded in <b>lots</b>. The smallest common size, <b>0.01 lot, is 1 ounce</b>. For every $1 gold moves, 0.01 lot makes or
loses <b>$1</b>. With leverage of 1:500, the margin (deposit held) for 0.01 lot is roughly <b>$8 when gold is near $4,000</b>, so a
small account can open a trade. Small accounts should still stay at 0.01 lot: a $10 move against you is a $10 loss.</p>
<h2>Five habits of traders who last</h2>
<ol><li><b>Practise on a demo first.</b> A free <a href="/trading-accounts/">demo account</a> uses real prices with virtual money.</li>
<li><b>Risk 1–2% per trade.</b> On a $100 account that is $1–2, so pick your stop loss first and the lot size second.</li>
<li><b>Always use a stop loss.</b> Place it beyond the last swing, not a few cents away, or normal noise will hit it.</li>
<li><b>Know the news days.</b> CPI, NFP and the Fed move gold the most. Check the <a href="/calendar/">calendar</a>.</li>
<li><b>Keep a journal.</b> Write why you entered and how it ended; patterns in your mistakes appear within weeks.</li></ol>
<h2>Funding from Kenya</h2>
<p>On PATREOAIX you can fund your wallet with M-Pesa, through P2P or with USDT, and open a real or demo account with the leverage you
choose. Start with the <a href="/academy/">free Academy lessons</a>, then the demo, then a small real account.</p>'''),

    ('leverage-explained', 'Leverage explained: what 1:500 really means (and what it does not)',
     'Leverage decides how much margin a trade locks, not your profit per pip or the spread. A clear explanation with gold and EUR/USD examples.',
     '''<p><b>Leverage</b> lets you open a trade bigger than the money you put down. At <b>1:500</b>, every $1 of margin controls $500 of
the market. It is useful, and it is the most misunderstood setting in trading.</p>
<h2>What leverage changes</h2>
<p>Leverage only changes the <b>margin</b>: the part of your balance the broker holds while the trade is open. For 1 lot of EUR/USD
(€100,000): at 1:100 the margin is about $1,170, at 1:500 about $234. Lower margin means you can open trades with a smaller balance.</p>
<h2>What leverage does NOT change</h2>
<ul><li><b>Not the profit or loss per pip.</b> 0.01 lot of gold is $1 per $1 move at 1:10 and at 1:2000.</li>
<li><b>Not the spread.</b> The cost to open is the same at any leverage.</li></ul>
<h2>The real danger: lot size</h2>
<p>High leverage lets you open lots far too big for your balance. A $100 account can open 1 lot of gold at high leverage, and then a
$10 move wipes it out. The fix is simple: choose the lot from your stop loss and a 1–2% risk, never from "how much the margin allows".</p>
<p>On PATREOAIX you pick your leverage when you open an account, from 1:10 to 1:2000, with lower maximums for big balances. See
<a href="/accounts/leverage/">how leverage works on your account</a>.</p>'''),

    ('where-to-put-stop-loss', 'Where to put your stop loss so it is not hit too fast',
     'Most stops are hit by normal market noise, not by a wrong idea. How to place a stop beyond structure, size the trade, and avoid chasing.',
     '''<p>If your stop losses are hit within minutes, the problem is usually <b>where</b> the stop is, or <b>when</b> you entered, not the
direction you picked.</p>
<h2>1. Put the stop beyond structure</h2>
<p>Place a buy stop below the last <b>swing low</b> (the last clear dip), and a sell stop above the last swing high. Add a small buffer
beyond it: stop hunts often wick a little past obvious levels before the real move.</p>
<h2>2. Respect normal noise (ATR)</h2>
<p>The <b>Average True Range (ATR)</b> shows how far price normally moves per candle. A stop closer than about 1 ATR sits inside
normal noise and is likely to be touched even if your idea is right.</p>
<h2>3. Don't chase the move</h2>
<p>Buying right after a big green candle puts your entry at the top and your stop inside the next normal pullback. Wait for a pullback
into the 38–62% Fibonacci zone of the last leg and a sign that buyers are back. The PATREOAIX Entry Coach shows this as
<b>"Wait for the pullback"</b> or <b>"Entry confirmed"</b>.</p>
<h2>4. Size the trade from the stop</h2>
<p>Decide the stop first, then choose the lot so that hitting the stop costs only 1–2% of your account. A wider stop simply means a
smaller lot.</p>
<p>Practise placing stops in the <a href="/academy/practice/">practice simulator</a>, and see how every AI signal's stop played out on the
<a href="/results/">public track record</a>.</p>'''),
]


def add(apps, schema_editor):
    BlogPost = apps.get_model('store', 'BlogPost')
    now = timezone.now()
    for i, (slug, title, summary, body) in enumerate(ARTICLES):
        BlogPost.objects.get_or_create(slug=slug, defaults={
            'title': title, 'summary': summary[:300], 'body': body + RISK, 'kind': 'article',
            'published_at': now - timedelta(days=i), 'is_published': True, 'auto': False})


class Migration(migrations.Migration):
    dependencies = [('store', '0082_userprofile_space')]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
