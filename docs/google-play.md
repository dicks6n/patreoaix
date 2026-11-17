# Putting PATREOAIX on Google Play

Everything to paste into the Play Console, plus what to prepare. Publishing on Play removes the
"harmful app / unsafe app blocked" warning that Android shows for apps installed from a website.

## Before you start: two things to know

1. **Google's financial-services rule.** Apps that let people trade forex, gold or other CFDs must show Google that
   the company is licensed (or exempt) in each country where the app is offered. In the Play Console you will fill in
   the **Financial features declaration**. If you do not have a licence yet, the safest route is:
   - publish the **Academy / learning** side first (education, demo account, news, no real-money trading in the app), or
   - limit the countries to ones where you hold the licence, and add the licence documents when asked.
   Ask a local lawyer about the Capital Markets Authority (Kenya) rules before offering real-money trading.
2. **Play needs an Android App Bundle (.aab), not an .apk**, and a recent target Android version. The current
   `android/build.sh` makes an .apk for the website. Building the .aab is the next technical step (ask Claude:
   "build the Play Store bundle").

## Account
- Create the developer account at **play.google.com/console** ($25 once). Choose **Organisation** if PATREOAIX is a
  registered company (needs a D-U-N-S number), otherwise **Personal**.
- Personal accounts created recently must run a **closed test with at least 12 testers for 14 days** before the app
  can go public. Invite friends and clients early.

## Store listing (copy and paste)

**App name** (max 30): `PATREOAIX: Learn & Trade`

**Short description** (max 80):
`Learn trading, practise on a free demo and follow AI market analysis for gold & forex.`

**Full description** (max 4000):

```
PATREOAIX helps you learn trading step by step and practise with live market prices, with AI analysis to show you what the charts are saying.

LEARN
• Free Trading Academy: candlesticks, chart patterns, support & resistance, risk management
• Practice simulator: read a real chart, choose buy, sell or skip, and see what happened next
• Read My Chart: upload a chart screenshot and the AI coach explains trend, patterns and levels
• Cheat sheets for every candlestick and chart pattern

PRACTISE
• Free demo account with up to $100,000 of virtual money
• MT5-style terminal: live quotes, charts with drawing tools, stop loss and take profit
• Choose your leverage and see exactly how much margin a trade needs

AI MARKET ANALYSIS
• The AI scanner grades setups on gold, forex and indices and shows entry, stop loss and target
• An entry coach that tells you when to wait instead of chasing the price
• A public track record of every signal, wins and losses included

NEWS
• Economic calendar with CPI, NFP, PPI and central-bank decisions
• How each release moved gold and the dollar in the past

Plans: start with a free 7-day Pro trial. After that, learning and practice stay free; AI signals and alerts are part of the paid plans.

Risk warning: trading forex, gold and other leveraged products carries a high risk of losing money. Only trade money you can afford to lose. AI analysis is education, not financial advice.
```

**App category:** Finance (or Education if you publish the learning side first)
**Tags:** Education, Finance, Stocks & trading
**Contact email:** patreoaix@gmail.com
**Website:** https://www.patreoaix.com
**Privacy policy:** https://www.patreoaix.com/privacy/

**Graphics**
- App icon 512×512: `store/static/store/icons/icon-512.png`
- Feature graphic 1024×500: use the gold "PATREOAIX" banner (ask Claude to make one)
- Phone screenshots (2–8, at least 1080 px tall): Academy, Practice simulator, Demo terminal, Market overview,
  Results page, Economic calendar. Show the demo account, not real balances.

## App content answers

| Section | Answer |
|---|---|
| Privacy policy | https://www.patreoaix.com/privacy/ |
| Ads | No, the app has no ads |
| App access | Some features need login: give Google a **test account** (username + password) with a demo account opened |
| Content rating | Questionnaire category "Reference, news or educational" (or "Utility"); no violence, no gambling content → usually rated **Everyone / PEGI 3**. Answer **Yes** to "users can interact / share" (P2P chat) |
| Target audience | **18 and over** only |
| News app | No |
| Financial features | Declare: trading / investment (forex & CFDs), crypto P2P marketplace, money transfers. Upload licences where asked |
| Government app | No |
| Health | No |

## Data safety form

| Data type | Collected | Shared | Why | Required |
|---|---|---|---|---|
| Name, email, phone number | Yes | No | Account management, security | Required |
| User IDs | Yes | No | Account management | Required |
| Photos / files (ID documents, payment proof) | Yes | No | Identity verification (KYC), fraud prevention | Optional |
| Financial info (payment and trade history) | Yes | Yes, with payment processors (M-Pesa, Deriv) | Payments, account functionality | Required for trading |
| Messages (support, P2P chat) | Yes | No | Customer support, P2P orders | Optional |
| App activity (pages, settings) | Yes | No | Analytics, personalisation | Required |
| Device IDs (notification token) | Yes | No | Push notifications | Optional |
| Location | No | | | |

- Data is **encrypted in transit**: Yes (HTTPS only).
- Users can **request deletion**: Yes, by emailing patreoaix@gmail.com (state it on the privacy page).

## Release steps
1. Create app → name, default language English, App, Free.
2. Fill **Store listing**, **App content** and **Data safety** with the answers above.
3. **Testing → Closed testing**: upload the .aab, add at least 12 testers' emails, run 14 days.
4. **Production → Create release**, upload the same .aab, roll out. Review usually takes 1–7 days
   (finance apps can take longer).
5. On the website, change the download page button to the Play Store link.
