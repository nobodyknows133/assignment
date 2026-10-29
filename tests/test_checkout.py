from decimal import Decimal

from app.models import Cart, Coupon, Product


def create_cart(client, product_id="p-001", quantity=1):
    cart_response = client.post("/carts")
    assert cart_response.status_code == 201
    cart = cart_response.json()
    add_response = client.post(
        f"/carts/{cart['id']}/items",
        json={"product_id": product_id, "quantity": quantity},
    )
    assert add_response.status_code == 201
    return cart["id"]


def test_checkout_retry_returns_same_order_and_charges_inventory_once(client):
    products = client.get("/products").json()
    backpack = next(product for product in products if product["id"] == "p-001")
    cart_id = create_cart(client, quantity=2)
    original_inventory = backpack["inventory"]

    first = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "checkout-request-1"},
        json={},
    )
    retry = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "checkout-request-1"},
        json={},
    )

    assert first.status_code == retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"]
    assert first.json()["gross_total"] == "99.90"
    with client.app.state.test_session_factory() as session:
        backpack = session.get(Product, "p-001")
        backpack.name = "Renamed after purchase"
        backpack.unit_price = Decimal("1.00")
        session.commit()
    historical_order = client.get(f"/orders/{first.json()['id']}").json()
    assert historical_order["gross_total"] == "99.90"
    assert historical_order["lines"][0]["product_name"] == "Everyday Backpack"
    assert historical_order["lines"][0]["unit_price"] == "49.95"
    remaining = next(
        product["inventory"]
        for product in client.get("/products").json()
        if product["id"] == "p-001"
    )
    assert remaining == original_inventory - 2

    different_key = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "checkout-request-2"},
        json={},
    )
    assert different_key.status_code == 409
    assert different_key.json()["error"]["code"] == "cart_checked_out"


def test_invalid_coupon_rolls_back_order_and_inventory(client):
    cart_id = create_cart(client, product_id="p-002", quantity=1)
    before = next(
        product["inventory"]
        for product in client.get("/products").json()
        if product["id"] == "p-002"
    )

    response = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "bad-coupon-attempt"},
        json={"coupon_code": "DOES-NOT-EXIST"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "coupon_not_found"
    assert client.get("/admin/report").json()["total_orders"] == 0
    after = next(
        product["inventory"]
        for product in client.get("/products").json()
        if product["id"] == "p-002"
    )
    assert after == before


def test_failed_checkout_does_not_consume_a_valid_coupon(client):
    cart_id = create_cart(client, product_id="p-002", quantity=1)
    with client.app.state.test_session_factory() as session:
        session.add(
            Coupon(
                id="still-available",
                code="TRY-LATER",
                milestone_order_count=5,
                discount_percent=Decimal("10"),
                status="available",
            )
        )
        product = session.get(Product, "p-002")
        product.inventory = 0
        session.commit()

    failed = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "failed-stock-check"},
        json={"coupon_code": "TRY-LATER"},
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "insufficient_inventory"
    with client.app.state.test_session_factory() as session:
        coupon = session.get(Coupon, "still-available")
        cart = session.get(Cart, cart_id)
        assert coupon.status == "available"
        assert cart.status == "open"


def test_coupon_is_consumed_only_by_successful_checkout_and_report_is_read_only(client):
    cart_id = create_cart(client, product_id="p-002", quantity=2)
    with client.app.state.test_session_factory() as session:
        session.add(
            Coupon(
                id="coupon-test",
                code="SAVE10",
                milestone_order_count=5,
                discount_percent=Decimal("10"),
                status="available",
            )
        )
        session.commit()

    checkout = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "coupon-checkout"},
        json={"coupon_code": "SAVE10"},
    )
    assert checkout.status_code == 200
    assert checkout.json()["gross_total"] == "37.00"
    assert checkout.json()["discount_total"] == "3.70"
    assert checkout.json()["net_total"] == "33.30"

    report_before = client.get("/admin/report").json()
    report_after = client.get("/admin/report").json()
    assert report_before == report_after
    assert report_before["gross_revenue"] == "37.00"
    assert report_before["total_discounts"] == "3.70"
    assert report_before["net_revenue"] == "33.30"
    assert report_before["coupons_redeemed"] == 1
    assert report_before["total_orders"] == 1
    assert report_before["successfully_purchased"] == [
        {
            "product_id": "p-002",
            "product_name": "Insulated Bottle",
            "quantity": 2,
        }
    ]


def test_cart_quantity_and_current_availability_are_validated(client):
    cart_id = client.post("/carts").json()["id"]
    missing = client.post(
        f"/carts/{cart_id}/items",
        json={"product_id": "not-a-product", "quantity": 1},
    )
    zero = client.post(
        f"/carts/{cart_id}/items",
        json={"product_id": "p-001", "quantity": 0},
    )
    too_many = client.post(
        f"/carts/{cart_id}/items",
        json={"product_id": "p-005", "quantity": 3},
    )
    assert missing.status_code == 404
    assert zero.status_code == 422
    assert too_many.status_code == 409


def test_cart_quantity_can_be_replaced_and_item_removed(client):
    cart_id = create_cart(client, product_id="p-002", quantity=1)
    updated = client.put(
        f"/carts/{cart_id}/items/p-002",
        json={"product_id": "p-002", "quantity": 3},
    )
    assert updated.status_code == 200
    assert updated.json()["items"][0]["quantity"] == 3
    assert updated.json()["gross_total"] == "55.50"

    removed = client.delete(f"/carts/{cart_id}/items/p-002")
    assert removed.status_code == 200
    assert removed.json()["items"] == []
    assert removed.json()["gross_total"] == "0.00"
    missing = client.delete(f"/carts/{cart_id}/items/p-002")
    assert missing.status_code == 404


def test_unavailable_coupon_does_not_create_second_order(client):
    cart_id = create_cart(client, product_id="p-002")
    coupon_id = "already-used"
    coupon = Coupon(
        id=coupon_id,
        code="USED10",
        milestone_order_count=5,
        discount_percent=Decimal("10"),
        status="available",
    )
    with client.app.state.test_session_factory() as session:
        session.add(coupon)
        session.commit()

    successful = client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": "used-first"},
        json={"coupon_code": "USED10"},
    )
    assert successful.status_code == 200
    retry_cart = create_cart(client, product_id="p-002")
    rejected = client.post(
        f"/carts/{retry_cart}/checkout",
        headers={"Idempotency-Key": "used-second"},
        json={"coupon_code": "USED10"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "coupon_unavailable"
    assert client.get("/admin/report").json()["total_orders"] == 1


def test_idempotency_key_cannot_be_reused_for_another_cart(client):
    completed_cart = create_cart(client, product_id="p-002")
    first = client.post(
        f"/carts/{completed_cart}/checkout",
        headers={"Idempotency-Key": "globally-unique-request"},
        json={},
    )
    assert first.status_code == 200
    another_cart = create_cart(client, product_id="p-002")
    conflict = client.post(
        f"/carts/{another_cart}/checkout",
        headers={"Idempotency-Key": "globally-unique-request"},
        json={},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_admin_generates_one_coupon_for_each_reached_milestone(client):
    for index in range(5):
        cart_id = create_cart(client, product_id="p-002")
        response = client.post(
            f"/carts/{cart_id}/checkout",
            headers={"Idempotency-Key": f"milestone-order-{index}"},
            json={},
        )
        assert response.status_code == 200

    generated = client.post("/admin/coupons/generate")
    assert generated.status_code == 201
    assert generated.json()["milestone_order_count"] == 5
    assert generated.json()["discount_percent"] == "10.00"
    assert generated.json()["status"] == "available"
    duplicate = client.post("/admin/coupons/generate")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "coupon_already_generated"
    report = client.get("/admin/report").json()
    assert report["coupons_generated"] == report["coupons_available"] == 1
    assert report["coupons_redeemed"] == 0
    assert report["total_orders"] == 5
