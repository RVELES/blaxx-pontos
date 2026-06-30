"""Sprint 1-2 (P0) · Tests for hardening changes.

Cobre:
  - /redeem idempotencia em DB (mesma Idempotency-Key → mesmo payout, sem 2o debito)
  - /auth/refresh rotation + family-kill em reuse detection
  - DELETE /auth/account: anonimizacao, revoga sessoes, deleta notifications
  - /auth/dev/verify-email → 404 sem ENABLE_DEV_ENDPOINTS=1

Roda com:
    pytest -v tests/test_sprint12_hardening.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (
    User, Wallet, PixPayout, RevokedToken, Notification, SocialAccount,
)


VALID_CPF = "52998224725"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk_user(app, *, email="user@test.com", cpf=VALID_CPF, balance=10_000):
    with app.app_context():
        u = User(name="Test User", email=email, cpf=cpf, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        w = Wallet(user_id=u.id, balance_pts=balance, pending_pts=0)
        db.session.add(w)
        db.session.commit()
        return u.id


def _login(client, email="user@test.com", password="StrongP@ss1!"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    return body["access_token"], body.get("refresh_token")


# ============================================================================
# /redeem idempotencia em DB
# ============================================================================

class TestRedeemDbIdempotency:

    def test_same_idempotency_key_returns_original_payout(self, app, client):
        _mk_user(app, balance=10_000)
        access, _ = _login(client)
        headers = {"Authorization": f"Bearer {access}", "Idempotency-Key": "uuid-abc"}
        body = {"points": 1000, "pix_key": "user@test.com", "password": "StrongP@ss1!"}

        r1 = client.post("/redeem/", json=body, headers=headers)
        assert r1.status_code == 201, r1.get_json()
        payout1 = r1.get_json()

        # Retry com a MESMA key — deve devolver o mesmo payout (status 200) sem 2o debito
        r2 = client.post("/redeem/", json=body, headers=headers)
        assert r2.status_code == 200, r2.get_json()
        payout2 = r2.get_json()
        assert payout1["id"] == payout2["id"]
        assert payout1["points_debited"] == payout2["points_debited"]

        # Confirma que so existe 1 PixPayout na base
        with app.app_context():
            count = db.session.query(PixPayout).count()
            assert count == 1

    def test_different_keys_create_distinct_payouts(self, app, client):
        _mk_user(app, balance=10_000)
        access, _ = _login(client)
        body = {"points": 500, "pix_key": "user@test.com", "password": "StrongP@ss1!"}

        r1 = client.post("/redeem/", json=body, headers={
            "Authorization": f"Bearer {access}", "Idempotency-Key": "k-1"})
        r2 = client.post("/redeem/", json=body, headers={
            "Authorization": f"Bearer {access}", "Idempotency-Key": "k-2"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.get_json()["id"] != r2.get_json()["id"]


# ============================================================================
# /auth/refresh — rotation + reuse detection
# ============================================================================

class TestRefreshRotation:

    def test_refresh_rotates_and_revokes_old_jti(self, app, client):
        _mk_user(app)
        _, refresh1 = _login(client)
        assert refresh1
        # Limpa cookies do test client — apps nativos usam SO Bearer; sem
        # isso o framework prioriza o cookie httpOnly setado pelo /login.
        client.delete_cookie("blaxx_refresh", path="/auth/refresh")
        client.delete_cookie("blaxx_session", path="/")

        r = client.post("/auth/refresh",
                        headers={"Authorization": f"Bearer {refresh1}"})
        assert r.status_code == 200, r.get_json()
        new_body = r.get_json()
        new_refresh = new_body.get("refresh_token")
        assert new_refresh and new_refresh != refresh1

        # O refresh antigo agora esta na blocklist
        from flask_jwt_extended import decode_token
        with app.app_context():
            claims = decode_token(refresh1)
            assert db.session.get(RevokedToken, claims["jti"]) is not None

    def test_reuse_of_old_refresh_kills_family(self, app, client):
        import time
        _mk_user(app)
        access, refresh1 = _login(client)
        client.delete_cookie("blaxx_refresh", path="/auth/refresh")
        client.delete_cookie("blaxx_session", path="/")

        # Primeira rotacao OK
        r1 = client.post("/auth/refresh",
                         headers={"Authorization": f"Bearer {refresh1}"})
        assert r1.status_code == 200
        # Re-limpa cookies setados pelo rotate (apps nativos os ignoram)
        client.delete_cookie("blaxx_refresh", path="/auth/refresh")
        client.delete_cookie("blaxx_session", path="/")

        # Resolucao do iat e' em segundos; espera 1.1s pra garantir que o
        # password_changed_at bumpado em r2 seja > iat de new_refresh emitido
        # em r1, validando o family-kill via blocklist loader.
        time.sleep(1.1)

        # Reusar o refresh ANTIGO = roubo. Deve falhar (401) e bumpar
        # password_changed_at, invalidando o NOVO refresh tambem.
        r2 = client.post("/auth/refresh",
                         headers={"Authorization": f"Bearer {refresh1}"})
        assert r2.status_code == 401, r2.get_json()

        # O NOVO refresh emitido em r1 nao serve mais (family kill via
        # password_changed_at bump no blocklist loader).
        new_refresh = r1.get_json().get("refresh_token")
        client.delete_cookie("blaxx_refresh", path="/auth/refresh")
        client.delete_cookie("blaxx_session", path="/")
        r3 = client.post("/auth/refresh",
                         headers={"Authorization": f"Bearer {new_refresh}"})
        assert r3.status_code == 401


# ============================================================================
# DELETE /auth/account — anonimizacao e revogacao de sessoes
# ============================================================================

class TestDeleteAccount:

    def test_delete_anonymizes_and_revokes(self, app, client):
        import time
        uid = _mk_user(app)
        access, _ = _login(client)
        client.delete_cookie("blaxx_session", path="/")

        with app.app_context():
            db.session.add(Notification(
                user_id=uid, type="system", title="t", body="b", icon="x"))
            db.session.commit()

        # iat tem resolucao em segundos — espera pra password_changed_at bumpado
        # no delete ficar > iat do access token (validacao do blocklist loader).
        time.sleep(1.1)

        r = client.delete("/auth/account",
                          headers={"Authorization": f"Bearer {access}"},
                          json={"password": "StrongP@ss1!",
                                "confirm": "EXCLUIR MINHA CONTA"})
        assert r.status_code == 200, r.get_json()

        with app.app_context():
            u = db.session.get(User, uid)
            assert u is not None
            assert u.email.startswith("deleted_") and u.email.endswith("@removed.local")
            assert u.name == "Conta Excluida"
            assert u.phone is None
            assert u.pix_key is None
            assert u.password_hash == ""
            # Notification limpa
            assert db.session.query(Notification).filter_by(user_id=uid).count() == 0

        # Token antigo agora ja era (family-killed por password_changed_at bump)
        r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
        assert r2.status_code == 401

    def test_delete_requires_password(self, app, client):
        _mk_user(app)
        access, _ = _login(client)
        r = client.delete("/auth/account",
                          headers={"Authorization": f"Bearer {access}"},
                          json={"password": "wrong", "confirm": "EXCLUIR MINHA CONTA"})
        assert r.status_code == 401

    def test_delete_requires_confirm_string(self, app, client):
        _mk_user(app)
        access, _ = _login(client)
        r = client.delete("/auth/account",
                          headers={"Authorization": f"Bearer {access}"},
                          json={"password": "StrongP@ss1!", "confirm": "yes"})
        assert r.status_code == 400


# ============================================================================
# /auth/dev/verify-email — gate
# ============================================================================

class TestDevEndpointGate:
    """Gate de /auth/dev/verify-email — intenção:
      - Em DEV/TEST: sempre permite (não precisa de flag)
      - Em PRODUÇÃO: só permite com ENABLE_DEV_ENDPOINTS=1

    Bug fix 2026-06-30: lógica anterior usava `or` (404 em qualquer caso sem
    flag, inclusive prod com flag). Agora `and` (só 404 em prod sem flag).
    """

    def test_dev_endpoint_allows_in_test_mode_without_flag(self, app, client, monkeypatch):
        """Test mode sem flag → permite (intenção: dev/test livre)."""
        _mk_user(app)
        access, _ = _login(client)
        monkeypatch.delenv("ENABLE_DEV_ENDPOINTS", raising=False)
        r = client.post("/auth/dev/verify-email",
                        headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 200

    def test_dev_endpoint_works_with_flag_in_test(self, app, client, monkeypatch):
        """Test mode com flag=1 → permite (idem)."""
        _mk_user(app)
        access, _ = _login(client)
        monkeypatch.setenv("ENABLE_DEV_ENDPOINTS", "1")
        r = client.post("/auth/dev/verify-email",
                        headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 200

    def test_dev_endpoint_blocks_in_production_without_flag(self, app, client, monkeypatch):
        """Production sem flag → 404 (gate de segurança)."""
        _mk_user(app)
        access, _ = _login(client)
        monkeypatch.delenv("ENABLE_DEV_ENDPOINTS", raising=False)
        # Força _is_production() a retornar True via monkeypatch
        from app import _is_production as orig  # noqa
        monkeypatch.setattr("app._is_production", lambda *a, **k: True)
        monkeypatch.setattr("app.api.auth._is_production", lambda *a, **k: True) if False else None
        # O import dentro do view é "from .. import _is_production" → resolve
        # via app/__init__.py, então patcheamos lá.
        import app as app_module
        monkeypatch.setattr(app_module, "_is_production", lambda *a, **k: True)
        r = client.post("/auth/dev/verify-email",
                        headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 404, "Sem flag em prod precisa retornar 404"

    def test_dev_endpoint_works_in_production_with_flag(self, app, client, monkeypatch):
        """Production COM flag=1 → permite. Este é o caso de homologação."""
        _mk_user(app)
        access, _ = _login(client)
        monkeypatch.setenv("ENABLE_DEV_ENDPOINTS", "1")
        import app as app_module
        monkeypatch.setattr(app_module, "_is_production", lambda *a, **k: True)
        r = client.post("/auth/dev/verify-email",
                        headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 200, "Prod + flag=1 deve permitir (homologação)"
