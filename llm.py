"""Responses API transport with structured roles, streaming and usage accounting."""
from __future__ import annotations

import hashlib
import time

from openai import OpenAI

from personalization import ContextBudgetError, ModelRequest, tokens


class ModelGateway:
    def __init__(self, api_key, model, budget=3500):
        self.api_key, self.model, self.budget = api_key, model, budget
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                raise ValueError("Укажи OPENAI_API_KEY в .env или config.json")
            self._client = OpenAI(api_key=self.api_key, timeout=45, max_retries=1)
        return self._client

    def events(self, request, max_tokens=400, temperature=0.6):
        if isinstance(request, str):
            request = ModelRequest("Выполни задачу из сообщения. Верни только запрошенный результат.", [{"role": "user", "content": request}])
        if request.count() > self.budget:
            raise ContextBudgetError("Запрос превышает бюджет контекста")
        kwargs = dict(model=self.model, instructions=request.instructions, input=request.messages,
                      max_output_tokens=max_tokens, stream=True, store=False)
        # These models support no-reasoning mode, matching the short chat output budget.
        if self.model.startswith(("gpt-6.1-terra", "gpt-6-luna")):
            kwargs["reasoning"] = {"effort": "none"}
        if not self.model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
            kwargs["temperature"] = temperature
        if request.owner_id:
            owner_hash = hashlib.sha256(request.owner_id.encode()).hexdigest()[:24]
            kwargs["prompt_cache_key"] = "twin-v2-" + owner_hash
        started, first = time.perf_counter(), None
        pieces, completed = [], None
        stream = self.client.responses.create(**kwargs)
        try:
            for event in stream:
                if event.type == "response.output_text.delta":
                    if first is None:
                        first = time.perf_counter()
                    pieces.append(event.delta)
                    yield {"delta": event.delta}
                elif event.type == "response.completed":
                    completed = event.response
                elif event.type in {"response.failed", "response.incomplete", "error"}:
                    raise RuntimeError("Модель не завершила ответ. Попробуй ещё раз.")
        finally:
            stream.close()
        answer = "".join(pieces).strip()
        if not completed or not answer:
            raise RuntimeError("Модель вернула пустой или незавершённый ответ")
        usage = completed.usage
        details = getattr(usage, "input_tokens_details", None)
        yield {"complete": answer, "ttft_ms": round(1000 * ((first or time.perf_counter()) - started), 1),
               "usage": {"input_tokens": getattr(usage, "input_tokens", 0),
                         "output_tokens": getattr(usage, "output_tokens", 0),
                         "cached_tokens": getattr(details, "cached_tokens", 0)}}

    def complete(self, request, max_tokens=400, temperature=0.6):
        for event in self.events(request, max_tokens, temperature):
            if "complete" in event:
                return event["complete"]
        raise RuntimeError("Не получен завершённый ответ")
