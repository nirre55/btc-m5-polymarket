"""Live adapter, instantiated ONLY by explicitly enabled live mode.

SDK pinned: private typed-data helpers are used solely for durable order identity.
No allowance recovery, wallet deployment, transaction or redemption is performed.
"""
import os
import time
from dataclasses import asdict
from decimal import Decimal


def credential_options(env):
    """Called only after the live gate. Never log or serialize the returned secrets."""
    def alias(primary, legacy):
        a,b=env.get(primary),env.get(legacy)
        if a and b and a!=b:
            raise RuntimeError('conflicting_credential_aliases')
        value=a or b
        if not value:
            raise RuntimeError('missing_local_credential')
        return value
    url=env.get('POLYMARKET_API_URL','https://clob.polymarket.com').rstrip('/')
    if url!='https://clob.polymarket.com':
        raise RuntimeError('unsupported_clob_api_url')
    kind=env.get('POLYMARKET_SIGNATURE_TYPE')
    if kind is not None and kind not in ('0','1','2','3'):
        raise RuntimeError('invalid_signature_type')
    options={'private_key':alias('POLYMARKET_PRIVATE_KEY','POLY_PRIVATE_KEY'),
             'wallet':alias('POLYMARKET_FUNDER','POLY_WALLET_ADDRESS')}
    trio=[env.get(k) for k in ('POLY_API_KEY','POLY_API_SECRET','POLY_API_PASSPHRASE')]
    if any(trio) and not all(trio):
        raise RuntimeError('incomplete_optional_clob_credentials')
    if all(trio):
        from polymarket import ApiKeyCreds
        options['credentials']=ApiKeyCreds(apiKey=trio[0],secret=trio[1],passphrase=trio[2])
    # Without credentials SecureClient.create performs CLOB L1 auth, then creates/derives L2.
    return options,int(kind) if kind is not None else None


def order_identity(signed, client, neg_risk):
    from polymarket._internal.actions.orders.types import UnsignedOrder
    from polymarket._internal.actions.orders.context import resolve_order_exchange_address
    from polymarket._internal.actions.orders.typed_data import _app_domain_separator, _order_contents_hash
    from polymarket._internal.protocol import is_v2_position_id
    from eth_utils.crypto import keccak
    ctx = client._ctx
    values = asdict(signed)
    values.pop('signature')
    values.pop('post_only')
    version = '3' if is_v2_position_id(signed.token_id) else '2'
    unsigned = UnsignedOrder(**values, chain_id=ctx.environment_config.chain_id,
                            exchange_address=resolve_order_exchange_address(
                                ctx.environment_config, asset_id=signed.token_id, neg_risk=neg_risk),
                            protocol_version=version)
    domain = bytes.fromhex(_app_domain_separator(unsigned, protocol_version=version)[2:])
    return '0x' + keccak(b'\x19\x01' + domain + _order_contents_hash(unsigned)).hex()


class LiveAdapter:
    def __init__(self, config):
        if config['mode'] != 'live' or config['enable_live'] is not True or os.environ.get('POLY_ENABLE_LIVE') != 'I_ACCEPT_LIVE_ORDERS':
            raise RuntimeError('live_not_explicitly_enabled')
        from polymarket import SecureClient
        from polymarket._internal.wallet import signature_type_for

        options,expected_signature_type=credential_options(os.environ)

        class ExistingWalletClient(SecureClient):
            def _deploy_default_deposit_wallet(self):
                raise RuntimeError('wallet_not_deployed_configure_it_yourself')

        # Explicit existing funder prevents implicit default-wallet selection.
        self.client=ExistingWalletClient.create(**options)
        if expected_signature_type is not None and signature_type_for(self.client.wallet_type)!=expected_signature_type:
            self.client.close()
            raise RuntimeError('signature_type_does_not_match_existing_wallet')

    def sign(self, intent):
        signed = self.client.create_limit_order(asset_id=intent['token'], side='BUY',
                    price=intent['price'], size=intent['size'], post_only=False,
                    expiration=intent['expiration'] if intent['order_type'] == 'GTD' else None)
        return signed, order_identity(signed, self.client, intent['neg_risk'])

    def funds(self, intent, reservations, fee_reserve):
        """Read only. No approvals, redemptions or collateral transfers."""
        from polymarket._internal.actions.orders.context import resolve_order_exchange_address
        from polymarket._internal.actions.account import build_update_balance_allowance_request
        from polymarket._internal.wallet import signature_type_for
        # Refresh the CLOB balance index after deposits/auto-redeem. This is
        # an authenticated GET, not an on-chain approval or transfer.
        clock=time.monotonic()
        if clock-getattr(self,'_last_balance_refresh',float('-inf'))>=30:
            path,params=build_update_balance_allowance_request(asset_type='COLLATERAL',
                      signature_type=signature_type_for(self.client.wallet_type))
            self.client._ctx.secure_clob.get_bytes(path,params=params)
            self._last_balance_refresh=clock
        balance=self.client.get_balance_allowance(asset_type='COLLATERAL')
        spender=str(resolve_order_exchange_address(self.client._ctx.environment_config,
                    asset_id=intent['token'],neg_risk=intent['neg_risk'])).lower()
        allowance=next((v for k,v in balance.allowances.items() if k.lower()==spender),0)
        if balance.balance<0 or allowance<0:
            raise ValueError('invalid_account_balance')
        factor=1+Decimal(fee_reserve)
        reserved=Decimal(0); seen=set()
        for order in self.client.list_open_orders().iter_items():
            if order.id in seen:
                raise ValueError('duplicate_open_order')
            seen.add(order.id)
            if not (order.price.is_finite() and 0<order.price<1 and
                    order.original_size.is_finite() and order.size_matched.is_finite() and
                    0<=order.size_matched<=order.original_size):
                raise ValueError('invalid_open_order_amounts')
            if order.side=='BUY':
                reserved+=(order.original_size-order.size_matched)*order.price*factor
        extra=sum((Decimal(x['pending'] if x['order_id'] in seen else x['missing'])
                   for x in reservations),Decimal(0))
        cash=Decimal(balance.balance)/Decimal(1000000)
        approved=Decimal(allowance)/Decimal(1000000)
        return {'balance':str(cash),'allowance':str(approved),'open_buy_reserved':str(reserved),
                'local_reserved':str(extra),'available':str(max(Decimal(0),min(cash,approved)-reserved-extra))}

    def submit(self, signed):
        # post_order has no automatic allowance recovery in pinned SDK 0.12.0.
        from polymarket.errors import RequestRejectedError
        try:
            r = self.client.post_order(signed)
        except RequestRejectedError as e:
            # Only a definitive HTTP 400 funds rejection is safe to retry.
            # Never persist SDK exception text: it may include request headers.
            if e.status==400 and (e.code=='not_enough_balance' or any(x in str(e).lower() for x in
                    ('not enough balance / allowance','allowance is not enough'))):
                return {'ok':False,'order_id':None,'status':'REJECTED','trade_ids':[],
                        'code':'not_enough_balance','retryable_funds':True}
            if e.status in (401,403,422):
                return {'ok':False,'order_id':None,'status':'REJECTED','trade_ids':[],
                        'code':'explicit_http_rejection','retryable_funds':False}
            raise
        return {'ok':r.ok, 'order_id':r.order_id if r.ok else None,
                'status':r.status if r.ok else 'REJECTED',
                'trade_ids':list(r.trade_ids) if r.ok else [], 'code':None if r.ok else r.code,
                'retryable_funds':not r.ok and r.code=='not_enough_balance'}

    def reconcile(self, intent):
        try:
            order = self.client.get_order(order_id=intent['order_id'])
        except Exception:
            # A failed single-order read does not prove that the POST failed.
            # Try independent authenticated views; identity must still match.
            try:
                orders = list(self.client.list_open_orders(id=intent['order_id'],
                              asset_id=intent['token']).iter_items())
            except Exception:
                return self.reconcile_trades(intent)
            exact = [o for o in orders if o.id == intent['order_id']]
            if len(exact)>1:
                raise ValueError('duplicate_reconciliation_order')
            if exact:
                order=exact[0]
            else:
                return self.reconcile_trades(intent)
        if (str(order.asset_id) != intent['token'] or order.side != 'BUY'
                or str(order.condition_id) != intent['condition'] or order.id != intent['order_id']
                or order.price != Decimal(intent['price']) or order.original_size != Decimal(intent['size'])):
            raise ValueError('authenticated_order_mismatch')
        fills = []
        # Fetch every trade by exact ID; never scan unrelated account trades.
        for trade_id in sorted(set(order.associate_trades) | set(intent.get('trade_ids',[]))):
            for trade in self.client.list_account_trades(id=trade_id).iter_items():
                if trade.id != trade_id:
                    raise ValueError('wrong_trade_id')
                if trade.taker_order_id == order.id:
                    if str(trade.asset_id) != intent['token'] or trade.side != 'BUY':
                        raise ValueError('wrong_taker_asset')
                    fills.append({'id':trade.id, 'leg':'taker', 'qty':str(trade.size),
                                  'price':str(trade.price), 'status':str(trade.status),
                                  'transaction_hash':trade.transaction_hash})
                else:
                    for leg in trade.maker_orders:
                        if leg.order_id == order.id:
                            if str(leg.asset_id) != intent['token'] or leg.side != 'BUY':
                                raise ValueError('wrong_maker_asset')
                            fills.append({'id':trade.id, 'leg':'maker', 'qty':str(leg.matched_amount),
                                          'price':str(leg.price), 'status':str(trade.status),
                                          'transaction_hash':trade.transaction_hash})
        return {'status':order.status, 'matched_qty':str(order.size_matched), 'fills':fills}

    def reconcile_trades(self, intent):
        """Recover fills by exact order identity, never infer absence from a scan."""
        fills=[]; seen=set()
        trades=self.client.list_account_trades(asset_id=intent['token'],
                market=intent['condition'],after=str(int(intent['committed_at'])-60))
        for trade in trades.iter_items():
            legs=[]
            if trade.taker_order_id==intent['order_id']:
                if str(trade.asset_id)!=intent['token'] or trade.side!='BUY':
                    raise ValueError('wrong_taker_asset')
                legs=[('taker',trade.size,trade.price)]
            else:
                for leg in trade.maker_orders:
                    if leg.order_id==intent['order_id']:
                        if str(leg.asset_id)!=intent['token'] or leg.side!='BUY':
                            raise ValueError('wrong_maker_asset')
                        legs.append(('maker',leg.matched_amount,leg.price))
            for role,qty,price in legs:
                key=(trade.id,role)
                if key in seen:
                    raise ValueError('duplicate_reconciliation_fill')
                seen.add(key)
                qty,price=Decimal(qty),Decimal(price)
                if not qty.is_finite() or qty<=0 or not price.is_finite() or not 0<price<=Decimal(intent['price']):
                    raise ValueError('invalid_reconciliation_fill')
                fills.append({'id':trade.id,'leg':role,'qty':str(qty),'price':str(price),
                    'status':str(trade.status),'transaction_hash':trade.transaction_hash})
        matched=sum((Decimal(f['qty']) for f in fills if f['status'].removeprefix('TRADE_STATUS_')!='FAILED'),Decimal(0))
        confirmed=sum((Decimal(f['qty']) for f in fills if f['status'].removeprefix('TRADE_STATUS_')=='CONFIRMED'),Decimal(0))
        if matched>Decimal(intent['size']):
            raise ValueError('excess_reconciliation_quantity')
        # Full confirmed execution is positive evidence. Empty/partial history,
        # even after expiration, never authorizes a repost or reserve release.
        return {'status':'FILLED' if confirmed==Decimal(intent['size']) else 'UNKNOWN',
                'matched_qty':str(max(matched,Decimal(intent.get('matched_qty','0')))), 'fills':fills}

    def close(self):
        self.client.close()
