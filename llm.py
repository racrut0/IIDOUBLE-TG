"""Responses API transport with structured roles, streaming and usage accounting."""
from __future__ import annotations

import hashlib
import time

from openai import OpenAI, APIStatusError

from personalization import ContextBudgetError, ModelRequest


class ModelGateway:
    def __init__(self, api_key, model, budget=2200, spending=None, output_limit=240):
        self.api_key, self.model, self.budget = api_key, model, budget
        self.spending, self.output_limit = spending, output_limit
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                raise ValueError("Укажи OPENAI_API_KEY в .env или config.json")
            self._client = OpenAI(api_key=self.api_key, base_url="https://api.openai.com/v1", timeout=45, max_retries=0)
        return self._client

    def events(self, request, max_tokens=240, temperature=0.6):
        if isinstance(request, str):
            request = ModelRequest("Выполни задачу из сообщения. Верни только запрошенный результат.", [{"role": "user", "content": request}])
        if request.count() > self.budget:
            raise ContextBudgetError("Запрос превышает бюджет контекста")
        if self.spending is None:
            raise ValueError("Перед обращением к API нужен общий счётчик бюджета")
        max_tokens = min(int(max_tokens), self.output_limit)
        if max_tokens < 16:
            raise ValueError("Для ответа нужно не меньше 16 выходных токенов")
        kwargs = dict(model=self.model, instructions=request.instructions, input=request.messages,
                      max_output_tokens=max_tokens, stream=True, store=False, service_tier="default")
        # These models support no-reasoning mode, matching the short chat output budget.
        if self.model.startswith(("gpt-6.1-terra", "gpt-6-luna")):
            kwargs["reasoning"] = {"effort": "none"}
            kwargs["prompt_cache_options"] = {"mode": "implicit", "ttl": "30m"}
        if not self.model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
            kwargs["temperature"] = temperature
        if request.owner_id:
            owner_hash = hashlib.sha256(request.owner_id.encode()).hexdigest()[:24]
            kwargs["prompt_cache_key"] = "twin-v2-" + owner_hash
        started, first = time.perf_counter(), None
        pieces, terminal, stream, accounted = [], None, None, False
        client = self.client
        ticket = self.spending.reserve(request, self.model, max_tokens)
        try:
            stream = client.responses.create(**kwargs)
            for event in stream:
                if event.type == "response.output_text.delta":
                    if first is None:
                        first = time.perf_counter()
                    pieces.append(event.delta)
                    yield {"delta": event.delta}
                elif event.type in {"response.completed", "response.failed", "response.incomplete"}:
                    terminal = event.response
                    if event.type != "response.completed":
                        break
                elif event.type == "error":
                    break
            usage = getattr(terminal, "usage", None)
            details = getattr(usage, "input_tokens_details", None)
            observed = {"input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cached_tokens": getattr(details, "cached_tokens", None),
                "cache_write_tokens": getattr(details, "cache_write_tokens", None)}
            if usage is not None:
                cost = self.spending.settle(ticket, observed, getattr(terminal, "model", ""), getattr(terminal, "service_tier", ""))
                accounted = True
            else:
                raise RuntimeError("Не получены данные расхода API. Возможная стоимость учтена с запасом.")
            answer = "".join(pieces).strip()
            if getattr(terminal, "status", "") != "completed" or not answer:
                raise RuntimeError("Ответ не завершён. Расход учтён; автоматический повтор отключён.")
        except BaseException as exc:
            if not accounted:
                rejected = isinstance(exc, APIStatusError) and exc.status_code in {400, 401, 403, 404, 413, 422, 429}
                self.spending.uncertain(ticket, rejected=rejected)
            raise
        finally:
            if stream is not None:
                stream.close()
        yield {"complete": answer, "ttft_ms": round(1000 * ((first or time.perf_counter()) - started), 1),
               "usage": {**observed, "accounted_cost_usd": cost}}

    def complete(self, request, max_tokens=240, temperature=0.6):
        for event in self.events(request, max_tokens, temperature):
            if "complete" in event:
                return event["complete"]
        raise RuntimeError("Не получен завершённый ответ")
