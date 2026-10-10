from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import zoneinfo

from django.db import IntegrityError, transaction

from core.exceptions import (
    IdempotencyConflictError,
    InsufficientStockError,
    InvalidTransitionError,
    NotFound,
    OrderLockedError,
    ValidationError,
)
from core.models import DailyOrderSequence, Dealer, Inventory, Order, OrderItem, Product

GST_RATE = Decimal("0.18")
KOLKATA_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")


def compute_hash(data: dict) -> str:
    serialized = json.dumps(data, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def generate_order_number(today) -> str:
    seq, _ = DailyOrderSequence.objects.select_for_update().get_or_create(
        date=today,
        defaults={"last_number": 0},
    )
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"ORD-{today.strftime('%Y%m%d')}-{seq.last_number:04d}"


def calculate_line_item(unit_price: Decimal, quantity: int):
    line_subtotal = (unit_price * quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    line_tax = (line_subtotal * GST_RATE).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    line_total = line_subtotal + line_tax
    return line_subtotal, line_tax, line_total


def validate_and_prepare_items(raw_items):
    if not isinstance(raw_items, list) or len(raw_items) == 0:
        raise ValidationError("Order must contain at least one item.")

    seen_skus = set()
    cleaned_items = []

    for item in raw_items:
        if not isinstance(item, dict):
            raise ValidationError("Each item must be an object.")
        sku = item.get("sku")
        quantity = item.get("quantity")

        if not sku or not isinstance(sku, str):
            raise ValidationError("Each item must have a valid SKU string.")
        sku = sku.strip()

        if sku in seen_skus:
            raise ValidationError(f"Duplicate SKU '{sku}' in order items.")
        seen_skus.add(sku)

        if not isinstance(quantity, int) or quantity < 1:
            raise ValidationError(
                f"Item '{sku}' quantity must be an integer greater than or equal to 1."
            )

        cleaned_items.append((sku, quantity))

    # Fetch products
    skus = [sku for sku, _ in cleaned_items]
    products = {p.sku: p for p in Product.objects.filter(sku__in=skus)}

    missing_skus = [sku for sku in skus if sku not in products]
    if missing_skus:
        raise ValidationError(f"Unknown SKU(s): {', '.join(missing_skus)}")

    prepared = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")

    for sku, qty in cleaned_items:
        product = products[sku]
        unit_price = product.price
        l_sub, l_tax, l_tot = calculate_line_item(unit_price, qty)
        subtotal += l_sub
        tax_total += l_tax
        prepared.append({
            "product": product,
            "quantity": qty,
            "unit_price": unit_price,
            "line_subtotal": l_sub,
            "line_tax": l_tax,
            "line_total": l_tot,
        })

    grand_total = subtotal + tax_total
    return prepared, subtotal, tax_total, grand_total


def create_draft_order(dealer_id: int, raw_items: list, idempotency_key: str = None, raw_body_data: dict = None) -> Order:
    try:
        dealer = Dealer.objects.get(id=dealer_id)
    except (Dealer.DoesNotExist, ValueError, TypeError):
        raise ValidationError(f"Dealer with id {dealer_id} does not exist.")

    req_hash = compute_hash(raw_body_data) if raw_body_data is not None else ""

    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        existing = Order.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_hash == req_hash:
                return existing
            raise IdempotencyConflictError(
                "Idempotency key reused with different request body."
            )

    prepared, subtotal, tax_total, grand_total = validate_and_prepare_items(raw_items)

    today = datetime.now(KOLKATA_TZ).date()

    with transaction.atomic():
        if idempotency_key:
            existing = Order.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
            if existing:
                if existing.request_hash == req_hash:
                    return existing
                raise IdempotencyConflictError(
                    "Idempotency key reused with different request body."
                )

        order_no = generate_order_number(today)

        try:
            order = Order.objects.create(
                order_number=order_no,
                dealer=dealer,
                status=Order.Status.DRAFT,
                subtotal=subtotal,
                tax_total=tax_total,
                grand_total=grand_total,
                idempotency_key=idempotency_key or None,
                request_hash=req_hash,
            )
        except IntegrityError:
            if idempotency_key:
                existing = Order.objects.filter(idempotency_key=idempotency_key).first()
                if existing:
                    if existing.request_hash == req_hash:
                        return existing
                    raise IdempotencyConflictError(
                        "Idempotency key reused with different request body."
                    )
            raise

        order_items = [
            OrderItem(
                order=order,
                product=item["product"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                line_subtotal=item["line_subtotal"],
                line_tax=item["line_tax"],
                line_total=item["line_total"],
            )
            for item in prepared
        ]
        OrderItem.objects.bulk_create(order_items)

    return order


def update_draft_order(order_number: str, raw_items: list) -> Order:
    try:
        order = Order.objects.get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFound(f"Order '{order_number}' not found.")

    if order.status != Order.Status.DRAFT:
        raise OrderLockedError(f"Cannot edit order with status {order.status}.")

    prepared, subtotal, tax_total, grand_total = validate_and_prepare_items(raw_items)

    with transaction.atomic():
        order = Order.objects.select_for_update().get(order_number=order_number)
        if order.status != Order.Status.DRAFT:
            raise OrderLockedError(f"Cannot edit order with status {order.status}.")

        order.items.all().delete()

        order_items = [
            OrderItem(
                order=order,
                product=item["product"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                line_subtotal=item["line_subtotal"],
                line_tax=item["line_tax"],
                line_total=item["line_total"],
            )
            for item in prepared
        ]
        OrderItem.objects.bulk_create(order_items)

        order.subtotal = subtotal
        order.tax_total = tax_total
        order.grand_total = grand_total
        order.save(update_fields=["subtotal", "tax_total", "grand_total", "updated_at"])

    return order


def confirm_order(order_number: str) -> Order:
    try:
        order = Order.objects.get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFound(f"Order '{order_number}' not found.")

    if order.status != Order.Status.DRAFT:
        raise InvalidTransitionError(
            f"Cannot confirm order with current status '{order.status}'."
        )

    with transaction.atomic():
        order = Order.objects.select_for_update().get(order_number=order_number)
        if order.status != Order.Status.DRAFT:
            raise InvalidTransitionError(
                f"Cannot confirm order with current status '{order.status}'."
            )

        items = list(order.items.select_related("product").all())
        skus = sorted([it.product_id for it in items])

        inventories = {
            inv.product_id: inv
            for inv in Inventory.objects.select_for_update()
            .filter(product_id__in=skus)
            .order_by("product_id")
        }

        short_lines = []
        messages = []

        for it in items:
            inv = inventories.get(it.product_id)
            avail = inv.quantity if inv else 0
            if it.quantity > avail:
                short_lines.append({
                    "sku": it.product_id,
                    "requested": it.quantity,
                    "available": avail,
                })
                messages.append(
                    f"Insufficient stock for {it.product.name}. Available: {avail}, Requested: {it.quantity}"
                )

        if short_lines:
            raise InsufficientStockError(
                message=" ".join(messages),
                details=short_lines,
            )

        # Deduct stock
        for it in items:
            inv = inventories[it.product_id]
            inv.quantity -= it.quantity
            inv.save(update_fields=["quantity"])

        order.status = Order.Status.CONFIRMED
        order.save(update_fields=["status", "updated_at"])

    return order


def dispatch_order(order_number: str) -> Order:
    try:
        order = Order.objects.get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFound(f"Order '{order_number}' not found.")

    if order.status != Order.Status.CONFIRMED:
        raise InvalidTransitionError(
            f"Cannot dispatch order with status '{order.status}'."
        )

    with transaction.atomic():
        order = Order.objects.select_for_update().get(order_number=order_number)
        if order.status != Order.Status.CONFIRMED:
            raise InvalidTransitionError(
                f"Cannot dispatch order with status '{order.status}'."
            )

        order.status = Order.Status.DISPATCHED
        order.save(update_fields=["status", "updated_at"])

    return order


def deliver_order(order_number: str) -> Order:
    try:
        order = Order.objects.get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFound(f"Order '{order_number}' not found.")

    if order.status != Order.Status.DISPATCHED:
        raise InvalidTransitionError(
            f"Cannot deliver order with status '{order.status}'."
        )

    with transaction.atomic():
        order = Order.objects.select_for_update().get(order_number=order_number)
        if order.status != Order.Status.DISPATCHED:
            raise InvalidTransitionError(
                f"Cannot deliver order with status '{order.status}'."
            )

        order.status = Order.Status.DELIVERED
        order.save(update_fields=["status", "updated_at"])

    return order


def cancel_order(order_number: str) -> Order:
    try:
        order = Order.objects.get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFound(f"Order '{order_number}' not found.")

    if order.status not in (Order.Status.DRAFT, Order.Status.CONFIRMED):
        raise InvalidTransitionError(
            f"Cannot cancel order with status '{order.status}'."
        )

    with transaction.atomic():
        order = Order.objects.select_for_update().get(order_number=order_number)
        if order.status not in (Order.Status.DRAFT, Order.Status.CONFIRMED):
            raise InvalidTransitionError(
                f"Cannot cancel order with status '{order.status}'."
            )

        if order.status == Order.Status.CONFIRMED:
            # Restore stock
            items = list(order.items.select_related("product").all())
            skus = sorted([it.product_id for it in items])
            inventories = {
                inv.product_id: inv
                for inv in Inventory.objects.select_for_update()
                .filter(product_id__in=skus)
                .order_by("product_id")
            }
            for it in items:
                inv = inventories.get(it.product_id)
                if inv:
                    inv.quantity += it.quantity
                    inv.save(update_fields=["quantity"])

        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status", "updated_at"])

    return order
