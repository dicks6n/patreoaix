import os, django, sys, hmac, hashlib, time, requests

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')
django.setup()

from django.conf import settings

api_key = settings.BINANCE_API_KEY
api_secret = settings.BINANCE_API_SECRET

print("=" * 60)
print("BINANCE DIAGNOSTIC")
print("=" * 60)
print(f"API Key: {api_key[:10]}...{api_key[-4:]}  ({len(api_key)} chars)")
print(f"Secret:  {api_secret[:10]}...{api_secret[-4:]}  ({len(api_secret)} chars)")
print()

# Test 1: Public
print("[TEST 1] Binance reachable?")
try:
    r = requests.get('https://api.binance.com/api/v3/time', timeout=5)
    print(f"  Status: {r.status_code}")
    print(f"  Body:   {r.text}")
except Exception as e:
    print(f"  FAILED: {e}")
print()

# Test 2: Your IP
print("[TEST 2] Your public IP (whitelist this in Binance)")
try:
    ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
    print(f"  Your IP: {ip}")
    print(f"  → Add this to your Binance API key whitelist")
except Exception as e:
    print(f"  FAILED: {e}")
print()

# Test 3: Signed request
print("[TEST 3] Signed account request")
params = {
    'timestamp': int(time.time() * 1000),
    'recvWindow': 5000,
}
query = '&'.join([f'{k}={v}' for k, v in params.items()])
sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
url = f"https://api.binance.com/api/v3/account?{query}&signature={sig}"

try:
    r = requests.get(url, headers={'X-MBX-APIKEY': api_key}, timeout=5)
    print(f"  Status: {r.status_code}")
    print(f"  Body:   {r.text[:400]}")

    if r.status_code == 200:
        data = r.json()
        print()
        print("  ✅ SUCCESS")
        print(f"  Account type:  {data.get('accountType')}")
        print(f"  Can trade:     {data.get('canTrade')}")
        print(f"  Can withdraw:  {data.get('canWithdraw')}")
        print(f"  Can deposit:   {data.get('canDeposit')}")
        print()
        print("  Non-zero balances:")
        for b in data.get('balances', []):
            total = float(b['free']) + float(b['locked'])
            if total > 0:
                print(f"    {b['asset']}: {b['free']} free, {b['locked']} locked")
except Exception as e:
    print(f"  FAILED: {e}")

print()
print("=" * 60)
