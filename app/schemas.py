from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CartItemInput(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, strict=True)


class CheckoutInput(BaseModel):
    coupon_code: str | None = None


class ProductOut(BaseModel):
    id: str
    name: str
    unit_price: Decimal
    inventory: int


class CartLineOut(BaseModel):
    product_id: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class CartOut(BaseModel):
    id: str
    status: str
    items: list[CartLineOut]
    gross_total: Decimal


class OrderLineOut(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class OrderOut(BaseModel):
    id: str
    cart_id: str
    idempotency_key: str
    coupon_code: str | None
    gross_total: Decimal
    discount_total: Decimal
    net_total: Decimal
    created_at: datetime
    lines: list[OrderLineOut]


class CouponOut(BaseModel):
    code: str
    milestone_order_count: int
    discount_percent: Decimal
    status: str


class ProductQuantityOut(BaseModel):
    product_id: str
    product_name: str
    quantity: int


class ReportOut(BaseModel):
    successfully_purchased: list[ProductQuantityOut]
    gross_revenue: Decimal
    total_discounts: Decimal
    net_revenue: Decimal
    coupons_generated: int
    coupons_available: int
    coupons_redeemed: int
    total_orders: int
