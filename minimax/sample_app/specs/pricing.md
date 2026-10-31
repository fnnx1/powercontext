# Pricing rules

## Base totals

- Each line has a unit `price` and an integer `quantity`.
- `order_total` sums `price * quantity`, applies `tax_rate`, and rounds to two decimals.

## Volume discount (required change)

- A line with `quantity >= 10` receives a **10% discount** on that line's subtotal.
- A line with `quantity < 10` receives no discount.
- The discount applies before tax.
- The returned value remains rounded to two decimals.
- Empty orders return `0.0`.

## Compatibility

- Existing callers pass lists of dicts with `price` and `quantity`.
- Tax behaviour and rounding for small quantities must not change.
