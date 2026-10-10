# VIKMO DealerDesk — Technical Submission

**Track**: `django` (with all optional parts implemented: Order Engine, Supplier Sync, AI Assistant, Forecasting, and Frontend Portal)

**Author / Candidate**: Developer Submission  
**Video Walkthrough**: `https://drive.google.com/file/d/YOUR_VIDEO_LINK_HERE` *(Unlisted walkthrough link)*

---

## 1. Executive Summary & Architecture

DealerDesk is an enterprise B2B distributor commerce backend and dealer portal built for automotive and spare parts distributors across South India. It eliminates overselling, automates supplier synchronisation, supports GenAI-driven parts search and order creation, and provides predictive stock replenishment.

### Components Implemented:
- **Part A1 · Backend Core**: Products, inventory, dealers, and draft orders with full filtering and validation.
- **Part A2 · Order Engine**: Concurrency-safe status flow (`DRAFT -> CONFIRMED -> DISPATCHED -> DELIVERED` / `CANCELLED`), pessimistic locking (`select_for_update`), all-or-nothing stock deduction, exact one-time stock restoration on cancellation, line-by-line GST rounding (`ROUND_HALF_UP`), and duplicate-safe idempotency.
- **Part B · Supplier Sync**: Cursor-based pagination, rate limit (`429`) and transient failure (`5xx`) handling with exponential backoff, atomic all-or-nothing rollback on failure, invalid price rejection, and deduplication by latest `updated_at`.
- **Part C · Dealer Assistant (GenAI)**: Conversational assistant supporting `search_parts`, `check_stock`, and `create_draft_order` tool calls grounded in live database state.
- **Part D · Demand Forecasting & Reorder Point (ML)**: 4-week future demand forecast for 40 SKUs in `ml/forecast.csv` using trend and promotional elasticity, with real-time safety stock and reorder point calculations.
- **Part E · B2B Frontend Portal**: Clean responsive web application served at `http://localhost:8000/` and `http://localhost:8000/portal/` connecting to all REST endpoints.

---

## 2. Setup & Installation

### Requirements
- Python 3.11+
- PostgreSQL (Neon, Supabase, or local PostgreSQL instance)

### Installation Steps

1. **Create and Activate Virtual Environment**:
   ```bash
   python -m venv .venv
   # Windows:
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
   Set your PostgreSQL connection string:
   ```env
   DATABASE_URL=postgresql://user:password@host:5432/dealerdesk?sslmode=require
   SUPPLIER_API_URL=http://127.0.0.1:8099
   SUPPLIER_API_KEY=dev-supplier-key
   DJANGO_SECRET_KEY=dev-secret-key
   # Optional LLM Key: GEMINI_API_KEY, GROQ_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY
   ```

4. **Run Migrations & Seed Catalogue**:
   ```bash
   python manage.py migrate --noinput
   python manage.py seed_data
   ```

---

## 3. Running the Services

### Start Mock Supplier (Terminal 1)
```bash
python supplier_mock/mock_supplier.py --calm
```

### Start Django Backend & Portal (Terminal 2)
```bash
python manage.py runserver 0.0.0.0:8000 --noreload
```

Open your browser at `http://127.0.0.1:8000/` to access the interactive portal.

---

## 4. Verification & Testing

### Run Automated Unit Tests
```bash
python manage.py test core --keepdb
```

### Run Official Contract Smoke Test
```bash
python smoke_test.py --base-url http://127.0.0.1:8000 --all
```
*Result: 12/12 checks passed (100%).*

---

## 5. Architectural Decisions
See [`DECISIONS.md`](DECISIONS.md) for detailed technical justifications and exact line citations for all 8 evaluation questions.
