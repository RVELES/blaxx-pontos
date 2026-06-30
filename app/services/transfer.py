"""Envio de pontos entre clientes inscritos (P2P interno).

Regras (espelham as telas enviar-pontos.html / confirmar-envio.html):
  * Mínimo 100 pts, máximo 50.000 pts/dia (somando todos os envios).
  * Sem taxa.
  * Identifica destinatário por e-mail OU CPF.
  * Exige senha do remetente para confirmar.
  * Não pode enviar para si mesmo.
  * Gera receipt_code (ENV-2026-XXXX-XXXX) — comprovante.
  * Atômico: se algo falhar, nem débito nem crédito acontecem.

Anti-duplicidade (Sprint QA — bug A1):
  * Idempotência exata: se o cliente mandar uma chave (`idempotency_key`,
    via header Idempotency-Key ou body request_id), um reenvio com a mesma
    chave NÃO gera segundo débito/crédito — devolve a transferência original.
    Implementado sobre Transaction.idempotency_key (já tem UNIQUE
    (wallet_id, idempotency_key)), sem mexer no schema.
  * Rede de segurança sem chave: detecta duplicata acidental (mesmo
    remetente→destinatário→valor) dentro de uma janela curta e devolve a
    transferência original em vez de debitar de novo (combate double-submit /
    retry de rede mesmo em clientes que ainda não mandam chave).

Notificação (bug A2) e auditoria (bug A3) acontecem dentro da mesma
transação do débito/crédito — tudo confirma junto ou nada confirma.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..config import Config
from ..extensions import db
from ..models import Notification, Transaction, Transfer, TxType, User, Wallet
from . import audit as audit_svc
from . import wallet as wallet_svc


class TransferError(Exception):
    pass


_CPF_RE = re.compile(r"\D+")
_CARD_ID_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)

# Janela curta p/ deduplicar reenvios acidentais quando o cliente NÃO mandou
# idempotency_key (double-tap no botão, retry automático de rede).
_DUP_WINDOW_SECONDS = 15


def _normalize_cpf(value: str) -> str:
    return _CPF_RE.sub("", value)


def find_recipient(identifier: str) -> User | None:
    """Aceita e-mail, CPF (com ou sem máscara) ou Cartão BlaXx (8 hex)."""
    identifier = (identifier or "").strip().lower()
    if "@" in identifier:
        return db.session.query(User).filter_by(email=identifier).one_or_none()
    cpf = _normalize_cpf(identifier)
    if cpf:
        user = db.session.query(User).filter_by(cpf=cpf).one_or_none()
        if user:
            return user
    card_id = re.sub(r"[\s\-]", "", identifier)
    if _CARD_ID_RE.match(card_id):
        return db.session.query(User).filter(
            User.id.like(f"{card_id}%")
        ).one_or_none()
    return None


def _out_key(sender_id: str, client_key: str) -> str:
    """Chave de idempotência do débito do remetente derivada da chave do cliente."""
    return f"transfer-out:{sender_id}:{client_key}"


def _in_key(sender_id: str, client_key: str) -> str:
    return f"transfer-in:{sender_id}:{client_key}"


def _transfer_for_debit_key(sender_id: str, out_key: str) -> Transfer | None:
    """Acha a Transfer original a partir do débito idempotente já registrado."""
    tx = (
        db.session.query(Transaction)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .filter(
            Wallet.user_id == sender_id,
            Transaction.idempotency_key == out_key,
        )
        .one_or_none()
    )
    if tx is None or not tx.reference:
        return None
    return db.session.get(Transfer, tx.reference)


def _recent_duplicate(
    sender_id: str, recipient_id: str, amount_pts: int
) -> Transfer | None:
    """Última transferência idêntica dentro da janela anti-double-submit."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DUP_WINDOW_SECONDS)
    return (
        db.session.query(Transfer)
        .filter(
            Transfer.sender_id == sender_id,
            Transfer.recipient_id == recipient_id,
            Transfer.amount_pts == amount_pts,
            Transfer.created_at >= cutoff,
        )
        .order_by(Transfer.created_at.desc())
        .first()
    )


def send(
    sender: User,
    *,
    recipient_identifier: str,
    amount_pts: int,
    password: str,
    message: str | None = None,
    idempotency_key: str | None = None,
    device_id: str | None = None,
    platform: str | None = None,
    mfa_code: str | None = None,
) -> Transfer:
    if not sender.check_password(password):
        raise TransferError("senha incorreta")

    if amount_pts < Config.TRANSFER_MIN_POINTS:
        raise TransferError(
            f"valor mínimo é {Config.TRANSFER_MIN_POINTS} pts"
        )

    # B13 · step-up 2FA p/ valores altos (só p/ quem tem 2FA ativo).
    from ..security import enforce_step_up_mfa
    enforce_step_up_mfa(sender, amount_pts, mfa_code)

    recipient = find_recipient(recipient_identifier)
    if recipient is None:
        raise TransferError("destinatário não encontrado")

    if recipient.id == sender.id:
        raise TransferError("não é possível enviar pontos para si mesmo")

    client_key = (idempotency_key or "").strip()[:64] or None

    # (A1) Idempotência exata: reenvio com a mesma chave devolve o original.
    if client_key:
        existing = _transfer_for_debit_key(sender.id, _out_key(sender.id, client_key))
        if existing is not None:
            return existing
    else:
        # (A1) Rede de segurança contra double-submit sem chave do cliente.
        dup = _recent_duplicate(sender.id, recipient.id, amount_pts)
        if dup is not None:
            return dup

    # VIP: usuários marcados pelo admin podem transferir sem limite diário.
    if not sender.is_vip:
        sent_today = wallet_svc.debited_today(sender.id, TxType.TRANSFER_OUT)
        if sent_today + amount_pts > Config.TRANSFER_MAX_POINTS_PER_DAY:
            remaining = Config.TRANSFER_MAX_POINTS_PER_DAY - sent_today
            raise TransferError(
                f"limite diário excedido — restam {max(remaining,0)} pts hoje"
            )

    if message and len(message) > 140:
        raise TransferError("mensagem com mais de 140 caracteres")

    transfer = Transfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        amount_pts=amount_pts,
        message=message,
        receipt_code=Transfer.make_receipt(),
    )
    db.session.add(transfer)
    db.session.flush()

    # Chaves de idempotência do ledger: usam a chave do cliente quando houver
    # (replay exato), senão caem no id da transfer (comportamento anterior).
    out_key = _out_key(sender.id, client_key) if client_key else f"transfer-out:{transfer.id}"
    in_key = _in_key(sender.id, client_key) if client_key else f"transfer-in:{transfer.id}"

    # Débito + crédito atômicos: se algo falhar, rollback derruba os dois
    try:
        wallet_svc.debit(
            user_id=sender.id,
            amount_pts=amount_pts,
            tx_type=TxType.TRANSFER_OUT,
            description=f"Envio para {recipient.name}",
            reference=transfer.id,
            idempotency_key=out_key,
        )
        wallet_svc.credit(
            user_id=recipient.id,
            amount_pts=amount_pts,
            tx_type=TxType.TRANSFER_IN,
            description=f"Recebido de {sender.name}",
            reference=transfer.id,
            idempotency_key=in_key,
        )

        # (A2) Notifica o destinatário — dentro da mesma transação.
        db.session.add(Notification(
            user_id=recipient.id,
            type="transfer",
            title="Você recebeu pontos",
            body=(
                f"{sender.name} te enviou {amount_pts} pts."
                + (f' "{message}"' if message else "")
            ),
            icon="↪",
            reference=transfer.id,
        ))

        # (A3) Auditoria com IP/user-agent (auto) + dispositivo/plataforma.
        audit_svc.log_event(
            "transfer_sent",
            user_id=sender.id,
            status="ok",
            device_id=device_id,
            extra={
                "transfer_id": transfer.id,
                "receipt_code": transfer.receipt_code,
                "recipient_id": recipient.id,
                "amount_pts": amount_pts,
                "platform": platform,
                "idempotent": bool(client_key),
            },
            commit=False,
        )

        # (B14) Detecção de transação suspeita — alerta admin (não bloqueia).
        from . import fraud as fraud_svc
        fraud_svc.evaluate_transfer(sender.id, recipient.id, amount_pts)
    except wallet_svc.InsufficientBalance as exc:
        db.session.rollback()
        raise TransferError(str(exc)) from exc
    except IntegrityError as exc:
        # Corrida: dois reenvios simultâneos com a mesma idempotency_key —
        # o UNIQUE (wallet_id, idempotency_key) barra o segundo débito.
        db.session.rollback()
        if client_key:
            existing = _transfer_for_debit_key(sender.id, _out_key(sender.id, client_key))
            if existing is not None:
                return existing
        raise TransferError("transferência duplicada detectada") from exc

    db.session.commit()
    return transfer
