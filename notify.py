# -*- coding: utf-8 -*-
"""Уведомления в Telegram при смене статуса счёта.

Настраивается переменными окружения (см. server_start.bat):
    TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
    TELEGRAM_CHAT_ID    — id чата или группы, куда слать
    NOTIFY_STATUSES     — при каких статусах слать (через запятую),
                          по умолчанию: to_pay,paid
    APP_BASE_URL        — адрес приложения для ссылки в сообщении,
                          например https://scheta.company.ru

Если токен или чат не заданы — уведомления просто выключены, приложение
работает как обычно. Отправка идёт в фоне: недоступный Telegram никогда
не задержит и не сломает сохранение счёта.
"""
import html
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
NOTIFY_STATUSES = [s.strip() for s in
                   os.environ.get("NOTIFY_STATUSES", "to_pay,paid").split(",")
                   if s.strip()]

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10

# Что показываем в заголовке сообщения для каждого статуса.
TITLES = {
    "new": "📥 Счёт возвращён во «Входящие»",
    "to_pay": "📝 Счёт отправлен В ОПЛАТУ",
    "paid": "✅ Счёт оплачен",
    "received": "📦 Товар забрали",
}


def enabled():
    return bool(BOT_TOKEN and CHAT_ID)


def _call(method, params):
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(API.format(token=BOT_TOKEN, method=method),
                                 data=body)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send(text):
    """Отправляет сообщение в фоне. Ошибки только пишутся в консоль."""
    if not enabled():
        return

    def run():
        try:
            result = _call("sendMessage", {
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
            if not result.get("ok"):
                print("Telegram отказал:", result.get("description"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("Не удалось отправить уведомление в Telegram:", exc)

    threading.Thread(target=run, daemon=True).start()


def _money(value):
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def invoice_link(invoice_id, fallback_root=""):
    """Ссылка на счёт: из APP_BASE_URL, иначе из адреса текущего запроса."""
    root = BASE_URL or (fallback_root or "").rstrip("/")
    return f"{root}/invoices/{invoice_id}" if root else ""


def status_changed(invoice, status, status_label, vat_label, link=""):
    """Сообщение о смене статуса счёта."""
    if not enabled() or status not in NOTIFY_STATUSES:
        return
    e = html.escape
    lines = [f"<b>{e(TITLES.get(status, status_label))}</b>", ""]
    if invoice["number"]:
        lines.append(f"Счёт № {e(str(invoice['number']))}")
    lines.append(f"Проект: <b>{e(invoice['project_name'])}</b>")
    lines.append(f"Сумма: <b>{_money(invoice['amount'])} ₽</b> ({e(vat_label)})")
    if invoice["supplier_name"]:
        lines.append(f"Поставщик: {e(invoice['supplier_name'])}")
    if invoice["comment"]:
        lines.append(f"Что: {e(invoice['comment'])}")
    if invoice["due_date"] and status == "to_pay":
        lines.append(f"Оплатить до: <b>{e(str(invoice['due_date']))}</b>")
    if invoice["is_critical"]:
        lines.append("❗ Критичный — без него встанет этап")
    if link:
        lines.append("")
        lines.append(f'<a href="{e(link)}">Открыть счёт</a>')
    send("\n".join(lines))
