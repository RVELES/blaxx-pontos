"""Testes do Google Sign-In · /auth/google.

Cobertura (16 testes obrigatórios):
  Validação criptográfica:
    1. id_token ausente → 400
    2. id_token inválido (assinatura quebrada) → 401
    3. id_token expirado → 401
    4. id_token com audience errado → 401
    5. id_token com issuer errado → 401
    6. email_verified=false do Google → 401
  Anti-replay (nonce):
    7. cliente envia nonce, payload tem o mesmo → 200
    8. cliente envia nonce, payload tem outro → 401
    9. cliente envia nonce, payload sem nonce → 401
    10. cliente não envia nonce (legacy) → 200 (compat)
  Flow de conta:
    11. primeiro login: cria User + Wallet + SocialAccount
    12. segundo login mesmo sub: NÃO duplica User, atualiza SocialAccount
    13. login por email pré-existente: faz link e seta google_sub
    14. avatar (picture) é gravado no User e SocialAccount
  Configuração:
    15. config sem GOOGLE_*_CLIENT_ID → 503
    16. audit log é gravado em sucesso E em falha

Roda com:
    cd backend && pytest -v tests/test_google_oauth.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")
os.environ.setdefault("GOOGLE_WEB_CLIENT_ID", "test-web-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_IOS_CLIENT_ID", "test-ios-client.apps.googleusercontent.com")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User, Wallet, SocialAccount, AuditLog


@pytest.fixture
def app():
    a = create_app(TestConfig)
    a.config["GOOGLE_WEB_CLIENT_ID"] = "test-web-client.apps.googleusercontent.com"
    a.config["GOOGLE_IOS_CLIENT_ID"] = "test-ios-client.apps.googleusercontent.com"
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _good_payload(**overrides):
    """Payload válido típico do Google ID token (já validado)."""
    base = {
        "iss": "https://accounts.google.com",
        "aud": "test-web-client.apps.googleusercontent.com",
        "azp": "test-web-client.apps.googleusercontent.com",
        "sub": "1234567890",
        "email": "joao@gmail.com",
        "email_verified": True,
        "name": "João Silva",
        "given_name": "João",
        "family_name": "Silva",
        "picture": "https://lh3.googleusercontent.com/a/abc=s96-c",
        "iat": int((datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    base.update(overrides)
    return base


def _patch_verify(monkeypatch, payload=None, raises=None):
    """Patcha google.oauth2.id_token.verify_oauth2_token pra devolver
    o payload (válido) ou levantar ValueError (inválido)."""
    import app.api.auth as auth_mod  # noqa: F401  (garante import)

    def fake_verify(token, request, aud):
        if raises:
            raise raises
        if payload is None:
            raise ValueError("no payload")
        # Simula validação de audience pela lib do Google.
        if payload.get("aud") != aud:
            raise ValueError("audience mismatch")
        return payload

    # Patch em ambos caminhos possíveis (importado tardiamente dentro da view).
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        fake_verify,
        raising=False,
    )


# ============================================================
# 1. id_token ausente → 400
# ============================================================
def test_01_missing_id_token_returns_400(client):
    r = client.post("/auth/google", json={})
    assert r.status_code == 400
    assert "id_token" in r.json["error"].lower()


# ============================================================
# 2. id_token inválido (assinatura quebrada) → 401
# ============================================================
def test_02_invalid_signature_returns_401(client, monkeypatch):
    _patch_verify(monkeypatch, raises=ValueError("Token signature is invalid"))
    r = client.post("/auth/google", json={"id_token": "fake.broken.token"})
    assert r.status_code == 401
    assert "google" in r.json["error"].lower()


# ============================================================
# 3. id_token expirado → 401 (google-auth levanta ValueError)
# ============================================================
def test_03_expired_token_returns_401(client, monkeypatch):
    _patch_verify(monkeypatch, raises=ValueError("Token expired"))
    r = client.post("/auth/google", json={"id_token": "expired.token"})
    assert r.status_code == 401


# ============================================================
# 4. id_token com audience errado → 401
# ============================================================
def test_04_wrong_audience_returns_401(client, monkeypatch):
    payload = _good_payload(aud="not-our-client.googleusercontent.com")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "wrong.aud.token"})
    assert r.status_code == 401


# ============================================================
# 5. id_token com issuer errado → 401
# ============================================================
def test_05_wrong_issuer_returns_401(client, monkeypatch):
    payload = _good_payload(iss="https://evil.com")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "bad.iss.token"})
    assert r.status_code == 401
    assert "issuer" in r.json["error"].lower()


# ============================================================
# 6. email_verified=false → 401
# ============================================================
def test_06_email_not_verified_returns_401(client, monkeypatch):
    payload = _good_payload(email_verified=False)
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "unverified.token"})
    assert r.status_code == 401
    assert "verificado" in r.json["error"].lower() or "verified" in r.json["error"].lower()


# ============================================================
# 7. Nonce válido (cliente e token batem) → 200
# ============================================================
def test_07_nonce_match_returns_200(client, monkeypatch):
    payload = _good_payload(nonce="abc123nonce")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={
        "id_token": "good.token",
        "nonce": "abc123nonce",
    })
    assert r.status_code == 200
    assert "access_token" in r.json or "token" in r.json


# ============================================================
# 8. Nonce mismatch (cliente envia A, token tem B) → 401
# ============================================================
def test_08_nonce_mismatch_returns_401(client, monkeypatch):
    payload = _good_payload(nonce="nonceB")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={
        "id_token": "good.token",
        "nonce": "nonceA",
    })
    assert r.status_code == 401
    assert "nonce" in r.json["error"].lower() or "replay" in r.json["error"].lower()


# ============================================================
# 9. Cliente envia nonce, mas payload não tem → 401
# ============================================================
def test_09_nonce_missing_in_token_returns_401(client, monkeypatch):
    payload = _good_payload()  # sem nonce
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={
        "id_token": "good.token",
        "nonce": "expectedNonce",
    })
    assert r.status_code == 401


# ============================================================
# 10. Cliente não envia nonce (modo legacy) → 200 mantém compat
# ============================================================
def test_10_no_nonce_legacy_compat_returns_200(client, monkeypatch):
    payload = _good_payload()  # sem nonce
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "good.token"})
    assert r.status_code == 200


# ============================================================
# 11. Primeiro login: cria User + Wallet + SocialAccount
# ============================================================
def test_11_first_login_creates_user_wallet_social(client, monkeypatch, app):
    payload = _good_payload()
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "good.token"})
    assert r.status_code == 200

    with app.app_context():
        user = db.session.query(User).filter_by(email="joao@gmail.com").one()
        assert user.google_sub == "1234567890"
        wallet = db.session.query(Wallet).filter_by(user_id=user.id).one()
        assert wallet.balance_pts == 0
        social = db.session.query(SocialAccount).filter_by(
            provider="google", provider_user_id="1234567890"
        ).one()
        assert social.user_id == user.id
        assert social.provider_email == "joao@gmail.com"


# ============================================================
# 12. Segundo login mesmo sub: NÃO duplica User, atualiza SocialAccount
# ============================================================
def test_12_second_login_same_sub_does_not_duplicate(client, monkeypatch, app):
    payload = _good_payload()
    _patch_verify(monkeypatch, payload=payload)

    r1 = client.post("/auth/google", json={"id_token": "t1"})
    assert r1.status_code == 200
    r2 = client.post("/auth/google", json={"id_token": "t2"})
    assert r2.status_code == 200

    with app.app_context():
        users = db.session.query(User).filter_by(email="joao@gmail.com").all()
        assert len(users) == 1
        socials = db.session.query(SocialAccount).filter_by(
            provider_user_id="1234567890"
        ).all()
        assert len(socials) == 1


# ============================================================
# 13. Login Google em conta pré-existente (email+senha): faz link
# ============================================================
def test_13_link_existing_email_account(client, monkeypatch, app):
    # Cria User pré-existente sem google_sub
    with app.app_context():
        user = User(
            name="João Existente",
            email="joao@gmail.com",
            cpf="52998224725",
            password_hash=None,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance_pts=500, pending_pts=0))
        db.session.commit()
        user_id_before = user.id

    payload = _good_payload()
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "good.token"})
    assert r.status_code == 200

    with app.app_context():
        user = db.session.query(User).filter_by(email="joao@gmail.com").one()
        assert user.id == user_id_before  # NÃO criou novo
        assert user.google_sub == "1234567890"  # linkou
        # Wallet preservou saldo
        wallet = db.session.query(Wallet).filter_by(user_id=user.id).one()
        assert wallet.balance_pts == 500


# ============================================================
# 14. Avatar (picture) gravado no User e SocialAccount
# ============================================================
def test_14_avatar_url_persisted(client, monkeypatch, app):
    payload = _good_payload(picture="https://example.com/avatar.jpg")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "good.token"})
    assert r.status_code == 200

    with app.app_context():
        user = db.session.query(User).filter_by(email="joao@gmail.com").one()
        assert user.avatar_url == "https://example.com/avatar.jpg"
        social = db.session.query(SocialAccount).filter_by(
            provider_user_id="1234567890"
        ).one()
        assert social.avatar_url == "https://example.com/avatar.jpg"


# ============================================================
# 15. Sem GOOGLE_*_CLIENT_ID configurado → 503
# ============================================================
def test_15_unconfigured_returns_503(client, monkeypatch, app):
    app.config["GOOGLE_WEB_CLIENT_ID"] = ""
    app.config["GOOGLE_IOS_CLIENT_ID"] = ""
    app.config["GOOGLE_WEB_CLIENT_IDS_EXTRAS"] = []

    # Patcha Config.google_allowed_audiences pra refletir config nova
    from app.config import Config
    monkeypatch.setattr(Config, "GOOGLE_WEB_CLIENT_ID", "")
    monkeypatch.setattr(Config, "GOOGLE_IOS_CLIENT_ID", "")
    monkeypatch.setattr(Config, "GOOGLE_WEB_CLIENT_IDS_EXTRAS", [])

    r = client.post("/auth/google", json={"id_token": "any.token"})
    assert r.status_code == 503
    assert "configurado" in r.json["error"].lower() or "configured" in r.json["error"].lower()


# ============================================================
# 16. Audit log gravado em sucesso E em falha
# ============================================================
def test_16_audit_log_on_success_and_failure(client, monkeypatch, app):
    # Falha primeiro
    _patch_verify(monkeypatch, raises=ValueError("bad token"))
    client.post("/auth/google", json={"id_token": "bad"})

    with app.app_context():
        # AuditLog.event (não "action") — coluna correta no modelo
        failures = db.session.query(AuditLog).filter_by(
            event="google_login_failed"
        ).all()
        assert len(failures) >= 1

    # Sucesso agora
    payload = _good_payload(sub="9999", email="ana@gmail.com")
    _patch_verify(monkeypatch, payload=payload)
    r = client.post("/auth/google", json={"id_token": "good"})
    assert r.status_code == 200

    with app.app_context():
        successes = db.session.query(AuditLog).filter_by(
            event="google_login_ok"
        ).all()
        assert len(successes) >= 1


# ---------------------------------------------------------------------------
# 17. O aud publicado pelo front tem de ser aceito pelo backend
# ---------------------------------------------------------------------------
# Em 02/08/2026 o default WEB do backend apontava para um projeto do Google
# (1086156839608) onde nenhum client do BlaXx existe, enquanto o PWA publicava
# um client de outro projeto (105341431878). Como o backend só aceita ID token
# cujo `aud` esteja na lista, TODO login web devolvia 401 "Token Google
# inválido" — e nada nos testes pegava, porque eles usam client IDs fictícios.
#
# Este teste compara os arquivos reais. O `blaxx/` é outro repositório: quando
# o backend é clonado sozinho (CI do Render), o arquivo não existe e o teste é
# pulado com motivo explícito, em vez de falhar por ausência de contexto.
def test_17_client_id_do_front_esta_entre_os_audiences_aceitos():
    import re
    from pathlib import Path

    from app.config import Config

    raiz = Path(__file__).resolve().parents[3]
    config_front = raiz / "blaxx" / "assets" / "blaxx-config.js"
    if not config_front.is_file():
        pytest.skip(f"blaxx-config.js fora deste clone ({config_front}) — nada a comparar")

    texto = config_front.read_text("utf-8", errors="ignore")
    m = re.search(
        r"BLAXX_GOOGLE_CLIENT_ID\s*=\s*[\"']([^\"']+)[\"']", texto
    )
    assert m, "BLAXX_GOOGLE_CLIENT_ID não encontrado em blaxx/assets/blaxx-config.js"
    aud_do_front = m.group(1)

    # Comparar contra os DEFAULTS, não contra google_allowed_audiences(): este
    # próprio arquivo de teste seta GOOGLE_WEB_CLIENT_ID/GOOGLE_IOS_CLIENT_ID
    # fictícios no import, então os audiences "efetivos" aqui são os de teste.
    # O que importa em produção é o fallback embutido — foi ele que estava
    # apontando para o projeto errado.
    aceitos = [
        Config.GOOGLE_WEB_CLIENT_ID_DEFAULT,
        Config.GOOGLE_IOS_CLIENT_ID_DEFAULT,
    ]
    assert aud_do_front in aceitos, (
        "O Client ID que o PWA publica não está entre os audiences aceitos pelo "
        "backend — o login web vai retornar 401 para todos os usuários.\n"
        f"  front   (blaxx/assets/blaxx-config.js): {aud_do_front}\n"
        f"  backend (defaults de app/config.py):    {aceitos}\n"
        "Corrija GOOGLE_WEB_CLIENT_ID_DEFAULT em app/config.py ou o valor no "
        "blaxx-config.js — os dois precisam apontar para o mesmo client do "
        "mesmo projeto Google Cloud."
    )


# ============================================================
# 18. Troca de client Web (16/10/2026): o client da conta da empresa é aceito
#     mesmo com GOOGLE_WEB_CLIENT_ID (env no Render) ainda no valor antigo,
#     e o anterior continua aceito na transição.
# ============================================================
def test_18_client_da_empresa_e_anterior_aceitos_com_env_antiga(client, monkeypatch, app):
    from app.config import Config

    app.config["GOOGLE_WEB_CLIENT_ID"] = Config.GOOGLE_WEB_CLIENT_ID_ANTERIOR
    app.config["GOOGLE_WEB_CLIENT_IDS_EXTRAS"] = list(Config.GOOGLE_WEB_CLIENT_IDS_EXTRAS)

    novo = Config.GOOGLE_WEB_CLIENT_ID_DEFAULT
    _patch_verify(monkeypatch, payload=_good_payload(aud=novo, azp=novo))
    r = client.post("/auth/google", json={"id_token": "token.do.client.novo"})
    assert r.status_code == 200, r.json

    anterior = Config.GOOGLE_WEB_CLIENT_ID_ANTERIOR
    _patch_verify(monkeypatch, payload=_good_payload(aud=anterior, azp=anterior, sub="999", email="maria@gmail.com"))
    r = client.post("/auth/google", json={"id_token": "token.do.client.anterior"})
    assert r.status_code == 200, r.json

    _patch_verify(monkeypatch, payload=_good_payload(aud="outro-projeto.apps.googleusercontent.com", sub="777", email="x@gmail.com"))
    r = client.post("/auth/google", json={"id_token": "token.de.outro.projeto"})
    assert r.status_code == 401
