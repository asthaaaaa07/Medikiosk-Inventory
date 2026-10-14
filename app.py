"""MediKiosk - Flask web application (routes only; business logic lives in inventory.py)."""
import csv
import io
import secrets
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   session, url_for)
from pymongo.errors import PyMongoError

import inventory as inv
from config import Config
from db import get_db


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(overrides or {})

    # ------------------------------------------------------------ security helpers
    @app.before_request
    def check_csrf():
        if request.method == "POST" and app.config["CSRF_ENABLED"]:
            sent = request.form.get("csrf_token", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
                app.logger.warning("CSRF failed: submitted_token_missing=%s, session_token_missing=%s", not bool(sent), not bool(session.get("csrf_token")))
                abort(400, "Form expired or invalid. Please reload the page and try again.")

    @app.context_processor
    def template_globals():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return {"csrf_token": session["csrf_token"], "soon_days_default": app.config["EXPIRY_SOON_DAYS"]}

    tz = ZoneInfo(app.config["DISPLAY_TZ"])

    @app.template_filter("localtime")
    def localtime(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d %b %Y, %H:%M") if dt else ""

    @app.template_filter("date")
    def fmt_date(dt):
        return dt.strftime("%d %b %Y") if dt else ""

    @app.template_filter("inr")
    def inr(value):
        return f"₹{value:,.2f}"

    @app.template_global()
    def status(med, days=None):
        return inv.medicine_status(med, days or app.config["EXPIRY_SOON_DAYS"])

    @app.errorhandler(PyMongoError)
    @app.errorhandler(RuntimeError)
    def db_error(err):
        app.logger.exception("Database problem")
        return render_template("db_error.html", error=str(err)), 503

    # ------------------------------------------------------------ helpers
    def soon_days():
        try:
            return max(1, min(int(request.args.get("days", app.config["EXPIRY_SOON_DAYS"])), 365))
        except ValueError:
            return app.config["EXPIRY_SOON_DAYS"]

    def medicine_or_404(medicine_id):
        oid = inv.to_object_id(medicine_id)
        med = inv.get_medicine(get_db(), oid) if oid else None
        if med is None:
            abort(404)
        return med

    def choices(db):
        return {
            "categories": inv.distinct_values(db, "category", inv.CATEGORIES),
            "locations": inv.distinct_values(db, "storage_location", inv.LOCATIONS),
        }

    # ------------------------------------------------------------ dashboard
    @app.route("/")
    def dashboard():
        days = soon_days()
        return render_template("dashboard.html", s=inv.dashboard_stats(get_db(), days), days=days)

    # ------------------------------------------------------------ medicines
    @app.route("/medicines")
    def medicines():
        db = get_db()
        days = soon_days()
        f = {k: request.args.get(k, "") for k in ("q", "category", "location", "stock", "expiry")}
        meds = inv.list_medicines(db, sort=request.args.get("sort", "name"), soon_days=days, **f)
        return render_template("medicines.html", meds=meds, f=f, days=days,
                               sort=request.args.get("sort", "name"), **choices(db))

    @app.route("/medicines/new", methods=["GET", "POST"])
    def medicine_new():
        db = get_db()
        if request.method == "POST":
            try:
                new_id = inv.create_medicine(db, request.form)
                flash("Medicine added successfully.", "success")
                return redirect(url_for("medicine_detail", medicine_id=str(new_id)))
            except inv.ValidationError as e:
                for msg in e.errors:
                    flash(msg, "danger")
        return render_template("medicine_form.html", med=request.form, editing=False, **choices(db))

    @app.route("/medicines/<medicine_id>")
    def medicine_detail(medicine_id):
        med = medicine_or_404(medicine_id)
        history = inv.list_transactions(get_db(), medicine_id=med["_id"], limit=50)
        return render_template("medicine_detail.html", med=med, history=history,
                               reasons_in=inv.REASONS_IN, reasons_out=inv.REASONS_OUT)

    @app.route("/medicines/<medicine_id>/edit", methods=["GET", "POST"])
    def medicine_edit(medicine_id):
        db = get_db()
        med = medicine_or_404(medicine_id)
        form_data = dict(med, expiry_date=med["expiry_date"].strftime("%Y-%m-%d"))
        if request.method == "POST":
            try:
                inv.update_medicine(db, med["_id"], request.form)
                flash("Medicine details updated.", "success")
                return redirect(url_for("medicine_detail", medicine_id=medicine_id))
            except inv.ValidationError as e:
                for msg in e.errors:
                    flash(msg, "danger")
                form_data.update(request.form.to_dict())
        return render_template("medicine_form.html", med=form_data, editing=True, **choices(db))

    @app.route("/medicines/<medicine_id>/delete", methods=["POST"])
    def medicine_delete(medicine_id):
        med = medicine_or_404(medicine_id)
        if request.form.get("confirm_code", "").strip().upper() != med["code"]:
            flash("Delete cancelled: the confirmation code did not match.", "warning")
            return redirect(url_for("medicine_detail", medicine_id=medicine_id))
        inv.delete_medicine(get_db(), med["_id"])
        flash(f"Medicine {med['code']} deleted. Its stock history is kept for audit.", "success")
        return redirect(url_for("medicines"))

    # ------------------------------------------------------------ stock operations
    @app.route("/stock", methods=["GET", "POST"])
    def stock():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action")
            oid = inv.to_object_id(request.form.get("medicine_id"))
            try:
                if oid is None or action not in ("receive", "issue"):
                    raise inv.ValidationError(["Please choose a medicine and an action."])
                op = inv.receive_stock if action == "receive" else inv.issue_stock
                med = op(db, oid, request.form)
                verb = "received" if action == "receive" else "issued"
                flash(f"{request.form['quantity']} units {verb} for {med['name']}. "
                      f"New balance: {med['quantity']}.", "success")
                if med["quantity"] <= med["reorder_level"]:
                    flash(f"Low-stock alert: {med['name']} is at or below its reorder level "
                          f"({med['reorder_level']}).", "warning")
            except inv.ValidationError as e:
                for msg in e.errors:
                    flash(msg, "danger")
            back = request.form.get("next", "")
            return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("stock"))
        tx_type = request.args.get("type", "")
        meds = list(db.medicines.find({}, {"name": 1, "code": 1, "quantity": 1}).sort("name", 1))
        return render_template("stock.html", meds=meds, tx=inv.list_transactions(db, tx_type=tx_type, limit=50),
                               tx_type=tx_type, selected=request.args.get("medicine_id", ""),
                               reasons_in=inv.REASONS_IN, reasons_out=inv.REASONS_OUT)

    # ------------------------------------------------------------ alerts
    @app.route("/alerts")
    def alerts():
        days = soon_days()
        return render_template("alerts.html", a=inv.get_alerts(get_db(), days), days=days)

    # ------------------------------------------------------------ reports
    REPORTS = {
        "summary": "Stock summary",
        "low-stock": "Low-stock report",
        "expiry": "Expiry report",
        "movements": "Stock movement report",
    }

    def report_rows(kind, days, period):
        """Return (headers, rows) for a report; shared by the HTML page and the CSV export."""
        db = get_db()
        if kind == "movements":
            txs = inv.list_transactions(db, tx_type=request.args.get("type", ""), days=period, limit=5000)
            return (["Date/time", "Code", "Medicine", "Type", "Quantity", "Before", "After", "Reason", "Note"],
                    [[localtime(t["created_at"]), t["medicine_code"], t["medicine_name"], t["type"],
                      t["quantity"], t["balance_before"], t["balance_after"], t["reason"], t.get("note", "")]
                     for t in txs])
        if kind == "low-stock":
            meds = inv.get_alerts(db, days)["low_stock"]
            return (["Code", "Medicine", "Location", "Quantity", "Reorder level", "Suggested order", "Supplier"],
                    [[m["code"], m["name"], m["storage_location"], m["quantity"], m["reorder_level"],
                      m["suggested_order"], m.get("supplier", "")] for m in meds])
        if kind == "expiry":
            meds = list(db.medicines.find(inv.build_filter(expiry="soon", soon_days=days))) + \
                list(db.medicines.find(inv.build_filter(expiry="expired")))
            meds.sort(key=lambda m: m["expiry_date"])
            return (["Code", "Medicine", "Batch", "Location", "Quantity", "Expiry date", "Days left", "Value at risk"],
                    [[m["code"], m["name"], m["batch_number"], m["storage_location"], m["quantity"],
                      fmt_date(m["expiry_date"]), inv.medicine_status(m, days)["days_to_expiry"],
                      round(m["quantity"] * m["unit_price"], 2)] for m in meds])
        meds = inv.list_medicines(db)
        return (["Code", "Medicine", "Category", "Batch", "Location", "Quantity", "Reorder level",
                 "Unit price", "Stock value", "Expiry date"],
                [[m["code"], m["name"], m["category"], m["batch_number"], m["storage_location"],
                  m["quantity"], m["reorder_level"], m["unit_price"], round(m["quantity"] * m["unit_price"], 2),
                  fmt_date(m["expiry_date"])] for m in meds])

    def period_arg():
        try:
            return max(0, min(int(request.args.get("period", 30)), 3650))
        except ValueError:
            return 30

    @app.route("/reports")
    def reports():
        kind = request.args.get("kind", "summary")
        if kind not in REPORTS:
            abort(404)
        days, period = soon_days(), period_arg()
        headers, rows = report_rows(kind, days, period)
        return render_template("reports.html", kind=kind, reports=REPORTS, headers=headers, rows=rows,
                               days=days, period=period, tx_type=request.args.get("type", ""))

    @app.route("/reports/<kind>.csv")
    def report_csv(kind):
        if kind not in REPORTS:
            abort(404)
        headers, rows = report_rows(kind, soon_days(), period_arg())
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for row in rows:
            # Prefix cells that start with = + - @ so spreadsheet apps don't run them as formulas.
            writer.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else c for c in row])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=medikiosk_{kind}.csv"})

    # ------------------------------------------------------------ health check
    @app.route("/health")
    def health():
        get_db().command("ping")
        return {"status": "ok", "database": app.config["MONGODB_DB"]}

    @app.errorhandler(404)
    def not_found(_):
        return render_template("message.html", title="Not found",
                               message="That page or medicine does not exist."), 404

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("message.html", title="Request rejected", message=err.description), 400

    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5000)
