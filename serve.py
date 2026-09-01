# -*- coding: utf-8 -*-
"""Боевой запуск приложения на офисном сервере.

В отличие от `python app.py` (встроенный сервер Flask, только для проб),
здесь используется waitress — он держит нагрузку нескольких сотрудников
и работает на Windows. Запуск: python serve.py
"""
import os
import sys

from app import APP_PASSWORD, app

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))


def main():
    if not APP_PASSWORD:
        print("!" * 62)
        print("ВНИМАНИЕ: пароль не задан (APP_PASSWORD пуст).")
        print("Если приложение доступно из интернета — обязательно задайте")
        print("пароль в server_start.bat, иначе войти сможет кто угодно.")
        print("!" * 62)
    try:
        from waitress import serve
    except ImportError:
        print("waitress не установлен, запускаю встроенный сервер Flask.")
        print("Для боевого режима выполните: pip install waitress")
        app.run(host=HOST, port=PORT)
        return
    print(f"Приложение запущено на порту {PORT}. Остановить — Ctrl+C.")
    sys.stdout.flush()
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
