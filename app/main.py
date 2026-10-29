import secrets
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.errors import DomainError
from app.models import Cart, CartItem, Coupon, Order, OrderLine, Product
from app.schemas import (
    CartItemInput,
    CartOut,
    CheckoutInput,
    CouponOut,
    OrderOut,
    ProductOut,
    ReportOut,
)

SEED_PRODUCTS = (
    ("p-001", "Everyday Backpack", Decimal("49.95"), 25),
    ("p-002", "Insulated Bottle", Decimal("18.50"), 40),
    ("p-003", "Wireless Mouse", Decimal("29.99"), 15),
    ("p-004", "Desk Lamp", Decimal("64.00"), 8),
    ("p-005", "Limited Edition Notebook", Decimal("12.75"), 2),
)
CENT = Decimal("0.01")
router = APIRouter()


def create_app(
    db_engine: Engine = engine,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(bind=db_engine)
        with session_factory.begin() as session:
            if db_engine.dialect.name in {"postgresql", "sqlite"}:
                insert_product = (
                    pg_insert(Product)
                    if db_engine.dialect.name == "postgresql"
                    else sqlite_insert(Product)
                )
                session.execute(
                    insert_product.on_conflict_do_nothing(index_elements=[Product.id]),
                    [
                        {
                            "id": product_id,
                            "name": name,
                            "unit_price": price,
                            "inventory": inventory,
                        }
                        for product_id, name, price, inventory in SEED_PRODUCTS
                    ],
                )
            else:
                for product_id, name, price, inventory in SEED_PRODUCTS:
                    if session.get(Product, product_id) is None:
                        session.add(
                            Product(
                                id=product_id,
                                name=name,
                                unit_price=price,
                                inventory=inventory,
                            )
                        )
        yield

    application = FastAPI(
        title="Checkout and Rewards API", version="1.0.0", lifespan=lifespan
    )

    def override_get_db():
        with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = override_get_db
    application.add_exception_handler(DomainError, handle_domain_error)
    application.include_router(router)
    return application


def handle_domain_error(_, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _get_cart(session: Session, cart_id: str, *, lock: bool = False) -> Cart:
    statement = select(Cart).where(Cart.id == cart_id)
    if lock:
        statement = statement.with_for_update()
    cart = session.scalar(statement)
    if cart is None:
        raise DomainError(404, "cart_not_found", "The requested cart does not exist.")
    return cart


def _cart_response(cart: Cart) -> dict:
    if cart.status == "checked_out" and cart.order is not None:
        lines = [
            {
                "product_id": line.product_id,
                "name": line.product_name,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "line_total": line.line_total,
            }
            for line in cart.order.lines
        ]
        return {
            "id": cart.id,
            "status": cart.status,
            "items": lines,
            "gross_total": cart.order.gross_total,
        }

    lines = []
    gross_total = Decimal("0.00")
    for item in sorted(cart.items, key=lambda row: row.product_id):
        line_total = _money(item.product.unit_price * item.quantity)
        gross_total += line_total
        lines.append(
            {
                "product_id": item.product_id,
                "name": item.product.name,
                "quantity": item.quantity,
                "unit_price": item.product.unit_price,
                "line_total": line_total,
            }
        )
    return {
        "id": cart.id,
        "status": cart.status,
        "items": lines,
        "gross_total": _money(gross_total),
    }


def _order_response(session: Session, order: Order) -> dict:
    coupon_code = None
    if order.coupon_id is not None:
        coupon_code = session.scalar(
            select(Coupon.code).where(Coupon.id == order.coupon_id)
        )
    return {
        "id": order.id,
        "cart_id": order.cart_id,
        "idempotency_key": order.idempotency_key,
        "coupon_code": coupon_code,
        "gross_total": order.gross_total,
        "discount_total": order.discount_total,
        "net_total": order.net_total,
        "created_at": order.created_at,
        "lines": [
            {
                "product_id": line.product_id,
                "product_name": line.product_name,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "line_total": line.line_total,
            }
            for line in order.lines
        ],
    }


@router.get("/products", response_model=list[ProductOut])
def list_products(session: Session = Depends(get_db)):
    return session.scalars(select(Product).order_by(Product.id)).all()


@router.post("/carts", response_model=CartOut, status_code=status.HTTP_201_CREATED)
def create_cart(session: Session = Depends(get_db)):
    cart = Cart(id=str(uuid4()), status="open")
    session.add(cart)
    session.commit()
    session.refresh(cart)
    return _cart_response(cart)


@router.get("/carts/{cart_id}", response_model=CartOut)
def get_cart(cart_id: str, session: Session = Depends(get_db)):
    cart = session.scalar(
        select(Cart)
        .options(
            joinedload(Cart.items).joinedload(CartItem.product),
            joinedload(Cart.order).joinedload(Order.lines),
        )
        .where(Cart.id == cart_id)
    )
    if cart is None:
        raise DomainError(404, "cart_not_found", "The requested cart does not exist.")
    return _cart_response(cart)


@router.post(
    "/carts/{cart_id}/items",
    response_model=CartOut,
    status_code=status.HTTP_201_CREATED,
)
def add_cart_item(
    cart_id: str, item_input: CartItemInput, session: Session = Depends(get_db)
):
    with session.begin():
        cart = _get_cart(session, cart_id, lock=True)
        if cart.status != "open":
            raise DomainError(409, "cart_checked_out", "A checked-out cart cannot be changed.")
        product = session.scalar(
            select(Product).where(Product.id == item_input.product_id).with_for_update()
        )
        if product is None:
            raise DomainError(404, "product_not_found", "The requested product does not exist.")
        existing = session.get(CartItem, (cart_id, item_input.product_id))
        quantity = item_input.quantity + (existing.quantity if existing else 0)
        if quantity > product.inventory:
            raise DomainError(
                409,
                "insufficient_inventory",
                f"Only {product.inventory} units are currently available.",
            )
        if existing:
            existing.quantity = quantity
        else:
            session.add(
                CartItem(
                    cart_id=cart_id,
                    product_id=item_input.product_id,
                    quantity=quantity,
                )
            )
    return get_cart(cart_id, session)


@router.put("/carts/{cart_id}/items/{product_id}", response_model=CartOut)
def update_cart_item(
    cart_id: str,
    product_id: str,
    item_input: CartItemInput,
    session: Session = Depends(get_db),
):
    if item_input.product_id != product_id:
        raise DomainError(400, "product_id_mismatch", "The path and body product IDs must match.")
    with session.begin():
        cart = _get_cart(session, cart_id, lock=True)
        if cart.status != "open":
            raise DomainError(409, "cart_checked_out", "A checked-out cart cannot be changed.")
        product = session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        if product is None:
            raise DomainError(404, "product_not_found", "The requested product does not exist.")
        cart_item = session.get(CartItem, (cart_id, product_id))
        if cart_item is None:
            raise DomainError(404, "cart_item_not_found", "The product is not in this cart.")
        if item_input.quantity > product.inventory:
            raise DomainError(
                409,
                "insufficient_inventory",
                f"Only {product.inventory} units are currently available.",
            )
        cart_item.quantity = item_input.quantity
    return get_cart(cart_id, session)


@router.delete("/carts/{cart_id}/items/{product_id}", response_model=CartOut)
def remove_cart_item(cart_id: str, product_id: str, session: Session = Depends(get_db)):
    with session.begin():
        cart = _get_cart(session, cart_id, lock=True)
        if cart.status != "open":
            raise DomainError(409, "cart_checked_out", "A checked-out cart cannot be changed.")
        cart_item = session.get(CartItem, (cart_id, product_id))
        if cart_item is None:
            raise DomainError(404, "cart_item_not_found", "The product is not in this cart.")
        session.delete(cart_item)
    return get_cart(cart_id, session)


def _checkout_once(
    session: Session, cart_id: str, idempotency_key: str, coupon_code: str | None
) -> Order:
    with session.begin():
        cart = _get_cart(session, cart_id, lock=True)
        previous_key_order = session.scalar(
            select(Order).where(Order.idempotency_key == idempotency_key)
        )
        if previous_key_order is not None:
            if previous_key_order.cart_id != cart_id:
                raise DomainError(
                    409,
                    "idempotency_key_conflict",
                    "This idempotency key was already used for a different cart.",
                )
            return previous_key_order
        if cart.status != "open":
            raise DomainError(409, "cart_checked_out", "This cart has already been checked out.")

        cart_items = session.scalars(
            select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.product_id)
        ).all()
        if not cart_items:
            raise DomainError(409, "empty_cart", "An empty cart cannot be checked out.")

        product_ids = [item.product_id for item in cart_items]
        products = session.scalars(
            select(Product)
            .where(Product.id.in_(product_ids))
            .order_by(Product.id)
            .with_for_update()
        ).all()
        products_by_id = {product.id: product for product in products}
        gross_total = Decimal("0.00")
        lines: list[OrderLine] = []
        for item in cart_items:
            product = products_by_id.get(item.product_id)
            if product is None:
                raise DomainError(409, "product_unavailable", "A cart product is no longer available.")
            if item.quantity > product.inventory:
                raise DomainError(
                    409,
                    "insufficient_inventory",
                    f"Insufficient inventory for {product.name}; "
                    f"{product.inventory} units remain.",
                )
            line_total = _money(product.unit_price * item.quantity)
            gross_total += line_total
            lines.append(
                OrderLine(
                    product_id=product.id,
                    product_name=product.name,
                    quantity=item.quantity,
                    unit_price=product.unit_price,
                    line_total=line_total,
                )
            )

        coupon = None
        discount_total = Decimal("0.00")
        if coupon_code is not None:
            coupon = session.scalar(
                select(Coupon).where(Coupon.code == coupon_code).with_for_update()
            )
            if coupon is None:
                raise DomainError(404, "coupon_not_found", "The requested coupon does not exist.")
            if coupon.status != "available":
                raise DomainError(409, "coupon_unavailable", "The coupon has already been redeemed.")
            discount_total = min(
                gross_total,
                _money(gross_total * coupon.discount_percent / Decimal("100")),
            )

        order = Order(
            id=str(uuid4()),
            cart_id=cart.id,
            idempotency_key=idempotency_key,
            coupon_id=coupon.id if coupon else None,
            gross_total=_money(gross_total),
            discount_total=discount_total,
            net_total=max(Decimal("0.00"), _money(gross_total - discount_total)),
            lines=lines,
        )
        session.add(order)
        for item in cart_items:
            product = products_by_id[item.product_id]
            product.inventory -= item.quantity
        if coupon is not None:
            coupon.status = "redeemed"
        cart.status = "checked_out"
        session.flush()
        return order


@router.post("/carts/{cart_id}/checkout", response_model=OrderOut)
def checkout(
    cart_id: str,
    checkout_input: CheckoutInput,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    session: Session = Depends(get_db),
):
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise DomainError(400, "invalid_idempotency_key", "The idempotency key cannot be blank.")
    try:
        order = _checkout_once(
            session, cart_id, idempotency_key, checkout_input.coupon_code
        )
    except IntegrityError as exc:
        constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        sqlite_unique_conflict = str(exc.orig) in {
            "UNIQUE constraint failed: orders.cart_id",
            "UNIQUE constraint failed: orders.idempotency_key",
        }
        if constraint_name not in {"uq_orders_cart", "uq_orders_idempotency_key"} and not sqlite_unique_conflict:
            raise
        session.rollback()
        order = session.scalar(
            select(Order).options(joinedload(Order.lines)).where(
                Order.idempotency_key == idempotency_key
            )
        )
        if order is not None:
            if order.cart_id == cart_id:
                return _order_response(session, order)
            raise DomainError(
                409,
                "idempotency_key_conflict",
                "This idempotency key is already associated with another checkout.",
            ) from exc
        if session.scalar(select(Order.id).where(Order.cart_id == cart_id)) is not None:
            raise DomainError(
                409, "cart_checked_out", "This cart has already been checked out."
            ) from exc
        raise DomainError(
            409,
            "idempotency_key_conflict",
            "This idempotency key is already associated with another checkout.",
        ) from exc
    return _order_response(session, order)


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: str, session: Session = Depends(get_db)):
    order = session.scalar(
        select(Order).options(joinedload(Order.lines)).where(Order.id == order_id)
    )
    if order is None:
        raise DomainError(404, "order_not_found", "The requested order does not exist.")
    return _order_response(session, order)


@router.post(
    "/admin/coupons/generate",
    response_model=CouponOut,
    status_code=status.HTTP_201_CREATED,
)
def generate_coupon(session: Session = Depends(get_db)):
    try:
        with session.begin():
            # A common committed order row serializes competing generators.
            latest_order = session.scalar(
                select(Order.id)
                .order_by(Order.created_at.desc(), Order.id.desc())
                .limit(1)
                .with_for_update()
            )
            order_count = session.scalar(select(func.count(Order.id))) or 0
            reached_milestone = (
                order_count // settings.reward_order_interval
            ) * settings.reward_order_interval
            if latest_order is None or reached_milestone == 0:
                raise DomainError(
                    409, "coupon_not_eligible", "No reward milestone has been reached."
                )
            existing_milestones = set(
                session.scalars(
                    select(Coupon.milestone_order_count).where(
                        Coupon.milestone_order_count <= reached_milestone
                    )
                ).all()
            )
            milestone = next(
                (
                    count
                    for count in range(
                        settings.reward_order_interval,
                        reached_milestone + 1,
                        settings.reward_order_interval,
                    )
                    if count not in existing_milestones
                ),
                None,
            )
            if milestone is None:
                raise DomainError(
                    409,
                    "coupon_already_generated",
                    "Every reached reward milestone already has a coupon.",
                )
            coupon = Coupon(
                id=str(uuid4()),
                code=secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper(),
                milestone_order_count=milestone,
                discount_percent=settings.reward_discount_percent,
                status="available",
            )
            session.add(coupon)
            session.flush()
    except IntegrityError as exc:
        constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        sqlite_unique_conflict = str(exc.orig) in {
            "UNIQUE constraint failed: coupons.milestone_order_count",
            "UNIQUE constraint failed: coupons.code",
        }
        if constraint_name not in {"uq_coupons_milestone", "uq_coupons_code"} and not sqlite_unique_conflict:
            raise
        session.rollback()
        raise DomainError(
            409,
            "coupon_generation_conflict",
            "Another administrator generated a coupon for this milestone.",
        ) from exc
    return {
        "code": coupon.code,
        "milestone_order_count": coupon.milestone_order_count,
        "discount_percent": _money(coupon.discount_percent),
        "status": coupon.status,
    }


@router.get("/admin/report", response_model=ReportOut)
def report(session: Session = Depends(get_db)):
    totals = session.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.gross_total), 0),
            func.coalesce(func.sum(Order.discount_total), 0),
            func.coalesce(func.sum(Order.net_total), 0),
        )
    ).one()
    purchased = session.execute(
        select(
            OrderLine.product_id,
            func.coalesce(Product.name, func.max(OrderLine.product_name)),
            func.sum(OrderLine.quantity),
        )
        .join(Order, Order.id == OrderLine.order_id)
        .outerjoin(Product, Product.id == OrderLine.product_id)
        .group_by(OrderLine.product_id, Product.name)
        .order_by(OrderLine.product_id)
    ).all()
    coupon_counts = dict(
        session.execute(
            select(Coupon.status, func.count(Coupon.id)).group_by(Coupon.status)
        ).all()
    )
    return {
        "successfully_purchased": [
            {
                "product_id": row[0],
                "product_name": row[1],
                "quantity": row[2],
            }
            for row in purchased
        ],
        "gross_revenue": _money(Decimal(totals[1])),
        "total_discounts": _money(Decimal(totals[2])),
        "net_revenue": _money(Decimal(totals[3])),
        "coupons_generated": sum(coupon_counts.values()),
        "coupons_available": coupon_counts.get("available", 0),
        "coupons_redeemed": coupon_counts.get("redeemed", 0),
        "total_orders": totals[0],
    }


app = create_app()
