from collections import defaultdict
import csv
import math
from pathlib import Path
import statistics

from django.conf import settings

from core.exceptions import NotFound
from core.models import Inventory

FORECAST_WEEKS = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]


def get_forecast_for_sku(sku: str) -> dict:
    sku = sku.strip()
    forecast_file = settings.BASE_DIR / "ml" / "forecast.csv"
    if not forecast_file.exists():
        raise NotFound(f"Forecast file not found.")

    weeks_data = []
    with open(forecast_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["sku"].strip() == sku:
                weeks_data.append({
                    "week_start": row["week_start"].strip(),
                    "forecast_units": float(row["forecast_units"]),
                })

    if not weeks_data:
        raise NotFound(f"No forecast found for SKU '{sku}'.")

    # Sort in order of FORECAST_WEEKS
    weeks_order = {w: i for i, w in enumerate(FORECAST_WEEKS)}
    weeks_data.sort(key=lambda x: weeks_order.get(x["week_start"], 999))

    return {
        "sku": sku,
        "forecast": weeks_data,
    }


def get_reorder_suggestions() -> list:
    base_dir = settings.BASE_DIR
    data_dir = getattr(settings, "DATA_DIR", base_dir / "data")
    forecast_file = base_dir / "ml" / "forecast.csv"

    # 1. Lead times
    lead_times = {}
    lead_time_path = data_dir / "lead_times.csv"
    with open(lead_time_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lead_times[r["sku"].strip()] = int(r["lead_time_weeks"])

    # 2. Forecasts: sku -> list of forecast_units for the 4 weeks
    forecasts = defaultdict(list)
    if forecast_file.exists():
        with open(forecast_file, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                sku = r["sku"].strip()
                if sku in lead_times:
                    forecasts[sku].append({
                        "week_start": r["week_start"].strip(),
                        "forecast_units": float(r["forecast_units"]),
                    })

    weeks_order = {w: i for i, w in enumerate(FORECAST_WEEKS)}
    for sku in forecasts:
        forecasts[sku].sort(key=lambda x: weeks_order.get(x["week_start"], 999))

    # 3. Sales history: sku -> most recent 12 weeks units_sold
    sales_history = defaultdict(list)
    sales_path = data_dir / "sales_history.csv"
    with open(sales_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sku = r["sku"].strip()
            if sku in lead_times:
                sales_history[sku].append({
                    "week_start": r["week_start"].strip(),
                    "units_sold": float(r["units_sold"]),
                })

    for sku in sales_history:
        sales_history[sku].sort(key=lambda x: x["week_start"])

    # 4. Live inventory quantities
    inventories = {
        inv.product_id: inv.quantity
        for inv in Inventory.objects.filter(product_id__in=list(lead_times.keys()))
    }

    # 5. Compute reorder suggestions
    results = []
    for sku in sorted(lead_times.keys()):
        L = lead_times[sku]
        sku_forecast = forecasts.get(sku, [])
        first_l_forecasts = [w["forecast_units"] for w in sku_forecast[:L]]
        lead_time_demand = round(sum(first_l_forecasts), 1)

        # Most recent 12 weeks of sales
        sku_sales = sales_history.get(sku, [])
        recent_12 = [s["units_sold"] for s in sku_sales[-12:]]

        if len(recent_12) > 1:
            sigma = statistics.stdev(recent_12)
        else:
            sigma = 0.0

        safety_stock = math.ceil(1.65 * sigma * math.sqrt(L))
        reorder_point = round(lead_time_demand + safety_stock, 1)
        current_stock = inventories.get(sku, 0)
        reorder_qty = max(0, math.ceil(reorder_point - current_stock))

        results.append({
            "sku": sku,
            "current_stock": current_stock,
            "lead_time_weeks": L,
            "lead_time_demand": lead_time_demand,
            "safety_stock": safety_stock,
            "reorder_point": reorder_point,
            "reorder_qty": reorder_qty,
        })

    return results
