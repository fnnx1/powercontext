def order_total(lines, tax_rate=0.0):
    """Return the order total for ``lines`` plus tax.

    Each line is a mapping with ``price`` and ``quantity`` keys. Lines with a
    quantity of at least 10 receive a 10% discount before tax is applied.
    """
    subtotal = 0.0
    for line in lines:
        quantity = int(line["quantity"])
        line_subtotal = float(line["price"]) * quantity
        if quantity >= 10:
            line_subtotal *= 0.9
        subtotal += line_subtotal
    return round(subtotal * (1.0 + float(tax_rate)), 2)
