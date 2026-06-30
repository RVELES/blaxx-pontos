"""Endpoints de saldo e extrato."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Transaction, Wallet
from ..services import transfer as transfer_svc
from .auth import login_required

bp = Blueprint("wallet", __name__)


def _lazy_promote_pending(user_id: str) -> None:
    """Promove silenciosamente transferências P2P pending cuja janela de 60s
    já passou. Chamado em endpoints de leitura para o saldo refletir a
    realidade sem depender de cron.

    Falha aqui NÃO derruba o request (best-effort): se a promoção falhar,
    a próxima leitura tenta de novo.
    """
    try:
        transfer_svc.promote_pending_for_user(user_id)
    except Exception:
        from flask import current_app
        current_app.logger.warning(
            "lazy promote_pending falhou para user=%s", user_id, exc_info=True
        )


@bp.get("/")
@login_required
def get_wallet():
    _lazy_promote_pending(g.current_user.id)
    wallet = db.session.query(Wallet).filter_by(user_id=g.current_user.id).one()
    return jsonify(wallet.to_dict())


@bp.get("/transactions")
@login_required
def list_transactions():
    _lazy_promote_pending(g.current_user.id)
    limit = min(int(request.args.get("limit", 20)), 100)
    wallet = db.session.query(Wallet).filter_by(user_id=g.current_user.id).one()
    txs = (
        db.session.query(Transaction)
        .filter_by(wallet_id=wallet.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [t.to_dict() for t in txs]})
