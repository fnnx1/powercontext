"""Small pricing module used as the business project for the SDD demo."""


def order_total(lines, tax_rate=0.0):
    """Return the order total for ``lines`` plus tax.

    Each line is a mapping with ``price`` and ``quantity`` keys.
    """
    subtotal = 0.0
    for line in lines:
        subtotal += float(line["price"]) * int(line["quantity"])
    return round(subtotal * (1.0 + float(tax_rate)), 2)
