from datetime import timedelta

import pytest

import inventory as inv
from app import create_app
from seed_data import DEMO, seed


def add(client, form):
    return client.post("/medicines/new", data=form, follow_redirects=True)


# ---------------------------------------------------------------- validation

def test_valid_form_is_cleaned(valid_form):
    doc = inv.validate_medicine(valid_form)
    assert doc["code"] == "TST-001" and doc["quantity"] == 100 and doc["unit_price"] == 1.5


@pytest.mark.parametrize("field,value,message", [
    ("name", "", "Medicine name is required"),
    ("code", "x", "Medicine code must be"),
    ("code", "AB C", "Medicine code must be"),
    ("quantity", "-5", "Opening quantity must be"),
    ("quantity", "2.5", "Opening quantity must be"),
    ("unit_price", "abc", "Unit price must be"),
    ("unit_price", "nan", "Unit price must be"),
    ("reorder_level", "", "Reorder level must be"),
    ("expiry_date", "31-12-2030", "Expiry date is required"),
    ("storage_location", "", "Storage location is required"),
])
def test_invalid_fields_rejected(valid_form, field, value, message):
    valid_form[field] = value
    with pytest.raises(inv.ValidationError) as err:
        inv.validate_medicine(valid_form)
    assert any(message in e for e in err.value.errors)


# ---------------------------------------------------------------- CRUD through the web routes

def test_create_medicine_logs_opening_stock(client, db, valid_form):
    resp = add(client, valid_form)
    assert b"Medicine added successfully" in resp.data
    med = db.medicines.find_one({"code": "TST-001"})
    assert med["quantity"] == 100
    tx = db.stock_transactions.find_one({"medicine_id": med["_id"]})
    assert tx["type"] == "RECEIVED" and tx["quantity"] == 100 and tx["balance_after"] == 100


def test_duplicate_code_rejected(client, db, valid_form):
    add(client, valid_form)
    resp = add(client, valid_form)
    assert b"already exists" in resp.data
    assert db.medicines.count_documents({"code": "TST-001"}) == 1


def test_invalid_form_shows_errors_and_saves_nothing(client, db, valid_form):
    valid_form["quantity"] = "-1"
    resp = add(client, valid_form)
    assert b"Opening quantity must be" in resp.data
    assert db.medicines.count_documents({}) == 0


def test_edit_changes_details_but_not_code_or_quantity(client, db, valid_form):
    add(client, valid_form)
    med = db.medicines.find_one({"code": "TST-001"})
    edited = dict(valid_form, name="Renamed", code="HACK-1", quantity="999", reorder_level="50")
    resp = client.post(f"/medicines/{med['_id']}/edit", data=edited, follow_redirects=True)
    assert b"Medicine details updated" in resp.data
    med = db.medicines.find_one({"_id": med["_id"]})
    assert med["name"] == "Renamed" and med["reorder_level"] == 50
    assert med["code"] == "TST-001" and med["quantity"] == 100


def test_delete_requires_matching_code(client, db, valid_form):
    add(client, valid_form)
    mid = db.medicines.find_one({"code": "TST-001"})["_id"]
    resp = client.post(f"/medicines/{mid}/delete", data={"confirm_code": "WRONG"}, follow_redirects=True)
    assert b"Delete cancelled" in resp.data and db.medicines.count_documents({}) == 1
    resp = client.post(f"/medicines/{mid}/delete", data={"confirm_code": "tst-001"}, follow_redirects=True)
    assert b"deleted" in resp.data and db.medicines.count_documents({}) == 0
    assert db.stock_transactions.count_documents({"medicine_id": mid}) == 1  # audit trail kept


def test_unknown_or_malformed_id_returns_404(client):
    assert client.get("/medicines/not-an-id").status_code == 404
    assert client.get("/medicines/65f000000000000000000000").status_code == 404


# ---------------------------------------------------------------- stock movements

def _stock(client, mid, action, qty, reason):
    return client.post("/stock", data={"medicine_id": str(mid), "action": action, "quantity": str(qty),
                                       "reason": reason}, follow_redirects=True)


def test_receive_and_issue_update_quantity_and_history(client, db, valid_form):
    add(client, valid_form)
    mid = db.medicines.find_one({"code": "TST-001"})["_id"]
    _stock(client, mid, "receive", 50, "Purchase / supplier delivery")
    resp = _stock(client, mid, "issue", 30, "Issued to ward")
    assert b"New balance: 120" in resp.data
    assert db.medicines.find_one({"_id": mid})["quantity"] == 120
    last = db.stock_transactions.find({"medicine_id": mid}).sort("created_at", -1)[0]
    assert (last["type"], last["balance_before"], last["balance_after"]) == ("ISSUED", 150, 120)


def test_issue_more_than_stock_is_rejected(client, db, valid_form):
    add(client, valid_form)
    mid = db.medicines.find_one({"code": "TST-001"})["_id"]
    resp = _stock(client, mid, "issue", 101, "Issued to ward")
    assert b"only 100 in stock" in resp.data
    assert db.medicines.find_one({"_id": mid})["quantity"] == 100
    assert db.stock_transactions.count_documents({"type": "ISSUED"}) == 0


@pytest.mark.parametrize("qty,reason", [(0, "Issued to ward"), ("abc", "Issued to ward"), (5, "Made up reason")])
def test_bad_stock_input_rejected(client, db, valid_form, qty, reason):
    add(client, valid_form)
    mid = db.medicines.find_one({"code": "TST-001"})["_id"]
    _stock(client, mid, "issue", qty, reason)
    assert db.medicines.find_one({"_id": mid})["quantity"] == 100


def test_issue_below_reorder_level_triggers_alert(client, db, valid_form):
    add(client, valid_form)  # qty 100, reorder 20
    mid = db.medicines.find_one({"code": "TST-001"})["_id"]
    resp = _stock(client, mid, "issue", 85, "Issued to ward")
    assert b"Low-stock alert" in resp.data
    assert "Test Paracetamol" in client.get("/alerts").get_data(as_text=True)


# ---------------------------------------------------------------- alerts, filters, reports

def test_alert_rules(db, valid_form):
    start = inv.today()
    for code, qty, days in [("OK-1", 100, 400), ("LOW-1", 5, 400), ("EXP-1", 100, -1), ("SOON-1", 100, 10)]:
        doc = inv.validate_medicine(dict(valid_form, code=code, quantity=str(qty),
                                         expiry_date=(start + timedelta(days=days)).strftime("%Y-%m-%d")))
        db.medicines.insert_one(doc)
    a = inv.get_alerts(db, soon_days=30)
    assert [m["code"] for m in a["low_stock"]] == ["LOW-1"]
    assert [m["code"] for m in a["expired"]] == ["EXP-1"]
    assert [m["code"] for m in a["expiring_soon"]] == ["SOON-1"]
    assert inv.get_alerts(db, soon_days=5)["expiring_soon"] == []  # configurable window
    assert a["low_stock"][0]["suggested_order"] == 35  # 2*20 - 5


def test_search_and_filters(client, db):
    seed(db)
    page = client.get("/medicines?q=para").get_data(as_text=True)
    assert "Paracetamol 500 mg tablet" in page and "Ibuprofen" not in page
    page = client.get("/medicines?q=ns-500").get_data(as_text=True)
    assert "Normal saline" in page and "Paracetamol 500 mg tablet" not in page
    page = client.get("/medicines?category=IV+Fluid").get_data(as_text=True)
    assert "Normal saline" in page and "Paracetamol 500 mg tablet" not in page
    page = client.get("/medicines?location=Cold+Storage&stock=low").get_data(as_text=True)
    assert "Insulin" in page and "Tetanus" not in page
    page = client.get("/medicines?stock=out").get_data(as_text=True)
    assert "Ringer lactate" in page and "Normal saline" not in page
    page = client.get("/medicines?expiry=expired").get_data(as_text=True)
    assert "Cetirizine" in page and "Paracetamol 500 mg tablet" not in page
    page = client.get("/medicines?expiry=soon&days=7").get_data(as_text=True)
    assert "Ondansetron" in page and "Azithromycin" not in page
    # regex special characters are treated as plain text, not as a pattern
    assert client.get("/medicines?q=(.*").status_code == 200


def test_user_text_is_html_escaped(client, db, valid_form):
    add(client, dict(valid_form, name="<script>alert(1)</script>"))
    page = client.get("/medicines").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


@pytest.mark.parametrize("kind", ["summary", "low-stock", "expiry", "movements"])
def test_reports_and_csv(client, db, kind):
    seed(db)
    assert client.get(f"/reports?kind={kind}").status_code == 200
    resp = client.get(f"/reports/{kind}.csv")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert len(resp.get_data(as_text=True).strip().splitlines()) > 1


def test_csv_neutralises_formulas(client, db, valid_form):
    add(client, dict(valid_form, name="=HYPERLINK(1)"))
    assert "'=HYPERLINK(1)" in client.get("/reports/summary.csv").get_data(as_text=True)


def test_dashboard_and_pages_render(client, db):
    seed(db)
    for url in ["/", "/medicines", "/stock", "/alerts?days=60", "/reports", "/health"]:
        assert client.get(url).status_code == 200, url
    page = client.get("/").get_data(as_text=True)
    assert "Total medicines" in page and str(len(DEMO)) in page


# ---------------------------------------------------------------- seeding and safety

def test_seed_is_repeatable_and_history_consistent(db):
    seed(db)
    first = (db.medicines.count_documents({}), db.stock_transactions.count_documents({}))
    seed(db)
    assert (db.medicines.count_documents({}), db.stock_transactions.count_documents({})) == first
    assert first[0] == len(DEMO)
    for med in db.medicines.find():
        txs = list(db.stock_transactions.find({"medicine_id": med["_id"]}).sort("created_at", 1))
        assert txs[-1]["balance_after"] == med["quantity"]
        assert all(t["balance_before"] >= 0 and t["balance_after"] >= 0 for t in txs)


def test_code_index_is_unique(db):
    seed(db)
    info = db.medicines.index_information()
    assert info["uniq_code"]["unique"] is True


def test_csrf_blocks_forged_post(mongo, valid_form):
    app = create_app({"TESTING": True, "MONGO_CLIENT": mongo, "MONGODB_DB": "medikiosk_test"})
    c = app.test_client()
    assert c.post("/medicines/new", data=valid_form).status_code == 400
    c.get("/")  # page render puts a token in the session
    with c.session_transaction() as s:
        token = s["csrf_token"]
    resp = c.post("/medicines/new", data=dict(valid_form, csrf_token=token))
    assert resp.status_code == 302


def test_missing_uri_shows_setup_page():
    app = create_app({"TESTING": True, "MONGODB_URI": "", "MONGO_CLIENT": None})
    resp = app.test_client().get("/")
    assert resp.status_code == 503 and b"MONGODB_URI is not set" in resp.data
