"""The only door to a store platform (Step E). Agents see this interface and nothing else.

MockShop (Postgres or SQLite) and ShopifyBackend are two implementations of it.
Errors are raised as AppError subclasses from shoppilot.core.errors (OrderNotFound, NotFound, ShopBackendError ...).
Amounts are whole PKR. Datetimes are naive UTC.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

PaymentMethod = Literal["prepaid", "cod"]


class Customer(BaseModel):
    id: str
    name: str
    email: str
    phone: str = ""
    city: str = ""
    flagged: bool = False


class OrderItem(BaseModel):
    sku: str
    title: str
    qty: int
    unit_price_pkr: int


class Order(BaseModel):
    """Order facts as the agent and the policy engine need them. `note` is untrusted customer text."""

    id: str
    customer_id: str
    customer_email: str
    status: str  # placed | shipped | delivered | cancelled | refunded
    payment_method: PaymentMethod
    amount_paid: int
    refunded_total: int = 0
    placed_at: datetime
    estimated_delivery: datetime
    delivered_at: datetime | None = None
    tracking_no: str | None = None
    items: list[OrderItem] = Field(default_factory=list)
    note: str = ""
    # facts computed by the backend at read time (same names the policy engine reads)
    days_late: int = 0
    is_overdue: bool = False
    customer_flagged: bool = False
    refunds_last_90d: int = 0
    has_open_refund: bool = False
    non_refundable: bool = False


class ShipmentEvent(BaseModel):
    ts: datetime
    status: str
    location: str = ""


class Shipment(BaseModel):
    tracking_no: str
    order_id: str
    courier: str
    status: str  # in_transit | delayed | delivered
    last_update: datetime
    events: list[ShipmentEvent] = Field(default_factory=list)


class Refund(BaseModel):
    id: int
    order_id: str
    amount_pkr: int
    reason: str = ""
    status: str  # issued | pending
    idempotency_key: str
    created_at: datetime


class Product(BaseModel):
    sku: str
    title: str
    category: str
    price_pkr: int
    refundable: bool = True
    supplier: str = ""


class InventoryLevel(BaseModel):
    sku: str
    on_hand: int
    reorder_point: int
    avg_daily_sales: float


class ProductDraft(BaseModel):
    id: int
    title: str
    status: str = "draft"
    fields: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PurchaseOrderDraft(BaseModel):
    id: int
    sku: str
    qty: int
    supplier: str
    status: str = "draft"
    created_at: datetime


@runtime_checkable
class ShopBackend(Protocol):
    def get_order(self, order_id: str) -> Order: ...

    def find_orders(self, email: str, limit: int = 5) -> list[Order]: ...

    def get_customer(self, ref: str) -> Customer: ...

    def track_shipment(self, tracking_no: str) -> Shipment: ...

    def create_refund(self, order_id: str, amount_pkr: int, key: str, reason: str = "") -> Refund: ...

    def cancel_order(self, order_id: str) -> Order: ...

    def get_product(self, sku: str) -> Product: ...

    def get_inventory(self, sku: str) -> InventoryLevel: ...

    def create_product_draft(self, fields: dict[str, Any]) -> ProductDraft: ...

    def create_purchase_order_draft(self, sku: str, qty: int, supplier: str) -> PurchaseOrderDraft: ...
