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


def _pending_balance(user_id):
    """Saldo retido em pending_pts (P2P dentro da janela de 60s)."""
    return db.session.query(Wallet).filter_by(user_id=user_id).one().pending_pts


def _force_promote(*transfers):
    """Simula passagem da janela de 60s: marca committed_at no passado e
    chama promote_pending. Evita sleep(60) nos testes.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    for t in transfers:
        t.committed_at = now - timedelta(seconds=1)
    db.session.flush()
    return transfer_svc.promote_pending(list(transfers))


# ---------------------------------------------------------------- happy path
def test_basic_transfer_pending_state(app):
    """Estado pending: sender debitado, recipient ainda não vê o saldo."""
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", balance=0, cpf="22222222222")
        t = transfer_svc.send(
            a, recipient_identifier="beta@blaxx.test",
            amount_pts=1000, password="StrongP@ss1!",
        )
        assert isinstance(t, Transfer)
        assert t.status == Transfer.STATUS_PENDING
        assert t.is_cancellable is True
        # Sender: saldo disponível caiu, mas o valor está retido em pending_pts
        assert _balance(a.id) == 9000
        assert _pending_balance(a.id) == 1000
        # Recipient: AINDA NÃO viu o crédito (janela de 60s)
        assert _balance(b.id) == 0
        # Só o débito do sender entrou no ledger
        assert db.session.query(Transaction).count() == 1


def test_transfer_promotes_after_window(app):
    """Após a janela: recipient recebe + notificação + status=committed."""
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", balance=0, cpf="22222222222")
        t = transfer_svc.send(
            a, recipient_identifier="beta@blaxx.test",
            amount_pts=1000, password="StrongP@ss1!",
        )
        promoted = _force_promote(t)
        assert promoted == 1
        db.session.refresh(t)
        assert t.status == Transfer.STATUS_COMMITTED
        # Recipient agora vê o saldo, sender saiu do pending
        assert _balance(b.id) == 1000
        assert _pending_balance(a.id) == 0
        assert _balance(a.id) == 9000


# -------------------------------------------------------------------- A2
def test_recipient_gets_notification_after_promote(app):
    """Notificação chega APÓS a janela de 60s (não imediato)."""
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        t = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=500, password="StrongP@ss1!", message="valeu")
        # Antes do promote: ainda sem notificação
        assert db.session.query(Notification).filter_by(user_id=b.id).count() == 0
        _force_promote(t)
        notes = db.session.query(Notification).filter_by(user_id=b.id).all()
        assert len(notes) == 1
        assert notes[0].type == "transfer"
        assert "500" in notes[0].body
        # remetente NÃO recebe notificação
        assert db.session.query(Notification).filter_by(user_id=a.id).count() == 0


# -------------------------------------------------------------------- A3
def test_audit_event_recorded_at_pending(app):
    """Audit transfer_pending registrado imediatamente; transfer_committed após promote."""
    with app.test_request_context(
        "/transfer",
        headers={"User-Agent": "pytest-agent", "X-Forwarded-For": "203.0.113.7"},
    ):
        a = _make_user("alpha@blaxx.test", balance=5000, cpf="11111111111")
        _make_user("beta@blaxx.test", cpf="22222222222")
        t = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=300, password="StrongP@ss1!",
                              device_id="dev-123", platform="ios")
        log = db.session.query(AuditLog).filter_by(event="transfer_pending").one()
        assert log.user_id == a.id
        assert log.ip == "203.0.113.7"
        assert log.user_agent == "pytest-agent"
        assert log.device_id == "dev-123"
        assert "ios" in (log.extra_data or "")
        # Promove e checa o evento de commit
        _force_promote(t)
        commit_log = db.session.query(AuditLog).filter_by(event="transfer_committed").one()
        assert commit_log.user_id == a.id


# ----------------------------------------------------- ToS Sec. 9.2: cancel
def test_cancel_within_window_reverses_pending(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        t = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=1000, password="StrongP@ss1!")
        assert t.status == Transfer.STATUS_PENDING
        cancelled = transfer_svc.cancel(t.id, sender=a)
        assert cancelled.status == Transfer.STATUS_CANCELLED
        # Saldo volta ao normal — pending_pts zerado, balance restaurado
        assert _balance(a.id) == 10_000
        assert _pending_balance(a.id) == 0
        # Recipient nunca viu
        assert _balance(b.id) == 0


def test_cancel_after_window_blocks(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        _make_user("beta@blaxx.test", cpf="22222222222")
        t = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=1000, password="StrongP@ss1!")
        _force_promote(t)
        with pytest.raises(transfer_svc.TransferError, match="já efetivada"):
            transfer_svc.cancel(t.id, sender=a)


def test_cancel_by_non_sender_blocks(app):
    with app.test_request_context("/transfer", headers={"User-Agent": "pytest"}):
        a = _make_user("alpha@blaxx.test", balance=10_000, cpf="11111111111")
        b = _make_user("beta@blaxx.test", cpf="22222222222")
        t = transfer_svc.send(a, recipient_identifier="beta@blaxx.test",
                              amount_pts=1000, password="StrongP@ss1!")
        with pytest.raises(transfer_svc.TransferError, match="remetente"):
            transfer_svc.cancel(t.id, sender=b)


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
        # Debitou só uma vez do balance (vai pro pending)
        assert _balance(a.id) == 9000
        assert _pending_balance(a.id) == 1000
        # Recipient ainda não vê (estado pending)
        assert _balance(b.id) == 0
        assert db.session.query(Transfer).count() == 1
        # Após promover: recipient vê, pending zera
        _force_promote(t1)
        assert _balance(b.id) == 1000
        assert _pending_balance(a.id) == 0


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
