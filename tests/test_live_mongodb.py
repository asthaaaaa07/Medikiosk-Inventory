"""Optional test against a REAL MongoDB server (Atlas or local).

Skipped unless MONGODB_TEST_URI is set, e.g.
    MONGODB_TEST_URI="mongodb+srv://..." pytest tests/test_live_mongodb.py -v
It uses a separate database (medikiosk_live_test) and drops it afterwards.
"""
import os

import pytest
from pymongo import MongoClient

import inventory as inv
from seed_data import seed

URI = os.getenv("MONGODB_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="set MONGODB_TEST_URI to run against a real MongoDB")


@pytest.fixture
def live_db():
    client = MongoClient(URI, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    db = client["medikiosk_live_test"]
    yield db
    client.drop_database("medikiosk_live_test")


def test_crud_stock_and_aggregation_on_real_mongodb(live_db):
    seed(live_db)
    assert live_db.medicines.count_documents({}) > 0
    mid = inv.create_medicine(live_db, {
        "name": "Live test item", "code": "LIVE-1", "category": "Other", "batch_number": "L1",
        "quantity": "10", "unit_price": "1", "reorder_level": "5", "expiry_date": "2099-01-01",
        "storage_location": "Main Pharmacy"})
    with pytest.raises(inv.ValidationError):
        inv.create_medicine(live_db, {"name": "dup", "code": "LIVE-1", "category": "Other", "batch_number": "x",
                                      "quantity": "1", "unit_price": "1", "reorder_level": "1",
                                      "expiry_date": "2099-01-01", "storage_location": "ICU"})
    inv.issue_stock(live_db, mid, {"quantity": "7", "reason": "Issued to ward"})
    with pytest.raises(inv.ValidationError):
        inv.issue_stock(live_db, mid, {"quantity": "4", "reason": "Issued to ward"})
    assert live_db.medicines.find_one({"_id": mid})["quantity"] == 3
    assert any(m["code"] == "LIVE-1" for m in inv.get_alerts(live_db, 30)["low_stock"])
    stats = inv.dashboard_stats(live_db, 30)
    assert stats["total_medicines"] == live_db.medicines.count_documents({})
    assert inv.delete_medicine(live_db, mid)
