"""The achievement catalogue's rules; thresholds are server-owned."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    slug: str
    title: str
    how_to_earn: str
    triggers: frozenset[str]
    target: float = 1
    counted: bool = False
    soon: bool = False


def _rule(
    slug: str,
    title: str,
    text: str,
    triggers: str,
    target: float = 1,
    *,
    counted: bool = False,
    soon: bool = False,
) -> Rule:
    return Rule(slug, title, text, frozenset(triggers.split()), target, counted, soon)


RULES = {
    rule.slug: rule
    for rule in (
        _rule("marathoner", "Марафонец", "Пройти 48 км за 7 суток", "completion", 48, counted=True),
        _rule("same-way", "Ты норм?", "Дважды пройти один и тот же маршрут", "completion"),
        _rule(
            "berlin", "Ура Советам", "Пройти суммарно 2 000 км", "completion", 2000, counted=True
        ),
        _rule(
            "sunrise",
            "Ранняя пташка",
            "Отметить точку рядом с собой в первый час после восхода",
            "stop",
        ),
        _rule(
            "water",
            "К воде",
            "Пройти 3 маршрута с точкой категории «Пляж»",
            "completion",
            3,
            counted=True,
        ),
        _rule(
            "caves",
            "Подземный гость",
            "Отметить точку у Чуфут-Кале, Мангупа, Эски-Кермена, "
            "Тепе-Кермена или Баклы, находясь рядом",
            "stop",
        ),
        _rule(
            "photo",
            "Кадр дня",
            "Опубликовать 10 фото в своих отзывах о местах",
            "review",
            10,
            counted=True,
        ),
        _rule(
            "night",
            "Ночной дозор",
            "Завершить маршрут после заката у последней отмеченной точки",
            "completion",
        ),
        _rule("group", "Компания", "Совместные прохождения появятся позже", "", soon=True),
        _rule(
            "season", "Все сезоны", "Пройти маршруты зимой и летом", "completion", 2, counted=True
        ),
        _rule(
            "local",
            "Местный",
            "Отметить рядом с собой 20 разных мест в завершённых маршрутах",
            "stop completion",
            20,
            counted=True,
        ),
        _rule("guide", "Свой гид", "Учёт прослушивания аудиогидов появится позже", "", soon=True),
        _rule("distance", "Сто км", "Пройти суммарно 100 км", "completion", 100, counted=True),
        _rule(
            "favorite",
            "Коллекционер",
            "Сохранить одновременно 15 маршрутов в избранном",
            "favorite",
            15,
            counted=True,
        ),
        _rule(
            "review",
            "Отзывчивый",
            "Опубликовать 5 отзывов о маршрутах и местах",
            "review",
            5,
            counted=True,
        ),
        _rule("swallow", "У гнезда", "Отметить точку у Ласточкина гнезда, находясь рядом", "stop"),
        _rule("fiolent", "На краю", "Отметить точку у мыса Фиолент, находясь рядом", "stop"),
        _rule("ai-petri", "Выше облаков", "Отметить точку у Ай-Петри, находясь рядом", "stop"),
        _rule("first-step", "Первые шаги", "Завершить первое прохождение маршрута", "completion"),
        _rule(
            "social",
            "Душа компании",
            "Подписаться на 10 путешественников",
            "follow",
            10,
            counted=True,
        ),
        _rule(
            "author",
            "Автор тропы",
            "Опубликовать свой общедоступный маршрут после модерации",
            "route",
        ),
        _rule(
            "bakhchisaray", "Ханский гость", "Отметить место в Бахчисарае, находясь рядом", "stop"
        ),
        _rule("winter", "Зимний Крым", "Завершить маршрут в январе", "completion"),
        _rule(
            "sea-breeze",
            "Морской бриз",
            "Пройти 5 маршрутов с точкой категории «Пляж»",
            "completion",
            5,
            counted=True,
        ),
        _rule(
            "photographer",
            "Летописец",
            "Опубликовать свой маршрут с собственной загруженной обложкой",
            "route",
        ),
        _rule(
            "yalta-lights",
            "Огни Ялты",
            "Завершить маршрут после 18:00, отметив последнюю точку рядом с собой в Ялте",
            "completion",
        ),
        _rule(
            "legend-path",
            "По следам легенд",
            "Пройти маршрут сложности «очень сложный»",
            "completion",
        ),
        _rule(
            "new-svet",
            "Новый Свет",
            "Отметить точку у Тропы Голицына или Царского пляжа, находясь рядом",
            "stop",
        ),
        _rule("veteran", "Бывалый", "Завершить 10 прохождений", "completion", 10, counted=True),
        _rule("pen", "Перо", "Опубликовать свою статью", "article"),
        _rule(
            "people-author",
            "Народный автор",
            "Получить суммарно 50 лайков на своих опубликованных статьях",
            "article_like article",
            50,
            counted=True,
        ),
        _rule(
            "navigator", "Штурман", "Пройти маршрут, собранный ИИ-помощником для вас", "completion"
        ),
    )
}
