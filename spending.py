"""Persistent, shared API spending guard. Amounts use integer nanodollars."""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


SCALE = 1_000_000_000
# Standard text prices, verified 2026-11-02. No paid tools are enabled by the gateway.
# https://developers.openai.com/api/docs/models/gpt-6-luna
# https://developers.openai.com/api/docs/models/gpt-6.1-terra
RATES = {"gpt-6-luna": (100, 10, 125, 500), "gpt-6.1-terra": (1000, 50, 1250, 5000)}


class SpendingLimitError(ValueError):
    pass


def amount(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0:
            raise ValueError
        return math.ceil(value * SCALE)
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Денежный лимит должен быть положительным числом") from exc


def rates_for(model):
    for name, rates in RATES.items():
        if model == name or re.fullmatch(re.escape(name) + r"-\d{4}-\d{2}-\d{2}", model):
            return rates
    raise SpendingLimitError("Для этой модели не настроен проверенный тариф. Выбери gpt-6-luna или gpt-6.1-terra.")


def input_bound(request):
    # UTF-8 bytes are a deliberately loose text-token bound, with framing headroom.
    # Local tokenizer counts alone are not an authoritative API token count.
    texts = [request.instructions, *(m["content"] for m in request.messages)]
    return max(request.count() + 128, sum(len(t.encode("utf-8")) for t in texts) + 256 + 64 * len(texts))


def padded(value):
    return (value * 110 + 99) // 100  # Include 10% billing headroom, including regional premiums.


@dataclass(frozen=True)
class Reservation:
    id: str
    maximum: int
    input_limit: int
    output_limit: int
    model: str


class SpendingController:
    def __init__(self, store_provider, total_usd=4, daily_usd=0.5, daily_requests=200, user_daily_requests=100):
        self.store_provider = store_provider
        self.total, self.daily = amount(total_usd), amount(daily_usd)
        self.daily_requests, self.user_daily_requests = int(daily_requests), int(user_daily_requests)
        if min(self.daily_requests, self.user_daily_requests) < 1:
            raise ValueError("Лимит запросов должен быть положительным")

    def quote(self, request, model, output_limit):
        ordinary, cached, written, output = rates_for(model)
        bound = input_bound(request)
        # Reserve even for 12h cache writes, although requests explicitly select 30m.
        maximum = padded(bound * max(ordinary, cached, written * 2) + output_limit * output)
        return maximum, bound

    def reserve(self, request, model, output_limit):
        maximum, bound = self.quote(request, model, output_limit)
        now, uid = time.time(), str(request.owner_id)
        with self.store_provider().transaction() as db:
            row = db.execute("""SELECT COALESCE(SUM(charged),0) total,
                COALESCE(SUM(CASE WHEN created>=? THEN charged ELSE 0 END),0) daily,
                SUM(CASE WHEN created>=? THEN 1 ELSE 0 END) requests,
                SUM(CASE WHEN created>=? AND uid=? THEN 1 ELSE 0 END) user_requests,
                SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked FROM api_spending""",
                (now - 86400, now - 86400, now - 86400, uid)).fetchone()
            if row["blocked"]:
                raise SpendingLimitError("Учёт стоимости требует проверки. Платные запросы остановлены; обучение и память доступны.")
            if row["total"] + maximum > self.total:
                raise SpendingLimitError("Достигнут общий бюджет API. Обучение, импорт и память продолжают работать бесплатно.")
            if row["daily"] + maximum > self.daily:
                raise SpendingLimitError("Достигнут денежный лимит за последние 24 часа. Платные запросы временно остановлены.")
            if (row["requests"] or 0) >= self.daily_requests or (row["user_requests"] or 0) >= self.user_daily_requests:
                raise SpendingLimitError("Достигнут лимит запросов за последние 24 часа. Обучение доступно без API.")
            ticket = Reservation(uuid.uuid4().hex, maximum, bound, output_limit, model)
            db.execute("""INSERT INTO api_spending(id,uid,created,model,reserved,charged,status)
                VALUES(?,?,?,?,?,?,'reserved')""", (ticket.id, uid, now, model, maximum, maximum))
        return ticket

    def settle(self, ticket, usage, response_model, tier):
        if not (response_model == ticket.model or re.fullmatch(re.escape(ticket.model) + r"-\d{4}-\d{2}-\d{2}", str(response_model))):
            self.uncertain(ticket, blocked=True)
            raise SpendingLimitError("API вернул другую модель. Запросы остановлены для проверки стоимости.")
        if tier != "default":
            self.uncertain(ticket, blocked=True)
            raise SpendingLimitError("API вернул другой тариф обработки. Запросы остановлены для проверки стоимости.")
        ordinary_rate, cache_rate, write_rate, output_rate = rates_for(ticket.model)
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        cached, written = usage.get("cached_tokens"), usage.get("cache_write_tokens")
        integers = (incoming, outgoing)
        if any(type(n) is not int or n < 0 for n in integers) or incoming > ticket.input_limit or outgoing > ticket.output_limit:
            self.uncertain(ticket, blocked=True)
            raise SpendingLimitError("Не удалось проверить расход токенов. Платные запросы остановлены.")
        estimated = cached is None or written is None
        if estimated:
            cost = padded(incoming * write_rate * 2 + outgoing * output_rate)
        else:
            if any(type(n) is not int or n < 0 for n in (cached, written)) or cached + written > incoming:
                self.uncertain(ticket, blocked=True)
                raise SpendingLimitError("Некорректные данные стоимости от API. Платные запросы остановлены.")
            cost = padded((incoming - cached - written) * ordinary_rate + cached * cache_rate + written * write_rate + outgoing * output_rate)
        if cost > ticket.maximum:
            self.uncertain(ticket, blocked=True)
            raise SpendingLimitError("Стоимость превысила резерв. Платные запросы остановлены для проверки тарифа.")
        with self.store_provider().transaction() as db:
            db.execute("""UPDATE api_spending SET charged=?,status=?,input_tokens=?,output_tokens=?,cached_tokens=?,cache_write_tokens=?
                WHERE id=? AND status='reserved'""", (cost, "estimated" if estimated else "settled", incoming, outgoing, cached, written, ticket.id))
        return cost / SCALE

    def uncertain(self, ticket, blocked=False, rejected=False):
        # Crashes, cancellation and network failures retain the whole reservation.
        with self.store_provider().transaction() as db:
            db.execute("UPDATE api_spending SET status=?,charged=? WHERE id=? AND status='reserved'",
                ("blocked" if blocked else "rejected" if rejected else "uncertain", 0 if rejected else ticket.maximum, ticket.id))

    def status(self):
        now = time.time()
        with self.store_provider().transaction() as db:
            row = db.execute("""SELECT COALESCE(SUM(charged),0) total,
                COALESCE(SUM(CASE WHEN created>=? THEN charged ELSE 0 END),0) daily,
                COUNT(*) requests, COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain','estimated','blocked') THEN charged ELSE 0 END),0) conservative,
                COALESCE(SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END),0) blocked FROM api_spending""", (now - 86400,)).fetchone()
        return {"limit_usd": self.total / SCALE, "accounted_usd": row["total"] / SCALE,
            "remaining_usd": max(0, self.total - row["total"]) / SCALE, "daily_limit_usd": self.daily / SCALE,
            "daily_accounted_usd": row["daily"] / SCALE, "requests": row["requests"],
            "conservative_usd": row["conservative"] / SCALE, "blocked": bool(row["blocked"])}

    def describe(self):
        data = self.status()
        return (f"Бюджет приложения: ${data['limit_usd']:.2f}. Учтено с запасом: ${data['accounted_usd']:.4f}. "
            f"Доступно в приложении: ${data['remaining_usd']:.4f}.\n"
            f"За последние 24 часа: ${data['daily_accounted_usd']:.4f} из ${data['daily_limit_usd']:.2f}. "
            f"Попыток API: {data['requests']}.\nЭто местный счётчик с момента включения защиты, не баланс аккаунта OpenAI.")
