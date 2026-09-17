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


def is_stripe_secret(value: str) -> tuple[bool, str]:
    """Chave secreta da Stripe: sk_live_… (produção) ou sk_test_… (teste)."""
    if not value.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
        return False, "deve começar com sk_live_ (produção) ou sk_test_ (teste)"
    if len(value) < 20:
        return False, "parece truncada"
    return True, ""


def is_stripe_webhook_secret(value: str) -> tuple[bool, str]:
    if not value.startswith("whsec_"):
        return False, "deve começar com whsec_ (Stripe → Webhooks → signing secret)"
    return True, ""


def is_stripe_publishable(value: str) -> tuple[bool, str]:
    """Chave PUBLICÁVEL da Stripe (pk_…). Pública por design.

    Validar o prefixo evita o erro clássico de colar a SECRET key aqui — o que
    a exporia no JavaScript do browser.
    """
    if value.startswith(("sk_", "rk_")):
        return False, "isto é uma chave SECRETA — nunca use no frontend. Use pk_…"
    if not value.startswith("pk_"):
        return False, "deve começar com pk_live_ (produção) ou pk_test_ (teste)"
    return True, ""


def is_uuid(value: str) -> tuple[bool, str]:
    """UUID (8-4-4-4-12 hex): formato do client_id do C6."""
    import re

    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return True, ""
    return False, "deve ser um UUID (ex.: 00000000-0000-4000-8000-000000000000)"


def is_asaas_key(value: str) -> tuple[bool, str]:
    """API key do Asaas: `$aact_prod_…` (produção) ou `$aact_hmlg_…` (sandbox).

    Validar o prefixo evita o pior erro operacional possível aqui: subir a
    chave de sandbox em produção e "pagar" resgates que nunca saem.
    """
    if not value.startswith("$aact_"):
        return False, "deve começar com $aact_prod_ (produção) ou $aact_hmlg_ (sandbox)"
    if len(value) < 40:
        return False, "parece truncada (chave do Asaas é longa)"
    return True, ""


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
    ("PIX_PROVIDER",    False, is_in("mock", "asaas", "c6"),
     "c6 | asaas (produção) | mock (homologação)"),
    ("SMS_BACKEND",     False, is_in("console", "twilio"),
     "console|twilio"),

    # Cartão (Stripe) e PIX (Asaas) — ver blocos abaixo.
    ("CARD_ENABLED",     False, is_in("0", "1"),
     "0|1 — liga o checkout com cartão de crédito"),
    ("BLAXX_HOMOLOGACAO", False, is_in("0", "1"),
     "0|1 — declara ambiente de avaliação (libera chave Stripe de teste). "
     "REMOVER antes do go-live"),
    ("CARD_MAX_INSTALLMENTS", False, is_int_in_range(1, 12),
     "parcelas máximas no cartão (1..12)"),
    ("PAYOUT_MODE",      False, is_in("auto", "manual"),
     "auto|manual — manual = fila admin enquanto não há provider de payout"),

    # Asaas — PIX de saída (resgate). Obrigatoriedade condicional em
    # validate_env(): com PAYOUT_MODE=auto, a key e o token de webhook viram
    # obrigatórios em produção.
    ("ASAAS_API_KEY",    False, is_asaas_key,
     "API key do Asaas ($aact_prod_… em produção, $aact_hmlg_… em sandbox)"),
    ("ASAAS_ENV",        False, is_in("sandbox", "production"),
     "sandbox|production — 'sandbox' NÃO envia PIX de verdade"),
    ("ASAAS_WEBHOOK_TOKEN", False, is_secret_min_32,
     "token do header asaas-access-token (32+ chars) — autentica o webhook"),

    # C6 Bank — PIX de entrada (cobrança). Obrigatoriedade condicional em
    # validate_env(): com PIX_PROVIDER=c6, credenciais, chave, certificado e
    # token de webhook viram obrigatórios em produção.
    ("C6_CLIENT_ID",     False, is_uuid,
     "client_id OAuth2 do C6 (UUID)"),
    ("C6_ENV",           False, is_in("sandbox", "production"),
     "sandbox|production — 'sandbox' NÃO recebe dinheiro de verdade"),
    ("C6_WEBHOOK_TOKEN", False, is_secret_min_32,
     "segredo no caminho da URL do webhook (32+ chars)"),

    # Stripe — cartão internacional.
    ("STRIPE_API_KEY",   False, is_stripe_secret,
     "chave secreta da Stripe (sk_live_… em produção)"),
    ("STRIPE_WEBHOOK_SECRET", False, is_stripe_webhook_secret,
     "signing secret do webhook (whsec_…) — a Stripe assina o corpo"),
    ("STRIPE_PUBLISHABLE_KEY", False, is_stripe_publishable,
     "chave publicável (pk_live_… em produção) — usada pelo Elements no browser"),

    # Numéricos com range razoável
    ("BLAXX_JWT_ACCESS_MIN", False, is_int_in_range(5, 1440),
     "min de TTL do access token (5..1440)"),
]


# ---------------------------------------------------------------------------- #
# Modo homologação                                                             #
# ---------------------------------------------------------------------------- #
# `BLAXX_HOMOLOGACAO=1` declara que este ambiente, embora rode com
# FLASK_ENV=production, é de AVALIAÇÃO — sem clientes reais e sem dinheiro de
# verdade. Serve ao período em que o produto ainda não foi a mercado e a equipe
# testa contra o deploy real.
#
# Por que uma flag e não afrouxar FLASK_ENV: mexer em FLASK_ENV desligaria o
# strict inteiro do validate_env, e o app subiria sem os segredos que ele
# deveria exigir. A flag é cirúrgica — libera exatamente uma checagem (chave
# Stripe de teste) e, em troca, torna BARULHENTO tudo o que não é real.
#
# ⚠️ Antes do go-live: remover esta variável do Render. Sem ela, chave de teste
# volta a derrubar o boot, que é o comportamento correto com clientes reais.
def _homologacao() -> bool:
    return os.environ.get("BLAXX_HOMOLOGACAO", "").strip() == "1"


def _avisar_homologacao(mensagem: str) -> None:
    print(f"[env_schema] HOMOLOGAÇÃO · {mensagem}", file=sys.stderr, flush=True)


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

    # ---- Obrigatoriedade condicional (só em prod/strict) ----
    if strict:
        # PIX é Asaas: sem a key não há como cobrar, sem o token de webhook
        # nenhuma compra credita pontos.
        if os.environ.get("PIX_PROVIDER", "").strip().lower() == "asaas":
            if not os.environ.get("ASAAS_API_KEY", "").strip():
                issues.append(
                    "[ASAAS_API_KEY] obrigatório com PIX_PROVIDER=asaas"
                )
            if not os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip():
                issues.append(
                    "[ASAAS_WEBHOOK_TOKEN] obrigatório com PIX_PROVIDER=asaas — sem ele "
                    "o webhook é rejeitado e nenhuma compra credita pontos"
                )

        # PIX é C6: sem credencial/chave/certificado não há como cobrar; sem o
        # token de webhook nenhuma compra credita pontos.
        if os.environ.get("PIX_PROVIDER", "").strip().lower() == "c6":
            for var in ("C6_CLIENT_ID", "C6_CLIENT_SECRET", "C6_PIX_KEY", "C6_WEBHOOK_TOKEN"):
                if not os.environ.get(var, "").strip():
                    issues.append(f"[{var}] obrigatório com PIX_PROVIDER=c6")
            has_files = bool(os.environ.get("C6_CERT_PATH", "").strip()
                             and os.environ.get("C6_KEY_PATH", "").strip())
            has_pem = bool(os.environ.get("C6_CERT_PEM", "").strip()
                           and os.environ.get("C6_KEY_PEM", "").strip())
            if not (has_files or has_pem):
                issues.append(
                    "[C6_CERT_PATH+C6_KEY_PATH ou C6_CERT_PEM+C6_KEY_PEM] obrigatório "
                    "com PIX_PROVIDER=c6: a API do C6 exige mTLS"
                )
            if os.environ.get("C6_ENV", "sandbox").strip().lower() != "production":
                if _homologacao():
                    _avisar_homologacao(
                        "C6_ENV é SANDBOX: cobrança PIX simulada credita pontos "
                        "reais na base e nenhum dinheiro entra de verdade."
                    )
                else:
                    issues.append(
                        "[C6_ENV] precisa ser 'production' em produção; em sandbox "
                        "nenhuma cobrança recebe dinheiro real. Se este ambiente é de "
                        "homologação, declare com BLAXX_HOMOLOGACAO=1."
                    )

        # Payout automático exige o provider de saída configurado por inteiro.
        if os.environ.get("PAYOUT_MODE", "").strip().lower() == "auto":
            if not os.environ.get("ASAAS_API_KEY", "").strip():
                issues.append(
                    "[ASAAS_API_KEY] obrigatório com PAYOUT_MODE=auto — sem provider "
                    "de saída o resgate não tem como pagar o usuário"
                )
            if os.environ.get("ASAAS_ENV", "sandbox").strip().lower() != "production":
                issues.append(
                    "[ASAAS_ENV] precisa ser 'production' com PAYOUT_MODE=auto — em "
                    "sandbox o PIX do resgate NÃO é enviado de verdade"
                )

        # Stripe configurado exige o webhook secret e chave live em produção.
        if os.environ.get("STRIPE_API_KEY", "").strip():
            if not os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip():
                issues.append(
                    "[STRIPE_WEBHOOK_SECRET] obrigatório com STRIPE_API_KEY — sem ele "
                    "o webhook é rejeitado e nenhuma compra no cartão credita"
                )
            if not os.environ.get("STRIPE_API_KEY", "").startswith("sk_live_"):
                if _homologacao():
                    # Exceção deliberada: BLAXX_HOMOLOGACAO=1 declara que este
                    # ambiente "de produção" é de avaliação, sem clientes reais.
                    # O produto ainda não foi a mercado e a equipe testa aqui.
                    # A trava continua valendo por padrão — sem a flag, chave de
                    # teste segue derrubando o boot.
                    _avisar_homologacao(
                        "STRIPE_API_KEY não é sk_live_ — nenhuma cobrança de cartão "
                        "movimenta dinheiro de verdade."
                    )
                else:
                    issues.append(
                        "[STRIPE_API_KEY] em produção precisa ser sk_live_… — "
                        "sk_test_ não cobra de verdade. Se este ambiente é de "
                        "homologação, declare com BLAXX_HOMOLOGACAO=1."
                    )

        # O Asaas não tem trava equivalente: `is_asaas_key` aceita $aact_prod_ e
        # $aact_hmlg_, e ASAAS_ENV só é exigido com PAYOUT_MODE=auto. Ou seja,
        # chave de sandbox sobe calada. Com a flag ligada, ao menos o boot diz
        # em voz alta o que está acontecendo.
        if _homologacao():
            if os.environ.get("ASAAS_API_KEY", "").startswith("$aact_hmlg_"):
                _avisar_homologacao(
                    "ASAAS_API_KEY é de SANDBOX — cobrança PIX simulada credita "
                    "pontos reais na base, e resgate nenhum sai de verdade."
                )
            if os.environ.get("ENABLE_DEV_ENDPOINTS", "").strip() == "1":
                _avisar_homologacao(
                    "ENABLE_DEV_ENDPOINTS=1 — endpoints de desenvolvimento "
                    "expostos (ex.: verificar e-mail sem receber e-mail)."
                )

        # Cartão agora é Stripe (tem Elements → PAN não toca o backend).
        if os.environ.get("CARD_ENABLED", "").strip() == "1":
            for dep, why in (
                ("STRIPE_API_KEY", "processar o pagamento com cartão"),
                ("STRIPE_PUBLISHABLE_KEY", "o Elements tokenizar no browser"),
                ("STRIPE_WEBHOOK_SECRET", "confirmar o pagamento via webhook assinado"),
            ):
                if not os.environ.get(dep, "").strip():
                    issues.append(f"[{dep}] obrigatório com CARD_ENABLED=1 — {why}")

    if issues and strict:
        msg = "Env vars inválidas (recusando subir):\n  · " + "\n  · ".join(issues)
        # Imprime no stderr pra Render mostrar no Events tab
        print(msg, file=sys.stderr)
        raise EnvError(msg)

    return issues
