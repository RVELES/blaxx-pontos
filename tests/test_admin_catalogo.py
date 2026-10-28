"""Endpoints admin de parceiro, benefício e campanha.

Existem porque o Render free não tem Shell: sem eles, popular o catálogo em
produção exigiria acesso direto ao banco.

Protegem: nome duplicado recusado (o script de carga é reexecutável), benefício
não fica órfão de parceiro inexistente, e só admin acessa.
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
from app.models import Benefit, Campaign, Partner, User, Wallet

SENHA = "StrongP@ss1!"


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


def _tok(app, client, cpf="15350946056", email="a@x.test", role="admin"):
    with app.app_context():
        u = User(name="U", email=email, cpf=cpf, role=role)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
    t = client.post("/auth/login", json={"email": email, "password": SENHA}).get_json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_cria_parceiro_beneficio_e_campanha(app, client):
    H = _tok(app, client)

    p = client.post("/admin/partners", json={
        "name": "Pão & Cia", "category": "Mercados", "logo_emoji": "🛒",
        "accrual_rule": "1 pt a cada R$ 1,80"}, headers=H)
    assert p.status_code == 201

    b = client.post("/admin/benefits", json={
        "name": "Voucher R$ 50", "partner_name": "Pão & Cia",
        "cost_pts": 588, "category": "voucher"}, headers=H)
    assert b.status_code == 201
    assert b.get_json()["cost_pts"] == 588

    c = client.post("/admin/campaigns", json={
        "name": "Maio em dobro", "target_brl": 50_000, "reward_pts": 2_000,
        "mechanic": "Gaste R$ 500"}, headers=H)
    assert c.status_code == 201
    assert c.get_json()["target_brl"] == 500.0        # to_dict divide por 100

    assert len(client.get("/campaigns/").get_json()["items"]) == 1
    assert len(client.get("/benefits/").get_json()["items"]) == 1


def test_nome_duplicado_recusado_para_os_tres(app, client):
    H = _tok(app, client)
    corpos = [
        ("/admin/partners", {"name": "X", "category": "Y"}),
        ("/admin/benefits", {"name": "X", "cost_pts": 10}),
        ("/admin/campaigns", {"name": "X", "target_brl": 100, "reward_pts": 5}),
    ]
    for rota, corpo in corpos:
        assert client.post(rota, json=corpo, headers=H).status_code == 201
        r2 = client.post(rota, json=corpo, headers=H)
        assert r2.status_code == 409, rota
        assert r2.get_json()["code"] == "duplicate"


def test_beneficio_com_parceiro_inexistente_e_recusado(app, client):
    """Benefício órfão apareceria na tela sem dizer de quem é."""
    H = _tok(app, client)
    r = client.post("/admin/benefits", json={
        "name": "Solto", "partner_name": "Não Existe", "cost_pts": 10}, headers=H)
    assert r.status_code == 400
    assert r.get_json()["code"] == "partner_not_found"
    with app.app_context():
        assert db.session.query(Benefit).count() == 0


def test_valores_invalidos_recusados(app, client):
    H = _tok(app, client)
    assert client.post("/admin/benefits", json={"name": "A", "cost_pts": 0},
                       headers=H).status_code == 400
    assert client.post("/admin/campaigns", json={"name": "B", "target_brl": 0,
                                                 "reward_pts": 10}, headers=H).status_code == 400


def test_desativar_campanha_some_da_listagem_publica(app, client):
    H = _tok(app, client)
    cid = client.post("/admin/campaigns", json={"name": "C", "target_brl": 100,
                                                "reward_pts": 5}, headers=H).get_json()["id"]
    assert len(client.get("/campaigns/").get_json()["items"]) == 1
    assert client.patch(f"/admin/campaigns/{cid}", json={"is_active": False},
                        headers=H).status_code == 200
    assert client.get("/campaigns/").get_json()["items"] == []


def test_desativar_parceiro_some_da_listagem_publica(app, client):
    H = _tok(app, client)
    pid = client.post("/admin/partners", json={"name": "P", "category": "X"},
                      headers=H).get_json()["id"]
    assert len(client.get("/partners/").get_json()["items"]) == 1
    assert client.patch(f"/admin/partners/{pid}", json={"is_active": False},
                        headers=H).status_code == 200
    assert client.get("/partners/").get_json()["items"] == []
    assert client.get(f"/partners/{pid}").status_code == 404


def test_usuario_comum_nao_desativa_parceiro(app, client):
    H = _tok(app, client)
    pid = client.post("/admin/partners", json={"name": "P", "category": "X"},
                      headers=H).get_json()["id"]
    U = _tok(app, client, cpf="52998224725", email="u@x.test", role="user")
    assert client.patch(f"/admin/partners/{pid}", json={"is_active": False},
                        headers=U).status_code == 403
    assert len(client.get("/partners/").get_json()["items"]) == 1


def test_usuario_comum_nao_cria_catalogo(app, client):
    H = _tok(app, client, cpf="52998224725", email="u@x.test", role="user")
    for rota in ("/admin/partners", "/admin/benefits", "/admin/campaigns"):
        assert client.post(rota, json={"name": "Z"}, headers=H).status_code == 403
