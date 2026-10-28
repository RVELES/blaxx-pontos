"""Cada movimentação de pontos manda o e-mail certo, uma vez só, e um erro no
e-mail nunca desfaz a operação."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Benefit, PixPayout, PixPayoutStatus, User, Wallet
from app.services import mailer as mailer_mod
from app.services import redeem as redeem_svc
from app.services import transfer as transfer_svc

SENHA = "StrongP@ss1!"


class _Captura:
    name = "captura"

    def __init__(self):
        self.msgs = []

    def send(self, msg):
        self.msgs.append(msg)
        return True


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def caixa(monkeypatch):
    cap = _Captura()
    monkeypatch.setattr(mailer_mod, "_singleton", cap)
    return cap


def _user(email, nome, *, saldo=0, cpf="00000000000"):
    u = User(name=nome, email=email, cpf=cpf, role="user")
    u.set_password(SENHA)
    u.email_verified_at = datetime.now(timezone.utc)
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance_pts=saldo, pending_pts=0))
    db.session.commit()
    return u


def test_envio_avisa_quem_mandou_e_quem_recebeu_uma_vez(app, caixa):
    ana = _user("ana@x.test", "Ana Souza", saldo=5_000)
    beto = _user("beto@x.test", "Beto Lima", cpf="11111111111")
    with app.test_request_context("/transfer/"):
        t = transfer_svc.send(ana, recipient_identifier="beto@x.test", amount_pts=1_200, password=SENHA,
                              message="Valeu pelo almoço!")
    assert caixa.msgs == []                     # pendente: ainda pode cancelar
    t.committed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.flush()
    with app.test_request_context("/wallet/"):
        assert transfer_svc.promote_pending([t]) == 1
        assert transfer_svc.promote_pending([t]) == 0   # 2ª promoção não reenvia

    para = sorted(m.to for m in caixa.msgs)
    assert para == ["ana@x.test", "beto@x.test"]
    recebido = next(m for m in caixa.msgs if m.to == "beto@x.test")
    assert "1.200 pts" in recebido.subject and "Ana Souza" in recebido.subject
    assert "Valeu pelo almoço!" in recebido.body_html
    enviado = next(m for m in caixa.msgs if m.to == "ana@x.test")
    assert "3.800 pts" in enviado.body_text      # saldo depois do envio


def test_troca_por_produto_manda_voucher(app, caixa):
    client = app.test_client()
    _user("c@x.test", "Carla", saldo=10_000, cpf="52998224725")
    b = Benefit(name="Cinema 2D", category="voucher", cost_pts=1_000,
                stock=-1, expires_in_days=90)
    db.session.add(b)
    db.session.commit()
    tok = client.post("/auth/login", json={"email": "c@x.test", "password": SENHA}).get_json()["token"]
    r = client.post(f"/benefits/{b.id}/redeem", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 201
    codigo = r.get_json()["code"]
    [msg] = caixa.msgs
    assert "Cinema 2D" in msg.subject
    assert codigo in msg.body_html and codigo in msg.body_text


def _payout(u, status=PixPayoutStatus.PROCESSING):
    p = PixPayout(user_id=u.id, points_debited=2_000, amount_cents=1_850,
                  pix_key="ana@pix.test", status=status)
    db.session.add(p)
    db.session.commit()
    return p


def test_venda_paga_e_venda_falha(app, caixa):
    u = _user("d@x.test", "Duda")
    redeem_svc.confirm_payout(_payout(u).txid)
    redeem_svc.fail_payout(_payout(u).txid, reason="chave Pix inexistente")
    paga, falha = caixa.msgs
    assert "R$ 18,50" in paga.subject
    assert "ana@pix.test" not in paga.body_html          # chave mascarada
    assert "voltaram" in falha.subject and "chave Pix inexistente" in falha.body_text


def test_erro_no_email_nao_desfaz_a_operacao(app, monkeypatch):
    class _Quebrado:
        name = "quebrado"

        def send(self, msg):
            raise RuntimeError("Resend fora do ar")

    monkeypatch.setattr(mailer_mod, "_singleton", _Quebrado())
    u = _user("e@x.test", "Eva")
    p = redeem_svc.confirm_payout(_payout(u).txid)
    assert p.status == PixPayoutStatus.PAID
