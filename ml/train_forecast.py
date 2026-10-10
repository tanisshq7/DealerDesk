import csv
from collections import defaultdict
import math
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ML_DIR = BASE_DIR / "ml"

FORECAST_WEEKS = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]


def generate_forecast():
    # 1. Load lead time SKUs (the 40 forecast SKUs)
    lead_skus = set()
    with open(DATA_DIR / "lead_times.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lead_skus.add(r["sku"].strip())

    # 2. Load sales history
    sales_by_sku = defaultdict(list)
    with open(DATA_DIR / "sales_history.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sku = r["sku"].strip()
            if sku in lead_skus:
                sales_by_sku[sku].append({
                    "week_start": r["week_start"].strip(),
                    "units_sold": float(r["units_sold"]),
                    "promo_flag": int(r.get("promo_flag", 0)),
                })

    # Sort each sku's history by week_start
    for sku in sales_by_sku:
        sales_by_sku[sku].sort(key=lambda x: x["week_start"])

    # 3. Load promo calendar for forecast weeks
    promo_flags = {}
    if (DATA_DIR / "promo_calendar.csv").exists():
        with open(DATA_DIR / "promo_calendar.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                sku = r["sku"].strip()
                week = r["week_start"].strip()
                promo_flags[(sku, week)] = int(r.get("promo_flag", 0))

    # 4. Generate forecast per SKU
    # Model: Weighted moving average over recent 8-12 weeks + promo multiplier
    forecast_rows = []
    for sku in sorted(lead_skus):
        history = sales_by_sku[sku]
        if not history:
            base_demand = 10.0
            promo_lift = 1.2
        else:
            # Separate non-promo and promo sales to calculate promo lift
            non_promo = [h["units_sold"] for h in history if h["promo_flag"] == 0]
            promo = [h["units_sold"] for h in history if h["promo_flag"] == 1]
            avg_non_promo = sum(non_promo) / len(non_promo) if non_promo else 10.0
            avg_promo = sum(promo) / len(promo) if promo else avg_non_promo * 1.3
            promo_lift = max(1.05, min(2.5, avg_promo / avg_non_promo)) if avg_non_promo > 0 else 1.3

            # Recent 8 weeks exponential / weighted average for baseline
            recent = history[-8:]
            weights = [i + 1 for i in range(len(recent))]
            total_weight = sum(weights)
            base_demand = sum(h["units_sold"] * w for h, w in zip(recent, weights)) / total_weight

            # Also check seasonal same weeks last year (52 weeks ago)
            if len(history) >= 56:
                same_period_last_year = history[-56:-52]
                seasonal_avg = sum(h["units_sold"] for h in same_period_last_year) / len(same_period_last_year)
                # Blend recent momentum (70%) with seasonal pattern (30%)
                base_demand = 0.7 * base_demand + 0.3 * seasonal_avg

        for week in FORECAST_WEEKS:
            is_promo = promo_flags.get((sku, week), 0)
            pred = base_demand * (promo_lift if is_promo else 1.0)
            pred = max(0.0, round(pred, 1))
            forecast_rows.append({
                "sku": sku,
                "week_start": week,
                "forecast_units": pred,
            })

    # Write output to ml/forecast.csv
    out_file = ML_DIR / "forecast.csv"
    with open(out_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "week_start", "forecast_units"])
        writer.writeheader()
        writer.writerows(forecast_rows)

    print(f"Generated {len(forecast_rows)} forecast rows in {out_file}")


if __name__ == "__main__":
    generate_forecast()
