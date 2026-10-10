from decimal import Decimal, InvalidOperation
import time
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction
import requests

from core.exceptions import SupplierUnavailableError
from core.models import Inventory, Product


def is_valid_price(price_val):
    try:
        dec = Decimal(str(price_val))
        if dec <= 0:
            return False, None
        if abs(dec.as_tuple().exponent) > 2:
            return False, None
        return True, dec
    except (InvalidOperation, TypeError, ValueError):
        return False, None


def run_supplier_sync() -> dict:
    api_url = getattr(settings, "SUPPLIER_API_URL", "http://127.0.0.1:8099").rstrip("/")
    api_key = getattr(settings, "SUPPLIER_API_KEY", "")

    endpoint = f"{api_url}/v1/products"
    headers = {"X-Api-Key": api_key}

    cursor = None
    pages_fetched = 0
    requests_made = 0
    all_raw_items = []

    while True:
        params = {"limit": 20}
        if cursor is not None:
            params["cursor"] = cursor

        success = False
        last_error = None

        for attempt in range(1, 6):
            requests_made += 1
            try:
                resp = requests.get(
                    endpoint,
                    headers=headers,
                    params=params,
                    timeout=20,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    pages_fetched += 1
                    all_raw_items.extend(data.get("items", []))
                    cursor = data.get("next_cursor")
                    success = True
                    break

                if resp.status_code == 401:
                    raise SupplierUnavailableError("Supplier API key rejected.")

                if resp.status_code == 429:
                    retry_after_str = resp.headers.get("Retry-After", "1")
                    try:
                        wait_sec = float(retry_after_str)
                    except ValueError:
                        wait_sec = 1.0
                    time.sleep(wait_sec)
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue

                # Any other unexpected status code
                last_error = f"HTTP {resp.status_code}: {resp.text}"
                time.sleep(0.5 * attempt)

            except requests.RequestException as e:
                last_error = str(e)
                time.sleep(0.5 * (2 ** (attempt - 1)))

        if not success:
            raise SupplierUnavailableError(
                f"Supplier API request failed after 5 attempts. Last error: {last_error}"
            )

        if cursor is None:
            break

    # Process items
    rejected = []
    valid_items_by_sku = {}

    for item in all_raw_items:
        supplier_sku = str(item.get("supplier_sku", "")).strip()
        sku = str(item.get("sku", "")).strip()

        if not sku:
            rejected.append({
                "supplier_sku": supplier_sku,
                "reason": "missing or empty sku",
            })
            continue

        raw_price = item.get("price")
        valid, price_dec = is_valid_price(raw_price)
        if not valid:
            rejected.append({
                "supplier_sku": supplier_sku,
                "reason": "invalid price",
            })
            continue

        updated_at = str(item.get("updated_at", ""))

        if sku in valid_items_by_sku:
            prev_updated_at = valid_items_by_sku[sku]["updated_at"]
            if updated_at > prev_updated_at:
                valid_items_by_sku[sku] = {
                    "item": item,
                    "price_dec": price_dec,
                    "updated_at": updated_at,
                }
        else:
            valid_items_by_sku[sku] = {
                "item": item,
                "price_dec": price_dec,
                "updated_at": updated_at,
            }

    # Apply changes in atomic transaction
    created_count = 0
    updated_count = 0
    unchanged_count = 0

    with transaction.atomic():
        existing_products = {
            p.sku: p
            for p in Product.objects.filter(sku__in=list(valid_items_by_sku.keys()))
        }

        products_to_create = []
        inventories_to_create = []
        products_to_update = []

        for sku, record in valid_items_by_sku.items():
            item = record["item"]
            price_dec = record["price_dec"]
            name = str(item.get("name", "")).strip()
            category = str(item.get("category", "")).strip()
            brand = str(item.get("brand", "")).strip()
            vehicle_fitment = str(item.get("vehicle_fitment", "")).strip()
            description = str(item.get("description", "")).strip()

            if sku not in existing_products:
                new_prod = Product(
                    sku=sku,
                    name=name,
                    category=category,
                    brand=brand,
                    vehicle_fitment=vehicle_fitment,
                    price=price_dec,
                    description=description,
                )
                products_to_create.append(new_prod)
                inventories_to_create.append(Inventory(product=new_prod, quantity=0))
                created_count += 1
            else:
                existing = existing_products[sku]
                if (
                    existing.name != name
                    or existing.category != category
                    or existing.brand != brand
                    or existing.vehicle_fitment != vehicle_fitment
                    or existing.description != description
                    or existing.price != price_dec
                ):
                    existing.name = name
                    existing.category = category
                    existing.brand = brand
                    existing.vehicle_fitment = vehicle_fitment
                    existing.description = description
                    existing.price = price_dec
                    products_to_update.append(existing)
                    updated_count += 1
                else:
                    unchanged_count += 1

        if products_to_create:
            Product.objects.bulk_create(products_to_create)
            Inventory.objects.bulk_create(inventories_to_create)

        if products_to_update:
            Product.objects.bulk_update(
                products_to_update,
                fields=["name", "category", "brand", "vehicle_fitment", "description", "price"],
            )

    return {
        "created": created_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "rejected": rejected,
        "pages_fetched": pages_fetched,
        "requests_made": requests_made,
    }
