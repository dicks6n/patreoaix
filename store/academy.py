# store/academy.py
"""
Beginner academy: six short lessons, each with a 3-question quiz.

Finishing every lesson ("graduating") unlocks:
  • ACADEMY_PRO_DAYS days of Pro (default 3), once per account (membership.grant_pro_days);
  • the first-deposit bonus, when ACADEMY_REQUIRED_FOR_BONUS=True. In that mode a first deposit made before
    graduating gets its bonus at graduation (bonus.claim_after_academy).
"""
from django.conf import settings
from django.utils import timezone

LESSONS = [
    {
        'key': 'basics', 'icon': 'fa-coins', 'mins': 3,
        'title': 'Forex, gold and crypto in 3 minutes',
        'body': [
            "When you trade, you don't buy the gold or the euro itself: you open a position that wins when the price moves your way. <b>Buy</b> if you think the price will rise, <b>sell</b> if you think it will fall. You can make or lose money both ways.",
            "Size is measured in <b>lots</b>. On EURUSD, 1 lot is 100,000 euros and every 0.0001 move (one <b>pip</b>) is worth $10; 0.01 lot (a micro lot) is worth $0.10 per pip. On gold (XAUUSD), 0.01 lot moves $1 for every $1 move in the gold price.",
            "<b>Leverage</b> lets a small deposit control a big position. It multiplies profits and losses alike, which is why the next two lessons (stops and risk) matter more than anything else.",
        ],
        'quiz': [
            ('You think gold will fall. What do you do?', ['Buy gold', 'Sell gold', 'Nothing: you can only make money when prices rise'], 1),
            ('On EURUSD, roughly how much is 1 pip worth with 0.01 lots?', ['$10', '$1', '$0.10'], 2),
            ('What does leverage do?', ['Multiplies profits only', 'Multiplies profits and losses', 'Removes the risk of losing'], 1),
        ],
    },
    {
        'key': 'stops', 'icon': 'fa-shield-halved', 'mins': 3,
        'title': 'Stop loss and take profit',
        'body': [
            "A <b>stop loss</b> closes your trade automatically at a price you choose, so one bad trade can never empty your account. A <b>take profit</b> closes it automatically when the target is reached.",
            "Set both <b>before</b> you enter, from the chart (below support for a buy, above resistance for a sell), not from how much you hope to make. Every PATREOAIX signal comes with both.",
            "Never move your stop further away to 'give it room'. If the market hits your stop, the idea was wrong: accept the small loss and wait for the next setup.",
        ],
        'quiz': [
            ('When should you set your stop loss?', ['Before entering the trade', 'After the trade goes against you', 'Only on big trades'], 0),
            ('Your buy trade is near its stop. What do you do?', ['Move the stop lower to give it room', 'Leave the stop where the plan put it', 'Remove the stop'], 1),
            ('Where does a stop usually go on a buy?', ['Just below support', 'Just above resistance', 'Anywhere, it does not matter'], 0),
        ],
    },
    {
        'key': 'risk', 'icon': 'fa-scale-balanced', 'mins': 4,
        'title': 'The 1% rule: how big should my trade be?',
        'body': [
            "Professional traders risk about <b>1% of their balance per trade</b>. With $200, that is $2 per trade. Ten losses in a row would cost about 10%, not your whole account.",
            "Your lot size comes from the stop distance: <b>lots = money at risk ÷ loss per lot at the stop</b>. A wide stop needs a smaller size, a tight stop allows a bigger one. The <b>risk calculator</b> inside every signal does this for you: type your balance and it shows the lots.",
            "If the smallest size (0.01 lots) risks more than 1%, skip the trade or use a Deriv Multipliers stake, where you can never lose more than the stake.",
        ],
        'quiz': [
            ('You have $500. About how much should you risk on one trade?', ['$5', '$50', '$250'], 0),
            ('The stop is twice as far away as usual. What happens to your lot size?', ['It doubles', 'It stays the same', 'It halves'], 2),
            ('Where can you see the right lot size for a signal?', ['Nowhere, guess it', 'The risk calculator in the signal', 'Use the biggest size allowed'], 1),
        ],
    },
    {
        'key': 'signals', 'icon': 'fa-robot', 'mins': 3,
        'title': 'How to read a PATREOAIX AI signal',
        'body': [
            "Each signal has a <b>grade</b>: A+ is the strongest (most scanners agree), then A and B. Beginners should take A+ and A only.",
            "It gives an <b>entry</b>, a <b>stop loss</b> and <b>targets</b>. The <b>R</b> number is reward ÷ risk: 2R means the target pays twice what the stop costs. With 2R you can be right only 40% of the time and still make money.",
            "The coach also says <b>when</b>: READY means enter now; WAIT / GET READY means the price is not at the entry yet. Use 'Alert me at entry' or a price alert instead of chasing.",
        ],
        'quiz': [
            ('Which signals should a beginner focus on?', ['B only', 'A+ and A', 'All of them, as many as possible'], 1),
            ('The stop costs $10 and the target pays $20. What is the R?', ['0.5R', '2R', '20R'], 1),
            ('The coach says WAIT. What is the best move?', ['Enter now anyway', 'Set an alert and wait for the entry', 'Double the size'], 1),
        ],
    },
    {
        'key': 'mindset', 'icon': 'fa-brain', 'mins': 3,
        'title': 'Trading psychology: the silent account killer',
        'body': [
            "<b>Revenge trading</b>: after a loss you jump straight back in to 'win it back', usually bigger and without a plan. Rule: after a loss, step away for 30 minutes.",
            "<b>FOMO</b> (fear of missing out): chasing a move that already happened. There is always another setup tomorrow.",
            "Keep a <b>journal</b>. The PATREOAIX journal fills itself from your trades; tag how you felt and every Monday the AI coach shows you which habits cost money.",
        ],
        'quiz': [
            ('You just lost a trade. What should you do?', ['Open a bigger trade to win it back', 'Take a break before the next trade', 'Remove stops on the next trades'], 1),
            ('Gold already jumped $40 and you feel you must buy now. That is…', ['FOMO', 'A strong plan', 'Risk management'], 0),
            ('Why keep a journal?', ['To see which habits cost you money', 'It is required by law', 'It is not useful'], 0),
        ],
    },
    {
        'key': 'platform', 'icon': 'fa-rocket', 'mins': 3,
        'title': 'Using PATREOAIX safely',
        'body': [
            "<b>Practise first</b> on a Deriv demo account: connect it, then trade signals from Market Overview or switch on <b>AI Copy Trading</b> to copy every A+ signal automatically, with stops set on Deriv.",
            "Install the app and turn on notifications to get signals, price alerts ('tell me when gold reaches 4,050') and copy trades on your phone.",
            "Only move to real money once you have a month of demo results you are happy with, and then start small. Never trade money you cannot afford to lose.",
        ],
        'quiz': [
            ('Where should you start?', ['A demo account', 'Your biggest real deposit', 'A loan'], 0),
            ('What does AI Copy Trading do?', ['Guarantees profit', 'Copies A+ signals onto your Deriv account with stops', 'Withdraws your money'], 1),
            ('When should you move to real money?', ['Today', 'After a month of good demo results, starting small', 'Never use stops on real money'], 1),
        ],
    },
]
BY_KEY = {l['key']: l for l in LESSONS}


def pro_days():
    try:
        return int(getattr(settings, 'ACADEMY_PRO_DAYS', 3) or 0)
    except (TypeError, ValueError):
        return 3


def required_for_bonus():
    return str(getattr(settings, 'ACADEMY_REQUIRED_FOR_BONUS', 'False')) == 'True'


def passed(user):
    from .models import AcademyProgress
    if not user.is_authenticated:
        return set()
    return set(AcademyProgress.objects.filter(user=user).exclude(lesson='_graduated').values_list('lesson', flat=True))


def graduated(user):
    from .models import AcademyProgress
    return user.is_authenticated and AcademyProgress.objects.filter(user=user, lesson='_graduated').exists()


def grade(lesson, answers):
    """answers: list of chosen option indexes → (score, list of correct flags)."""
    ok = []
    for i, (_, _, right) in enumerate(lesson['quiz']):
        try:
            ok.append(int(answers[i]) == right)
        except (IndexError, TypeError, ValueError):
            ok.append(False)
    return sum(ok), ok


def submit(user, key, answers):
    """→ dict(passed, score, correct, graduated_now, reward)."""
    from .models import AcademyProgress
    lesson = BY_KEY[key]
    score, ok = grade(lesson, answers)
    out = {'passed': all(ok), 'score': score, 'total': len(ok), 'correct': ok, 'graduated_now': False, 'reward': ''}
    if not out['passed']:
        return out
    AcademyProgress.objects.get_or_create(user=user, lesson=key, defaults={'score': score})
    if len(passed(user) & set(BY_KEY)) == len(LESSONS) and not graduated(user):
        AcademyProgress.objects.create(user=user, lesson='_graduated', score=0)
        out['graduated_now'] = True
        out['reward'] = graduate(user)
    return out


def graduate(user):
    """Unlock the rewards once. Returns a short text of what was unlocked."""
    parts = []
    days = pro_days()
    if days > 0:
        from .membership import grant_pro_days
        grant_pro_days(user, days, f'🎓 Academy complete: +{days} days of Pro',
                       f'Well done! You finished the PATREOAIX academy. {days} days of Pro are unlocked: every AI signal, copy trading and the weekly coach.')
        parts.append(f'{days} days of Pro')
    from . import bonus
    if bonus.enabled() and required_for_bonus():
        b = bonus.claim_after_academy(user)
        parts.append(f'your ${b.amount} deposit bonus' if b else f'your {bonus.pct():.0f}% first-deposit bonus')
    return ' and '.join(parts)
