# -*- coding: utf-8 -*-
"""Учёт счетов и КП по проектам — простое веб-приложение для компании.

Запуск:  python app.py  (или через gunicorn / docker, см. README.md)
Данные хранятся в SQLite (data/invoices.db), файлы счетов — в data/uploads/.
"""
import csv
import io
import os
import sqlite3
import uuid
from datetime import date

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

import finance
import notify
from db import DB_PATH, SECRET_FILE, UPLOAD_DIR, get_db, now

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # пусто = без пароля

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif",
               ".xls", ".xlsx", ".doc", ".docx"}
MAX_UPLOAD_MB = 25

DOC_TYPES = {"invoice": "Счёт", "quote": "КП"}
STATUSES = {"new": "Входящий", "to_pay": "В оплату",
            "paid": "Оплачен", "received": "Забрали"}
# Статусы, при которых счёт ещё не оплачен (попадает в платёжный календарь).
UNPAID_STATUSES = ("new", "to_pay")
VAT_RATES = ["22", "20", "10", "7", "5", "0", "none"]
VAT_LABELS = {"22": "НДС 22%", "20": "НДС 20%", "10": "НДС 10%",
              "7": "НДС 7%", "5": "НДС 5%", "0": "НДС 0%", "none": "Без НДС"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.register_blueprint(finance.bp)

if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(uuid.uuid4().hex)
app.secret_key = open(SECRET_FILE).read().strip()


# ---------------------------------------------------------------- база данных

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    contacts TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    doc_type TEXT NOT NULL DEFAULT 'invoice',      -- invoice | quote
    number TEXT DEFAULT '',
    supplier_name TEXT DEFAULT '',
    supplier_contacts TEXT DEFAULT '',
    pickup_info TEXT DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    vat_rate TEXT NOT NULL DEFAULT '20',           -- 22|20|10|7|5|0|none
    status TEXT NOT NULL DEFAULT 'new',            -- new | to_pay | paid | received
    comment TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    received_at TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
"""


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


with sqlite3.connect(DB_PATH) as _conn:
    _conn.executescript(SCHEMA)
    finance.init_db(_conn)
    # Один раз переносим уже внесённых поставщиков в справочник,
    # чтобы они появились в выпадающем списке с их контактами.
    if _conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
        _conn.execute(
            "INSERT OR IGNORE INTO suppliers (name, contacts, created_at) "
            "SELECT i.supplier_name, "
            "  (SELECT i2.supplier_contacts FROM invoices i2 "
            "   WHERE i2.supplier_name = i.supplier_name "
            "   ORDER BY i2.id DESC LIMIT 1), "
            "  MIN(i.created_at) "
            "FROM invoices i WHERE i.supplier_name <> '' "
            "GROUP BY i.supplier_name")


def vat_amount(amount, vat_rate):
    """НДС, выделенный из суммы (сумма считается с НДС внутри)."""
    try:
        rate = int(vat_rate)
    except (TypeError, ValueError):
        return 0.0
    if rate <= 0:
        return 0.0
    return round(amount * rate / (100 + rate), 2)


app.jinja_env.globals.update(
    DOC_TYPES=DOC_TYPES, STATUSES=STATUSES,
    VAT_RATES=VAT_RATES, VAT_LABELS=VAT_LABELS, vat_amount=vat_amount,
    BG_CATEGORIES=finance.BG_CATEGORIES, INCOME_KINDS=finance.INCOME_KINDS,
    INCOME_STATUSES=finance.INCOME_STATUSES,
    REC_CATEGORIES=finance.REC_CATEGORIES)


@app.template_filter("money")
def money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "0,00"


# ---------------------------------------------------------------------- вход

@app.before_request
def require_login():
    if not APP_PASSWORD:
        return
    if request.endpoint in ("login", "static", "manifest"):
        return
    if not session.get("authed"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Неверный пароль"
    return render_template("login.html", error=error)


# ------------------------------------------------------------ общая таблица

def apply_filters(where, params):
    project_id = request.args.get("project", "")
    doc_type = request.args.get("type", "")
    status = request.args.get("status", "")
    group_id = request.args.get("group", "")
    q = request.args.get("q", "").strip()
    if project_id.isdigit():
        where.append("i.project_id = ?")
        params.append(int(project_id))
    if doc_type in DOC_TYPES:
        where.append("i.doc_type = ?")
        params.append(doc_type)
    if status in STATUSES:
        where.append("i.status = ?")
        params.append(status)
    if group_id.isdigit():
        where.append("i.budget_group_id = ?")
        params.append(int(group_id))
    elif group_id == "none":
        where.append("i.budget_group_id IS NULL")
    if q:
        # Ищем по каждому слову отдельно: без учёта регистра (в т.ч. русские
        # буквы), по части слова и в любом месте строки. Найдётся, только
        # если все слова запроса встретились хоть в одном из полей.
        fields = ("i.supplier_name", "i.number", "i.comment", "i.pickup_info",
                  "p.name", "i.supplier_contacts")
        for word in q.split():
            where.append("(" + " OR ".join(
                f"ulower({f}) LIKE ulower(?)" for f in fields) + ")")
            params.extend([f"%{word}%"] * len(fields))


INVOICE_SELECT = """
SELECT i.*, p.name AS project_name, bg.name AS group_name,
       (SELECT COUNT(*) FROM attachments a WHERE a.invoice_id = i.id) AS files
FROM invoices i JOIN projects p ON p.id = i.project_id
LEFT JOIN budget_groups bg ON bg.id = i.budget_group_id
"""


def group_invoices(invoices):
    """Группирует счета по проектам, считает итоги по каждому."""
    buckets = {}
    for r in invoices:
        key = r["project_id"]
        if key not in buckets:
            buckets[key] = {"id": key, "name": r["project_name"],
                            "invoices": [], "total": 0.0}
        buckets[key]["invoices"].append(r)
        buckets[key]["total"] += r["amount"]
    sections = list(buckets.values())
    sections.sort(key=lambda s: s["name"].lower())
    return sections


@app.route("/")
def index():
    db = get_db()
    where, params = [], []
    apply_filters(where, params)
    sql = INVOICE_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.id DESC"
    invoices = db.execute(sql, params).fetchall()
    projects = db.execute("SELECT * FROM projects ORDER BY name").fetchall()
    budget_groups = db.execute(
        "SELECT bg.id, bg.name, p.name AS project_name FROM budget_groups bg "
        "JOIN projects p ON p.id = bg.project_id "
        "ORDER BY p.name, bg.name").fetchall()
    total = sum(r["amount"] for r in invoices)
    total_vat = sum(vat_amount(r["amount"], r["vat_rate"]) for r in invoices)
    view = "projects" if request.args.get("view") == "projects" else "list"
    sections = group_invoices(invoices) if view == "projects" else None
    # аргументы фильтров без view — для переключателя вида и ссылок
    base_args = {k: request.args.get(k) for k in
                 ("project", "type", "status", "group", "q")
                 if request.args.get(k)}
    return render_template("index.html", invoices=invoices, projects=projects,
                           budget_groups=budget_groups, total=total,
                           total_vat=total_vat, view=view, sections=sections,
                           base_args=base_args)


@app.route("/export.csv")
def export_csv():
    db = get_db()
    where, params = [], []
    apply_filters(where, params)
    sql = INVOICE_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.id DESC"
    rows = db.execute(sql, params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Тип", "Номер", "Проект", "Поставщик", "Сумма",
                "НДС", "Сумма НДС", "Статус", "Оплачен", "Забрали",
                "Контакты", "Откуда забирать", "Комментарий"])
    for r in rows:
        w.writerow([r["created_at"], DOC_TYPES[r["doc_type"]], r["number"],
                    r["project_name"], r["supplier_name"],
                    f'{r["amount"]:.2f}'.replace(".", ","),
                    VAT_LABELS[r["vat_rate"]],
                    f'{vat_amount(r["amount"], r["vat_rate"]):.2f}'.replace(".", ","),
                    STATUSES[r["status"]], r["paid_at"] or "",
                    r["received_at"] or "", r["supplier_contacts"],
                    r["pickup_info"], r["comment"]])
    data = buf.getvalue().encode("utf-8-sig")
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=invoices.csv"})


# -------------------------------------------------------- создание/изменение

def get_or_create_project(db):
    new_name = request.form.get("new_project", "").strip()
    if new_name:
        row = db.execute("SELECT id FROM projects WHERE name = ?",
                         (new_name,)).fetchone()
        if row:
            return row["id"]
        cur = db.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)",
                         (new_name, now()))
        return cur.lastrowid
    project_id = request.form.get("project_id", "")
    if project_id.isdigit():
        return int(project_id)
    return None


def get_or_create_supplier(db):
    """Возвращает (имя, контакты) поставщика и запоминает его в справочнике.

    Работает как с проектами: можно выбрать из списка или вписать нового.
    """
    contacts = request.form.get("supplier_contacts", "").strip()
    name = request.form.get("new_supplier", "").strip()
    if not name:
        supplier_id = request.form.get("supplier_id", "")
        if supplier_id.isdigit():
            row = db.execute("SELECT name FROM suppliers WHERE id = ?",
                             (int(supplier_id),)).fetchone()
            if row:
                name = row["name"]
    if not name:
        return "", contacts
    existing = db.execute("SELECT id, contacts FROM suppliers WHERE name = ?",
                          (name,)).fetchone()
    if existing:
        if contacts and contacts != existing["contacts"]:
            db.execute("UPDATE suppliers SET contacts = ? WHERE id = ?",
                       (contacts, existing["id"]))
        elif not contacts:
            contacts = existing["contacts"]
    else:
        db.execute("INSERT INTO suppliers (name, contacts, created_at) "
                   "VALUES (?, ?, ?)", (name, contacts, now()))
    return name, contacts


def parse_amount():
    raw = request.form.get("amount", "0").replace(" ", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return 0.0


def save_attachments(db, invoice_id):
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            flash(f"Файл «{f.filename}» не сохранён: недопустимый тип")
            continue
        stored = uuid.uuid4().hex + ext
        f.save(os.path.join(UPLOAD_DIR, stored))
        db.execute("INSERT INTO attachments (invoice_id, stored_name, "
                   "original_name, uploaded_at) VALUES (?, ?, ?, ?)",
                   (invoice_id, stored, f.filename, now()))


def form_fields():
    doc_type = request.form.get("doc_type", "invoice")
    vat_rate = request.form.get("vat_rate", "20")
    group_id = request.form.get("budget_group_id", "")
    return {
        "doc_type": doc_type if doc_type in DOC_TYPES else "invoice",
        "number": request.form.get("number", "").strip(),
        "pickup_info": request.form.get("pickup_info", "").strip(),
        "amount": parse_amount(),
        "vat_rate": vat_rate if vat_rate in VAT_RATES else "20",
        "comment": request.form.get("comment", "").strip(),
        "due_date": request.form.get("due_date", "").strip() or None,
        "is_critical": 1 if request.form.get("is_critical") else 0,
        "budget_group_id": int(group_id) if group_id.isdigit() else None,
        "in_plan": 1 if request.form.get("in_plan") else 0,
    }


def budget_groups_for_form(db):
    return db.execute(
        "SELECT bg.id, bg.name, bg.project_id, p.name AS project_name "
        "FROM budget_groups bg JOIN projects p ON p.id = bg.project_id "
        "ORDER BY p.name, bg.name").fetchall()


@app.route("/invoices/new", methods=["GET", "POST"])
def invoice_new():
    db = get_db()
    projects = db.execute("SELECT * FROM projects ORDER BY name").fetchall()
    suppliers = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    groups = budget_groups_for_form(db)
    if request.method == "POST":
        project_id = get_or_create_project(db)
        if project_id is None:
            flash("Укажите проект")
            return render_template("form.html", invoice=None, projects=projects,
                                   suppliers=suppliers, groups=groups)
        f = form_fields()
        f["supplier_name"], f["supplier_contacts"] = get_or_create_supplier(db)
        cur = db.execute(
            "INSERT INTO invoices (project_id, doc_type, number, supplier_name,"
            " supplier_contacts, pickup_info, amount, vat_rate, comment,"
            " due_date, is_critical, budget_group_id, in_plan, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, f["doc_type"], f["number"], f["supplier_name"],
             f["supplier_contacts"], f["pickup_info"], f["amount"],
             f["vat_rate"], f["comment"], f["due_date"], f["is_critical"],
             f["budget_group_id"], f["in_plan"], now()))
        save_attachments(db, cur.lastrowid)
        db.commit()
        flash("Сохранено")
        return redirect(url_for("invoice_detail", invoice_id=cur.lastrowid))
    return render_template("form.html", invoice=None, projects=projects,
                           suppliers=suppliers, groups=groups,
                           today=date.today().isoformat())


def load_invoice(db, invoice_id):
    row = db.execute(INVOICE_SELECT + " WHERE i.id = ?",
                     (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/invoices/<int:invoice_id>")
def invoice_detail(invoice_id):
    db = get_db()
    invoice = load_invoice(db, invoice_id)
    attachments = db.execute(
        "SELECT * FROM attachments WHERE invoice_id = ? ORDER BY id",
        (invoice_id,)).fetchall()
    return render_template("detail.html", invoice=invoice,
                           attachments=attachments)


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def invoice_edit(invoice_id):
    db = get_db()
    invoice = load_invoice(db, invoice_id)
    projects = db.execute("SELECT * FROM projects ORDER BY name").fetchall()
    suppliers = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    groups = budget_groups_for_form(db)
    if request.method == "POST":
        project_id = get_or_create_project(db) or invoice["project_id"]
        f = form_fields()
        f["supplier_name"], f["supplier_contacts"] = get_or_create_supplier(db)
        db.execute(
            "UPDATE invoices SET project_id=?, doc_type=?, number=?,"
            " supplier_name=?, supplier_contacts=?, pickup_info=?, amount=?,"
            " vat_rate=?, comment=?, due_date=?, is_critical=?,"
            " budget_group_id=?, in_plan=? WHERE id=?",
            (project_id, f["doc_type"], f["number"], f["supplier_name"],
             f["supplier_contacts"], f["pickup_info"], f["amount"],
             f["vat_rate"], f["comment"], f["due_date"], f["is_critical"],
             f["budget_group_id"], f["in_plan"], invoice_id))
        save_attachments(db, invoice_id)
        db.commit()
        flash("Изменения сохранены")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    return render_template("form.html", invoice=invoice, projects=projects,
                           suppliers=suppliers, groups=groups,
                           today=date.today().isoformat())


@app.route("/invoices/<int:invoice_id>/status", methods=["POST"])
def invoice_status(invoice_id):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400)
    db = get_db()
    invoice = load_invoice(db, invoice_id)
    # Даты проставляются один раз и снимаются при возврате назад по цепочке
    # Входящий → В оплату → Оплачен → Забрали.
    paid_at, received_at = invoice["paid_at"], invoice["received_at"]
    if status in UNPAID_STATUSES:          # ещё не оплачен
        paid_at = received_at = None
    elif status == "paid":
        paid_at = paid_at or now()
        received_at = None
    elif status == "received":
        paid_at = paid_at or now()
        received_at = received_at or now()
    db.execute("UPDATE invoices SET status=?, paid_at=?, received_at=? "
               "WHERE id=?", (status, paid_at, received_at, invoice_id))
    db.commit()
    if status != invoice["status"]:        # уведомляем только о реальной смене
        notify.status_changed(
            invoice, status, STATUSES[status], VAT_LABELS[invoice["vat_rate"]],
            notify.invoice_link(invoice_id, request.url_root))
    return redirect(request.form.get("back") or
                    url_for("invoice_detail", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
def invoice_delete(invoice_id):
    db = get_db()
    load_invoice(db, invoice_id)
    for a in db.execute("SELECT stored_name FROM attachments WHERE invoice_id=?",
                        (invoice_id,)):
        try:
            os.remove(os.path.join(UPLOAD_DIR, a["stored_name"]))
        except OSError:
            pass
    db.execute("DELETE FROM attachments WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    db.commit()
    flash("Запись удалена")
    return redirect(url_for("index"))


# --------------------------------------------------------------------- файлы

@app.route("/files/<int:attachment_id>")
def download_file(attachment_id):
    db = get_db()
    a = db.execute("SELECT * FROM attachments WHERE id=?",
                   (attachment_id,)).fetchone()
    if a is None:
        abort(404)
    path = os.path.join(UPLOAD_DIR, a["stored_name"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, download_name=a["original_name"])


@app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
def attachment_delete(attachment_id):
    db = get_db()
    a = db.execute("SELECT * FROM attachments WHERE id=?",
                   (attachment_id,)).fetchone()
    if a is None:
        abort(404)
    try:
        os.remove(os.path.join(UPLOAD_DIR, a["stored_name"]))
    except OSError:
        pass
    db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    db.commit()
    return redirect(url_for("invoice_detail", invoice_id=a["invoice_id"]))


@app.route("/manifest.json")
def manifest():
    return {
        "name": "Счета и КП",
        "short_name": "Счета",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f4f5f7",
        "theme_color": "#1d4ed8",
        "icons": [{"src": url_for("static", filename="icon.svg"),
                   "sizes": "any", "type": "image/svg+xml"}],
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)),
            debug=os.environ.get("DEBUG") == "1")
