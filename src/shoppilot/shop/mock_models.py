"""MockShop tables (Step E), shaped like the Shopify Admin API.

Table and column names follow Shopify (orders.name, financial_status, line_items, fulfillments,
refunds + transactions ...) so that ShopifyBackend can later return the same data without a new mapping.
Columns marked "mock-only" do not exist in Shopify; they are for tests, evaluation or custom features.

Separate metadata from the application tables (tickets, approvals ...) of Step H. Works on Postgres and SQLite.
Money is Numeric(12, 2) in `currency` (PKR), like Shopify. MockShop converts to whole PKR for the agent.
Datetimes are naive UTC.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Shopify ids are 64-bit numbers. SQLite only auto-numbers a plain INTEGER primary key.
BigId = BigInteger().with_variant(Integer(), "sqlite")


class MockBase(DeclarativeBase):
    pass


class CustomerRow(MockBase):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String, default="")
    last_name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    phone: Mapped[str] = mapped_column(String, default="")
    # Shopify default_address, flattened
    address1: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    province: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str] = mapped_column(String, default="Pakistan")
    tags: Mapped[str] = mapped_column(String, default="")  # comma separated; "flagged" marks a risky customer
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ProductRow(MockBase):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    body_html: Mapped[str] = mapped_column(Text, default="")
    vendor: Mapped[str] = mapped_column(String, default="")  # we use it as the supplier
    product_type: Mapped[str] = mapped_column(String, default="")  # we use it as the category
    status: Mapped[str] = mapped_column(String, default="active")  # active | draft | archived (draft = product draft)
    tags: Mapped[str] = mapped_column(String, default="")  # "non-refundable" marks a non-refundable product
    created_at: Mapped[datetime] = mapped_column(DateTime)


class VariantRow(MockBase):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, default="Default Title")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    inventory_item_id: Mapped[int] = mapped_column(BigInteger, unique=True)


class InventoryLevelRow(MockBase):
    __tablename__ = "inventory_levels"

    inventory_item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    location_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    available: Mapped[int] = mapped_column(Integer)
    # mock-only: Shopify has no reorder point or sales velocity; the Inventory agent needs them
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    avg_daily_sales: Mapped[float] = mapped_column(Float, default=0.0)


class OrderRow(MockBase):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)  # "#88731", what the customer quotes
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # pending | paid | partially_refunded | refunded   (COD stays pending, prepaid is paid)
    financial_status: Mapped[str] = mapped_column(String)
    # None | partial | fulfilled
    fulfillment_status: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="PKR")
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    payment_gateway_names: Mapped[str] = mapped_column(String)  # "Cash on Delivery (COD)" or e.g. "JazzCash"
    note: Mapped[str] = mapped_column(Text, default="")  # untrusted customer text
    tags: Mapped[str] = mapped_column(String, default="")
    # mock-only: seed label for tests and evaluation. It is never returned to the agent.
    scenario: Mapped[str] = mapped_column(String, default="")


class LineItemRow(MockBase):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"))
    sku: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # unit price


class FulfillmentRow(MockBase):
    """Shopify has no 'shipments' table: shipping data lives on the fulfillment."""

    __tablename__ = "fulfillments"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="success")  # success | cancelled
    # in_transit | out_for_delivery | delivered | failure ... (Shopify shipment_status)
    shipment_status: Mapped[str] = mapped_column(String)
    tracking_company: Mapped[str] = mapped_column(String, default="")
    tracking_number: Mapped[str] = mapped_column(String, unique=True, index=True)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{ts, status, location}]


class RefundRow(MockBase):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    note: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # mock-only: Shopify has no such column; Step H also keeps this key in the `actions` table
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)


class TransactionRow(MockBase):
    """Money movements. The refunded amount of an order is the sum of its kind='refund' transactions."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    refund_id: Mapped[int | None] = mapped_column(ForeignKey("refunds.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # sale | refund
    status: Mapped[str] = mapped_column(String)  # success | pending | failure
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String, default="PKR")
    gateway: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PurchaseOrderDraftRow(MockBase):
    """mock-only: Shopify has no native purchase orders, so this stays our own table."""

    __tablename__ = "purchase_order_drafts"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(ForeignKey("variants.sku"))
    qty: Mapped[int] = mapped_column(Integer)
    supplier: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime)
