# DECISIONS.md — Technical Architecture & Evaluation Questions

This document directly answers the evaluation questions from Section 12 of the VIKMO DealerDesk Assignment Brief, citing exact code lines.

---

## Django Track Questions

### 1. Where exactly is overselling prevented? Cite the code, and explain what would happen without it when two confirms arrive together.

**Code Citation**: [`core/order_service.py:232-276`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L232-L276)
```python
with transaction.atomic():
    order = Order.objects.select_for_update().get(order_number=order_number)
    ...
    skus = sorted([it.product_id for it in items])
    inventories = {
        inv.product_id: inv
        for inv in Inventory.objects.select_for_update()
        .filter(product_id__in=skus)
        .order_by("product_id")
    }
    for it in items:
        inv = inventories.get(it.product_id)
        avail = inv.quantity if inv else 0
        if it.quantity > avail:
            short_lines.append(...)
    if short_lines:
        raise InsufficientStockError(...)
    for it in items:
        inv = inventories[it.product_id]
        inv.quantity -= it.quantity
        inv.save(update_fields=["quantity"])
```

**Explanation**:
Overselling is prevented by acquiring row-level exclusive locks (`FOR UPDATE`) on the `Inventory` table via `select_for_update()` inside a database transaction (`transaction.atomic()`). Furthermore, SKUs are sorted (`order_by("product_id")` at line 246) before locking to eliminate cyclic deadlocks between concurrent multi-item orders.

**What would happen without it**:
Without `select_for_update()`, a classic *read-modify-write* race condition occurs:
1. Two concurrent threads (Thread A and Thread B) both read `quantity = 10` for an item simultaneously.
2. Both check `requested (8) <= available (10)` and pass the validation check.
3. Thread A writes `quantity = 10 - 8 = 2`.
4. Thread B writes `quantity = 10 - 8 = 2` (or `2 - 8 = -6` if using F-expressions without constraints).
Both orders are confirmed, selling 16 units out of only 10 in stock. With `select_for_update()`, Thread B blocks at the database level until Thread A commits; Thread B then reads the updated `quantity = 2`, detects the shortage, and raises `409 INSUFFICIENT_STOCK`.

---

### 2. How do you calculate and round GST? What would go wrong with floats, or with Decimal's default rounding?

**Code Citation**: [`core/order_service.py:38-46`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L38-L46)
```python
def calculate_line_item(unit_price: Decimal, quantity: int):
    line_subtotal = (unit_price * quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    line_tax = (line_subtotal * GST_RATE).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    line_total = line_subtotal + line_tax
    return line_subtotal, line_tax, line_total
```

**Explanation**:
- **Why floats fail**: Floats use IEEE 754 binary floating-point representation where base-10 fractions (such as `0.18` or `0.01`) cannot be represented exactly (e.g. `1459.50 * 0.18` becomes `262.70999999999997`). Accumulating float inaccuracies produces rounding discrepancies of 1–2 paise across line items and order totals.
- **Why Decimal's default rounding fails**: Python `Decimal` defaults to `ROUND_HALF_EVEN` (Banker's rounding), where `.005` rounds to the nearest *even* digit (`0.025 -> 0.02`, but `0.035 -> 0.04`). The statutory Indian GST invoicing standard and contract section 7.4 mandate **`ROUND_HALF_UP`** (`0.005 -> 0.01`), calculated line-by-line rather than on the order total sum.

---

### 3. How do you guarantee unique order numbers and idempotent order creation when duplicate requests arrive at the same moment?

**Code Citation (Order Numbers)**: [`core/order_service.py:28-35`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L28-L35)
```python
def generate_order_number(today) -> str:
    seq, _ = DailyOrderSequence.objects.select_for_update().get_or_create(
        date=today,
        defaults={"last_number": 0},
    )
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"ORD-{today.strftime('%Y%m%d')}-{seq.last_number:04d}"
```

**Code Citation (Idempotency)**: [`core/order_service.py:126-160`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L126-L160)
```python
req_hash = compute_hash(raw_body_data) if raw_body_data is not None else ""
if idempotency_key:
    idempotency_key = idempotency_key.strip()
    existing = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_hash == req_hash:
            return existing
        raise IdempotencyConflictError("Idempotency key reused with different request body.")

with transaction.atomic():
    if idempotency_key:
        existing = Order.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing:
            ...
```

**Explanation**:
- **Unique Order Numbers**: `DailyOrderSequence` isolates daily counters. Wrapping `select_for_update().get_or_create(date=today)` within the transaction forces simultaneous creation threads to serialize their sequence increments, guaranteeing monotonic, non-colliding order numbers.
- **Idempotency**: Requests carry an optional `Idempotency-Key` header. We compute a canonical SHA-256 hash of the request body. If the key exists with the identical hash, the existing order is replayed. If the payload differs, `409 IDEMPOTENCY_CONFLICT` is raised. Database-level `unique=True` on `Order.idempotency_key` ensures concurrent simultaneous requests cannot insert duplicates.

---

### 4. Explain your schema: each model, its relationships, and the on_delete choice for every foreign key, with your reasons.

**Code Citation**: [`core/models.py:4-131`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/models.py#L4-L131)

1. **`Product`**: Primary key `sku`. Contains catalogue master data.
2. **`Inventory`**: `OneToOneField(Product, on_delete=models.CASCADE, related_name="inventory")`.
   - *Reason*: Decouples mutable stock quantities from static catalogue attributes, enabling row locks during checkout without blocking product catalogue queries. If a product is removed, its stock record cascades.
3. **`Dealer`**: Customer account master with `dealer_code` and `credit_limit`.
4. **`DailyOrderSequence`**: `date` (unique) and `last_number`. Manages atomic sequence generation.
5. **`Order`**:
   - `dealer = ForeignKey(Dealer, on_delete=models.PROTECT)`:
     - *Reason*: **`PROTECT`** ensures a dealer cannot be deleted if historical sales orders exist, preserving legal audit trails and accounting records.
   - `idempotency_key = CharField(unique=True, null=True, blank=True)`: Enforces database-level uniqueness.
6. **`OrderItem`**:
   - `order = ForeignKey(Order, on_delete=models.CASCADE)`: If an order is purged, its dependent line items cascade.
   - `product = ForeignKey(Product, on_delete=models.PROTECT)`: **`PROTECT`** prevents deleting catalogue products referenced in prior orders, ensuring historical orders preserve product integrity.
   - `UniqueConstraint(fields=["order", "product"])`: Prevents duplicate lines for the same product in a single order.
   - `CheckConstraint(check=Q(quantity__gte=1))`: Database-enforced positive quantities.

---

### 5. Where are status transitions enforced, and how do you guarantee a cancel restores stock exactly once?

**Code Citation**: [`core/order_service.py:221-369`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L221-L369)
- Transition guards:
  - `confirm_order`: `if order.status != Order.Status.DRAFT: raise InvalidTransitionError(...)` (line 227)
  - `dispatch_order`: `if order.status != Order.Status.CONFIRMED: raise InvalidTransitionError(...)` (line 290)
  - `deliver_order`: `if order.status != Order.Status.DISPATCHED: raise InvalidTransitionError(...)` (line 314)
  - `cancel_order`: `if order.status not in (DRAFT, CONFIRMED): raise InvalidTransitionError(...)` (line 337)
- Edit lock: `update_draft_order`: `if order.status != DRAFT: raise OrderLockedError(...)` (line 187)

**Guaranteeing Stock is Restored Exactly Once**:
In `cancel_order` ([`core/order_service.py:342-367`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/order_service.py#L342-L367)):
```python
with transaction.atomic():
    order = Order.objects.select_for_update().get(order_number=order_number)
    if order.status not in (Order.Status.DRAFT, Order.Status.CONFIRMED):
        raise InvalidTransitionError(...)
    if order.status == Order.Status.CONFIRMED:
        # Lock inventory rows and restore quantities
        for it in items:
            inv = inventories.get(it.product_id)
            inv.quantity += it.quantity
            inv.save(update_fields=["quantity"])
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status", "updated_at"])
```
Because the order row is locked with `select_for_update()` and immediately updated to `CANCELLED`, any subsequent attempt to cancel the same order fails at the status guard (`order.status not in (DRAFT, CONFIRMED)`), ensuring stock is restored exactly once.

---

### 6. How does your supplier sync handle rate limits, failures and duplicates? How do you guarantee it changes nothing when the feed can't be fully fetched?

**Code Citation**: [`core/supplier_sync.py:45-135`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/supplier_sync.py#L45-L135)

- **Rate Limits (`429`)**: Lines 66–73 inspect `Retry-After` header, sleep the requested duration, and retry without incrementing error failure penalties.
- **Transient Failures (`5xx` / Connection Errors)**: Lines 75–82 apply exponential backoff (`0.5 * (2 ** (attempt - 1))`) for up to 5 attempts per request.
- **All-or-Nothing Guarantee**: All HTTP requests are fetched and held in memory (`all_raw_items`) **before** opening any database transaction. If any page fails after 5 retries or returns `401`, `SupplierUnavailableError` (HTTP 502) is raised at line 85. The database transaction is never entered, guaranteeing zero partial writes.
- **Duplicates**: Lines 119–134 deduplicate across pages by tracking `updated_at`:
  ```python
  if sku in valid_items_by_sku:
      if updated_at > valid_items_by_sku[sku]["updated_at"]:
          valid_items_by_sku[sku] = {"item": item, ...}
  ```

---

### 7. How does the sync decide between created, updated and unchanged, and why does a second run change nothing?

**Code Citation**: [`core/supplier_sync.py:160-192`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/core/supplier_sync.py#L160-L192)

```python
if sku not in existing_products:
    # Created: stock initialized to 0
    created_count += 1
else:
    existing = existing_products[sku]
    if (existing.name != name or existing.category != category
        or existing.brand != brand or existing.vehicle_fitment != vehicle_fitment
        or existing.description != description or existing.price != price_dec):
        # Updated
        updated_count += 1
    else:
        # Unchanged
        unchanged_count += 1
```

**Why a second run changes nothing**:
On the second run against an identical feed, all valid SKUs already exist in `Product`, meaning `created_count = 0`. Furthermore, all catalogue attributes match the database values, so the diff check evaluates to `False`, recording every SKU as `unchanged_count` and performing zero database mutations. Local inventory stock is never modified.

---

### 8. What is the weakest part of your submission, and what would you do with two more days?

1. **Weakest Part**:
   - The supplier sync ingests the entire catalog feed in memory before committing. While this guarantees the contract's atomic all-or-nothing rollback for moderate catalogues (e.g. 600–5,000 SKUs), feeds with hundreds of thousands of items would require staging tables in PostgreSQL (`supplier_sync_staging`) rather than application memory.
2. **What I would do with two more days**:
   - **Staging Table Sync**: Stream pages directly into an unlogged staging table in PostgreSQL, perform set-based `UPSERT` queries (`INSERT ... ON CONFLICT DO UPDATE`), and swap data atomically.
   - **Redis Distributed Locks / Celery Workers**: Offload the supplier sync and demand forecasting to background task queues with Webhook/SSE progress updates to the portal.
   - **Vector Search for Assistant**: Add `pgvector` to the PostgreSQL database for cosine-similarity semantic retrieval of parts matching vague descriptions or colloquial vehicle nicknames.

---

## AI / ML Track Questions

### 6. Describe your forecasting approach and how you validated it. Cite the line that stops future data leaking into training.

**Code Citation**: [`ml/train_forecast.py:46-77`](file:///c:/Users/mtani/Downloads/VIKMO_DealerDesk_Assignment%20(1)/DealerDesk/ml/train_forecast.py#L46-L77)
- **Approach**: Combines an 8-week recency-weighted exponential baseline with seasonal matching from 52 weeks prior (30% weight) and promotional elasticity multipliers computed from historical promo sales lifts.
- **Leakage Prevention**: Line 13 explicitly fixes `FORECAST_WEEKS = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]`. All training inputs are restricted to history strictly prior to `2026-09-28` (up to week `2026-09-21`), ensuring zero future information enters model training.

### 7. Which SKUs does your forecast handle worst, and why? How did you handle promotions, Diwali, rarely sold SKUs and new SKUs?
- **Worst Handled SKUs**: SKUs with zero or single-digit historical sales, where standard deviation is near zero and intermittent demand does not follow Poisson/Gaussian distributions.
- **Promotions & Diwali**: Multipliers calculate average promo sales divided by non-promo sales; forecast weeks with `promo_flag == 1` apply this elasticity factor.
- **Rarely Sold & New SKUs**: Fallback default baselines (`base_demand = 10.0`) prevent zero-variance underestimation in safety stock formulas.
