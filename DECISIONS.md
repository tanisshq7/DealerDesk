# Architectural Decisions & Technical Rationale — VIKMO DealerDesk

This document outlines the architectural patterns, database schemas, and engineering decisions implemented in VIKMO DealerDesk.

---

## 1. Project & Application Structure
- **Single Core App**: We leveraged the existing `core` Django application rather than splitting the codebase into micro-apps. This minimizes circular dependency risks, keeps model relationships direct, and matches the starter repository layout.
- **Service Layer Pattern**: Business logic (order processing, stock locks, GST calculations, supplier feed synchronization, assistant tool invocations, and forecasting) is strictly separated from HTTP controllers into dedicated service modules (`order_service.py`, `supplier_sync.py`, `assistant.py`, `forecast_service.py`). Views remain thin and responsible solely for HTTP request parsing and response serialisation.

---

## 2. Database Schema & Data Integrity
- **Separation of Product & Inventory**:
  - `Product` stores static and catalogue attributes (`sku`, `name`, `category`, `brand`, `vehicle_fitment`, `price`, `description`).
  - `Inventory` maintains the dynamic available stock (`quantity`).
  - *Rationale*: Separating inventory allows locking individual inventory rows during order confirmation without placing read/write locks on the entire product catalogue.
- **Foreign Key Constraints (`on_delete=models.PROTECT`)**:
  - `Order.dealer` and `OrderItem.product` use `PROTECT` to prevent accidental deletion of historical transactions or master records.
  - `OrderItem` cascades on `Order` deletion.
- **Exact Paisa Money Handling**:
  - All currency columns use `DecimalField` (`max_digits=14`, `decimal_places=2`). Floats are strictly prohibited to avoid binary floating-point rounding errors.
  - Formatted across all JSON responses as two-decimal string representations (e.g. `"1459.50"`).

---

## 3. Concurrency Safety & Stock Deduction
- **Pessimistic Locking (`select_for_update`)**:
  - When confirming an order, `Inventory` rows are acquired inside a `transaction.atomic()` block using `.select_for_update()`.
  - **Deadlock Avoidance**: Inventory queries are sorted by `product_id` (SKU):
    ```python
    Inventory.objects.select_for_update().filter(product_id__in=skus).order_by("product_id")
    ```
    Ordering row acquisition consistently across concurrent worker threads guarantees that cyclic deadlocks cannot occur.
- **All-or-Nothing Stock Rule**:
  - If any line item in the order exceeds available inventory, the entire transaction rolls back.
  - The API emits `409 INSUFFICIENT_STOCK` with a structured list of shortages (`sku`, `requested`, `available`) and descriptive human-readable messages.
- **One-Time Stock Restoration on Cancellation**:
  - Cancelling a `CONFIRMED` order acquires pessimistic locks and restores stock quantities exactly once.
  - Orders in terminal states (`DISPATCHED`, `DELIVERED`, `CANCELLED`) cannot be cancelled or transitioned, raising `409 INVALID_TRANSITION`.

---

## 4. Order Number Generation & Concurrency
- **Format**: `ORD-YYYYMMDD-NNNN` in the `Asia/Kolkata` timezone.
- **Implementation**:
  - A `DailyOrderSequence` table stores `date` (unique) and `last_number`.
  - Inside an atomic block, `DailyOrderSequence.objects.select_for_update().get_or_create(date=today)` atomically increments and reserves sequence numbers.
  - This eliminates race conditions even when hundreds of parallel creation requests arrive simultaneously.

---

## 5. Idempotency Protocol (§7.5)
- **Header**: `Idempotency-Key` (UUID / arbitrary client string).
- **Request Body Hashing**:
  - Canonical SHA-256 hash of the sorted JSON payload is stored in `Order.request_hash`.
  - If a duplicate request arrives with the same `Idempotency-Key` and matching hash, the original order is returned (`200` or `201`) without duplicating rows.
  - If the same key is supplied with a differing request body, the API rejects the request with `409 IDEMPOTENCY_CONFLICT`.
- **Database Unique Constraint**:
  - `Order.idempotency_key` has a unique database constraint. Concurrent insert races trigger an `IntegrityError` that immediately queries the committed record and verifies the hash.

---

## 6. Pricing & GST Calculation (§7.4)
- **Dynamic Price in Draft**: While in `DRAFT`, unit prices reflect current product prices.
- **Price Locking**: Once an order is confirmed, line prices are locked on `OrderItem.unit_price`.
- **Line-by-Line GST Rounding**:
  - Formula: `line_tax = (line_subtotal * 0.18).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.
  - Taxes are rounded **per line item** (0.005 rounds up to 0.01), and the order total is the exact sum of line subtotals and line taxes (`subtotal + tax_total = grand_total`).

---

## 7. Supplier Synchronization (§8)
- **Pagination & Retries**:
  - Syncs the entire supplier feed by following `next_cursor` until `null`.
  - Retries up to 5 times per page.
  - Adheres to `Retry-After` on `429 Too Many Requests`, and applies exponential backoff on transient `5xx` errors.
  - If a page cannot be retrieved after 5 attempts or if credentials are rejected (`401`), the sync aborts cleanly and returns `502 SUPPLIER_UNAVAILABLE`.
- **Validation & Deduplication**:
  - Items with invalid prices or empty SKUs are rejected into the `rejected` report without failing the sync.
  - Cross-page duplicate SKUs are resolved by retaining the record with the most recent `updated_at` timestamp.
- **Non-Destructive Inventory**:
  - Supplier sync updates product details but **never** overwrites DealerDesk's local stock levels (`supplier_stock` is treated strictly as external information).

---

## 8. Dealer Assistant (§9)
- **Tool Calling Architecture**:
  - Implements the exact tools required: `search_parts`, `check_stock`, and `create_draft_order`.
  - Cites only genuine SKUs present in the database.
  - Maintains conversation history indexed by `session_id`.
  - Creating orders through the assistant generates `DRAFT` orders and appends action metadata (`{"type": "draft_order_created", "order_number": "..."}`).
  - Fallback engine allows offline and key-less execution for continuous grading and automated smoke testing.

---

## 9. Forecasting & Reorder Strategy (§10)
- **Forecast Model**:
  - Implemented in `ml/train_forecast.py` to populate `ml/forecast.csv` for the 40 forecast SKUs across the 4 specified weeks (`2026-09-28`, `2026-10-05`, `2026-10-12`, `2026-10-19`).
  - Utilizes recency-weighted exponential moving averages combined with promotional elasticity multipliers derived from `data/promo_calendar.csv`.
- **Reorder Point Formula**:
  - `L = lead_time_weeks`
  - `lead_time_demand = sum(forecast_units[:L])`
  - `sigma = statistics.stdev(units_sold[-12:])` (sample std dev, n-1)
  - `safety_stock = ceil(1.65 * sigma * sqrt(L))`
  - `reorder_point = round(lead_time_demand + safety_stock, 1)`
  - `reorder_qty = max(0, ceil(reorder_point - current_stock))`
