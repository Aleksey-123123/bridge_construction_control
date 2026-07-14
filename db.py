# -*- coding: utf-8 -*-
"""Общие настройки БД и вспомогательные функции.

Используются и основным приложением (app.py), и финансовым модулем
(finance.py), чтобы не было циклических импортов.
"""
import os
import sqlite3
from datetime import datetime

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "invoices.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
SECRET_FILE = os.path.join(DATA_DIR, ".secret")

os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def ensure_column(conn, table, column, decl):
    """Мягкая миграция: добавляет колонку, если её ещё нет."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
