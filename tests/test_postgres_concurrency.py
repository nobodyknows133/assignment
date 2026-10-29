import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.errors import DomainError
from app.main import _checkout_once, create_app
from app.models import Coupon, Order, Product

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to run PostgreSQL row-lock concurrency tests.",
)


def test_postgres_serializes_inventory_and_idempotent_checkout():
    schema = f"checkout_test_{uuid4().hex}"
    admin_engine = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    with admin_engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))

    test_engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    sessions = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    application = create_app(test_engine, sessions)

    try:
        from fastapi.testclient import TestClient

        with TestClient(application) as client:
            cart_ids = []
            for _ in range(3):
                cart_id = client.post("/carts").json()["id"]
                response = client.post(
                    f"/carts/{cart_id}/items",
                    json={"product_id": "p-005", "quantity": 1},
                )
                assert response.status_code == 201
                cart_ids.append(cart_id)

            def checkout(cart_id, key, coupon_code=None):
                with sessions() as session:
                    try:
                        order = _checkout_once(session, cart_id, key, coupon_code)
                        return ("ok", order.id)
                    except DomainError as exc:
                        return ("error", exc.code)

            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(
                    executor.map(
                        lambda args: checkout(*args),
                        [(cart_id, f"limited-{i}") for i, cart_id in enumerate(cart_ids)],
                    )
                )
            assert sum(result[0] == "ok" for result in results) == 2
            assert [result[1] for result in results if result[0] == "error"] == [
                "insufficient_inventory"
            ]

            repeated_cart = client.post("/carts").json()["id"]
            assert client.post(
                f"/carts/{repeated_cart}/items",
                json={"product_id": "p-001", "quantity": 1},
            ).status_code == 201
            with ThreadPoolExecutor(max_workers=2) as executor:
                repeated = list(
                    executor.map(
                        lambda _: checkout(repeated_cart, "same-concurrent-key"),
                        range(2),
                    )
                )
            assert repeated[0] == repeated[1]
            assert repeated[0][0] == "ok"

            coupon_cart_ids = []
            for product_id in ("p-001", "p-002"):
                cart_id = client.post("/carts").json()["id"]
                assert client.post(
                    f"/carts/{cart_id}/items",
                    json={"product_id": product_id, "quantity": 1},
                ).status_code == 201
                coupon_cart_ids.append(cart_id)
            with sessions() as session:
                session.add(
                    Coupon(
                        id="competing-coupon",
                        code="ONE-USE-ONLY",
                        milestone_order_count=5,
                        discount_percent=Decimal("10"),
                        status="available",
                    )
                )
                session.commit()
            with ThreadPoolExecutor(max_workers=2) as executor:
                coupon_results = list(
                    executor.map(
                        lambda args: checkout(*args, "ONE-USE-ONLY"),
                        [
                            (coupon_cart_ids[0], "coupon-contender-a"),
                            (coupon_cart_ids[1], "coupon-contender-b"),
                        ],
                    )
                )
            assert sum(result[0] == "ok" for result in coupon_results) == 1
            assert sum(
                result == ("error", "coupon_unavailable")
                for result in coupon_results
            ) == 1

        with sessions() as session:
            assert session.scalar(select(func.count(Order.id))) == 4
            limited = session.get(Product, "p-005")
            assert limited.inventory == 0
    finally:
        test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin_engine.dispose()
