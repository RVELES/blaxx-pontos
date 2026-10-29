"""Liberar a identidade de uma conta de teste: o mesmo e-mail, CPF e celular
voltam a poder se cadastrar, a conta antiga não entra mais, e só admin faz,
nunca em si mesmo nem em outro admin."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, User, Wallet

SENHA = "StrongP@ss1!"
EMAIL, CPF, CEL = "teste@x.test", "52998224725", "+5511987654321"


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


def _user(email, cpf, role="user", phone=None):
    u = User(name="Fulano Teste", email=email, cpf=cpf, role=role, phone=phone)
    u.set_password(SENHA)
    u.email_verified_at = datetime.now(timezone.utc)
    db.session.add(u); db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance_pts=1234)); db.session.commit()
    return u.id


def _h(client, email):
    t = client.post("/auth/login", json={"email": email, "password": SENHA}).get_json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _cadastro(client):
    return client.post("/auth/register", json={
        "name": "Fulano Novo", "email": EMAIL, "cpf": CPF, "password": "Outr@Senha9!",
        "phone": "(11) 98765-4321", "birth_date": "1990-01-01",
        "accept_terms": True, "accept_privacy": True, "accept_lgpd": True,
    })


def test_libera_email_cpf_e_celular_para_cadastro_novo(app, client):
    _user("adm@x.test", "15350946056", role="admin")
    alvo = _user(EMAIL, CPF, phone=CEL)
    H = _h(client, "adm@x.test")
    assert _cadastro(client).status_code == 409          # ocupado antes

    r = client.post(f"/admin/users/{alvo}/liberar-identidade",
                    json={"confirm": "LIBERAR", "reason": "vídeo Apple"}, headers=H)
    assert r.status_code == 200, r.get_json()

    novo = _cadastro(client)
    assert novo.status_code in (200, 201), novo.get_json()
    antigo = db.session.get(User, alvo)
    assert antigo.status == "closed" and antigo.email.endswith("@removed.local")
    assert db.session.query(Wallet).filter_by(user_id=alvo).one().balance_pts == 1234  # ledger intacto
    ev = db.session.query(AuditLog).filter_by(event="admin_liberar_identidade").one()
    assert CPF not in str(ev.extra_data) and EMAIL not in str(ev.extra_data)        # só hashes


def test_exige_confirmacao_e_protege_admin(app, client):
    adm = _user("adm@x.test", "15350946056", role="admin")
    outro_adm = _user("adm2@x.test", "11144477735", role="admin")
    alvo = _user(EMAIL, CPF)
    H = _h(client, "adm@x.test")
    assert client.post(f"/admin/users/{alvo}/liberar-identidade", json={}, headers=H).status_code == 400
    assert client.post(f"/admin/users/{adm}/liberar-identidade", json={"confirm": "LIBERAR"}, headers=H).status_code == 403
    assert client.post(f"/admin/users/{outro_adm}/liberar-identidade", json={"confirm": "LIBERAR"}, headers=H).status_code == 403


def test_usuario_comum_nao_libera(app, client):
    _user("c@x.test", "15350946056")
    alvo = _user(EMAIL, CPF)
    r = client.post(f"/admin/users/{alvo}/liberar-identidade",
                    json={"confirm": "LIBERAR"}, headers=_h(client, "c@x.test"))
    assert r.status_code == 403
