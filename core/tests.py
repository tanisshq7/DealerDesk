from decimal import Decimal
import json

from django.test import TestCase, Client
from django.urls import reverse

from core.models import Dealer, Inventory, Order, OrderItem, Product


class DealerDeskTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create a test product
        self.p1 = Product.objects.create(
            sku="TST-1001",
            name="Test Brake Pad",
            category="Brakes",
            brand="Endurance",
            vehicle_fitment="Bajaj Pulsar 150",
            price=Decimal("100.00"),
            description="High quality brake pad",
        )
        self.inv1 = Inventory.objects.create(product=self.p1, quantity=10)

        self.p2 = Product.objects.create(
            sku="TST-1002",
            name="Test Oil Filter",
            category="Filters",
            brand="Bosch",
            vehicle_fitment="Bajaj Pulsar 220",
            price=Decimal("50.00"),
            description="High flow filter",
        )
        self.inv2 = Inventory.objects.create(product=self.p2, quantity=5)

        self.dealer = Dealer.objects.create(
            dealer_code="DLR-TEST-01",
            name="Test Dealer Ltd",
            city="Bengaluru",
            phone="9876543210",
            email="test@dealer.com",
            gstin="29ABCDE1234F1Z5",
            credit_limit=Decimal("50000.00"),
        )

    def test_health_check(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_products_list_and_filters(self):
        resp = self.client.get("/api/products/?search=Endurance")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["sku"], "TST-1001")

        # Category filter
        resp2 = self.client.get("/api/products/?category=Filters")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["count"], 1)

        # Vehicle filter
        resp3 = self.client.get("/api/products/?vehicle=pulsar")
        self.assertEqual(resp3.status_code, 200)
        self.assertEqual(resp3.json()["count"], 2)

    def test_product_create_and_validation(self):
        # Valid create
        resp = self.client.post(
            "/api/products/",
            data=json.dumps({
                "sku": "NEW-001",
                "name": "New Part",
                "price": "250.00",
                "category": "Engine",
                "brand": "Hero",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["stock"], 0)

        # Duplicate SKU -> 409 DUPLICATE
        dup = self.client.post(
            "/api/products/",
            data=json.dumps({
                "sku": "NEW-001",
                "name": "New Part 2",
                "price": "300.00",
            }),
            content_type="application/json",
        )
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(dup.json()["error"]["code"], "DUPLICATE")

        # Invalid price -> 400 VALIDATION_ERROR
        bad_price = self.client.post(
            "/api/products/",
            data=json.dumps({
                "sku": "NEW-002",
                "name": "Bad Price",
                "price": "-10.00",
            }),
            content_type="application/json",
        )
        self.assertEqual(bad_price.status_code, 400)
        self.assertEqual(bad_price.json()["error"]["code"], "VALIDATION_ERROR")

    def test_inventory_update(self):
        resp = self.client.put(
            f"/api/inventory/{self.p1.sku}/",
            data=json.dumps({"quantity": 25, "reason": "Audit"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"sku": "TST-1001", "quantity": 25})
        self.p1.inventory.refresh_from_db()
        self.assertEqual(self.p1.inventory.quantity, 25)

        # Negative quantity
        bad = self.client.put(
            f"/api/inventory/{self.p1.sku}/",
            data=json.dumps({"quantity": -5, "reason": "Audit"}),
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "VALIDATION_ERROR")

        # 404 Not Found
        nfound = self.client.put(
            "/api/inventory/NONEXISTENT/",
            data=json.dumps({"quantity": 10, "reason": "Audit"}),
            content_type="application/json",
        )
        self.assertEqual(nfound.status_code, 404)
        self.assertEqual(nfound.json()["error"]["code"], "NOT_FOUND")

    def test_order_creation_gst_and_lifecycle(self):
        # Create draft order
        # item 1: 2 x 100 = 200 subtotal, 36 tax (18%), 236 total
        # item 2: 1 x 50 = 50 subtotal, 9 tax (18%), 59 total
        # Order total: 250 subtotal, 45 tax, 295 grand_total
        payload = {
            "dealer_id": self.dealer.id,
            "items": [
                {"sku": "TST-1001", "quantity": 2},
                {"sku": "TST-1002", "quantity": 1},
            ],
        }
        resp = self.client.post(
            "/api/orders/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-123",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        order_no = data["order_number"]
        self.assertTrue(order_no.startswith("ORD-"))
        self.assertEqual(data["status"], "DRAFT")
        self.assertEqual(data["subtotal"], "250.00")
        self.assertEqual(data["tax_total"], "45.00")
        self.assertEqual(data["grand_total"], "295.00")

        # Idempotent replay with same body returns 201 or 200
        replay = self.client.post(
            "/api/orders/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-123",
        )
        self.assertIn(replay.status_code, (200, 201))
        self.assertEqual(replay.json()["order_number"], order_no)

        # Idempotency conflict with different body -> 409
        diff_payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "TST-1001", "quantity": 5}],
        }
        conflict = self.client.post(
            "/api/orders/",
            data=json.dumps(diff_payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-123",
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")

        # Confirm order: stocks deduct 10-2=8, 5-1=4
        c_resp = self.client.post(f"/api/orders/{order_no}/confirm/")
        self.assertEqual(c_resp.status_code, 200)
        self.assertEqual(c_resp.json()["status"], "CONFIRMED")
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 8)
        self.assertEqual(self.inv2.quantity, 4)

        # Confirming again -> 409 INVALID_TRANSITION
        c2 = self.client.post(f"/api/orders/{order_no}/confirm/")
        self.assertEqual(c2.status_code, 409)
        self.assertEqual(c2.json()["error"]["code"], "INVALID_TRANSITION")

        # Dispatch
        d_resp = self.client.post(f"/api/orders/{order_no}/dispatch/")
        self.assertEqual(d_resp.status_code, 200)
        self.assertEqual(d_resp.json()["status"], "DISPATCHED")

        # Deliver
        del_resp = self.client.post(f"/api/orders/{order_no}/deliver/")
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(del_resp.json()["status"], "DELIVERED")

        # Cannot cancel delivered order
        can = self.client.post(f"/api/orders/{order_no}/cancel/")
        self.assertEqual(can.status_code, 409)
        self.assertEqual(can.json()["error"]["code"], "INVALID_TRANSITION")

    def test_stock_shortage_on_confirm(self):
        # Create order requesting 100 units (available is 10)
        payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "TST-1001", "quantity": 100}],
        }
        resp = self.client.post(
            "/api/orders/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        order_no = resp.json()["order_number"]

        # Confirm fails
        c_resp = self.client.post(f"/api/orders/{order_no}/confirm/")
        self.assertEqual(c_resp.status_code, 409)
        err = c_resp.json()["error"]
        self.assertEqual(err["code"], "INSUFFICIENT_STOCK")
        self.assertEqual(err["details"][0]["sku"], "TST-1001")
        self.assertEqual(err["details"][0]["requested"], 100)
        self.assertEqual(err["details"][0]["available"], 10)
        self.assertIn("Insufficient stock", err["message"])

        # Stock unchanged
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 10)

    def test_cancel_restores_stock(self):
        payload = {
            "dealer_id": self.dealer.id,
            "items": [{"sku": "TST-1001", "quantity": 3}],
        }
        resp = self.client.post(
            "/api/orders/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        order_no = resp.json()["order_number"]
        self.client.post(f"/api/orders/{order_no}/confirm/")
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 7)

        # Cancel confirmed order restores stock
        self.client.post(f"/api/orders/{order_no}/cancel/")
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.quantity, 10)

    def test_assistant_chat(self):
        resp = self.client.post(
            "/api/assistant/chat/",
            data=json.dumps({
                "session_id": "test-session",
                "dealer_id": self.dealer.id,
                "message": "Do you have brake pads for Bajaj Pulsar 150?",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("reply", data)
        self.assertIsInstance(data["cited_skus"], list)
        self.assertIsInstance(data["tool_calls"], list)
        self.assertIsInstance(data["actions"], list)
