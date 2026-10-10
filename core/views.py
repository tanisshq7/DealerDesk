from django.db import IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.assistant import handle_assistant_chat
from core.exceptions import DuplicateError, NotFound, ValidationError
from core.forecast_service import get_forecast_for_sku, get_reorder_suggestions
from core.models import Dealer, Inventory, Order, Product
from core.order_service import (
    cancel_order,
    confirm_order,
    create_draft_order,
    deliver_order,
    dispatch_order,
    update_draft_order,
)
from core.pagination import StandardPagination
from core.serializers import (
    DealerCreateSerializer,
    DealerSerializer,
    OrderSerializer,
    ProductCreateSerializer,
    ProductSerializer,
    ProductUpdateSerializer,
)
from core.supplier_sync import run_supplier_sync
from django.shortcuts import render


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


def portal_view(request):
    """Optional Part E — B2B Dealer Portal."""
    return render(request, "index.html")



# --------------------------------------------------------------------------------------
# 4. Products
# --------------------------------------------------------------------------------------
@api_view(["GET", "POST"])
def product_list(request):
    if request.method == "GET":
        qs = Product.objects.select_related("inventory").all().order_by("sku")

        search_query = request.query_params.get("search", "").strip()
        if search_query:
            qs = qs.filter(
                Q(sku__icontains=search_query)
                | Q(name__icontains=search_query)
                | Q(brand__icontains=search_query)
            )

        category = request.query_params.get("category", "").strip()
        if category:
            qs = qs.filter(category=category)

        vehicle = request.query_params.get("vehicle", "").strip()
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = ProductSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    elif request.method == "POST":
        serializer = ProductCreateSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            # Check duplicate SKU
            if "sku" in errors and any("already exists" in str(e).lower() for e in errors["sku"]):
                raise DuplicateError(f"Product with SKU '{request.data.get('sku')}' already exists.")
            first_err = next(iter(errors.values()))[0]
            raise ValidationError(str(first_err))

        sku = serializer.validated_data["sku"]
        if Product.objects.filter(sku=sku).exists():
            raise DuplicateError(f"Product with SKU '{sku}' already exists.")

        try:
            product = serializer.save()
            Inventory.objects.create(product=product, quantity=0)
        except IntegrityError:
            raise DuplicateError(f"Product with SKU '{sku}' already exists.")

        out = ProductSerializer(product)
        return Response(out.data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
def product_detail(request, sku):
    sku = sku.strip()
    try:
        product = Product.objects.select_related("inventory").get(sku=sku)
    except Product.DoesNotExist:
        raise NotFound(f"Product with SKU '{sku}' not found.")

    if request.method == "GET":
        serializer = ProductSerializer(product)
        return Response(serializer.data, status=status.HTTP_200_OK)

    elif request.method == "PATCH":
        # SKU cannot change
        update_data = request.data.copy()
        if "sku" in update_data and update_data["sku"] != product.sku:
            update_data.pop("sku", None)

        serializer = ProductUpdateSerializer(product, data=update_data, partial=True)
        if not serializer.is_valid():
            first_err = next(iter(serializer.errors.values()))[0]
            raise ValidationError(str(first_err))

        product = serializer.save()
        out = ProductSerializer(product)
        return Response(out.data, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 5. Inventory
# --------------------------------------------------------------------------------------
@api_view(["PUT"])
def inventory_update(request, sku):
    sku = sku.strip()
    try:
        product = Product.objects.get(sku=sku)
    except Product.DoesNotExist:
        raise NotFound(f"Product with SKU '{sku}' not found.")

    qty = request.data.get("quantity")
    if qty is None or not isinstance(qty, int) or qty < 0:
        raise ValidationError("Quantity must be an integer greater than or equal to 0.")

    inv, _ = Inventory.objects.get_or_create(product=product)
    inv.quantity = qty
    inv.save(update_fields=["quantity"])

    return Response({"sku": product.sku, "quantity": qty}, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 6. Dealers
# --------------------------------------------------------------------------------------
@api_view(["GET", "POST"])
def dealer_list(request):
    if request.method == "GET":
        qs = Dealer.objects.all().order_by("id")
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = DealerSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    elif request.method == "POST":
        serializer = DealerCreateSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            if "dealer_code" in errors and any("already exists" in str(e).lower() for e in errors["dealer_code"]):
                raise DuplicateError(f"Dealer with code '{request.data.get('dealer_code')}' already exists.")
            first_err = next(iter(errors.values()))[0]
            raise ValidationError(str(first_err))

        dealer_code = serializer.validated_data["dealer_code"]
        if Dealer.objects.filter(dealer_code=dealer_code).exists():
            raise DuplicateError(f"Dealer with code '{dealer_code}' already exists.")

        try:
            dealer = serializer.save()
        except IntegrityError:
            raise DuplicateError(f"Dealer with code '{dealer_code}' already exists.")

        out = DealerSerializer(dealer)
        return Response(out.data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def dealer_detail(request, pk):
    try:
        dealer = Dealer.objects.get(id=pk)
    except (Dealer.DoesNotExist, ValueError):
        raise NotFound(f"Dealer with id {pk} not found.")

    serializer = DealerSerializer(dealer)
    return Response(serializer.data, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 7. Orders
# --------------------------------------------------------------------------------------
@api_view(["GET", "POST"])
def order_list(request):
    if request.method == "GET":
        qs = Order.objects.select_related("dealer").prefetch_related("items__product").all().order_by("-created_at")

        dealer_id = request.query_params.get("dealer_id")
        if dealer_id:
            try:
                qs = qs.filter(dealer_id=int(dealer_id))
            except ValueError:
                pass

        order_status = request.query_params.get("status")
        if order_status:
            qs = qs.filter(status=order_status.strip().upper())

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = OrderSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    elif request.method == "POST":
        data = request.data
        if not isinstance(data, dict):
            raise ValidationError("Request body must be an object.")

        dealer_id = data.get("dealer_id")
        if dealer_id is None:
            raise ValidationError("dealer_id is required.")

        items = data.get("items")
        if items is None:
            raise ValidationError("items is required.")

        idempotency_key = request.headers.get("Idempotency-Key")

        order = create_draft_order(
            dealer_id=dealer_id,
            raw_items=items,
            idempotency_key=idempotency_key,
            raw_body_data=data,
        )

        out = OrderSerializer(order)
        # If order was created newly vs fetched by idempotency
        # Contract: "The same key with the same body returns the original order (200 or 201) and creates nothing new."
        return Response(out.data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT"])
def order_detail(request, order_number):
    order_number = order_number.strip()
    if request.method == "GET":
        try:
            order = (
                Order.objects.select_related("dealer")
                .prefetch_related("items__product")
                .get(order_number=order_number)
            )
        except Order.DoesNotExist:
            raise NotFound(f"Order '{order_number}' not found.")

        serializer = OrderSerializer(order)
        return Response(serializer.data, status=status.HTTP_200_OK)

    elif request.method == "PUT":
        items = request.data.get("items")
        if items is None:
            raise ValidationError("items is required.")

        order = update_draft_order(order_number, items)
        out = OrderSerializer(order)
        return Response(out.data, status=status.HTTP_200_OK)


@api_view(["POST"])
def order_confirm(request, order_number):
    order = confirm_order(order_number.strip())
    out = OrderSerializer(order)
    return Response(out.data, status=status.HTTP_200_OK)


@api_view(["POST"])
def order_dispatch(request, order_number):
    order = dispatch_order(order_number.strip())
    out = OrderSerializer(order)
    return Response(out.data, status=status.HTTP_200_OK)


@api_view(["POST"])
def order_deliver(request, order_number):
    order = deliver_order(order_number.strip())
    out = OrderSerializer(order)
    return Response(out.data, status=status.HTTP_200_OK)


@api_view(["POST"])
def order_cancel(request, order_number):
    order = cancel_order(order_number.strip())
    out = OrderSerializer(order)
    return Response(out.data, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 8. Supplier Sync
# --------------------------------------------------------------------------------------
@api_view(["POST"])
def supplier_sync(request):
    res = run_supplier_sync()
    return Response(res, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 9. Dealer Assistant
# --------------------------------------------------------------------------------------
@api_view(["POST"])
def assistant_chat(request):
    data = request.data
    if not isinstance(data, dict):
        raise ValidationError("Request body must be an object.")

    session_id = str(data.get("session_id", "")).strip()
    dealer_id = data.get("dealer_id")
    message = str(data.get("message", "")).strip()

    if not session_id:
        raise ValidationError("session_id is required.")
    if dealer_id is None:
        raise ValidationError("dealer_id is required.")
    if not message:
        raise ValidationError("message is required.")

    res = handle_assistant_chat(session_id, dealer_id, message)
    return Response(res, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------------------
# 10. Forecasting and Reorder
# --------------------------------------------------------------------------------------
@api_view(["GET"])
def forecast_detail(request, sku):
    res = get_forecast_for_sku(sku)
    return Response(res, status=status.HTTP_200_OK)


@api_view(["GET"])
def reorder_suggestions(request):
    results = get_reorder_suggestions()
    return Response({"results": results}, status=status.HTTP_200_OK)
