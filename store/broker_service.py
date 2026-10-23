"""
Unified Broker Service.
Routes balance fetches to the right sub-service (Deriv / Binance).
Updates the UserBrokerAccount cache fields.
"""
import logging
from decimal import Decimal
from django.utils import timezone
from asgiref.sync import async_to_sync

from .models import UserBrokerAccount, BrokerBalanceSnapshot
from .deriv_service import DerivService
from .binance_service import BinanceService

logger = logging.getLogger(__name__)


class BrokerService:

    # ==========================================
    # PUBLIC — Fetch live balance from broker
    # ==========================================

    def fetch_balance(self, broker_account):
        """Routes to the correct broker sub-service."""
        if broker_account.broker == 'DERIV':
            return self._fetch_deriv(broker_account)

        if broker_account.broker == 'BINANCE':
            return self._fetch_binance(broker_account)

        return {
            'success': False,
            'error': f"Broker '{broker_account.broker}' not supported yet.",
        }

    def _fetch_deriv(self, broker_account):
        """Fetch balance from Deriv via WebSocket (read-only)."""
        try:
            deriv = DerivService()
            info = async_to_sync(deriv.get_account_info)(
                token=broker_account.api_token,
                account_number=broker_account.account_number or '',
            )

            # DerivService returns a placeholder $10,000 demo balance when it can't log in
            if not info.get('connected'):
                return {'success': False, 'error': info.get('error') or 'Could not reach your Deriv account'}

            raw_balance = info.get('raw_balance')
            if raw_balance is None:
                bal_str = str(info.get('balance', '0')).replace(',', '')
                try:
                    raw_balance = float(bal_str)
                except ValueError:
                    raw_balance = 0.0

            # remember whether this is demo or real money (kept in server_name, no schema change)
            kind = 'Deriv Demo' if info.get('is_demo') else 'Deriv Real'
            if broker_account.server_name != kind:
                broker_account.server_name = kind
                broker_account.save(update_fields=['server_name'])
            return {
                'success': True,
                'balance': float(raw_balance),
                'currency': info.get('currency', 'USD'),
                'raw': info,
            }
        except Exception as e:
            logger.error(f"Deriv balance fetch failed: {e}")
            return {'success': False, 'error': str(e)}

    def _fetch_binance(self, broker_account):
        """Fetch stablecoin balance from Binance Spot."""
        try:
            token = broker_account.api_token or ''
            if '::' not in token:
                return {'success': False, 'error': 'Malformed Binance credentials'}

            api_key, api_secret = token.split('::', 1)
            binance = BinanceService(api_key=api_key, api_secret=api_secret)
            result = binance.get_stablecoin_balance()

            if not result.get('success'):
                return {'success': False, 'error': result.get('error', 'Unknown error')}

            return {
                'success': True,
                'balance': result['balance'],
                'currency': result.get('currency', 'USDT'),
                'raw': result,
            }
        except Exception as e:
            logger.error(f"Binance balance fetch failed: {e}")
            return {'success': False, 'error': str(e)}

    # ==========================================
    # PUBLIC — Sync + cache one account
    # ==========================================

    def sync_account(self, broker_account, save_snapshot=True):
        result = self.fetch_balance(broker_account)

        if result.get('success'):
            broker_account.last_known_balance = Decimal(str(result['balance']))
            broker_account.last_balance_currency = result['currency']
            broker_account.last_synced_at = timezone.now()
            broker_account.sync_status = 'OK'
            broker_account.sync_error_message = None
            broker_account.save(update_fields=[
                'last_known_balance', 'last_balance_currency',
                'last_synced_at', 'sync_status', 'sync_error_message',
            ])

            if save_snapshot:
                BrokerBalanceSnapshot.objects.create(
                    broker_account=broker_account,
                    balance=broker_account.last_known_balance,
                    currency=broker_account.last_balance_currency,
                )
        else:
            broker_account.sync_status = 'ERROR'
            broker_account.sync_error_message = result.get('error', 'Unknown error')
            broker_account.last_synced_at = timezone.now()
            broker_account.save(update_fields=[
                'sync_status', 'sync_error_message', 'last_synced_at',
            ])

        return result

    # ==========================================
    # PUBLIC — Sync all brokers for a user
    # ==========================================

    def sync_all_for_user(self, user):
        results = []
        accounts = UserBrokerAccount.objects.filter(user=user, is_active=True)

        for account in accounts:
            try:
                res = self.sync_account(account)
                results.append({
                    'broker': account.broker,
                    'account_number': account.account_number,
                    'success': res.get('success', False),
                    'balance': res.get('balance'),
                    'currency': res.get('currency'),
                    'error': res.get('error'),
                })
            except Exception as e:
                logger.error(f"Sync failed for {account}: {e}")
                results.append({
                    'broker': account.broker,
                    'account_number': account.account_number,
                    'success': False,
                    'error': str(e),
                })

        return results

    # ==========================================
    # PUBLIC — Build dashboard payload
    # ==========================================

    def get_user_broker_summary(self, user):
        brokers = []
        broker_total = Decimal('0.00')

        accounts = UserBrokerAccount.objects.filter(user=user, is_active=True)

        for acc in accounts:
            bal = acc.last_known_balance or Decimal('0.00')
            demo = self.is_demo(acc)
            # demo money is shown but never added to the real total
            if not demo and acc.last_balance_currency in ('USD', 'USDT', 'USDC', 'BUSD', 'FDUSD'):
                broker_total += bal

            brokers.append({
                'broker': acc.broker,
                'label': self._label_for(acc),
                'balance': float(bal),
                'currency': acc.last_balance_currency or 'USD',
                'synced_at': acc.last_synced_at,
                'status': acc.sync_status,
                'is_stale': acc.is_stale,
                'account_number': acc.account_number,
                'account_id': acc.id,
                'error': acc.sync_error_message,
                'is_demo': demo,
                'kind': 'DEMO' if demo else 'REAL',
            })

        return {
            'brokers': brokers,
            'broker_total_usd': broker_total,
        }

    @staticmethod
    def is_demo(account):
        if account.broker != 'DERIV':
            return False
        from .deriv_service import looks_demo
        if account.server_name in ('Deriv Demo', 'Deriv Real'):
            return account.server_name == 'Deriv Demo'
        return looks_demo(account.account_number)

    @classmethod
    def _label_for(cls, account):
        if account.broker == 'DERIV':
            return 'Deriv Demo' if cls.is_demo(account) else 'Deriv Real'
        labels = {
            'DERIV': 'Deriv MT5',
            'BINANCE': 'Binance Spot',
            'PEPPERSTONE_MT5': 'Pepperstone MT5',
            'PEPPERSTONE_CTRADER': 'cTrader',
        }
        return labels.get(account.broker, account.broker.title())


# ==========================================
# SINGLETON — MUST BE AT BOTTOM OF FILE
# ==========================================
broker_service = BrokerService()