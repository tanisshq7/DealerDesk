# VIKMO DealerDesk — Technical Submission

An enterprise B2B dealer management and order orchestration backend built with Django, Django REST Framework, and PostgreSQL.

---

## 1. Features Implemented

- **Core Catalogue & Dealers**:
  - Full product catalogue with search, category, and vehicle fitment filtering.
  - Manual inventory adjustments with validation.
  - Dealer management with credit limits and GSTIN verification.
- **Order Engine**:
  - Daily order numbering format: `ORD-YYYYMMDD-NNNN` (Asia/Kolkata timezone).
  - Exact paisa calculations with 18% GST rounded line-by-line using `ROUND_HALF_UP`.
  - Concurrency-safe status flow: `DRAFT -> CONFIRMED -> DISPATCHED -> DELIVERED` or `CANCELLED`.
  - Pessimistic locking (`select_for_update`) with deadlock-safe ordered lock acquisition.
  - All-or-nothing stock deduction and exact one-time stock restoration on cancellation.
  - Idempotent order placement via `Idempotency-Key` and SHA-256 payload hashing.
- **Supplier Feed Synchronization**:
  - Cursor-based paginated ingest with automatic retries up to 5 attempts.
  - Respects HTTP 429 `Retry-After` headers and applies exponential backoff for transient 5xx errors.
  - Rejects invalid pricing and deduplicates by latest `updated_at`.
  - All-or-nothing database updates without altering local stock levels.
- **Dealer Assistant (GenAI)**:
  - Session-based conversational agent with multi-turn memory.
  - Tool calling capabilities: `search_parts`, `check_stock`, `create_draft_order`.
  - Cites only real database SKUs and creates draft orders respecting all business rules.
- **Demand Forecasting & Reorder Suggestions (ML)**:
  - Generates 4-week future demand forecasts in `ml/forecast.csv` using weighted trends and promotional elasticity.
  - Real-time reorder point calculations incorporating lead times, sample standard deviation, safety stock, and live inventory.

---

## 2. Requirements & Setup

### Prerequisites
- Python 3.11+
- PostgreSQL database (Local or Cloud e.g., Neon / Supabase)

### Installation

1. **Clone & Virtual Environment**:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install Dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Configure your database and supplier settings:
   ```env
   DATABASE_URL=postgres://user:password@host:5432/dealerdesk?sslmode=require
   SUPPLIER_API_URL=http://127.0.0.1:8099
   SUPPLIER_API_KEY=dev-supplier-key
   DJANGO_SECRET_KEY=dev-secret-key
   ```

4. **Migrate & Seed**:
   ```bash
   python manage.py migrate --noinput
   python manage.py seed_data
   ```

---

## 3. Running the Server & Mock Supplier

To run the mock supplier (for testing supplier sync):
```bash
python supplier_mock/mock_supplier.py
```

To start the Django application server:
```bash
python manage.py runserver 0.0.0.0:8000
```

---

## 4. Verification & Testing

### Running Unit Tests
```bash
python manage.py test core --keepdb
```

### Running the End-to-End Contract Smoke Test
```bash
# Test all components (Core, Engine, Sync, Assistant, Forecast)
python smoke_test.py --base-url http://localhost:8000 --all
```
