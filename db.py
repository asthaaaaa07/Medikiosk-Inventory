"""MongoDB connection and index setup (PyMongo)."""
from flask import current_app, g
from pymongo import ASCENDING, DESCENDING, MongoClient

_client = None  # one MongoClient per process; it keeps its own connection pool


def get_client():
    """Return the shared MongoClient. Tests can inject one via app.config['MONGO_CLIENT']."""
    global _client
    injected = current_app.config.get("MONGO_CLIENT")
    if injected is not None:
        return injected
    if _client is None:
        uri = current_app.config["MONGODB_URI"]
        if not uri:
            raise RuntimeError(
                "MONGODB_URI is not set. Copy .env.example to .env and put your "
                "MongoDB Atlas (or local MongoDB) connection string in it."
            )
        _client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="MediKiosk")
    return _client


def get_db():
    if "db" not in g:
        db = get_client()[current_app.config["MONGODB_DB"]]
        if not current_app.config.get("_INDEXES_READY"):
            ensure_indexes(db)  # first request only; also confirms the server is reachable
            current_app.config["_INDEXES_READY"] = True
        g.db = db
    return g.db


def ensure_indexes(db):
    """Create indexes. Safe to call repeatedly: MongoDB ignores an index that already exists."""
    med = db.medicines
    med.create_index([("code", ASCENDING)], unique=True, name="uniq_code")
    med.create_index([("name", ASCENDING)], name="name")
    med.create_index([("category", ASCENDING)], name="category")
    med.create_index([("storage_location", ASCENDING)], name="storage_location")
    med.create_index([("expiry_date", ASCENDING)], name="expiry_date")

    tx = db.stock_transactions
    tx.create_index([("medicine_id", ASCENDING), ("created_at", DESCENDING)], name="medicine_history")
    tx.create_index([("created_at", DESCENDING)], name="recent")
    tx.create_index([("type", ASCENDING), ("created_at", DESCENDING)], name="type_recent")
