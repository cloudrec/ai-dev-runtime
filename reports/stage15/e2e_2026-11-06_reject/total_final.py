# owner: pricing-team
from decimal import Decimal, ROUND_HALF_UP


def total(prices, tax_rate):
    subtotal = sum(Decimal(str(price)) for price in prices)
    rate = Decimal(str(tax_rate))
    grand_total = subtotal + subtotal * rate
    return grand_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
