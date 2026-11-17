from modules.payment_recover import sale_to_ping_fields


def test_fields_carry_reference_and_price():
    f = sale_to_ping_fields({"id": "s1", "price": 500, "product_id": "p", "created_at": "2026-11-16T10:00:00Z"}, "REF1")
    assert f["url_params[reference]"] == "REF1" and f["sale_id"] == "s1" and f["price"] == "500"
    assert f["sale_timestamp"] == "2026-11-16T10:00:00Z" and "subscription_id" not in f


def test_subscription_and_test_flag():
    f = sale_to_ping_fields({"id": "s2", "subscription_id": "sub", "test": True}, "R")
    assert f["subscription_id"] == "sub" and f["test"] == "true"
