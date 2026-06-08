"""Sprint QA · Testes do envio P2P (services/transfer.py).

Cobre os 3 fixes do audit:
  - A1 idempotência exata (mesma Idempotency-Key não duplica débito/crédito)
  - A1 rede de segurança double-submit (sem chave, janela curta)
  - A2 destinatário recebe Notification
  - A3 evento de auditoria `transfer_sent` registrado com IP/device/platform
Mais as regras já existentes (saldo insuficiente, self-transfer, mínimo,
destinatário inexistente) pra garantir que não houve regressão.

Roda com:
    pytest -v tests/test_transfer.py
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
    AuditLog, Notification, Transaction, Transfer, TxType, User, Wallet,
)
from app.services import transfer as transfer_svc


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _make_user(email, *, balance=0, cpf="00000000000", password="StrongP@ss1!"):
    u = User(name=email.split("@")[0].title(), email=email, cpf=cpf, role="user")
    u.set_password(password)
    u.email_verified_at = datetime.now(timezone.utc)
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance_pts=balance, pending_pts=0))
    db.session.commit()
    return u


def _balance(user_id):
    return db.session.query(Wallet).filter_by(user_id=user_id).one().balance_pts


# ---------------------------------------------------------------- happy path
def test_basic_transfer_moves_balance(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", balance=0, cpf="22222222222")
        t = transfer_svc.send(
            a, recipient_identifier="beta@blaxx.test",
            amount_pts=1000, password="StrongP@ss1!",
        )
        assert isinstance(t, Transfer)
        assert _balance(a.id) == 9000
        assert _balance(b.id) == 1000
        # 2 lançamentos no ledger (out + in)
        assert db.session.query(Transaction).count() == 2


# -------------------------------------------------------------------- A2
def test_recipient_gets_notification(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                          amount_pts=500, password="StrongP@ss1!", message="valeu")
        notes = db.session.query(Notification).filter_by(user_id=b.id).all()
        assert len(notes) == 1
        assert notes[0].type == "transfer"
        assert "500" in notes[0].body
        # remetente NÃO recebe notificação
        assert db.session.query(Notification).filter_by(user_id=a.id).count() == 0


# -------------------------------------------------------------------- A3
def test_audit_event_recorded(app):
    with app.test_request_context(
        "/transfer",
        headers={"User-Agent": "pytest-agent", "X-Forwarded-For": "203.0.113.7"},
    ):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        _make_user("beta@blaxx.test", cpf="22222222222")
        transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                          amount_pts=300, password="StrongP@ss1!",
                          device_id="dev-123", platform="ios")
        log = db.session.query(AuditLog).filter_by(event="transfer_sent").one()
        assert log.user_id == a.id
        assert log.ip == "203.0.113.7"
        assert log.user_agent == "pytest-agent"
        assert log.device_id == "dev-123"
        assert "ios" in (log.extra_data or "")


# -------------------------------------------------------------- A1 exact key
def test_idempotency_key_no_double_debit(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        t1 = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                               amount_pts=1000, password="StrongP@ss1!",
                               idempotency_key="req-abc")
        # mesmo request_id → devolve a MESMA transferência, sem novo débito
        t2 = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                               amount_pts=1000, password="StrongP@ss1!",
                               idempotency_key="req-abc")
        assert t1.id == t2.id
        assert _balance(a.id) == 9000   # debitou só uma vez
        assert _balance(b.id) == 1000
        assert db.session.query(Transfer).count() == 1


# ---------------------------------------------------- A1 double-submit no key
def test_double_submit_without_key_is_deduped(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        t1 = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                               amount_pts=1000, password="StrongP@ss1!")
        # reenvio idêntico imediato (sem chave) cai na janela anti-duplicidade
        t2 = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                               amount_pts=1000, password="StrongP@ss1!")
        assert t1.id == t2.id
        assert _balance(a.id) == 9000
        assert db.session.query(Transfer).count() == 1


# ----------------------------------------------------------- regressões
def test_insufficient_balance_blocks(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        eta = _make_user("eta@blaxx.test", balance=0, cpf="33333333333")
        _make_user("beta@blaxx.test", cpf="22222222222")
        with pytest.raises(transfer_svc.TransferError):
            transfer_svc.send(eta, recipient_identifier="beta@blaxx.test",
                              amount_pts=1000, password="StrongP@ss1!")
        assert _balance(eta.id) == 0
        assert db.session.query(Transfer).count() == 0
        assert db.session.query(Notification).count() == 0


def test_cannot_send_to_self(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        with pytest.raises(transfer_svc.TransferError):
            transfer_svc.send(a, recipient_identifier="alpha@blaxx.test",
                              amount_pts=500, password="StrongP@ss1!")


def test_recipient_not_found(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        with pytest.raises(transfer_svc.TransferError):
            transfer_svc.send(a, recipient_identifier="ghost@blaxx.test",
                              amount_pts=500, password="StrongP@ss1!")


def test_below_minimum_blocks(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        _make_user("beta@blaxx.test", cpf="22222222222")
        with pytest.raises(transfer_svc.TransferError):
            transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=50, password="StrongP@ss1!")
