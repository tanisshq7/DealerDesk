from django.urls import path

from . import views

urlpatterns = [
    # Health check
    path("health/", views.health),

    # 4. Products
    path("products/", views.product_list),
    path("products/<str:sku>/", views.product_detail),

    # 5. Inventory
    path("inventory/<str:sku>/", views.inventory_update),

    # 6. Dealers
    path("dealers/", views.dealer_list),
    path("dealers/<int:pk>/", views.dealer_detail),

    # 7. Orders
    path("orders/", views.order_list),
    path("orders/<str:order_number>/", views.order_detail),
    path("orders/<str:order_number>/confirm/", views.order_confirm),
    path("orders/<str:order_number>/dispatch/", views.order_dispatch),
    path("orders/<str:order_number>/deliver/", views.order_deliver),
    path("orders/<str:order_number>/cancel/", views.order_cancel),

    # 8. Supplier Sync
    path("sync/supplier/", views.supplier_sync),

    # 9. Dealer Assistant
    path("assistant/chat/", views.assistant_chat),

    # 10. Forecasting and Reorder
    path("forecast/<str:sku>/", views.forecast_detail),
    path("reorder-suggestions/", views.reorder_suggestions),
]
