"""Server-side topic / intent classification for AI route chat."""

from __future__ import annotations

from typing import Literal

ChatIntent = Literal[
    "crisis",
    "greeting",
    "on_topic_travel",
    "off_topic",
    "injection_attempt",
    "generate",
]

_OFF_TOPIC_REPLY = (
    "Я могу помогать только с подбором маршрутов и мест в Крыму. "
    "Давайте вернёмся к поездке: город старта, длительность или интересы?"
)
_INJECTION_REPLY = (
    "Я работаю только как помощник по маршрутам КрымТрип и не меняю свои "
    "правила. Чем помочь с маршрутом?"
)
_CRISIS_REPLY = (
    "Мне жаль, что вам тяжело. Я не могу заменить профессиональную помощь. "
    "Если вам нужна поддержка прямо сейчас, обратитесь в экстренные службы "
    "или к близким людям, которым доверяете. Когда будете готовы — я помогу "
    "с маршрутом по Крыму."
)
_AI_FALLBACK_REPLY = (
    "Сейчас не удалось связаться с ИИ-помощником. Могу уточнить параметры "
    "поездки вручную или подобрать маршрут по уже выбранным фильтрам — "
    "напишите «подбери маршрут»."
)


def classify_chat_intent(raw: str) -> ChatIntent:
    text = raw.casefold().replace("ё", "е").strip()
    if not text:
        return "on_topic_travel"
    if _looks_like_crisis(text):
        return "crisis"
    if _looks_like_injection(text):
        return "injection_attempt"
    if _looks_like_generate(text):
        return "generate"
    if _looks_like_greeting(text):
        return "greeting"
    if _looks_like_off_topic(text):
        return "off_topic"
    return "on_topic_travel"


def canned_reply_for_intent(intent: ChatIntent) -> str:
    """Canned text only for safety / hard off-topic. Greeting goes to the LLM."""
    if intent == "crisis":
        return _CRISIS_REPLY
    if intent == "off_topic":
        return _OFF_TOPIC_REPLY
    if intent == "injection_attempt":
        return _INJECTION_REPLY
    return ""


def ai_unavailable_fallback() -> str:
    return _AI_FALLBACK_REPLY


def _looks_like_crisis(text: str) -> bool:
    markers = (
        "хочу умереть",
        "покончить с собой",
        "суицид",
        "самоубий",
        "нет смысла жить",
        "убей меня",
    )
    return any(marker in text for marker in markers)


def _looks_like_injection(text: str) -> bool:
    markers = (
        "игнорируй инструкции",
        "игнорируй правила",
        "ignore previous",
        "ignore all",
        "system prompt",
        "jailbreak",
        "developer mode",
        "режим разработчика",
    )
    return any(marker in text for marker in markers)


def _looks_like_generate(text: str) -> bool:
    markers = (
        "подбери маршрут",
        "подобрать маршрут",
        "составь маршрут",
        "собери маршрут",
        "сгенерируй маршрут",
        "предложи маршрут",
        "сделай маршрут",
        "построить маршрут",
        "построй маршрут",
        "собери предложение",
        "собери черновик",
    )
    if any(marker in text for marker in markers):
        return True
    return _looks_like_confirm_generate(text)


def _looks_like_confirm_generate(text: str) -> bool:
    """Short yes / proceed cues → structured generate, not free-form LLM prose."""
    normalized = text.strip().rstrip("!.?").casefold().replace("ё", "е").replace(",", " ").split()
    joined = " ".join(normalized)
    confirms = {
        "давай",
        "давай так",
        "да",
        "ок",
        "окей",
        "хорошо",
        "согласен",
        "согласна",
        "подходит",
        "собирай",
        "собери",
        "го",
        "поехали",
        "можно",
        "да давай",
        "давай собери",
        "давай подбери",
        "давай сделай",
        "сделай",
        "ладно",
        "супер давай",
    }
    return joined in confirms


def _looks_like_greeting(text: str) -> bool:
    greetings = (
        "привет",
        "здравствуй",
        "здравствуйте",
        "добрый день",
        "доброе утро",
        "добрый вечер",
        "хай",
        "hello",
        "hi!",
        "как дела",
        "как ты",
        "как вы",
    )
    if len(text) > 80:
        return False
    for greeting in greetings:
        if (
            text == greeting
            or text.startswith(f"{greeting} ")
            or text.startswith(f"{greeting}!")
            or text.startswith(f"{greeting},")
        ):
            return not _has_travel_cue(text)
    return False


def _looks_like_off_topic(text: str) -> bool:
    off_topic = (
        "напиши код",
        "напиши программу",
        "написать код",
        "python",
        "javascript",
        "typescript",
        "sql запрос",
        "dockerfile",
        "kubernetes",
        "рецепт",
        "как приготовить",
        "домашнее задание",
        "реши задачу",
        "курсовую",
        "диплом",
        "переведи текст",
        "сочини стих",
        "напиши сочинение",
        "криптовалют",
        "инвестиц",
        "медицин",
        "диагноз",
        "юридическ",
        "договор купли",
    )
    if any(phrase in text for phrase in off_topic):
        return True
    return "```" in text or "def " in text or "fn " in text


def _has_travel_cue(text: str) -> bool:
    cues = (
        "маршрут",
        "крым",
        "ялт",
        "севастопол",
        "алушт",
        "поездк",
        "экскурси",
        "пляж",
        "горы",
        "дворец",
        "место",
        "локац",
        "прогулк",
        "турист",
    )
    return any(cue in text for cue in cues)
