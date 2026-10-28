"""Ponte entre as operações de pontos e os e-mails transacionais.

Cada função recebe os objetos já gravados (commit feito), lê o que o e-mail
precisa enquanto a sessão está aberta e entrega o envio ao `mailer.disparar`,
que roda em segundo plano. Nada aqui pode derrubar a operação: qualquer erro
vira log e a movimentação segue valendo.
"""

from __future__ import annotations

import logging

from ..extensions import db
from ..models import User, Wallet
from . import mailer

logger = logging.getLogger(__name__)


def _usuario(user_id: str) -> User | None:
    return db.session.get(User, user_id)


def _saldo(user_id: str) -> int | None:
    w = db.session.query(Wallet).filter_by(user_id=user_id).one_or_none()
    return int(w.balance_pts) if w is not None else None


def _seguro(fn):
    def envolto(*a, **kw):
        try:
            fn(*a, **kw)
        except Exception:
            logger.exception("aviso por e-mail %s falhou", fn.__name__)
    envolto.__name__ = fn.__name__
    return envolto


@_seguro
def compra_confirmada(charge, *, meio: str) -> None:
    u = _usuario(charge.user_id)
    if u is None or not u.email:
        return
    mailer.disparar(
        mailer.send_compra_confirmada, u.email, u.name,
        pontos=charge.points_to_credit, valor_cents=charge.amount_cents,
        meio=meio, referencia=charge.id, saldo=_saldo(u.id),
    )


@_seguro
def transferencia_concluida(t) -> None:
    sender, recipient = _usuario(t.sender_id), _usuario(t.recipient_id)
    if sender is None or recipient is None:
        return
    if sender.email:
        mailer.disparar(
            mailer.send_envio_concluido, sender.email, sender.name,
            pontos=t.amount_pts, destinatario=recipient.name,
            codigo=t.receipt_code, mensagem=t.message, saldo=_saldo(sender.id),
        )
    if recipient.email:
        mailer.disparar(
            mailer.send_pontos_recebidos, recipient.email, recipient.name,
            pontos=t.amount_pts, remetente=sender.name,
            codigo=t.receipt_code, mensagem=t.message, saldo=_saldo(recipient.id),
        )


@_seguro
def venda(payout) -> None:
    """Escolhe o e-mail pelo status do resgate via Pix (venda de pontos)."""
    u = _usuario(payout.user_id)
    if u is None or not u.email:
        return
    status = getattr(payout.status, "value", payout.status)
    if status == "paid":
        mailer.disparar(mailer.send_venda_paga, u.email, u.name,
                        pontos=payout.points_debited,
                        valor_cents=payout.amount_cents, chave_pix=payout.pix_key)
    elif status == "failed":
        mailer.disparar(mailer.send_venda_falhou, u.email, u.name,
                        pontos=payout.points_debited,
                        valor_cents=payout.amount_cents,
                        motivo=payout.failure_reason or "falha no Pix")
    else:
        mailer.disparar(mailer.send_venda_solicitada, u.email, u.name,
                        pontos=payout.points_debited,
                        valor_cents=payout.amount_cents,
                        chave_pix=payout.pix_key, saldo=_saldo(u.id))


@_seguro
def troca_produto(voucher, benefit) -> None:
    u = _usuario(voucher.user_id)
    if u is None or not u.email:
        return
    mailer.disparar(
        mailer.send_troca_produto, u.email, u.name,
        produto=benefit.name, pontos=voucher.points_spent,
        codigo_voucher=voucher.code,
        validade=voucher.expires_at.strftime("%d/%m/%Y"),
        saldo=_saldo(u.id),
    )
