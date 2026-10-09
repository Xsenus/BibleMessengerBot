"""Native checkout contract, untrusted notifications and sanitized API failures."""
import json
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from app.payments.ledger import validate_payment
from app.payments.yookassa import MerchantSettings, PaymentError, YooKassa, minor_amount

ORDER_ID=UUID('10000000-0000-4000-8000-000000000001')
PAYMENT_ID='20000000-0000-4000-8000-000000000002'
KEY=UUID('30000000-0000-4000-8000-000000000003')
CONFIG=MerchantSettings('12345','fixture-secret-only','https://example.invalid/max/payments/return','fixture support')


def order():
    return dict(id=ORDER_ID,amount_minor=25000,method='sbp',idempotency_key=KEY,provider_payment_id=None)


def payment():
    return dict(id=PAYMENT_ID,amount={'value':'250.00','currency':'RUB'},status='succeeded',paid=True,test=True,
                metadata={'application':'bible-messenger-max','order_id':str(ORDER_ID)},
                recipient={'account_id':'12345'},payment_method={'type':'sbp'})


@pytest.mark.asyncio
@pytest.mark.parametrize('platform',['max','telegram'])
async def test_create_and_repeat_use_same_key_exact_amount_sbp_redirect_and_no_saved_method(platform):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json=payment())
    api=YooKassa(CONFIG,transport=httpx.MockTransport(handle))
    try:
        await api.create(dict(order(),platform=platform))
        await api.create(dict(order(),platform=platform))
    finally:
        await api.close()
    assert len(requests)==2 and requests[0].headers['Idempotence-Key']==requests[1].headers['Idempotence-Key']==str(KEY)
    for request in requests:
        body=json.loads(request.content)
        assert request.url==httpx.URL('https://api.yookassa.ru/v3/payments')
        assert request.headers['authorization'].startswith('Basic ')
        assert body['amount']=={'value':'250.00','currency':'RUB'}
        assert body['payment_method_data']=={'type':'sbp'}
        assert body['confirmation']=={'type':'redirect','return_url':CONFIG.return_url}
        assert body['save_payment_method'] is False and body['capture'] is True
        assert body['metadata']['order_id']==str(ORDER_ID)
        assert body['metadata']['application']=='bible-messenger-'+platform


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[302,401,429,500])
async def test_redirects_and_provider_errors_are_sanitized_without_second_request(status):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,headers={'Location':'https://evil.invalid'},json={'detail':CONFIG.secret_key})
    api=YooKassa(CONFIG,transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(PaymentError) as error:
            await api.payment(PAYMENT_ID)
        assert CONFIG.secret_key not in str(error.value)
        assert len(requests)==1
    finally:
        await api.close()


@pytest.mark.parametrize(('field','value'),[
    ('amount',{'value':'249.99','currency':'RUB'}),('amount',{'value':'250.00','currency':'XTR'}),
    ('metadata',{'order_id':str(ORDER_ID),'application':'other'}),
    ('metadata',{'order_id':str(KEY),'application':'bible-messenger-max'}),
    ('recipient',{'account_id':'other-shop'}),('paid',False),('status','waiting_for_capture'),
    ('payment_method',{'type':'bank_card'}),('test','true'),('id','bad-id'),
])
def test_wrong_amount_merchant_intent_method_or_payment_status_is_never_accepted(field,value):
    result=payment()
    result[field]=value
    with pytest.raises(PaymentError):
        validate_payment(order(),result,CONFIG.shop_id)


def test_exact_confirmed_payment_and_pending_order_are_distinct():
    assert validate_payment(order(),payment(),CONFIG.shop_id)==(PAYMENT_ID,'succeeded')
    result=payment()
    result.update(status='pending',paid=False)
    assert validate_payment(order(),result,CONFIG.shop_id)==(PAYMENT_ID,'pending')
    bound=order()
    bound['provider_payment_id']=str(KEY)
    with pytest.raises(PaymentError):
        validate_payment(bound,payment(),CONFIG.shop_id)


@pytest.mark.parametrize('value',['NaN','Infinity','-1','0','1.001','10001.00'])
def test_invalid_decimal_amounts_are_rejected(value):
    with pytest.raises(PaymentError):
        minor_amount({'value':value,'currency':'RUB'})


def test_credentials_not_exposed_and_unconfigured_shop_stays_disabled(monkeypatch):
    assert CONFIG.secret_key not in repr(CONFIG)
    assert not replace(CONFIG,secret_key='').enabled
    with pytest.raises(PaymentError):
        YooKassa(MerchantSettings())
    monkeypatch.setenv('YOOKASSA_RETURN_URL','http://example.invalid/return')
    with pytest.raises(ValueError):
        MerchantSettings.from_env()
