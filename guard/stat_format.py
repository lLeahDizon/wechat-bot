"""Stable, compact formatting for aggregate token and cost statistics."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TOKEN_SCALES=[(1,'Token'),(1_000,'K Token'),(1_000_000,'M Token'),
              (1_000_000_000,'B Token'),(1_000_000_000_000,'T Token'),
              (1_000_000_000_000_000,'P Token'),(1_000_000_000_000_000_000,'E Token')]
MONEY_SCALES=[(Decimal('1000000000'),'BUSD'),(Decimal('1000000'),'MUSD'),
              (Decimal('1000'),'KUSD'),(Decimal('1'),'USD'),
              (Decimal('0.001'),'mUSD'),(Decimal('0.000001'),'µUSD'),
              (Decimal('0.000000001'),'nUSD'),(Decimal('0.000000000001'),'pUSD')]
CENT=Decimal('0.01')


def token_scale(value):
    if type(value) is not int or value<0:raise ValueError('invalid_token_count')
    for index,(scale,unit) in enumerate(TOKEN_SCALES):
        rounded=(Decimal(value)/scale).quantize(CENT,rounding=ROUND_HALF_UP)
        if rounded<1000 or index==len(TOKEN_SCALES)-1:return scale,unit
    return TOKEN_SCALES[-1]


def format_tokens(value,scale=None):
    if type(value) is not int or value<0:raise ValueError('invalid_token_count')
    if scale is None:scale,_=token_scale(value)
    if scale==1:return f'{value:,}'
    return f'{(Decimal(value)/scale).quantize(CENT,rounding=ROUND_HALF_UP):,.2f}'


def format_token_quantity(value):
    scale,unit=token_scale(value)
    return f'{format_tokens(value,scale)} {unit}'


def decimal_amount(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,float,Decimal)):
        raise ValueError('invalid_usd_amount')
    try:amount=Decimal(str(value))
    except InvalidOperation:raise ValueError('invalid_usd_amount') from None
    if not amount.is_finite() or amount<0 or amount>Decimal('999999999999999999'):
        raise ValueError('invalid_usd_amount')
    return amount


def format_money(value):
    amount=decimal_amount(value)
    if amount==0:return '$0.00','USD'
    usd_rounded=amount.quantize(CENT,rounding=ROUND_HALF_UP)
    if Decimal('0.01')<=amount and usd_rounded<1000:
        return f'${usd_rounded:,.2f}','USD'
    for index,(scale,unit) in enumerate(MONEY_SCALES):
        scaled=amount/scale
        rounded=scaled.quantize(CENT,rounding=ROUND_HALF_UP)
        if Decimal('1')<=rounded<1000:
            rendered=f'{rounded:,.2f}'
            return (f'${rendered}' if unit=='USD' else f'{rendered} {unit}'),unit
        if index==0 and rounded>=1000:
            return f'{rounded:,.2f} {unit}',unit
    largest_scale,largest_unit=MONEY_SCALES[0]
    largest=(amount/largest_scale).quantize(CENT,rounding=ROUND_HALF_UP)
    if largest>=1000:
        return f'{largest:,.2f} {largest_unit}',largest_unit
    smallest_scale,smallest_unit=MONEY_SCALES[-1]
    smallest=(amount/smallest_scale).quantize(CENT,rounding=ROUND_HALF_UP)
    if smallest==0:
        return f'{amount:.2E} USD','USD'
    return f'{smallest:,.2f} {smallest_unit}',smallest_unit
