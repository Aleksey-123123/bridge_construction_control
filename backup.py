# -*- coding: utf-8 -*-
"""Резервная копия базы и файлов счетов.

Складывает архив в папку backups рядом с проектом и удаляет копии
старше KEEP_DAYS дней. База копируется штатным средством SQLite,
поэтому копия корректна даже если в этот момент кто-то пишет счёт.

Запуск: python backup.py [папка_для_копий]
"""
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta

from db import DATA_DIR, DB_PATH, UPLOAD_DIR

KEEP_DAYS = 30
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def backup_db(dest):
    """Копия БД без остановки приложения."""
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def cleanup(out_dir):
    edge = datetime.now() - timedelta(days=KEEP_DAYS)
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if not name.startswith("backup-") or not name.endswith(".zip"):
            continue
        if datetime.fromtimestamp(os.path.getmtime(path)) < edge:
            os.remove(path)
            print("удалена старая копия:", name)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR,
                                                                "backups")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    tmp_db = os.path.join(out_dir, f"invoices-{stamp}.db")
    backup_db(tmp_db)

    archive = os.path.join(out_dir, f"backup-{stamp}.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, "invoices.db")
        for root, _dirs, files in os.walk(UPLOAD_DIR):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.join("uploads",
                                           os.path.relpath(full, UPLOAD_DIR)))
    os.remove(tmp_db)

    size = os.path.getsize(archive) / 1024 / 1024
    print(f"Копия готова: {archive} ({size:.1f} МБ)")
    cleanup(out_dir)


if __name__ == "__main__":
    main()
