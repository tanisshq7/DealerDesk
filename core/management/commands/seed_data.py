import csv
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Dealer, Inventory, Product


class Command(BaseCommand):
    help = "Load catalogue and dealers safely from CSV files."

    @transaction.atomic
    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR
        catalogue_file = data_dir / "catalogue.csv"
        dealers_file = data_dir / "dealers.csv"

        if not catalogue_file.exists() or not dealers_file.exists():
            raise CommandError(
                "Required files catalogue.csv and dealers.csv "
                "must exist in the data directory."
            )

        existing_products = {p.sku: p for p in Product.objects.all()}
        existing_inventory_skus = set(
            Inventory.objects.values_list("product_id", flat=True)
        )
        existing_dealers = {d.dealer_code: d for d in Dealer.objects.all()}

        products_to_create = []
        products_to_update = []
        inventory_to_create = []

        with catalogue_file.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)

            for row in reader:
                sku = row["sku"].strip()
                name = row["name"].strip()
                category = row["category"].strip()
                brand = row["brand"].strip()
                vehicle_fitment = row["vehicle_fitment"].strip()
                price = Decimal(row["price"])
                description = row["description"].strip()
                stock = int(row["stock"])

                if sku in existing_products:
                    product = existing_products[sku]
                    product.name = name
                    product.category = category
                    product.brand = brand
                    product.vehicle_fitment = vehicle_fitment
                    product.price = price
                    product.description = description
                    products_to_update.append(product)
                else:
                    product = Product(
                        sku=sku,
                        name=name,
                        category=category,
                        brand=brand,
                        vehicle_fitment=vehicle_fitment,
                        price=price,
                        description=description,
                    )
                    products_to_create.append(product)

                if sku not in existing_inventory_skus:
                    inventory_to_create.append(
                        Inventory(product=product, quantity=stock)
                    )
                    existing_inventory_skus.add(sku)

        if products_to_create:
            Product.objects.bulk_create(products_to_create)
        if products_to_update:
            Product.objects.bulk_update(
                products_to_update,
                fields=[
                    "name",
                    "category",
                    "brand",
                    "vehicle_fitment",
                    "price",
                    "description",
                ],
            )
        if inventory_to_create:
            Inventory.objects.bulk_create(inventory_to_create)

        products_created = len(products_to_create)

        dealers_to_create = []
        dealers_to_update = []

        with dealers_file.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)

            for row in reader:
                dealer_code = row["dealer_code"].strip()
                name = row["name"].strip()
                city = row["city"].strip()
                phone = row["phone"].strip()
                email = row["email"].strip()
                gstin = row["gstin"].strip()
                credit_limit = Decimal(row["credit_limit"])

                if dealer_code in existing_dealers:
                    dealer = existing_dealers[dealer_code]
                    dealer.name = name
                    dealer.city = city
                    dealer.phone = phone
                    dealer.email = email
                    dealer.gstin = gstin
                    dealer.credit_limit = credit_limit
                    dealers_to_update.append(dealer)
                else:
                    dealer = Dealer(
                        dealer_code=dealer_code,
                        name=name,
                        city=city,
                        phone=phone,
                        email=email,
                        gstin=gstin,
                        credit_limit=credit_limit,
                    )
                    dealers_to_create.append(dealer)

        if dealers_to_create:
            Dealer.objects.bulk_create(dealers_to_create)
        if dealers_to_update:
            Dealer.objects.bulk_update(
                dealers_to_update,
                fields=[
                    "name",
                    "city",
                    "phone",
                    "email",
                    "gstin",
                    "credit_limit",
                ],
            )

        dealers_created = len(dealers_to_create)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeding complete. "
                f"New products: {products_created}, "
                f"new dealers: {dealers_created}."
            )
        )
