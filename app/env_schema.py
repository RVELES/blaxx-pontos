"""Validador de env vars no boot — fail-fast com mensagens claras.

Filosofia: se o operador errou uma env (placeholder, formato, valor inválido),
queremos que o backend RECUSE subir já no boot, com mensagem dizendo EXATAMENTE
qual var está errada e por quê. A alternativa (boot OK, crash em runtime no
primeiro request) é o pior dos mundos — foi o que aconteceu no deploy
2026-06-24 (DSN placeholder passou o guard simples e quebrou em runtime).

Como usar (em app/__init__.py):
    from .env_schema import validate_env
    validate_env()  # raise EnvError em produção / só warn em dev

Cada entrada tem: name, required (bool), validator (callable), description.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable


class EnvError(RuntimeError):
    """Erro de validação de env. Mensagem deve ser acionável (diz como corrigir)."""


# ---------------------------------------------------------------------------- #
# Validators reutilizáveis                                                     #
# ---------------------------------------------------------------------------- #
def _looks_like_url(value: str, *, allowed_schemes=("https", "http")) -> bool:
    if not value:
        return False
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://", value)
    return bool(m) and m.group(1).lower() in allowed_schemes


def is_sentry_dsn(value: str) -> tuple[bool, str]:
    """DSN do Sentry tem formato https://KEY@host.ingest.sentry.io/PROJECT_ID."""
    if not _looks_like_url(value, allowed_schemes=("https",)):
        return False, "precisa começar com https://"
    if "@" not in value:
        return False, "precisa conter '@' (separador key/host) — placeholder?"
    if not re.search(r"/\d+/?$", value):
        return False, "precisa terminar com /PROJECT_ID (número)"
    return True, ""


def is_url(value: str) -> tuple[bool, str]:
    if not _looks_like_url(value):
        return False, "precisa ser uma URL http(s)://"
    return True, ""


def is_secret_min_32(value: str) -> tuple[bool, str]:
    if len(value) < 32:
        return False, f"precisa ter ≥32 chars (atual: {len(value)})"
    if value == "dev-only-change-me":
        return False, "ainda é o placeholder de dev — gerar com `openssl rand -hex 32`"
    return True, ""


def is_in(*allowed: str) -> Callable[[str], tuple[bool, str]]:
    def _check(value: str) -> tuple[bool, str]:
        if value not in allowed:
            return False, f"valor deve ser um de: {', '.join(allowed)}"
        return True, ""
    return _check


def is_int_in_range(lo: int, hi: int) -> Callable[[str], tuple[bool, str]]:
    def _check(value: str) -> tuple[bool, str]:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return False, f"precisa ser inteiro entre {lo}..{hi}"
        if not (lo <= n <= hi):
            return False, f"fora do range {lo}..{hi} (recebido: {n})"
        return True, ""
    return _check


# ---------------------------------------------------------------------------- #
# Schema — único lugar pra adicionar nova env                                  #
# ---------------------------------------------------------------------------- #
# Tipo: (name, required_in_prod, validator, descrição-curta-pra-erro)
SCHEMA: list[tuple[str, bool, Callable[[str], tuple[bool, str]], str]] = [
    # Secrets obrigatórios em prod (sem default seguro)
    ("SECRET_KEY",      True,  is_secret_min_32, "chave do Flask (sessions, signing)"),
    ("JWT_SECRET_KEY",  True,  is_secret_min_32, "chave dos JWT access/refresh"),

    # Defaults seguros existem; só validamos formato se OPERADOR setou algo
    ("SENTRY_DSN",      False, is_sentry_dsn,    "DSN do Sentry — observability"),
    ("FRONTEND_URL",    False, is_url,           "base URL do SPA pra emails"),
    ("BLAXX_BACKEND_URL", False, is_url,         "override do backend URL"),

    # Booleans/enums com valores aceitos
    ("MAILER",          False, is_in("console", "resend", "noop"),
     "noop|console|resend"),
    ("PIX_PROVIDER",    False, is_in("mock", "mercadopago"),
     "mock|mercadopago — usar 'mock' em homologação"),
    ("SMS_BACKEND",     False, is_in("console", "twilio"),
     "console|twilio"),

    # Numéricos com range razoável
    ("BLAXX_JWT_ACCESS_MIN", False, is_int_in_range(5, 1440),
     "min de TTL do access token (5..1440)"),
]


# ---------------------------------------------------------------------------- #
# API pública                                                                  #
# ---------------------------------------------------------------------------- #
def validate_env(*, strict: bool | None = None) -> list[str]:
    """Valida todas as envs do SCHEMA. Retorna lista de problemas detectados.

    `strict=True` → levanta EnvError no primeiro problema crítico (default em prod).
    `strict=False` → só retorna a lista, não levanta.
    `strict=None` (default) → strict em prod (FLASK_ENV != development/test).

    Em prod, env vars MAL setadas (placeholder) costumam ser PIORES que ausentes.
    A regra é: se o valor NÃO está vazio, ele precisa passar o validator.
    """
    if strict is None:
        strict = os.environ.get("FLASK_ENV", "production").lower() not in ("development", "test")

    issues: list[str] = []
    for name, required, validator, desc in SCHEMA:
        value = os.environ.get(name, "").strip()
        if not value:
            if required and strict:
                issues.append(f"[{name}] obrigatório em produção — {desc}")
            continue
        ok, hint = validator(value)
        if not ok:
            # Truncar value pra não vazar segredo se for SECRET_KEY com 32+
            shown = value if len(value) < 30 else value[:12] + "…" + value[-4:]
            issues.append(f"[{name}={shown!r}] inválido — {hint}")

    if issues and strict:
        msg = "Env vars inválidas (recusando subir):\n  · " + "\n  · ".join(issues)
        # Imprime no stderr pra Render mostrar no Events tab
        print(msg, file=sys.stderr)
        raise EnvError(msg)

    return issues
