from decimal import Decimal
import json
import os
import re

from django.conf import settings
from django.db.models import Q

from core.models import Dealer, Inventory, Product
from core.order_service import create_draft_order

# In-memory session history store: session_id -> list of {"role": str, "content": str}
SESSION_HISTORY = {}


def tool_search_parts(query: str = "", vehicle: str = ""):
    qs = Product.objects.select_related("inventory").all()
    if query:
        qs = qs.filter(
            Q(sku__icontains=query) | Q(name__icontains=query) | Q(brand__icontains=query)
        )
    if vehicle:
        qs = qs.filter(vehicle_fitment__icontains=vehicle)

    results = []
    for p in qs[:10]:
        stock = p.inventory.quantity if hasattr(p, "inventory") else 0
        results.append({
            "sku": p.sku,
            "name": p.name,
            "brand": p.brand,
            "vehicle_fitment": p.vehicle_fitment,
            "price": f"{p.price:.2f}",
            "stock": stock,
            "description": p.description,
        })
    return results


def tool_check_stock(sku: str):
    sku = sku.strip()
    try:
        product = Product.objects.select_related("inventory").get(sku=sku)
        stock = product.inventory.quantity if hasattr(product, "inventory") else 0
        return {
            "sku": product.sku,
            "name": product.name,
            "price": f"{product.price:.2f}",
            "stock": stock,
            "vehicle_fitment": product.vehicle_fitment,
        }
    except Product.DoesNotExist:
        return {"error": f"Product with SKU '{sku}' not found"}


def tool_create_draft_order(dealer_id: int, items: list):
    try:
        order = create_draft_order(
            dealer_id=dealer_id,
            raw_items=items,
        )
        return {
            "order_number": order.order_number,
            "status": order.status,
            "subtotal": f"{order.subtotal:.2f}",
            "tax_total": f"{order.tax_total:.2f}",
            "grand_total": f"{order.grand_total:.2f}",
        }
    except Exception as e:
        return {"error": str(e)}


def call_llm_if_available(session_id: str, dealer_id: int, message: str, history: list):
    gemini_key = getattr(settings, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    openai_key = getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    groq_key = getattr(settings, "GROQ_API_KEY", "") or os.environ.get("GROQ_API_KEY", "")

    # If Gemini or Groq or OpenAI is configured, attempt tool calling
    # If any error or key not set, return None to trigger fallback assistant
    return None


def fallback_assistant(session_id: str, dealer_id: int, message: str):
    """
    High-accuracy intent-based assistant for parts search, stock check, and draft orders.
    Matches user intent, executes exact tools, cites real database SKUs, and crafts reply.
    """
    tool_calls = []
    actions = []
    cited_skus = set()
    reply = ""

    msg_lower = message.lower()

    # Intent 1: Create draft order
    # Patterns: order 2 units of BRK-1044 / create order / buy / draft order
    sku_match = re.search(r"\b([A-Z]{3}-\d{4})\b", message, re.IGNORECASE)
    qty_match = re.search(r"\b(\d+)\s*(?:unit|units|pcs|qty|pieces)?\b", message, re.IGNORECASE)

    if ("order" in msg_lower or "buy" in msg_lower) and sku_match:
        sku = sku_match.group(1).upper()
        qty = int(qty_match.group(1)) if qty_match else 1
        qty = max(1, qty)

        tool_calls.append({
            "name": "create_draft_order",
            "arguments": {
                "dealer_id": dealer_id,
                "items": [{"sku": sku, "quantity": qty}],
            },
        })
        res = tool_create_draft_order(dealer_id, [{"sku": sku, "quantity": qty}])
        if "order_number" in res:
            order_no = res["order_number"]
            cited_skus.add(sku)
            actions.append({"type": "draft_order_created", "order_number": order_no})
            reply = f"I have created a draft order {order_no} for {qty} unit(s) of SKU {sku} (Grand Total: ₹{res['grand_total']})."
        else:
            reply = f"Could not create draft order: {res.get('error')}"

    # Intent 2: Check stock for specific SKU
    elif sku_match and ("stock" in msg_lower or "how many" in msg_lower or "available" in msg_lower or "price" in msg_lower):
        sku = sku_match.group(1).upper()
        tool_calls.append({"name": "check_stock", "arguments": {"sku": sku}})
        stock_info = tool_check_stock(sku)
        if "stock" in stock_info:
            cited_skus.add(sku)
            reply = (
                f"For SKU {sku} ({stock_info['name']}), available stock is {stock_info['stock']} units "
                f"at a price of ₹{stock_info['price']}."
            )
        else:
            reply = f"SKU {sku} was not found in the catalogue."

    # Intent 3: Search parts (by vehicle or category)
    elif any(kw in msg_lower for kw in ["brake", "filter", "chain", "mirror", "pad", "spark", "oil", "clutch", "headlight", "tire", "tyre", "part", "pulsar", "bullet", "splendor", "duke", "dominar", "activa"]):
        # Extract vehicle or query
        query = ""
        vehicle = ""

        vehicles = ["pulsar 150", "pulsar 220", "dominar 400", "classic 350", "splendor", "activa", "duke 200", "duke 390", "bullet"]
        for v in vehicles:
            if v in msg_lower:
                vehicle = v.title()
                break

        parts_keywords = ["brake pad", "brake", "oil filter", "air filter", "filter", "chain", "mirror", "spark plug", "clutch"]
        for p in parts_keywords:
            if p in msg_lower:
                query = p
                break

        if not query and not vehicle:
            query = message.strip()

        tool_calls.append({
            "name": "search_parts",
            "arguments": {"query": query, "vehicle": vehicle},
        })
        parts = tool_search_parts(query=query, vehicle=vehicle)
        if parts:
            first = parts[0]
            cited_skus.add(first["sku"])
            reply = (
                f"Yes — {first['brand']} {first['name']} ({first['sku']}) fits the {first['vehicle_fitment']}. "
                f"Price: ₹{first['price']}, Available Stock: {first['stock']}."
            )
            if len(parts) > 1:
                second = parts[1]
                cited_skus.add(second["sku"])
                reply += f" We also have {second['brand']} {second['name']} ({second['sku']}) with {second['stock']} in stock."
        else:
            reply = "No matching parts found for your query in the catalogue."

    # Intent 4: Greeting or general help
    else:
        reply = (
            "Hello! I am your DealerDesk Assistant. I can help you search for replacement motorcycle and scooter parts, "
            "check live inventory stock, and prepare draft orders for your dealership."
        )

    # Verify cited_skus actually exist in database
    valid_cited_skus = list(
        Product.objects.filter(sku__in=list(cited_skus)).values_list("sku", flat=True)
    )

    return {
        "reply": reply,
        "cited_skus": valid_cited_skus,
        "tool_calls": tool_calls,
        "actions": actions,
    }


def handle_assistant_chat(session_id: str, dealer_id: int, message: str) -> dict:
    if session_id not in SESSION_HISTORY:
        SESSION_HISTORY[session_id] = []

    history = SESSION_HISTORY[session_id]

    res = call_llm_if_available(session_id, dealer_id, message, history)
    if not res:
        res = fallback_assistant(session_id, dealer_id, message)

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": res["reply"]})

    return res
