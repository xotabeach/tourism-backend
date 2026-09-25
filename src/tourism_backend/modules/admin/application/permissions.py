"""Effective admin permissions, independent of cookie-stored roles."""

from __future__ import annotations

from collections.abc import Iterable

# Stable keys are stored in the database and used for both pages and actions.
PERMISSION_LABELS = {
    "support.read": "Поддержка: просмотр",
    "support.write": "Поддержка: ответы и изменения",
    "users.brief": "Поддержка: краткие сведения о пользователе",
    "users.read": "Пользователи: просмотр",
    "users.write": "Пользователи: изменения",
    "routes.read": "Маршруты: просмотр",
    "routes.write": "Маршруты: изменения",
    "places.read": "Места: просмотр",
    "places.write": "Места: изменения",
    "geography.read": "География: просмотр",
    "geography.write": "География: изменения",
    "transit.read": "Транспорт: просмотр",
    "transit.write": "Транспорт: изменения",
    "reviews.read": "Отзывы: просмотр",
    "reviews.write": "Отзывы: модерация",
    "recommendations.read": "Рекомендации: просмотр",
    "recommendations.write": "Рекомендации: изменения",
    "content.read": "Контент: просмотр",
    "content.write": "Контент: изменения",
    "media.read": "Медиа: просмотр",
    "media.write": "Медиа: изменения",
    "moderation_route.read": "Жалобы на маршруты и места: просмотр",
    "moderation_route.write": "Жалобы на маршруты и места: модерация",
    "moderation_content.read": "Жалобы на статьи и комментарии: просмотр",
    "moderation_content.write": "Жалобы на статьи и комментарии: модерация",
    "moderation.read": "Жалобы: открыть очередь",
    "moderation.write": "Жалобы: выполнять действия",
    "achievements.read": "Достижения: просмотр",
    "achievements.write": "Достижения: изменения",
    "notifications.read": "Уведомления: просмотр",
    "notifications.write": "Уведомления: изменения",
    "antifraud.read": "Антифрод: просмотр",
    "antifraud.write": "Антифрод: действия",
    "antifraud.trust": "Антифрод: доверенный пользователь",
    "statistics.read": "Статистика: просмотр",
    "settings.read": "Настройки: просмотр",
    "settings.write": "Настройки: изменения",
    "access.read": "Доступ: просмотр",
    "access.manage": "Доступ: управление",
}


def effective_permissions(
    roles: Iterable[str],
    role_grants: Iterable[str],
    overrides: Iterable[tuple[str, str]],
) -> frozenset[str]:
    """Combine role grants with individual allow/deny; deny always wins."""
    if "admin" in roles:
        return frozenset(PERMISSION_LABELS)
    granted = set(role_grants) & PERMISSION_LABELS.keys()
    denied: set[str] = set()
    for permission, effect in overrides:
        if permission not in PERMISSION_LABELS:
            continue
        if effect == "allow":
            granted.add(permission)
        elif effect == "deny":
            denied.add(permission)
    return frozenset(granted - denied)
