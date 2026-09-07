"""
RAG-агент для студии танцев Soul.

Использует:
- SQLite FTS5 для полнотекстового поиска по описаниям направлений
- OpenRouter (через openai SDK) для генерации ответа по найденному контексту
- knowledge.yaml как источник данных
"""

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация моделей OpenRouter (порядок = приоритет)
# Сначала бесплатные, потом дешёвые платные (fallback при недоступности)
# ---------------------------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Модель по умолчанию (можно переопределить переменной DEFAULT_MODEL в .env)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "meta-llama/llama-3.2-3b-instruct:free")

MODELS = [
    # Бесплатные
    "meta-llama/llama-3.2-3b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "google/gemini-2.0-flash-exp:free",
    # Платные, но дешёвые (fallback, если бесплатные недоступны)
    "openai/gpt-4o-mini",
    "anthropic/claude-3-haiku",
]

# Если переменная DEFAULT_MODEL задана и не в списке — добавляем её первой
if DEFAULT_MODEL and DEFAULT_MODEL not in MODELS:
    MODELS.insert(0, DEFAULT_MODEL)

# Путь к файлу с базой знаний
KNOWLEDGE_YAML = Path(__file__).parent / "knowledge.yaml"

# ---------------------------------------------------------------------------
# Пул клиентов OpenRouter (по одному на модель, для переключения при ошибках)
# ---------------------------------------------------------------------------
_clients = {
    model: OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    for model in MODELS
}

# Кэш последней успешно работавшей модели (для экономии переключений)
_last_working_model: Optional[str] = None


# ---------------------------------------------------------------------------
# Работа с FTS5
# ---------------------------------------------------------------------------
def get_fts_connection() -> sqlite3.Connection:
    """Возвращает соединение с БД clients.db (там же живёт FTS5-таблица)."""
    conn = sqlite3.connect("clients.db")
    conn.row_factory = sqlite3.Row
    return conn


def sync_knowledge():
    """
    Читает knowledge.yaml, создаёт/пересоздаёт FTS5-таблицу и наполняет её.
    Вызывается один раз при старте бота.
    """
    if not KNOWLEDGE_YAML.exists():
        logger.warning("Файл %s не найден, FTS5-таблица не создана.", KNOWLEDGE_YAML)
        return

    with open(KNOWLEDGE_YAML, "r", encoding="utf-8") as f:
        docs = yaml.safe_load(f)

    if not docs:
        logger.warning("knowledge.yaml пуст.")
        return

    with get_fts_connection() as conn:
        # Создаём виртуальную таблицу FTS5.
        # tokenize='unicode61' — поддержка кириллицы и юникод-токенизации.
        # id UNINDEXED — не участвует в полнотекстовом поиске, только в выводе.
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_knowledge USING fts5(
                id UNINDEXED,
                title,
                description,
                url,
                tokenize='unicode61'
            )
            """
        )
        # Очищаем старые данные (проще перезалить всё, чем мержить)
        conn.execute("DELETE FROM fts_knowledge")

        # Вставляем записи
        for doc in docs:
            conn.execute(
                """
                INSERT INTO fts_knowledge (id, title, description, url)
                VALUES (?, ?, ?, ?)
                """,
                (doc["id"], doc["title"], doc["description"], doc.get("url", "")),
            )

        logger.info("Загружено %d записей в FTS5-таблицу.", len(docs))


def _escape_fts_query(query: str) -> str:
    """
    Экранирует пользовательский запрос для безопасной вставки в FTS5 MATCH.

    Правила:
    - Фразы в кавычках '...' или "..." — сохраняются как фразы "..."
    - Логические операторы AND/OR/NOT/NEAR — сохраняются (регистр не важен)
    - Из остальных токенов вырезаются все спецсимволы, остаются буквы/цифры
    - Если запрос состоит только из операторов (AND OR NOT) — возвращается ''
      (чтобы не вызвать fts5 syntax error)
    - Одинарные кавычки дублируются для SQLite-строкового литерала
    """
    q = query.strip()
    if not q:
        return ""

    # Токенизируем: фразы в кавычках или отдельные символы
    tokens = re.findall(r"""'[^']*'|"[^"]*"|\S+""", q)
    cleaned = []

    for tok in tokens:
        # Фраза в одинарных кавычках -> двойные кавычки FTS5
        if tok.startswith("'") and tok.endswith("'"):
            phrase = re.sub(r"\s+", " ", tok[1:-1].replace('"', " ")).strip()
            if phrase:
                cleaned.append('"' + phrase + '"')
            continue

        # Фраза в двойных кавычках — сохраняем как есть (точное совпадение)
        if tok.startswith('"') and tok.endswith('"'):
            phrase = re.sub(r"\s+", " ", tok[1:-1]).strip()
            if phrase:
                cleaned.append('"' + phrase + '"')
            continue

        # Сохраняем логические операторы FTS5
        upper = tok.upper()
        if upper in ("AND", "OR", "NOT", "NEAR"):
            cleaned.append(upper)
            continue

        # Вырезаем всё, кроме букв/цифр/подчёркивания.
        # \w в Python 3 с юникодом включает и кириллицу.
        word = re.sub(r"[^\w]", "", tok)
        if word:
            cleaned.append(word)

    if not cleaned:
        return ""

    # Если весь запрос состоит только из операторов (AND/OR/NOT/NEAR) — FTS5
    # выдаст syntax error, поэтому возвращаем пустую строку.
    if all(t.upper() in ("AND", "OR", "NOT", "NEAR") for t in cleaned):
        return ""

    result = " ".join(cleaned)
    # Дублируем одинарные кавычки для SQLite-строкового литерала
    return result.replace("'", "''")


def _build_fts_query_fallback(word_tokens: list) -> str:
    """
    Строит OR-запрос из списка чистых слов (без операторов и стоп-слов).

    Используется как fallback, когда AND-запрос не дал результатов.
    """
    # Убираем операторы FTS5 и очень короткие слова (1 буква) из fallback
    FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
    words = [w for w in word_tokens if w.upper() not in FTS_OPERATORS and len(w) > 1]
    if not words:
        return ""
    # Каждое слово через OR, без кавычек (чтобы искало частично)
    return " OR ".join(words)


def search(query: str, limit: int = 3) -> list[dict]:
    """
    Полнотекстовый поиск по FTS5-таблице, устойчивый к спецсимволам.

    FTS5 не поддерживает параметр ? для MATCH, поэтому запрос экранируется
    и вставляется напрямую в SQL. Если точный (AND) поиск не дал результатов,
    автоматически делается повторный поиск по OR между словами.

    Возвращает список словарей: id, title, description, url, rank.
    rank — оценка релевантности (меньше = лучше).
    """
    safe_query = _escape_fts_query(query)
    if not safe_query:
        # Если после экранирования ничего не осталось (например, запрос
        # состоял только из операторов) — пробуем OR по словам
        words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9_]+", query)
        safe_query = _build_fts_query_fallback(words)
        if not safe_query:
            return []

    with get_fts_connection() as conn:
        # LIMIT — целое число, приводим через int() для безопасности
        sql = f"""
            SELECT id, title, description, url, rank
            FROM fts_knowledge
            WHERE fts_knowledge MATCH '{safe_query}'
            ORDER BY rank
            LIMIT {int(limit)}
        """
        rows = conn.execute(sql).fetchall()

    results = [dict(row) for row in rows]

    # Если AND-поиск ничего не дал — пробуем OR по отдельным словам
    if not results:
        words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9_]+", query)
        or_query = _build_fts_query_fallback(words)
        if or_query and or_query != safe_query:
            with get_fts_connection() as conn:
                sql = f"""
                    SELECT id, title, description, url, rank
                    FROM fts_knowledge
                    WHERE fts_knowledge MATCH '{or_query}'
                    ORDER BY rank
                    LIMIT {int(limit)}
                """
                rows = conn.execute(sql).fetchall()
            results = [dict(row) for row in rows]

    return results


# ---------------------------------------------------------------------------
# Генерация ответа через OpenRouter
# ---------------------------------------------------------------------------
def ask(question: str, user_id: Optional[int] = None) -> str:
    """
    Основная функция: принимает вопрос пользователя, ищет контекст в БД,
    отправляет промпт в OpenRouter и возвращает ответ.

    Если ничего не найдено — возвращает сообщение с контактами для связи.
    """
    # 1. Поиск контекста
    results = search(question, limit=3)

    # 2. Если ничего не нашли — вежливый ответ с контактами
    if not results:
        return _build_no_results_answer()

    # 3. Формируем контекст из найденных фрагментов
    context_parts = []
    for r in results:
        block = f"## {r['title']}\n{r['description']}"
        if r["url"]:
            block += f"\nСсылка: {r['url']}"
        context_parts.append(block)

    context = "\n\n---\n\n".join(context_parts)

    # 4. Промпт для LLM
    system_prompt = (
        "Ты — дружелюбный помощник студии танцев и йоги Soul. Отвечай на русском языке "
        "кратко, по делу и тепло. Используй только информацию из предоставленного контекста. "
        "Если в контексте нет ответа на вопрос — честно скажи, что не знаешь, "
        "и предложи связаться со студией по телефону или написать администратору."
    )

    user_prompt = (
        f"Вопрос пользователя: {question}\n\n"
        f"Контекст (информация о студии):\n{context}\n\n"
        "Дай понятный и полезный ответ на вопрос, опираясь на контекст."
    )

    # 5. Отправляем запрос в OpenRouter с перебором моделей
    answer = _call_llm(system_prompt, user_prompt)

    return answer


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """
    Вызывает LLM через OpenRouter, перебирая модели по приоритету (сначала
    кэшированная модель, затем бесплатные, затем дешёвые платные).

    Возвращает ответ LLM или сообщение об ошибке, если ни одна модель
    не сработала.
    """
    global _last_working_model

    if not OPENROUTER_API_KEY:
        return "❌ Не настроен API-ключ OpenRouter. Добавьте OPENROUTER_API_KEY в .env"

    # Строим список моделей для перебора: кэшированная первой, затем все
    models_to_try = []
    if _last_working_model and _last_working_model in MODELS:
        models_to_try.append(_last_working_model)
    for m in MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_error = ""
    for idx, model in enumerate(models_to_try):
        client = _clients.get(model)
        if not client:
            continue

        try:
            action = "печатает..."
            logger.info(
                "LLM-запрос: попытка %d/%d, модель %s",
                idx + 1,
                len(models_to_try),
                model,
            )

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=800,
                timeout=30,  # таймаут на запрос
                extra_headers={
                    "HTTP-Referer": "https://soul.by",
                    "X-Title": "Soul Dance Bot",
                },
            )

            # Успех — запоминаем модель
            _last_working_model = model
            logger.info("Модель %s успешно ответила", model)
            return response.choices[0].message.content.strip()

        except Exception as e:
            last_error = str(e)
            logger.warning(
                "Модель %s не сработала: %s. Пробуем следующую...",
                model,
                last_error,
            )
            continue

    # Все модели исчерпаны
    logger.error("Все модели OpenRouter недоступны. Последняя ошибка: %s", last_error)
    return (
        "😔 Извините, сейчас не удалось получить ответ от AI. "
        "Пожалуйста, попробуйте позже или свяжитесь с нами напрямую.\n"
        f"(Ошибка: {last_error})"
    )


def _build_contact_lines() -> list[str]:
    """
    Возвращает строки с контактной информацией студии.
    Ссылки без markdown-обёртки — только читаемые URL.
    """
    admin_id = os.getenv("ADMIN_CHAT_ID", "")
    phone1 = "+375 (29) 351 23 61"
    phone2 = "+375 (29) 751 23 61"

    lines = [
        "",
        "📞 **Позвонить:**",
        f"• {phone1} — tel:{phone1.replace(' ', '').replace('(', '').replace(')', '')}",
        f"• {phone2} — tel:{phone2.replace(' ', '').replace('(', '').replace(')', '')}",
        "",
        "💬 **Написать в Telegram:**",
    ]

    if admin_id:
        lines.append(f"• Написать администратору — tg://user?id={admin_id}")
        lines.append(
            f"• Позвонить в Telegram — tg://user?id={admin_id} (доступно в приложении)"
        )
    else:
        lines.append("• Напишите нам в личные сообщения — @soul_dance_studio")

    lines.extend(
        [
            "",
            "📍 **Адрес:** ул. Шорная 20, Минск",
            "• Зал 1 — Шорная 20-12Н — https://soul.by/zal1/",
            "• Зал 2 — Шорная 20-4Н — https://soul.by/zal2/",
        ]
    )

    return lines


def _build_contact_block() -> str:
    """Возвращает контактную информацию как единый блок текста."""
    return "\n".join(_build_contact_lines())


def _build_no_results_answer() -> str:
    """
    Формирует ответ, когда по запросу ничего не найдено в базе знаний.
    """
    lines = [
        "🤷‍♀️ Я не нашёл информацию по вашему вопросу в своей базе знаний.",
        "",
        "Но вы можете связаться со студией напрямую:",
    ]

    lines.extend(_build_contact_lines())

    lines.extend(
        [
            "",
            "Или просто задайте вопрос по-другому — я обязательно помогу! 🌟",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Пример использования в цикле вопрос-ответ (для отладки)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    # Инициализация БД
    sync_knowledge()

    print("🤖 RAG-агент студии Soul запущен. Введите 'exit' для выхода.\n")
    while True:
        q = input("Ваш вопрос: ").strip()
        if q.lower() in ("exit", "quit"):
            break
        if not q:
            continue
        answer = ask(q)
        print(f"\n{answer}\n{'-' * 60}\n")
