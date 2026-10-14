# MediKiosk – Smart Hospital Medicine Inventory Management System

A student PBL prototype: a Flask + MongoDB web dashboard for tracking hospital/pharmacy medicine stock,
stock movements, low-stock and expiry alerts, and reports.

> **Not for clinical use.** MediKiosk is an academic prototype. Its alerts are simple rules
> (quantity ≤ reorder level, expiry date within N days). It stores no patient data and has not
> been validated for real hospital deployment.

## Features

| Area | What it does |
|---|---|
| Dashboard | Total medicines, total units, low-stock / expired / expiring-soon counts, 14-day stock-movement chart, units by location, items needing attention, recent movements |
| Medicines | Add, view, edit, delete (type-the-code confirmation), search by name/code, filter by category, location, stock status, expiry status, sort |
| Stock In / Out | Record received and issued stock with reason and note; issuing more than available is rejected atomically, so stock never goes negative; every movement is logged with balance before/after |
| Alerts | Expired, low/out-of-stock (with a simple suggested order quantity), expiring within a configurable number of days; urgent items highlighted |
| Reports | Stock summary, low-stock, expiry, stock-movement reports, each exportable to CSV |
| Safety | Server-side validation, Jinja auto-escaping, CSRF token on every form, escaped regex search, unique index on medicine code, secrets only in `.env` |

## Project structure

```
MediKiosk/
  app.py              Flask routes (thin; calls inventory.py)
  inventory.py        Business logic: validation, CRUD, stock movements, alerts, reports
  db.py               MongoClient + index creation
  config.py           Settings from environment variables / .env
  seed_data.py        Repeatable demo data loader (19 medicines + 14-day history)
  requirements.txt
  .env.example        Copy to .env and fill in MONGODB_URI
  templates/          Jinja2 + Bootstrap 5 pages
  static/css/         App stylesheet
  static/vendor/      Bootstrap, Bootstrap Icons, Chart.js (bundled so the demo works without a CDN)
  tests/              pytest suite (mongomock) + optional live-MongoDB test
  docs/               Research summary, report, DB design, viva Q&A, presentation outline, .pptx
```

## 1. Install

Requires Python 3.10 or newer.

```bash
cd MediKiosk
python -m venv venv
# Windows:        venv\Scripts\activate
# macOS / Linux:  source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure MongoDB Atlas (recommended)

1. Sign in at <https://cloud.mongodb.com> and create a free **M0** cluster.
2. **Database Access** → *Add New Database User* → username + password (role: *Read and write to any database*).
3. **Network Access** → *Add IP Address* → *Add Current IP Address*. (For a demo on an unknown network you can
   temporarily allow `0.0.0.0/0`; remove it afterwards.)
4. **Database** → *Connect* → *Drivers* → Python → copy the connection string.
5. Copy the env template and paste the string, replacing `<password>`:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

```
MONGODB_URI=mongodb+srv://myuser:MyPassword@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=medikiosk
SECRET_KEY=<output of: python -c "import secrets; print(secrets.token_hex(32))">
```

If the password contains `@ : / ? # [ ]` or `%`, URL-encode it (e.g. `@` → `%40`).

**Local MongoDB instead:** install MongoDB Community Server, start it, and set
`MONGODB_URI=mongodb://localhost:27017`.

`.env` is in `.gitignore`; never commit it.

## 3. Load demo data

```bash
python seed_data.py            # insert / refresh 19 demo medicines (safe to re-run, no duplicates)
python seed_data.py --reset    # wipe ALL medicines + history first (asks for confirmation)
```

The demo set includes normal stock, low stock (AMX-250, ASP-75, CEF-1G, INS-R40), out of stock (RL-500),
expired (CTZ-10) and expiring-soon items (ONDA-4, AZT-500, INS-R40, SAL-INH). Expiry dates are relative to
the day you run the script, so the alerts always look right on demo day. Suppliers are fictitious.

## 4. Run

```bash
python app.py
```

Open <http://127.0.0.1:5000>. Check the database connection at <http://127.0.0.1:5000/health>
(should return `{"status": "ok", ...}`). If MongoDB is unreachable, the app shows a setup-help page instead of crashing.

## 5. Run tests

```bash
pytest -q
```

The main suite uses **mongomock** (an in-memory PyMongo stand-in) so it runs without a database server.
To run the extra test against your real MongoDB (it uses and then drops a separate `medikiosk_live_test` database):

```bash
# macOS / Linux
MONGODB_TEST_URI="mongodb+srv://..." pytest tests/test_live_mongodb.py -v
# Windows PowerShell
$env:MONGODB_TEST_URI="mongodb+srv://..."; pytest tests/test_live_mongodb.py -v
```

## Demo walkthrough (about 3 minutes)

1. **Dashboard** – point out the five cards, the movement chart, and *Needs attention*.
2. **Medicines → Add medicine** – add `DEMO-1 / Dolo test 650 mg`, qty 50, reorder level 40, expiry next year.
   Try the same code again to show the duplicate-code error.
3. **Medicine page → Receive stock** 30 units → balance 80, logged in history.
4. **Issue stock** 45 units → balance 35 → yellow **Low-stock alert** appears.
5. Try issuing 100 → rejected: "only 35 in stock".
6. **Alerts** – change “expiring soon” from 30 to 7 days and show the list shrink.
7. **Reports → Expiry report → Export CSV**.
8. **Medicines** – search `para`, filter *Cold Storage* + *Low*. Optionally delete `DEMO-1` (type the code to confirm).

## Troubleshooting

| Symptom | Fix |
|---|---|
| “MONGODB_URI is not set” page | Create `.env` from `.env.example` and restart the app |
| `ServerSelectionTimeoutError` | Atlas *Network Access* doesn't include your IP, or wrong password |
| `bad auth : authentication failed` | Re-check username/password; URL-encode special characters |
| `ConfigurationError: ... dnspython` | Run `pip install "pymongo[srv]"` (already in requirements) |
| College Wi-Fi blocks port 27017 | Use a phone hotspot, or run local MongoDB |
