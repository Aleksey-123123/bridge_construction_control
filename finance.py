# -*- coding: utf-8 -*-
"""Финансовый модуль: платёжный календарь и прогноз денежного потока.

Принципы (важно для понимания кода):
  * Приложение НЕ ведёт учёт движения денег — точка опоры это фактический
    остаток на счетах (cash_snapshots), который вводится вручную из банка.
    Прогноз = остаток + ожидаемые поступления - планируемые платежи.
  * Смета неизменна. У каждой бюджетной группы три значения:
    по смете (smeta_amount), текущая оценка (estimate_amount, NULL = по смете)
    и факт (сумма оплаченных счетов, привязанных к группе).
  * Все даты денег вычисляются: приход по КС = конец месяца подписания +
    задержка оплаты заказчиком; возврат гарантийного удержания = конец месяца
    (подписание + N месяцев) + та же задержка.
"""
import calendar as _cal
import csv
import io
import re
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)

import ai
from db import ensure_column, get_db, now

bp = Blueprint("finance", __name__, url_prefix="/finance")

HORIZON_WEEKS = 12

BG_CATEGORIES = {"materials": "Материалы", "works": "Работы",
                 "other": "Прочее", "penalty": "Штраф"}
INCOME_KINDS = {"advance": "Аванс", "ks": "КС", "extra": "Допработы"}
INCOME_STATUSES = {"plan": "План", "signed": "Подписан", "paid": "Оплачен"}
REC_CATEGORIES = {"salary": "Зарплата", "tax": "Налоги", "rent": "Аренда",
                  "office": "Офис", "loan": "Кредит", "other": "Прочее"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'materials',   -- materials | works | other
    smeta_amount REAL NOT NULL DEFAULT 0,         -- по смете (неизменно)
    estimate_amount REAL,                         -- текущая оценка, NULL = по смете
    plan_month TEXT DEFAULT '',                   -- YYYY-MM, плановый месяц расхода
    off_smeta INTEGER NOT NULL DEFAULT 0,         -- 1 = вне сметы
    comment TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS income_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL DEFAULT 'ks',              -- advance | ks | extra
    title TEXT DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,               -- сумма по документу (до вычетов)
    plan_month TEXT NOT NULL,                     -- YYYY-MM, месяц подписания
    status TEXT NOT NULL DEFAULT 'plan',          -- plan | signed | paid
    signed_date TEXT,                             -- YYYY-MM-DD
    paid_date TEXT,
    paid_amount REAL,
    comment TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',       -- salary|tax|rent|office|loan|other
    amount REAL NOT NULL DEFAULT 0,
    day_of_month INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    comment TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL,                      -- YYYY-MM-DD
    amount REAL NOT NULL,
    comment TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init_db(conn):
    """Создаёт финансовые таблицы и мягко расширяет существующие."""
    conn.executescript(SCHEMA)
    # проекты: финансовые параметры контракта
    ensure_column(conn, "projects", "customer", "TEXT DEFAULT ''")
    ensure_column(conn, "projects", "gp_percent", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "projects", "retention_percent", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "projects", "retention_months", "INTEGER NOT NULL DEFAULT 12")
    ensure_column(conn, "projects", "pay_delay_days", "INTEGER NOT NULL DEFAULT 30")
    ensure_column(conn, "projects", "contract_price", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "projects", "smeta_coefficient", "REAL NOT NULL DEFAULT 1")
    # счета: срок оплаты, критичность, привязка к бюджетной группе
    ensure_column(conn, "invoices", "due_date", "TEXT")
    ensure_column(conn, "invoices", "is_critical", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "invoices", "budget_group_id",
                  "INTEGER REFERENCES budget_groups(id)")
    # «в расчёт» — выбранное КП из нескольких на один материал/работу
    ensure_column(conn, "invoices", "in_plan", "INTEGER NOT NULL DEFAULT 0")


# ------------------------------------------------------------------ даты

def parse_ym(s):
    """'YYYY-MM' -> (год, месяц) или None."""
    try:
        y, m = str(s or "").split("-")[:2]
        y, m = int(y), int(m)
        if 1 <= m <= 12:
            return y, m
    except (ValueError, AttributeError):
        pass
    return None


def parse_date(s):
    try:
        return datetime.strptime(str(s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def month_end(y, m):
    return date(y, m, _cal.monthrange(y, m)[1])


def add_months(y, m, n):
    m0 = (y * 12 + m - 1) + n
    return m0 // 12, m0 % 12 + 1


def month_label(ym):
    names = ["янв", "фев", "мар", "апр", "май", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек"]
    p = parse_ym(ym)
    return f"{names[p[1] - 1]} {p[0]}" if p else "—"


def d_label(d):
    return d.strftime("%d.%m")


# ------------------------------------------------------- расчёт календаря

def latest_snapshot(db):
    return db.execute("SELECT * FROM cash_snapshots "
                      "ORDER BY snap_date DESC, id DESC LIMIT 1").fetchone()


def income_events(db, today, end):
    """Ожидаемые поступления: КС/авансы/допработы + возвраты удержаний.

    Чистый приход = сумма - % генподряда - % удержания (удержание только у КС).
    Удержанная часть возвращается через retention_months после подписания КС.
    """
    rows = db.execute(
        "SELECT ip.*, p.name AS project_name, p.gp_percent, p.retention_percent,"
        "       p.retention_months, p.pay_delay_days "
        "FROM income_plan ip JOIN projects p ON p.id = ip.project_id").fetchall()
    events = []
    for r in rows:
        gp = (r["gp_percent"] or 0) / 100.0
        ret = (r["retention_percent"] or 0) / 100.0 if r["kind"] == "ks" else 0.0
        delay = int(r["pay_delay_days"] or 0)
        ym = parse_ym(r["plan_month"])

        if r["status"] != "paid":
            if r["status"] == "signed" and parse_date(r["signed_date"]):
                base = parse_date(r["signed_date"])
            elif ym:
                base = month_end(*ym)
            else:
                base = None
            if base is not None:
                d = max(base + timedelta(days=delay), today)
                net = round(r["amount"] * (1 - gp - ret), 2)
                if net > 0 and d <= end:
                    title = INCOME_KINDS.get(r["kind"], r["kind"])
                    if r["title"]:
                        title += f" {r['title']}"
                    events.append({
                        "date": d, "amount": net, "flow": "in",
                        "label": f"{title} · {r['project_name']}",
                        "note": ("ожидается со дня на день"
                                 if base + timedelta(days=delay) < today else ""),
                        "url": url_for("finance.income_edit", income_id=r["id"]),
                    })

        # возврат гарантийного удержания — считается и для оплаченных КС
        if r["kind"] == "ks" and ret > 0 and ym:
            back = month_end(*add_months(ym[0], ym[1],
                                         int(r["retention_months"] or 12)))
            d = max(back + timedelta(days=delay), today)
            amt = round(r["amount"] * ret, 2)
            if amt > 0 and d <= end:
                events.append({
                    "date": d, "amount": amt, "flow": "in",
                    "label": f"Возврат удержания · {r['project_name']}",
                    "note": "",
                    "url": url_for("finance.income_edit", income_id=r["id"]),
                })
    return events


def invoice_events(db, today, end):
    """Неоплаченные счета. Возвращает (события, счета без срока оплаты)."""
    rows = db.execute(
        "SELECT i.*, p.name AS project_name FROM invoices i "
        "JOIN projects p ON p.id = i.project_id "
        "WHERE i.doc_type = 'invoice' "
        "AND i.status IN ('new', 'to_pay')").fetchall()
    events, undated = [], []
    for r in rows:
        due = parse_date(r["due_date"])
        if due is None:
            undated.append(r)
            continue
        d = max(due, today)
        if r["amount"] > 0 and d <= end:
            label = f"Счёт · {r['supplier_name'] or 'без поставщика'} · {r['project_name']}"
            events.append({
                "date": d, "amount": r["amount"], "flow": "out",
                "label": label,
                "note": ("просрочен" if due < today else "") +
                        (" · критичный" if r["is_critical"] else ""),
                "url": url_for("invoice_detail", invoice_id=r["id"]),
            })
    return events, undated


def recurring_events(db, today, end):
    rows = db.execute("SELECT * FROM recurring_expenses WHERE active = 1").fetchall()
    events = []
    for r in rows:
        y, m = today.year, today.month
        while (y, m) <= (end.year, end.month):
            day = min(int(r["day_of_month"] or 1), _cal.monthrange(y, m)[1])
            d = date(y, m, day)
            if today <= d <= end and r["amount"] > 0:
                events.append({
                    "date": d, "amount": r["amount"], "flow": "out",
                    "label": f"{REC_CATEGORIES.get(r['category'], '')}: {r['name']}",
                    "note": "", "url": url_for("finance.recurring"),
                })
            y, m = add_months(y, m, 1)
    return events


def build_calendar(db):
    """Собирает прогноз остатка на HORIZON_WEEKS недель вперёд."""
    today = date.today()
    end = today + timedelta(days=HORIZON_WEEKS * 7 - 1)

    snap = latest_snapshot(db)
    start_balance = snap["amount"] if snap else 0.0

    events = income_events(db, today, end)
    inv_events, undated = invoice_events(db, today, end)
    events += inv_events
    events += recurring_events(db, today, end)
    # внутри дня сначала расходы — консервативная оценка минимума
    events.sort(key=lambda e: (e["date"], 0 if e["flow"] == "out" else 1))

    running = start_balance
    min_balance, min_date = start_balance, today
    weeks = []
    i = 0
    for w in range(HORIZON_WEEKS):
        w_start = today + timedelta(days=w * 7)
        w_end = w_start + timedelta(days=6)
        w_events, w_in, w_out = [], 0.0, 0.0
        w_min = running
        while i < len(events) and events[i]["date"] <= w_end:
            e = events[i]
            if e["flow"] == "in":
                running += e["amount"]
                w_in += e["amount"]
            else:
                running -= e["amount"]
                w_out += e["amount"]
            e["balance_after"] = running
            if running < w_min:
                w_min = running
            if running < min_balance:
                min_balance, min_date = running, e["date"]
            w_events.append(e)
            i += 1
        weeks.append({"start": w_start, "end": w_end, "events": w_events,
                      "income": w_in, "expense": w_out,
                      "end_balance": running, "min_balance": w_min})

    total_in = sum(e["amount"] for e in events if e["flow"] == "in")
    total_out = sum(e["amount"] for e in events if e["flow"] == "out")
    return {
        "today": today, "end": end, "snapshot": snap,
        "start_balance": start_balance, "weeks": weeks,
        "min_balance": min_balance, "min_date": min_date,
        "total_in": total_in, "total_out": total_out,
        "undated_invoices": undated,
        "undated_total": sum(r["amount"] for r in undated),
        "snapshot_stale": bool(
            snap and (today - (parse_date(snap["snap_date"]) or today)).days > 3),
    }


# ------------------------------------------------------------- страницы

def parse_money(field, default=0.0):
    raw = request.form.get(field, "").replace(" ", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return default


def parse_int(field, default=0):
    try:
        return int(request.form.get(field, ""))
    except ValueError:
        return default


@bp.route("/")
def calendar():
    db = get_db()
    cal = build_calendar(db)
    return render_template("finance_calendar.html", cal=cal, tab="calendar")


@bp.route("/balance", methods=["POST"])
def balance_update():
    amount = parse_money("amount")
    snap_date = request.form.get("snap_date") or date.today().isoformat()
    db = get_db()
    db.execute("INSERT INTO cash_snapshots (snap_date, amount, comment, created_at)"
               " VALUES (?, ?, ?, ?)",
               (snap_date, amount, request.form.get("comment", "").strip(), now()))
    db.commit()
    flash("Остаток обновлён")
    return redirect(url_for("finance.calendar"))


# --- поступления -------------------------------------------------------

@bp.route("/incomes")
def incomes():
    db = get_db()
    rows = db.execute(
        "SELECT ip.*, p.name AS project_name, p.gp_percent, p.retention_percent,"
        "       p.retention_months, p.pay_delay_days "
        "FROM income_plan ip JOIN projects p ON p.id = ip.project_id "
        "ORDER BY ip.status = 'paid', ip.plan_month, ip.id").fetchall()
    items = []
    for r in rows:
        gp = (r["gp_percent"] or 0) / 100.0
        ret = (r["retention_percent"] or 0) / 100.0 if r["kind"] == "ks" else 0.0
        expected = None
        if r["status"] == "signed" and parse_date(r["signed_date"]):
            expected = parse_date(r["signed_date"]) + \
                timedelta(days=int(r["pay_delay_days"] or 0))
        elif parse_ym(r["plan_month"]):
            expected = month_end(*parse_ym(r["plan_month"])) + \
                timedelta(days=int(r["pay_delay_days"] or 0))
        items.append({"row": r, "net": round(r["amount"] * (1 - gp - ret), 2),
                      "retention": round(r["amount"] * ret, 2),
                      "expected": expected})
    waiting = sum(x["net"] for x in items if x["row"]["status"] != "paid")
    return render_template("finance_incomes.html", items=items, tab="incomes",
                           waiting=waiting, month_label=month_label)


def income_form_data(db):
    projects = db.execute("SELECT * FROM projects ORDER BY name").fetchall()
    return projects


@bp.route("/incomes/new", methods=["GET", "POST"])
def income_new():
    db = get_db()
    if request.method == "POST":
        project_id = parse_int("project_id")
        if not project_id or parse_ym(request.form.get("plan_month")) is None:
            flash("Укажите проект и месяц")
        else:
            kind = request.form.get("kind", "ks")
            db.execute(
                "INSERT INTO income_plan (project_id, kind, title, amount,"
                " plan_month, comment, created_at) VALUES (?,?,?,?,?,?,?)",
                (project_id, kind if kind in INCOME_KINDS else "ks",
                 request.form.get("title", "").strip(), parse_money("amount"),
                 request.form.get("plan_month"),
                 request.form.get("comment", "").strip(), now()))
            db.commit()
            flash("Поступление добавлено")
            return redirect(url_for("finance.incomes"))
    return render_template("income_form.html", income=None,
                           projects=income_form_data(db), tab="incomes")


def load_income(db, income_id):
    row = db.execute("SELECT * FROM income_plan WHERE id = ?",
                     (income_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/incomes/<int:income_id>/edit", methods=["GET", "POST"])
def income_edit(income_id):
    db = get_db()
    income = load_income(db, income_id)
    if request.method == "POST":
        kind = request.form.get("kind", "ks")
        db.execute(
            "UPDATE income_plan SET project_id=?, kind=?, title=?, amount=?,"
            " plan_month=?, signed_date=?, paid_date=?, paid_amount=?, comment=?"
            " WHERE id=?",
            (parse_int("project_id", income["project_id"]),
             kind if kind in INCOME_KINDS else "ks",
             request.form.get("title", "").strip(), parse_money("amount"),
             request.form.get("plan_month") or income["plan_month"],
             request.form.get("signed_date") or None,
             request.form.get("paid_date") or None,
             parse_money("paid_amount") or None,
             request.form.get("comment", "").strip(), income_id))
        db.commit()
        flash("Изменения сохранены")
        return redirect(url_for("finance.incomes"))
    return render_template("income_form.html", income=income,
                           projects=income_form_data(db), tab="incomes")


@bp.route("/incomes/<int:income_id>/status", methods=["POST"])
def income_status(income_id):
    status = request.form.get("status", "")
    if status not in INCOME_STATUSES:
        abort(400)
    db = get_db()
    income = load_income(db, income_id)
    today = date.today().isoformat()
    if status == "plan":
        db.execute("UPDATE income_plan SET status='plan', signed_date=NULL,"
                   " paid_date=NULL, paid_amount=NULL WHERE id=?", (income_id,))
    elif status == "signed":
        db.execute("UPDATE income_plan SET status='signed',"
                   " signed_date=COALESCE(signed_date, ?) WHERE id=?",
                   (today, income_id))
    else:  # paid
        db.execute("UPDATE income_plan SET status='paid',"
                   " signed_date=COALESCE(signed_date, ?),"
                   " paid_date=COALESCE(paid_date, ?),"
                   " paid_amount=COALESCE(paid_amount, amount) WHERE id=?",
                   (today, today, income_id))
    db.commit()
    return redirect(request.form.get("back") or url_for("finance.incomes"))


@bp.route("/incomes/<int:income_id>/delete", methods=["POST"])
def income_delete(income_id):
    db = get_db()
    load_income(db, income_id)
    db.execute("DELETE FROM income_plan WHERE id=?", (income_id,))
    db.commit()
    flash("Поступление удалено")
    return redirect(url_for("finance.incomes"))


# --- проекты и бюджетные группы ----------------------------------------

@bp.route("/projects")
def projects():
    db = get_db()
    rows = db.execute("""
        SELECT p.*,
          (SELECT COALESCE(SUM(COALESCE(bg.estimate_amount, bg.smeta_amount)), 0)
             FROM budget_groups bg WHERE bg.project_id = p.id) AS budget_total,
          (SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
             WHERE i.project_id = p.id AND i.doc_type = 'invoice'
               AND i.status IN ('paid', 'received')) AS paid_total,
          (SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
             WHERE i.project_id = p.id AND i.doc_type = 'invoice'
               AND i.status IN ('new', 'to_pay')) AS incoming_total,
          (SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
             WHERE i.project_id = p.id AND i.doc_type = 'quote'
               AND i.in_plan = 1) AS quote_plan_total,
          (SELECT COALESCE(SUM(ip.amount), 0) FROM income_plan ip
             WHERE ip.project_id = p.id AND ip.status != 'paid') AS income_pending,
          (SELECT COALESCE(SUM(COALESCE(ip.paid_amount, ip.amount)), 0)
             FROM income_plan ip
             WHERE ip.project_id = p.id AND ip.status = 'paid') AS income_received
        FROM projects p ORDER BY p.name""").fetchall()
    totals = {
        "contract": sum(r["contract_price"] or 0 for r in rows),
        "income_pending": sum(r["income_pending"] for r in rows),
        "income_received": sum(r["income_received"] for r in rows),
        "paid": sum(r["paid_total"] for r in rows),
        "incoming": sum(r["incoming_total"] for r in rows),
        "quote_plan": sum(r["quote_plan_total"] for r in rows),
    }
    totals["ostatok"] = (totals["contract"] - totals["income_received"]
                         - totals["paid"] - totals["incoming"]
                         - totals["quote_plan"])
    return render_template("finance_projects.html", projects=rows,
                           totals=totals, tab="projects")


def load_project(db, project_id):
    row = db.execute("SELECT * FROM projects WHERE id = ?",
                     (project_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project(project_id):
    db = get_db()
    proj = load_project(db, project_id)
    if request.method == "POST":
        # отсутствующее в форме поле не затирает сохранённое значение
        db.execute(
            "UPDATE projects SET customer=?, gp_percent=?, retention_percent=?,"
            " retention_months=?, pay_delay_days=?, contract_price=? WHERE id=?",
            (request.form.get("customer", proj["customer"] or "").strip(),
             parse_money("gp_percent", proj["gp_percent"] or 0),
             parse_money("retention_percent", proj["retention_percent"] or 0),
             parse_int("retention_months", proj["retention_months"] or 12),
             parse_int("pay_delay_days", proj["pay_delay_days"] or 30),
             parse_money("contract_price", proj["contract_price"] or 0),
             project_id))
        db.commit()
        flash("Параметры проекта сохранены")
        return redirect(url_for("finance.project", project_id=project_id))
    groups = db.execute("""
        SELECT bg.*,
          (SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
             WHERE i.budget_group_id = bg.id AND i.doc_type = 'invoice'
               AND i.status IN ('paid', 'received')) AS fact,
          (SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
             WHERE i.budget_group_id = bg.id AND i.doc_type = 'invoice'
               AND i.status IN ('new', 'to_pay')) AS pending
        FROM budget_groups bg WHERE bg.project_id = ?
        ORDER BY bg.off_smeta, bg.plan_month, bg.id""", (project_id,)).fetchall()
    edit_id = request.args.get("group", "")
    edit_group = None
    if edit_id.isdigit():
        edit_group = next((g_ for g_ in groups if g_["id"] == int(edit_id)), None)
    totals = {
        "smeta": sum(g_["smeta_amount"] for g_ in groups),
        "estimate": sum(
            g_["estimate_amount"] if g_["estimate_amount"] is not None
            else g_["smeta_amount"] for g_ in groups),
        "fact": sum(g_["fact"] for g_ in groups),
        "pending": sum(g_["pending"] for g_ in groups),
    }
    # плановая маржа = цена контракта - плановая себестоимость (оценка групп)
    totals["margin"] = (proj["contract_price"] or 0) - totals["estimate"]
    return render_template("finance_project.html", project=proj, groups=groups,
                           totals=totals, edit_group=edit_group, tab="projects",
                           month_label=month_label)


@bp.route("/projects/<int:project_id>/groups", methods=["POST"])
def group_add(project_id):
    db = get_db()
    load_project(db, project_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Укажите название группы")
        return redirect(url_for("finance.project", project_id=project_id))
    category = request.form.get("category", "materials")
    estimate_raw = request.form.get("estimate_amount", "").strip()
    db.execute(
        "INSERT INTO budget_groups (project_id, name, category, smeta_amount,"
        " estimate_amount, plan_month, off_smeta, comment, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, name,
         category if category in BG_CATEGORIES else "materials",
         parse_money("smeta_amount"),
         parse_money("estimate_amount") if estimate_raw else None,
         request.form.get("plan_month", "").strip(),
         1 if request.form.get("off_smeta") else 0,
         request.form.get("comment", "").strip(), now()))
    db.commit()
    flash("Группа добавлена")
    return redirect(url_for("finance.project", project_id=project_id))


def load_group(db, group_id):
    row = db.execute("SELECT * FROM budget_groups WHERE id = ?",
                     (group_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/groups/<int:group_id>", methods=["POST"])
def group_edit(group_id):
    db = get_db()
    grp = load_group(db, group_id)
    category = request.form.get("category", "materials")
    estimate_raw = request.form.get("estimate_amount", "").strip()
    db.execute(
        "UPDATE budget_groups SET name=?, category=?, smeta_amount=?,"
        " estimate_amount=?, plan_month=?, off_smeta=?, comment=? WHERE id=?",
        (request.form.get("name", "").strip() or grp["name"],
         category if category in BG_CATEGORIES else "materials",
         parse_money("smeta_amount"),
         parse_money("estimate_amount") if estimate_raw else None,
         request.form.get("plan_month", "").strip(),
         1 if request.form.get("off_smeta") else 0,
         request.form.get("comment", "").strip(), group_id))
    db.commit()
    flash("Группа обновлена")
    return redirect(url_for("finance.project", project_id=grp["project_id"]))


@bp.route("/groups/<int:group_id>/delete", methods=["POST"])
def group_delete(group_id):
    db = get_db()
    grp = load_group(db, group_id)
    db.execute("UPDATE invoices SET budget_group_id = NULL "
               "WHERE budget_group_id = ?", (group_id,))
    db.execute("DELETE FROM budget_groups WHERE id = ?", (group_id,))
    db.commit()
    flash("Группа удалена (счета остались, отвязаны от группы)")
    return redirect(url_for("finance.project", project_id=grp["project_id"]))


# --- загрузка сметы через ИИ -------------------------------------------

def extract_smeta_text(file, pasted):
    """Достаёт текст сметы из вставленного текста или загруженного файла."""
    if pasted and pasted.strip():
        return pasted, None
    if not file or not file.filename:
        return "", None
    name = file.filename.lower()
    raw = file.read()
    if name.endswith((".txt", ".csv")):
        for enc in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                return raw.decode(enc), None
            except UnicodeDecodeError:
                continue
        return "", "Не удалось прочитать файл — попробуйте вставить текст."
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            return "", ("Для чтения Excel установите зависимости "
                        "(pip install -r requirements.txt) или вставьте текст.")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True,
                                        data_only=True)
        except Exception:
            return "", "Не удалось открыть Excel — попробуйте вставить текст."
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c not in (None, "")]
                if cells:
                    lines.append("\t".join(cells))
        return "\n".join(lines), None
    return "", ("Поддерживаются .xlsx, .csv, .txt. Файлы PDF/DOC — "
                "выгрузите в Excel или вставьте текст.")


@bp.route("/projects/<int:project_id>/smeta", methods=["GET", "POST"])
def smeta_upload(project_id):
    db = get_db()
    proj = load_project(db, project_id)
    if request.method == "POST":
        text, err = extract_smeta_text(request.files.get("file"),
                                       request.form.get("pasted", ""))
        if err:
            flash(err)
            return redirect(url_for("finance.smeta_upload", project_id=project_id))
        data, err = ai.parse_smeta(text)
        if err:
            flash(err)
            return redirect(url_for("finance.smeta_upload", project_id=project_id))
        if not data["groups"]:
            flash("ИИ не нашёл в тексте групп — проверьте, что это смета.")
            return redirect(url_for("finance.smeta_upload", project_id=project_id))
        groups_sum = round(sum(g["amount"] for g in data["groups"]), 2)
        # цена контракта: что ввёл пользователь, иначе итог по смете
        contract_price = parse_money("contract_price") or data["smeta_total"] \
            or groups_sum
        return render_template(
            "smeta_review.html", project=proj, groups=data["groups"],
            groups_sum=groups_sum, smeta_total=data["smeta_total"],
            contract_price=contract_price, truncated=data.get("truncated"),
            tab="projects")
    return render_template("smeta_upload.html", project=proj, tab="projects",
                           ai_enabled=ai.is_enabled())


def _to_float(s):
    """Разбирает сумму из строки: '4 000 000,00 ₽', '4000000.5', '1 200' и т.п."""
    s = re.sub(r"[^\d,.\-]", "", str(s or ""))
    if not s:
        return None
    if "," in s and "." in s:          # '.' — разделитель тысяч, ',' — дробная
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _category_from(text):
    t = (text or "").lower()
    if "штраф" in t:
        return "penalty"
    if "работ" in t:
        return "works"
    if "проч" in t or "other" in t:
        return "other"
    return "materials"


def parse_pasted_breakdown(text):
    """Разбирает готовую разбивку из claude.ai в группы (без ИИ, чистый код).

    Формат строки: «Название | Категория | Сумма». Разделители: | ; таб.
    Категория необязательна (тогда выводится по ключевым словам названия).
    """
    groups = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s*[|;\t]\s*", line) if p.strip()]
        if len(parts) < 2:
            continue
        # сумма — самое правое поле-число
        amount, amount_i = None, None
        for i in range(len(parts) - 1, -1, -1):
            v = _to_float(parts[i])
            if v is not None:
                amount, amount_i = v, i
                break
        if amount is None or amount_i == 0:
            continue                    # нужны и название, и сумма
        name = parts[0]
        middle = " ".join(parts[1:amount_i])
        cat = _category_from(middle) if middle else _category_from(name)
        # пропускаем строки-итоги
        if name.lower().startswith(("итог", "всего", "total")):
            continue
        groups.append({"name": name[:200], "category": cat, "amount": amount})
    return groups


@bp.route("/projects/<int:project_id>/smeta/paste", methods=["POST"])
def smeta_paste(project_id):
    """Импорт готовой разбивки (из claude.ai) — без ИИ-ключа."""
    db = get_db()
    proj = load_project(db, project_id)
    groups = parse_pasted_breakdown(request.form.get("breakdown", ""))
    if not groups:
        flash("Не удалось разобрать разбивку. Нужен формат по строкам: "
              "Название | Категория | Сумма")
        return redirect(url_for("finance.smeta_upload", project_id=project_id))
    groups_sum = round(sum(g["amount"] for g in groups), 2)
    contract_price = parse_money("contract_price") or groups_sum
    return render_template("smeta_review.html", project=proj, groups=groups,
                           groups_sum=groups_sum, smeta_total=groups_sum,
                           contract_price=contract_price, truncated=False,
                           tab="projects")


@bp.route("/projects/<int:project_id>/smeta/apply", methods=["POST"])
def smeta_apply(project_id):
    db = get_db()
    load_project(db, project_id)
    names = request.form.getlist("name")
    cats = request.form.getlist("category")
    amounts = request.form.getlist("amount")
    include = set(request.form.getlist("include"))  # индексы отмеченных строк

    contract_price = parse_money("contract_price")
    # коэффициент: из формы (можно поправить руками) либо цена/сумма групп
    base_sum = 0.0
    rows = []
    for idx, (nm, ct, am) in enumerate(zip(names, cats, amounts)):
        if str(idx) not in include or not nm.strip():
            continue
        try:
            val = round(float(str(am).replace(" ", "").replace(",", ".")), 2)
        except ValueError:
            val = 0.0
        rows.append((idx, nm.strip(), ct if ct in BG_CATEGORIES else "materials",
                     val))
        base_sum += val

    coeff = parse_money("coefficient")
    if coeff <= 0:
        coeff = round(contract_price / base_sum, 6) if (contract_price and base_sum) \
            else 1.0

    created = 0
    for _idx, nm, ct, val in rows:
        db.execute(
            "INSERT INTO budget_groups (project_id, name, category, smeta_amount,"
            " estimate_amount, plan_month, off_smeta, comment, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, nm, ct, round(val * coeff, 2), None, "",
             1 if ct == "penalty" else 0, "", now()))
        created += 1
    db.execute("UPDATE projects SET contract_price=?, smeta_coefficient=? WHERE id=?",
               (contract_price, coeff, project_id))
    db.commit()
    flash(f"Добавлено групп: {created}. Коэффициент к смете: {coeff:g}")
    return redirect(url_for("finance.project", project_id=project_id))


# --- постоянные расходы -------------------------------------------------

@bp.route("/recurring", methods=["GET", "POST"])
def recurring():
    db = get_db()
    if request.method == "POST":
        edit_id = request.form.get("edit_id", "")
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "other")
        if not name:
            flash("Укажите название")
        elif edit_id.isdigit():
            db.execute(
                "UPDATE recurring_expenses SET name=?, category=?, amount=?,"
                " day_of_month=?, comment=? WHERE id=?",
                (name, category if category in REC_CATEGORIES else "other",
                 parse_money("amount"),
                 min(max(parse_int("day_of_month", 10), 1), 31),
                 request.form.get("comment", "").strip(), int(edit_id)))
            db.commit()
            flash("Расход обновлён")
            return redirect(url_for("finance.recurring"))
        else:
            db.execute(
                "INSERT INTO recurring_expenses (name, category, amount,"
                " day_of_month, comment, created_at) VALUES (?,?,?,?,?,?)",
                (name, category if category in REC_CATEGORIES else "other",
                 parse_money("amount"),
                 min(max(parse_int("day_of_month", 10), 1), 31),
                 request.form.get("comment", "").strip(), now()))
            db.commit()
            flash("Расход добавлен")
            return redirect(url_for("finance.recurring"))
    rows = db.execute("SELECT * FROM recurring_expenses "
                      "ORDER BY active DESC, day_of_month, id").fetchall()
    edit_id = request.args.get("edit", "")
    edit_row = None
    if edit_id.isdigit():
        edit_row = next((r for r in rows if r["id"] == int(edit_id)), None)
    monthly_total = sum(r["amount"] for r in rows if r["active"])
    return render_template("finance_recurring.html", rows=rows, tab="recurring",
                           edit_row=edit_row, monthly_total=monthly_total)


@bp.route("/recurring/<int:rec_id>/toggle", methods=["POST"])
def recurring_toggle(rec_id):
    db = get_db()
    db.execute("UPDATE recurring_expenses SET active = 1 - active WHERE id=?",
               (rec_id,))
    db.commit()
    return redirect(url_for("finance.recurring"))


@bp.route("/recurring/<int:rec_id>/delete", methods=["POST"])
def recurring_delete(rec_id):
    db = get_db()
    db.execute("DELETE FROM recurring_expenses WHERE id=?", (rec_id,))
    db.commit()
    flash("Расход удалён")
    return redirect(url_for("finance.recurring"))
