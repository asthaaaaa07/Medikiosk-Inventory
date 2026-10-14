"""Inventory business logic: validation, CRUD, stock movements, alerts and reports.

Every function takes a PyMongo Database object, so the logic is independent of Flask
and easy to test. All queries are built as Python dicts (never as strings), so user
input cannot change the structure of a query.
"""
import re
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

CATEGORIES = [
    "Analgesic", "Antibiotic", "Antidiabetic", "Antihypertensive", "Antihistamine",
    "Antacid", "Cardiac", "Respiratory", "Vitamin/Supplement", "IV Fluid",
    "Antiseptic", "Vaccine", "Other",
]
LOCATIONS = ["Main Pharmacy", "Central Store", "Emergency Ward", "ICU", "OPD Counter", "Cold Storage"]
REASONS_IN = ["Purchase / supplier delivery", "Transfer in", "Return from ward", "Opening stock", "Other"]
REASONS_OUT = ["Issued to ward", "Issued to patient counter", "Transfer out", "Expired - disposed", "Damaged", "Other"]

CODE_RE = re.compile(r"^[A-Z0-9-]{2,20}$")
TEXT_LIMITS = {"name": 100, "category": 40, "batch_number": 30, "supplier": 100,
               "storage_location": 40, "description": 500}


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # PyMongo stores naive datetimes as UTC


def today():
    return datetime.combine(datetime.now().date(), datetime.min.time())


def to_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


# ---------------------------------------------------------------- validation

def validate_medicine(form, editing=False):
    """Clean and validate form data. Returns a dict ready for MongoDB or raises ValidationError."""
    errors, doc = [], {}

    def text(field, label, required=True):
        value = (form.get(field) or "").strip()
        if required and not value:
            errors.append(f"{label} is required.")
        elif len(value) > TEXT_LIMITS[field]:
            errors.append(f"{label} must be at most {TEXT_LIMITS[field]} characters.")
        doc[field] = value

    def number(field, label, cast, minimum=0):
        raw = (str(form.get(field, "")) or "").strip()
        try:
            value = cast(raw)
            if value != value or value < minimum:  # value != value catches NaN
                raise ValueError
            doc[field] = value
        except (ValueError, TypeError):
            kind = "a whole number" if cast is int else "a number"
            errors.append(f"{label} must be {kind} of at least {minimum}.")

    text("name", "Medicine name")
    if not editing:  # code is the business key; it is fixed after creation
        code = (form.get("code") or "").strip().upper()
        if not CODE_RE.match(code):
            errors.append("Medicine code must be 2-20 characters: letters, digits or '-'.")
        doc["code"] = code
        number("quantity", "Opening quantity", int)
    text("category", "Category")
    text("batch_number", "Batch number")
    text("supplier", "Supplier", required=False)
    number("unit_price", "Unit price", float)
    if "unit_price" in doc:
        doc["unit_price"] = round(doc["unit_price"], 2)
    number("reorder_level", "Reorder level", int)
    text("storage_location", "Storage location")
    text("description", "Description", required=False)

    raw_date = (form.get("expiry_date") or "").strip()
    try:
        doc["expiry_date"] = datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        errors.append("Expiry date is required (YYYY-MM-DD).")

    if errors:
        raise ValidationError(errors)
    return doc


# ---------------------------------------------------------------- medicines CRUD

def create_medicine(db, form):
    doc = validate_medicine(form)
    now = utcnow()
    doc.update(created_at=now, updated_at=now)
    try:
        result = db.medicines.insert_one(doc)  # unique index on `code` blocks duplicates
    except DuplicateKeyError:
        raise ValidationError([f"Medicine code {doc['code']} already exists."])
    if doc["quantity"] > 0:
        _log(db, doc | {"_id": result.inserted_id}, "RECEIVED", doc["quantity"], "Opening stock",
             before=0, after=doc["quantity"])
    return result.inserted_id


def update_medicine(db, medicine_id, form):
    """Edits details only. Quantity changes go through stock operations so every change is logged."""
    doc = validate_medicine(form, editing=True)
    doc["updated_at"] = utcnow()
    result = db.medicines.update_one({"_id": medicine_id}, {"$set": doc})
    return result.matched_count == 1


def delete_medicine(db, medicine_id):
    # Transaction history is kept on purpose (audit trail); it stores the code/name snapshot.
    return db.medicines.delete_one({"_id": medicine_id}).deleted_count == 1


def get_medicine(db, medicine_id):
    return db.medicines.find_one({"_id": medicine_id})


def build_filter(q="", category="", location="", stock="", expiry="", soon_days=30):
    """Turn search/filter inputs into a MongoDB filter document."""
    clauses = []
    q = (q or "").strip()[:100]
    if q:
        pattern = {"$regex": re.escape(q), "$options": "i"}  # escaped: user text is matched literally
        clauses.append({"$or": [{"name": pattern}, {"code": pattern}]})
    if category:
        clauses.append({"category": category})
    if location:
        clauses.append({"storage_location": location})
    if stock == "low":
        clauses.append({"$expr": {"$lte": ["$quantity", "$reorder_level"]}})
    elif stock == "out":
        clauses.append({"quantity": 0})
    start, soon = today(), today() + timedelta(days=soon_days)
    if expiry == "expired":
        clauses.append({"expiry_date": {"$lt": start}})
    elif expiry == "soon":
        clauses.append({"expiry_date": {"$gte": start, "$lte": soon}})
    elif expiry == "ok":
        clauses.append({"expiry_date": {"$gt": soon}})
    return {"$and": clauses} if clauses else {}


def list_medicines(db, sort="name", **filters):
    sort_field = {"name": "name", "code": "code", "quantity": "quantity", "expiry": "expiry_date"}.get(sort, "name")
    return list(db.medicines.find(build_filter(**filters)).sort(sort_field, 1))


def distinct_values(db, field, defaults):
    return sorted(set(defaults) | {v for v in db.medicines.distinct(field) if v})


# ---------------------------------------------------------------- stock movements

def _log(db, med, tx_type, qty, reason, before, after, note=""):
    db.stock_transactions.insert_one({
        "medicine_id": med["_id"],
        "medicine_code": med["code"],      # snapshot so history still reads well after deletion
        "medicine_name": med["name"],
        "type": tx_type,                   # RECEIVED | ISSUED
        "quantity": qty,
        "reason": reason,
        "note": note,
        "balance_before": before,
        "balance_after": after,
        "created_at": utcnow(),
    })


def _parse_stock_form(form, reasons):
    errors = []
    try:
        qty = int((form.get("quantity") or "").strip())
        if not 1 <= qty <= 100000:
            raise ValueError
    except ValueError:
        errors.append("Quantity must be a whole number between 1 and 100000.")
        qty = 0
    reason = (form.get("reason") or "").strip()
    if reason not in reasons:
        errors.append("Please choose a valid reason.")
    note = (form.get("note") or "").strip()[:200]
    if errors:
        raise ValidationError(errors)
    return qty, reason, note


def receive_stock(db, medicine_id, form):
    qty, reason, note = _parse_stock_form(form, REASONS_IN)
    med = db.medicines.find_one_and_update(
        {"_id": medicine_id},
        {"$inc": {"quantity": qty}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if med is None:
        raise ValidationError(["Medicine not found."])
    _log(db, med, "RECEIVED", qty, reason, med["quantity"] - qty, med["quantity"], note)
    return med


def issue_stock(db, medicine_id, form):
    """Issue stock without ever going negative.

    The check and the decrement happen in ONE atomic MongoDB operation: the document only
    matches if quantity >= qty, so two simultaneous issues cannot both succeed.
    """
    qty, reason, note = _parse_stock_form(form, REASONS_OUT)
    med = db.medicines.find_one_and_update(
        {"_id": medicine_id, "quantity": {"$gte": qty}},
        {"$inc": {"quantity": -qty}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if med is None:
        current = db.medicines.find_one({"_id": medicine_id}, {"quantity": 1})
        if current is None:
            raise ValidationError(["Medicine not found."])
        raise ValidationError([f"Cannot issue {qty} units: only {current['quantity']} in stock."])
    _log(db, med, "ISSUED", qty, reason, med["quantity"] + qty, med["quantity"], note)
    return med


def list_transactions(db, tx_type="", q="", days=0, limit=200, medicine_id=None):
    flt = {}
    if tx_type in ("RECEIVED", "ISSUED"):
        flt["type"] = tx_type
    if medicine_id is not None:
        flt["medicine_id"] = medicine_id
    q = (q or "").strip()[:100]
    if q:
        pattern = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"medicine_name": pattern}, {"medicine_code": pattern}]
    if days:
        flt["created_at"] = {"$gte": utcnow() - timedelta(days=days)}
    return list(db.stock_transactions.find(flt).sort("created_at", DESCENDING).limit(limit))


# ---------------------------------------------------------------- alerts, dashboard, reports

def medicine_status(med, soon_days):
    """Rule-based status flags for one medicine (used for badges and alerts)."""
    start = today()
    expiry = med["expiry_date"]
    return {
        "expired": expiry < start,
        "expiring_soon": start <= expiry <= start + timedelta(days=soon_days),
        "days_to_expiry": (expiry - start).days,
        "out_of_stock": med["quantity"] == 0,
        "low_stock": med["quantity"] <= med["reorder_level"],
    }


def get_alerts(db, soon_days):
    """Return alert lists. Urgent = expired or out of stock."""
    low = list(db.medicines.find(build_filter(stock="low")).sort("quantity", 1))
    expired = list(db.medicines.find(build_filter(expiry="expired")).sort("expiry_date", 1))
    soon = list(db.medicines.find(build_filter(expiry="soon", soon_days=soon_days)).sort("expiry_date", 1))
    for med in low:
        # Suggested order = bring stock back up to twice the reorder level (simple rule, not optimised).
        med["suggested_order"] = max(2 * med["reorder_level"] - med["quantity"], 0)
    return {
        "low_stock": low,
        "out_of_stock": [m for m in low if m["quantity"] == 0],
        "expired": expired,
        "expiring_soon": soon,
        "urgent_count": len(expired) + sum(1 for m in low if m["quantity"] == 0),
    }


def dashboard_stats(db, soon_days):
    totals = list(db.medicines.aggregate([
        {"$group": {
            "_id": None,
            "medicines": {"$sum": 1},
            "units": {"$sum": "$quantity"},
            "value": {"$sum": {"$multiply": ["$quantity", "$unit_price"]}},
        }}
    ]))
    totals = totals[0] if totals else {"medicines": 0, "units": 0, "value": 0}
    by_category = list(db.medicines.aggregate([
        {"$group": {"_id": "$category", "units": {"$sum": "$quantity"}, "count": {"$sum": 1}}},
        {"$sort": {"units": -1}},
    ]))
    by_location = list(db.medicines.aggregate([
        {"$group": {"_id": "$storage_location", "units": {"$sum": "$quantity"}}},
        {"$sort": {"_id": 1}},
    ]))
    alerts = get_alerts(db, soon_days)
    return {
        "total_medicines": totals["medicines"],
        "total_units": totals["units"],
        "stock_value": round(totals["value"], 2),
        "low_stock_count": len(alerts["low_stock"]),
        "expired_count": len(alerts["expired"]),
        "expiring_soon_count": len(alerts["expiring_soon"]),
        "alerts": alerts,
        "by_category": by_category,
        "by_location": by_location,
        "movement": movement_by_day(db, days=14),
        "recent": list_transactions(db, limit=8),
    }


def movement_by_day(db, days=14):
    """Units received vs issued per day for the last `days` days (aggregation pipeline)."""
    since = utcnow() - timedelta(days=days - 1)
    since = datetime(since.year, since.month, since.day)
    rows = db.stock_transactions.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {
            "_id": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "type": "$type"},
            "units": {"$sum": "$quantity"},
        }},
    ])
    labels = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    series = {"RECEIVED": dict.fromkeys(labels, 0), "ISSUED": dict.fromkeys(labels, 0)}
    for row in rows:
        day, kind = row["_id"]["day"], row["_id"]["type"]
        if kind in series and day in series[kind]:
            series[kind][day] = row["units"]
    return {"labels": labels, "received": list(series["RECEIVED"].values()),
            "issued": list(series["ISSUED"].values())}
