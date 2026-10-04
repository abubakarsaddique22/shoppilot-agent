"""MockShop: a deterministic fake store on SQLAlchemy (Postgres in dev, SQLite in tests). Step E.

It is a ShopBackend, so agents cannot tell it from Shopify. The tables follow Shopify (see mock_models.py);
this class maps them to the small models in base.py that the agent and the policy engine use.
`now` is injectable so tests and evaluation get the same answer every time.

Mapping notes (the real ShopifyBackend must return the same values):
- Order.id is the order name ("#88731"), because that is what the customer quotes.
- amount_paid is the order total_price (whole PKR). refunded_total is the sum of successful refund transactions.
- status: cancelled_at -> cancelled, financial_status refunded -> refunded, delivered fulfillment -> delivered,
  any fulfillment -> shipped, otherwise placed.
- estimated_delivery comes from the fulfillment; an unshipped order gets created_at + 5 days.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.core.errors import IdempotencyConflict, NotFound, OrderNotFound, ShopBackendError, ValidationFailed
from shoppilot.shop.base import (
    Customer,
    InventoryLevel,
    Order,
    OrderItem,
    Product,
    ProductDraft,
    PurchaseOrderDraft,
    Refund,
    Shipment,
)
from shoppilot.shop.mock_models import (
    CustomerRow,
    FulfillmentRow,
    InventoryLevelRow,
    LineItemRow,
    OrderRow,
    ProductRow,
    PurchaseOrderDraftRow,
    RefundRow,
    TransactionRow,
    VariantRow,
)

DEFAULT_DELIVERY_DAYS = 5  # estimate for an order that has no fulfillment yet


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _pkr(amount: Decimal) -> int:
    return int(round(amount))


def _has_tag(tags: str, tag: str) -> bool:
    return tag in [t.strip().lower() for t in tags.split(",")]


def _is_cod(gateway: str) -> bool:
    g = gateway.lower()
    return "cash on delivery" in g or "(cod)" in g


def _find_order_row(s: Session, ref: str) -> OrderRow:
    """Accepts '#88731', '88731' or the numeric id."""
    ref = str(ref).strip()
    row = s.scalar(select(OrderRow).where(OrderRow.name == (ref if ref.startswith("#") else "#" + ref)))
    if row is None and ref.isdigit():
        row = s.get(OrderRow, int(ref))
    if row is None:
        raise OrderNotFound(f"order {ref} not found", details={"order_id": ref})
    return row


def _refund_model(s: Session, row: RefundRow) -> Refund:
    order = s.get(OrderRow, row.order_id)
    txs = s.scalars(select(TransactionRow).where(TransactionRow.refund_id == row.id)).all()
    return Refund(
        id=row.id,
        order_id=order.name if order else "",
        amount_pkr=_pkr(sum((t.amount for t in txs), Decimal(0))),
        reason=row.note,
        status="pending" if any(t.status == "pending" for t in txs) else "issued",
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


class MockShop:
    def __init__(self, session_factory: sessionmaker[Session], now: Callable[[], datetime] = utc_now) -> None:
        self._sf = session_factory
        self._now = now

    # ------------------------------------------------------------------ reads
    def get_order(self, order_id: str) -> Order:
        with self._sf() as s:
            return self._order(s, _find_order_row(s, order_id))

    def find_orders(self, email: str, limit: int = 5) -> list[Order]:
        with self._sf() as s:
            rows = s.scalars(
                select(OrderRow)
                .where(func.lower(OrderRow.email) == email.strip().lower())
                .order_by(OrderRow.created_at.desc(), OrderRow.id)
                .limit(max(1, limit))
            ).all()
            return [self._order(s, r) for r in rows]

    def get_customer(self, ref: str) -> Customer:
        with self._sf() as s:
            ref = ref.strip()
            row = s.get(CustomerRow, int(ref)) if ref.isdigit() else None
            if row is None:
                row = s.scalar(select(CustomerRow).where(func.lower(CustomerRow.email) == ref.lower()))
            if row is None:
                raise NotFound(f"customer {ref} not found", code="CUSTOMER_NOT_FOUND")
            return Customer(
                id=str(row.id),
                name=f"{row.first_name} {row.last_name}".strip(),
                email=row.email,
                phone=row.phone,
                city=row.city,
                flagged=_has_tag(row.tags, "flagged"),
            )

    def track_shipment(self, tracking_no: str) -> Shipment:
        with self._sf() as s:
            row = s.scalar(select(FulfillmentRow).where(FulfillmentRow.tracking_number == tracking_no))
            if row is None:
                raise NotFound(f"shipment {tracking_no} not found", code="SHIPMENT_NOT_FOUND")
            order = s.get(OrderRow, row.order_id)
            return Shipment(
                tracking_no=row.tracking_number,
                order_id=order.name if order else "",
                courier=row.tracking_company,
                status=row.shipment_status,
                last_update=row.updated_at,
                events=row.events or [],  # type: ignore[arg-type]
            )

    def get_product(self, sku: str) -> Product:
        with self._sf() as s:
            variant, product = self._variant_and_product(s, sku)
            return Product(
                sku=variant.sku,
                title=product.title,
                category=product.product_type,
                price_pkr=_pkr(variant.price),
                refundable=not _has_tag(product.tags, "non-refundable"),
                supplier=product.vendor,
            )

    def get_inventory(self, sku: str) -> InventoryLevel:
        with self._sf() as s:
            variant, _ = self._variant_and_product(s, sku)
            level = s.scalar(
                select(InventoryLevelRow).where(InventoryLevelRow.inventory_item_id == variant.inventory_item_id)
            )
            if level is None:
                raise NotFound(f"inventory for {sku} not found", code="PRODUCT_NOT_FOUND")
            return InventoryLevel(
                sku=variant.sku,
                on_hand=level.available,
                reorder_point=level.reorder_point,
                avg_daily_sales=level.avg_daily_sales,
            )

    # ----------------------------------------------------------------- writes
    def create_refund(self, order_id: str, amount_pkr: int, key: str, reason: str = "") -> Refund:
        """Idempotent: the same key returns the earlier refund and never refunds twice.
        Safety net: never refunds more than the order total minus earlier refunds."""
        with self._sf() as s:
            order = _find_order_row(s, order_id)
            earlier = s.scalar(select(RefundRow).where(RefundRow.idempotency_key == key))
            if earlier is not None:
                return self._same_or_conflict(s, earlier, order.id, amount_pkr)

            if amount_pkr <= 0:
                raise ShopBackendError("refund amount must be positive", code="INVALID_AMOUNT")
            total = _pkr(order.total_price)
            refunded = self._refunded_total(s, order.id)
            if amount_pkr > total - refunded:
                raise ShopBackendError(
                    f"refund {amount_pkr} is above the refundable balance {total - refunded}",
                    code="REFUND_EXCEEDS_BALANCE",
                    details={"order_id": order.name},
                )
            now = self._now()
            refund = RefundRow(order_id=order.id, note=reason, created_at=now, idempotency_key=key)
            s.add(refund)
            s.flush()  # gives the refund its id
            s.add(
                TransactionRow(
                    order_id=order.id,
                    refund_id=refund.id,
                    kind="refund",
                    status="success",
                    amount=Decimal(amount_pkr),
                    currency=order.currency,
                    gateway=order.payment_gateway_names,
                    created_at=now,
                )
            )
            order.financial_status = "refunded" if refunded + amount_pkr == total else "partially_refunded"
            try:
                s.commit()
            except IntegrityError:  # two calls with the same key at the same moment
                s.rollback()
                earlier = s.scalar(select(RefundRow).where(RefundRow.idempotency_key == key))
                if earlier is None:
                    raise
                return self._same_or_conflict(s, earlier, order.id, amount_pkr)
            return _refund_model(s, refund)

    def cancel_order(self, order_id: str) -> Order:
        with self._sf() as s:
            row = _find_order_row(s, order_id)
            current = self._order(s, row)
            if current.status != "placed":
                raise ShopBackendError(
                    f"order is {current.status} and cannot be cancelled", code="ORDER_NOT_CANCELLABLE"
                )
            row.cancelled_at = self._now()
            s.commit()
            return self._order(s, row)

    def create_product_draft(self, fields: dict[str, Any]) -> ProductDraft:
        """A Shopify product draft is a normal product with status 'draft'. The agent never publishes it."""
        title = str(fields.get("title", "")).strip()
        if not title:
            raise ValidationFailed("a product draft needs a title")
        tags = fields.get("tags", "")
        if isinstance(tags, list):
            tags = ", ".join(str(t) for t in tags)
        with self._sf() as s:
            row = ProductRow(
                title=title,
                body_html=str(fields.get("body_html") or fields.get("description") or ""),
                vendor=str(fields.get("vendor", "")),
                product_type=str(fields.get("product_type", "")),
                status="draft",
                tags=str(tags),
                created_at=self._now(),
            )
            s.add(row)
            s.commit()
            return ProductDraft(id=row.id, title=row.title, status=row.status, fields=fields, created_at=row.created_at)

    def create_purchase_order_draft(self, sku: str, qty: int, supplier: str) -> PurchaseOrderDraft:
        if qty <= 0 or not supplier.strip():
            raise ValidationFailed("a purchase order draft needs a positive quantity and a supplier")
        with self._sf() as s:
            self._variant_and_product(s, sku)  # raises NotFound for an unknown sku
            row = PurchaseOrderDraftRow(
                sku=sku, qty=qty, supplier=supplier.strip(), status="draft", created_at=self._now()
            )
            s.add(row)
            s.commit()
            return PurchaseOrderDraft(
                id=row.id, sku=row.sku, qty=row.qty, supplier=row.supplier, status=row.status, created_at=row.created_at
            )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _variant_and_product(s: Session, sku: str) -> tuple[VariantRow, ProductRow]:
        variant = s.scalar(select(VariantRow).where(VariantRow.sku == sku))
        product = s.get(ProductRow, variant.product_id) if variant else None
        if variant is None or product is None:
            raise NotFound(f"product {sku} not found", code="PRODUCT_NOT_FOUND")
        return variant, product

    @staticmethod
    def _refunded_total(s: Session, order_pk: int) -> int:
        total = s.scalar(
            select(func.coalesce(func.sum(TransactionRow.amount), 0)).where(
                TransactionRow.order_id == order_pk,
                TransactionRow.kind == "refund",
                TransactionRow.status == "success",
            )
        )
        return _pkr(Decimal(total or 0))

    @staticmethod
    def _same_or_conflict(s: Session, earlier: RefundRow, order_pk: int, amount_pkr: int) -> Refund:
        refund = _refund_model(s, earlier)
        if earlier.order_id != order_pk or refund.amount_pkr != amount_pkr:
            raise IdempotencyConflict(
                "this idempotency key was already used for a different refund", details={"key": earlier.idempotency_key}
            )
        return refund

    def _order(self, s: Session, row: OrderRow) -> Order:
        now = self._now()
        customer = s.get(CustomerRow, row.customer_id)
        fulfillment = s.scalar(
            select(FulfillmentRow)
            .where(FulfillmentRow.order_id == row.id, FulfillmentRow.status == "success")
            .order_by(FulfillmentRow.id.desc())
        )
        item_rows = s.execute(
            select(LineItemRow, ProductRow)
            .join(ProductRow, ProductRow.id == LineItemRow.product_id)
            .where(LineItemRow.order_id == row.id)
            .order_by(LineItemRow.id)
        ).all()
        refund_txs = s.scalars(
            select(TransactionRow).where(TransactionRow.order_id == row.id, TransactionRow.kind == "refund")
        ).all()
        recent_refunds = s.scalar(
            select(func.count(TransactionRow.id))
            .select_from(TransactionRow)
            .join(OrderRow, OrderRow.id == TransactionRow.order_id)
            .where(
                OrderRow.customer_id == row.customer_id,
                TransactionRow.kind == "refund",
                TransactionRow.status == "success",
                TransactionRow.created_at >= now - timedelta(days=90),
            )
        )

        delivered_at = fulfillment.delivered_at if fulfillment else None
        estimated = (fulfillment.estimated_delivery_at if fulfillment else None) or (
            row.created_at + timedelta(days=DEFAULT_DELIVERY_DAYS)
        )
        if row.cancelled_at is not None:
            status = "cancelled"
        elif row.financial_status == "refunded":
            status = "refunded"
        elif delivered_at is not None:
            status = "delivered"
        elif fulfillment is not None:
            status = "shipped"
        else:
            status = "placed"

        if delivered_at is not None:
            is_overdue = False
            days_late = max(0, (delivered_at - estimated).days)
        else:
            is_overdue = status not in ("cancelled", "refunded") and now > estimated
            days_late = max(0, (now - estimated).days) if is_overdue else 0

        return Order(
            id=row.name,
            customer_id=str(row.customer_id),
            customer_email=row.email,
            status=status,
            payment_method="cod" if _is_cod(row.payment_gateway_names) else "prepaid",
            amount_paid=_pkr(row.total_price),
            refunded_total=_pkr(sum((t.amount for t in refund_txs if t.status == "success"), Decimal(0))),
            placed_at=row.created_at,
            estimated_delivery=estimated,
            delivered_at=delivered_at,
            tracking_no=fulfillment.tracking_number if fulfillment else None,
            items=[
                OrderItem(sku=i.sku, title=i.title, qty=i.quantity, unit_price_pkr=_pkr(i.price))
                for i, _ in item_rows
            ],
            note=row.note,
            days_late=days_late,
            is_overdue=is_overdue,
            customer_flagged=bool(customer and _has_tag(customer.tags, "flagged")),
            refunds_last_90d=int(recent_refunds or 0),
            has_open_refund=any(t.status == "pending" for t in refund_txs),
            non_refundable=any(_has_tag(p.tags, "non-refundable") for _, p in item_rows),
        )
