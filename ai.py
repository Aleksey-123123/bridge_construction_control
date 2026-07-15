# -*- coding: utf-8 -*-
"""ИИ-помощник: разбор сметы на укрупнённые группы бюджета.

Использует Claude (Anthropic API). Работает, только если задан ключ
ANTHROPIC_API_KEY. Модель — ANTHROPIC_MODEL (по умолчанию claude-opus-4-8);
для экономии можно поставить claude-haiku-4-5.

Принцип: ИИ ПРЕДЛАГАЕТ разбивку, человек проверяет и подтверждает на
отдельном экране. В базу пишется только подтверждённое (см. finance.py).
"""
import os

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
MAX_SMETA_CHARS = 120_000  # ~ несколько сотен строк сметы

CATEGORY_HINT = "materials — материалы, works — работы, other — прочее"

PROMPT = """Ты помогаешь строительной компании (мостостроение) спланировать \
бюджет по смете из контракта.

Ниже — текст сметы (возможно, выгрузка из Excel/Гранд-сметы, «грязная»).
Разбей её на 15–30 УКРУПНЁННЫХ групп для планирования расходов — не построчно,
а смысловыми блоками (например: «Металлоконструкции пролёта», «Деформационные \
швы», «Гидроизоляция», «Опоры — работы», «Асфальтобетон»). Для каждой группы:
  - name: короткое понятное название;
  - category: одно из ({hint});
  - amount: суммарная стоимость группы (число, в рублях, как в смете).

Также верни smeta_total — общую сумму по смете, как она указана в документе
(или сумму всех строк, если итога нет).

Не выдумывай позиции, которых нет. Если смета пустая или это не смета —
верни пустой список групп и smeta_total = 0.

ТЕКСТ СМЕТЫ:
{text}"""


def is_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _schema_model():
    """Определяем pydantic-модели лениво (pydantic ставится вместе с anthropic)."""
    from pydantic import BaseModel

    class SmetaGroup(BaseModel):
        name: str
        category: str
        amount: float

    class SmetaResult(BaseModel):
        groups: list[SmetaGroup]
        smeta_total: float

    return SmetaResult


def parse_smeta(text):
    """Разбирает текст сметы. Возвращает (result_dict, error_str).

    result_dict = {"groups": [{name, category, amount}], "smeta_total": float}
    При ошибке возвращает (None, "текст ошибки для показа пользователю").
    """
    if not is_enabled():
        return None, ("ИИ-разбор не настроен: не задан ключ ANTHROPIC_API_KEY. "
                      "Группы бюджета можно завести вручную ниже.")
    text = (text or "").strip()
    if not text:
        return None, "Пустой файл или текст сметы."
    truncated = len(text) > MAX_SMETA_CHARS
    if truncated:
        text = text[:MAX_SMETA_CHARS]

    try:
        import anthropic
    except ImportError:
        return None, ("Не установлена библиотека anthropic. Выполните "
                      "pip install -r requirements.txt и запустите снова.")

    try:
        client = anthropic.Anthropic()
        result_model = _schema_model()
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            messages=[{"role": "user",
                       "content": PROMPT.format(hint=CATEGORY_HINT, text=text)}],
            output_format=result_model,
        )
        parsed = resp.parsed_output
        if parsed is None:
            return None, "ИИ не смог разобрать смету — попробуйте вставить текст вручную."
        groups = []
        for g in parsed.groups:
            cat = g.category if g.category in ("materials", "works", "other") \
                else "materials"
            groups.append({"name": g.name.strip()[:200],
                           "category": cat,
                           "amount": round(float(g.amount), 2)})
        data = {"groups": groups,
                "smeta_total": round(float(parsed.smeta_total), 2),
                "truncated": truncated}
        return data, None
    except anthropic.AuthenticationError:
        return None, "Неверный ключ ANTHROPIC_API_KEY."
    except anthropic.RateLimitError:
        return None, "Слишком много запросов к ИИ — подождите минуту и повторите."
    except anthropic.APIError as e:
        return None, f"Ошибка ИИ: {getattr(e, 'message', str(e))}"
    except Exception as e:  # noqa: BLE001 — показываем причину пользователю
        return None, f"Не удалось обратиться к ИИ: {e}"
