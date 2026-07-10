"""
Security — API Key + HMAC проверка запросов.

Поддерживает два режима аутентификации:
  1. API Key (заголовок X-API-Key)
  2. HMAC подпись (заголовок X-Signature + X-Timestamp)

Ключи читаются из .env или переменных окружения.
"""
import os
import hmac
import hashlib
import time


# Эндпоинты, которые НЕ требуют авторизации
PUBLIC_PATHS = {"/", "/docs", "/redoc", "/openapi.json", "/health"}


def get_api_keys() -> list:
    """Список валидных API-ключей из окружения."""
    keys = []
    env_key = os.getenv("AI_RUNTIME_API_KEY", "")
    if env_key:
        keys.extend([k.strip() for k in env_key.split(",") if k.strip()])
    return keys


def get_hmac_secret() -> str:
    """Секрет для HMAC-подписи."""
    return os.getenv("AI_RUNTIME_HMAC_SECRET", "")


def check_api_key(provided_key: str) -> bool:
    """Проверяет API-ключ (constant-time)."""
    valid_keys = get_api_keys()
    if not valid_keys:
        # ключи не настроены — пропускаем (dev-режим)
        return True
    if not provided_key:
        return False
    for valid in valid_keys:
        if hmac.compare_digest(provided_key, valid):
            return True
    return False


def check_hmac(timestamp: str, signature: str, body: bytes) -> bool:
    """
    Проверяет HMAC-SHA256 подпись тела запроса.
    Формат подписи: HMAC-SHA256(secret, timestamp + body)
    """
    secret = get_hmac_secret()
    if not secret:
        return True  # не настроено — пропускаем

    if not timestamp or not signature:
        return False

    # защита от replay (окно 5 минут)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except ValueError:
        return False

    expected = hmac.new(
        secret.encode(),
        (timestamp + body.decode("utf-8", errors="replace")).encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def is_public_path(path: str) -> bool:
    """Нужно ли проверять авторизацию для этого пути."""
    return path in PUBLIC_PATHS
