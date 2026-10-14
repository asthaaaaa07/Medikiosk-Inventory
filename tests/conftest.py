import os
import sys

import mongomock
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

# mongomock quirk: on an EMPTY collection it evaluates $expr filters against {} and raises
# KeyError (real MongoDB returns no documents). Skip that check so tests match real behaviour.
_orig_iter = mongomock.collection.Collection._iter_documents


def _iter_documents(self, filter):
    if self._store.is_empty:
        return iter(())
    return _orig_iter(self, filter)


mongomock.collection.Collection._iter_documents = _iter_documents


@pytest.fixture
def mongo():
    """In-memory stand-in for a MongoDB server (tests only; the app itself uses real PyMongo)."""
    return mongomock.MongoClient()


@pytest.fixture
def app(mongo):
    return create_app({"TESTING": True, "CSRF_ENABLED": False, "MONGO_CLIENT": mongo,
                       "MONGODB_DB": "medikiosk_test", "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(mongo):
    return mongo["medikiosk_test"]


VALID = {
    "name": "Test Paracetamol 500 mg", "code": "tst-001", "category": "Analgesic",
    "batch_number": "B1", "supplier": "Demo Supplier", "quantity": "100", "unit_price": "1.5",
    "reorder_level": "20", "expiry_date": "2099-01-01", "storage_location": "Main Pharmacy",
    "description": "test item",
}


@pytest.fixture
def valid_form():
    return dict(VALID)
