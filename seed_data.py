"""Load realistic demo medicines into MongoDB.

    python seed_data.py           # insert/refresh demo data (safe to run again: no duplicates)
    python seed_data.py --reset   # delete ALL medicines and stock history first, then seed

Re-running is idempotent: medicines are upserted by their unique `code`, and seed-generated
history (marked seed=True) is replaced, so nothing is duplicated. Medicines you added
yourself are not touched unless you use --reset.

All supplier names are fictitious. No patient data is used.
"""
import random
import sys
from datetime import timedelta

from pymongo import MongoClient

from config import Config
from db import ensure_indexes
from inventory import today, utcnow

# code, name, category, location, quantity, reorder level, unit price (₹), expiry in days from today
DEMO = [
    ("PCM-500", "Paracetamol 500 mg tablet", "Analgesic", "Main Pharmacy", 1200, 300, 1.20, 540),
    ("IBU-400", "Ibuprofen 400 mg tablet", "Analgesic", "Emergency Ward", 450, 150, 2.10, 400),
    ("AMX-250", "Amoxicillin 250 mg capsule", "Antibiotic", "Main Pharmacy", 80, 200, 3.50, 300),
    ("AZT-500", "Azithromycin 500 mg tablet", "Antibiotic", "OPD Counter", 160, 60, 18.00, 12),
    ("CEF-1G", "Ceftriaxone 1 g injection", "Antibiotic", "ICU", 25, 40, 55.00, 210),
    ("MET-500", "Metformin 500 mg tablet", "Antidiabetic", "Main Pharmacy", 900, 250, 1.50, 480),
    ("INS-R40", "Insulin regular 40 IU/ml vial", "Antidiabetic", "Cold Storage", 18, 20, 145.00, 20),
    ("AML-5", "Amlodipine 5 mg tablet", "Antihypertensive", "Main Pharmacy", 640, 200, 1.80, 365),
    ("ASP-75", "Aspirin 75 mg tablet", "Cardiac", "Emergency Ward", 90, 120, 0.90, 330),
    ("CTZ-10", "Cetirizine 10 mg tablet", "Antihistamine", "OPD Counter", 220, 80, 1.10, -10),
    ("PAN-40", "Pantoprazole 40 mg tablet", "Antacid", "Main Pharmacy", 700, 200, 2.40, 450),
    ("SAL-INH", "Salbutamol 100 mcg inhaler", "Respiratory", "Emergency Ward", 30, 15, 135.00, 25),
    ("NS-500", "Normal saline 0.9% 500 ml", "IV Fluid", "Central Store", 300, 100, 28.00, 600),
    ("RL-500", "Ringer lactate 500 ml", "IV Fluid", "Central Store", 0, 80, 32.00, 500),
    ("ORS-21", "ORS sachet 21 g", "Other", "OPD Counter", 500, 150, 6.00, 365),
    ("ONDA-4", "Ondansetron 4 mg/2 ml injection", "Other", "ICU", 60, 30, 12.50, 5),
    ("POV-100", "Povidone-iodine 5% solution 100 ml", "Antiseptic", "Central Store", 75, 25, 65.00, 700),
    ("VTD-60K", "Vitamin D3 60,000 IU capsule", "Vitamin/Supplement", "OPD Counter", 260, 80, 22.00, 420),
    ("TT-VAC", "Tetanus toxoid vaccine 0.5 ml", "Vaccine", "Cold Storage", 45, 20, 18.00, 150),
]
SUPPLIERS = ["Sahyadri Pharma Distributors", "Deccan MedSupply", "Western Healthcare Traders", "Konkan Surgicals"]


def seed(db, rng=None):
    rng = rng or random.Random(42)  # fixed seed -> same demo data every time
    ensure_indexes(db)
    db.stock_transactions.delete_many({"seed": True})
    now, start = utcnow(), today()
    for i, (code, name, cat, loc, qty, reorder, price, expiry_days) in enumerate(DEMO):
        med = db.medicines.find_one_and_update(
            {"code": code},
            {"$set": {
                "name": name, "category": cat, "storage_location": loc, "quantity": qty,
                "reorder_level": reorder, "unit_price": price,
                "batch_number": f"B{now.year % 100}{i + 1:03d}{chr(65 + i % 26)}",
                "supplier": SUPPLIERS[i % len(SUPPLIERS)],
                "expiry_date": start + timedelta(days=expiry_days),
                "description": f"Demo item. {name}.",
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now - timedelta(days=15)}},
            upsert=True, return_document=True,
        )
        # Build a believable 14-day history that ends exactly at the current quantity.
        def when(days_ago):
            return now - timedelta(days=days_ago, hours=rng.randint(1, 8), minutes=rng.randint(0, 59))

        events = sorted(
            [(when(rng.randint(0, 13)), "ISSUED", max(1, reorder // rng.randint(4, 10)))
             for _ in range(rng.randint(2, 5))]
            + [(when(rng.randint(3, 13)), "RECEIVED", reorder)]
        )
        # Walk backwards from today's quantity so every balance is consistent and never negative.
        after, rows = qty, []
        for created_at, kind, q in reversed(events):
            if kind == "RECEIVED":
                q = min(q, after)  # a delivery can't be larger than the stock it produced
                if q == 0:
                    continue
            before = after - q if kind == "RECEIVED" else after + q
            rows.append({
                "medicine_id": med["_id"], "medicine_code": code, "medicine_name": name,
                "type": kind, "quantity": q,
                "reason": "Purchase / supplier delivery" if kind == "RECEIVED" else rng.choice(
                    ["Issued to ward", "Issued to patient counter"]),
                "note": "demo data", "balance_before": before, "balance_after": after,
                "created_at": created_at, "seed": True,
            })
            after = before
        if rows:
            db.stock_transactions.insert_many(rows)
    return len(DEMO)


def main():
    if not Config.MONGODB_URI:
        sys.exit("MONGODB_URI is not set. Copy .env.example to .env and add your connection string.")
    client = MongoClient(Config.MONGODB_URI, serverSelectionTimeoutMS=8000)
    db = client[Config.MONGODB_DB]
    if "--reset" in sys.argv:
        if input(f"Delete ALL medicines and stock history in '{Config.MONGODB_DB}'? Type yes: ").strip() != "yes":
            sys.exit("Cancelled.")
        db.medicines.delete_many({})
        db.stock_transactions.delete_many({})
    count = seed(db)
    print(f"Seeded {count} demo medicines into database '{Config.MONGODB_DB}'.")
    print(f"Collections now hold {db.medicines.count_documents({})} medicines and "
          f"{db.stock_transactions.count_documents({})} stock transactions.")


if __name__ == "__main__":
    main()
