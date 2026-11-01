"""Evaluate saved replies, or explicitly generate held-out replies with --live."""
import argparse
import json
import re
from contextlib import closing
from pathlib import Path


def load_cases(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Нужен непустой список проверочных диалогов")
    seen = set()
    for case in cases:
        if not all(case.get(key) for key in ("id", "chat_id", "context", "expected_answer")):
            raise ValueError("Для каждого примера нужны id, chat_id, context и expected_answer")
        if case["id"] in seen:
            raise ValueError("Идентификаторы проверочных примеров должны быть уникальными")
        seen.add(case["id"])
    return cases


def ensure_held_out(store, uid, cases):
    # Split by whole chat; the expected answer never goes into the generation prompt.
    with closing(store.connect()) as db:
        for case in cases:
            row = db.execute("""SELECT 1 FROM memory_items WHERE uid=?
                AND category IN ('imported_dialogues','examples','import_facts')
                AND json_extract(data,'$.chat_id')=? LIMIT 1""", (str(uid), str(case["chat_id"]))).fetchone()
            if row:
                raise ValueError(f"Диалог {case['chat_id']} уже есть в обучающей памяти. Выбери другой проверочный диалог.")


def compare(case, candidate):
    expected = case["expected_answer"]
    words = lambda text: re.findall(r"\w+", text.lower())
    expected_words, actual_words = set(words(expected)), set(words(candidate))
    union = expected_words | actual_words
    return {"id": case["id"], "context": case["context"], "expected_answer": expected,
            "candidate": candidate, "length_ratio": round(len(words(candidate)) / max(1, len(words(expected))), 2),
            "word_overlap": round(len(expected_words & actual_words) / max(1, len(union)), 3),
            "owner_rating": None, "owner_notes": ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--responses", help="JSON: список объектов {id, answer} с готовыми ответами")
    parser.add_argument("--live", action="store_true", help="Явно разрешить платные запросы к модели")
    parser.add_argument("--user-id", help="Владелец профиля для --live")
    parser.add_argument("--output", default="work/evaluation.json")
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    if args.live:
        if not args.user_id:
            parser.error("Для --live нужен --user-id")
        import bot
        ensure_held_out(bot.get_store(), args.user_id, cases)
        mem = bot.load_mem(args.user_id)
        candidates = {case["id"]: bot.ask_openai(bot.build_prompt(mem, [], case["context"]), bot.MAX_OUTPUT_TOKENS, 0.6) for case in cases}
    elif args.responses:
        values = json.loads(Path(args.responses).read_text(encoding="utf-8-sig"))
        candidates = {value["id"]: value["answer"] for value in values}
    else:
        parser.error("Укажи --responses для локальной оценки или --live для генерации")
    missing = [case["id"] for case in cases if case["id"] not in candidates]
    if missing:
        raise ValueError(f"Не хватает ответов: {', '.join(missing)}")
    result = {"note": "Длина и пересечение слов — вспомогательные показатели. Похожесть оценивает владелец.",
              "cases": [compare(case, candidates[case["id"]]) for case in cases]}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Подготовлено примеров для оценки: {len(cases)}. Результат: {output}")


if __name__ == "__main__":
    main()
