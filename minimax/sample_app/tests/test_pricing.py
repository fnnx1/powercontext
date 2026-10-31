import pytest

from sample_app.src.pricing import order_total


def test_target_volume_discount_applies_before_tax():
    lines = [{"price": 100.0, "quantity": 10}]
    assert order_total(lines, tax_rate=0.0) == 900.0


def test_target_volume_discount_is_per_line():
    lines = [
        {"price": 50.0, "quantity": 10},
        {"price": 20.0, "quantity": 2},
    ]
    assert order_total(lines) == 490.0


def test_regression_small_quantity_has_no_discount():
    assert order_total([{"price": 19.99, "quantity": 2}]) == 39.98


def test_regression_tax_still_applies():
    assert order_total([{"price": 100.0, "quantity": 1}], tax_rate=0.1) == 110.0


def test_regression_empty_order():
    assert order_total([]) == 0.0


def test_regression_rounding_two_decimals():
    assert order_total([{"price": 0.335, "quantity": 1}]) == 0.34
