"""Live adapter, instantiated ONLY by explicitly enabled live mode.

SDK pinned: private typed-data helpers are used solely for durable order identity.
No allowance recovery, wallet deployment, transaction or redemption is performed.
"""
import os
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

    def submit(self, signed):
        # post_order has no automatic allowance recovery in pinned SDK 0.12.0.
        r = self.client.post_order(signed)
        return {'ok':r.ok, 'order_id':r.order_id if r.ok else None,
                'status':r.status if r.ok else 'REJECTED',
                'trade_ids':list(r.trade_ids) if r.ok else [], 'code':None if r.ok else r.code}

    def reconcile(self, intent):
        order = self.client.get_order(order_id=intent['order_id'])
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

    def close(self):
        self.client.close()
