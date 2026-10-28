"""Endpoints de benefícios (marketplace) e vouchers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from ..extensions import db, limiter
from ..models import Benefit, Voucher, Notification, TxType
from ..services import wallet as wallet_svc
from ..services.wallet import InsufficientBalance
from .auth import login_required, email_verified_required

bp_benefits = Blueprint("benefits", __name__)
bp_vouchers = Blueprint("vouchers", __name__)


# --------------------- Benefícios (catálogo) ---------------------- #

@bp_benefits.get("/")
def list_benefits():
    """Cards do marketplace. Filtros: ?category=, ?max_pts=, ?partner_id=."""
    q = db.session.query(Benefit).filter_by(is_active=True)

    if (category := request.args.get("category", "").strip()):
        q = q.filter(Benefit.category == category)
    if (partner_id := request.args.get("partner_id", "").strip()):
        q = q.filter(Benefit.partner_id == partner_id)
    if (max_pts := request.args.get("max_pts", "").strip()):
        try: q = q.filter(Benefit.cost_pts <= int(max_pts))
        except ValueError: pass

    items = q.order_by(Benefit.cost_pts.asc()).all()
    return jsonify({"items": [b.to_dict() for b in items]})


@bp_benefits.get("/<benefit_id>")
def get_benefit(benefit_id: str):
    b = db.session.get(Benefit, benefit_id)
    if b is None or not b.is_active:
        return jsonify({"error": "Benefício não encontrado"}), 404
    return jsonify(b.to_dict())


@bp_benefits.post("/<benefit_id>/redeem")
@login_required
@email_verified_required
@limiter.limit("20 per hour",
               key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
def redeem(benefit_id: str):
    """Resgata um benefício: debita pontos e emite um Voucher.

    Achado M-2 (revisão de 20/07), corrigido aqui em quatro pontos:

      · idempotência era decorativa — a chave embutia o timestamp da chamada,
        então double-tap debitava duas vezes e emitia dois vouchers;
      · `stock -= 1` sem lock permitia overselling sob concorrência;
      · faltavam `@email_verified_required` e rate limit, que `/redeem` e
        `/transfer` já tinham;
      · o caminho de saldo insuficiente não fazia rollback explícito.

    A chave de idempotência vem do header `Idempotency-Key`. Sem ele, deriva de
    (benefício, usuário, minuto): colapsa o double-tap sem impedir que a pessoa
    resgate o mesmo item de novo mais tarde.
    """
    user = g.current_user
    chave = request.headers.get("Idempotency-Key")
    if not chave:
        minuto = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        chave = f"benefit:{benefit_id}:{user.id}:{minuto}"
    chave = chave[:128]

    # Replay: devolve o voucher já emitido em vez de debitar de novo.
    ja = db.session.query(Voucher).filter_by(
        user_id=user.id, idempotency_key=chave).one_or_none()
    if ja is not None:
        return jsonify(ja.to_dict()), 200

    # with_for_update: sem o lock, duas requisições liam o mesmo estoque e
    # ambas decrementavam — vendia mais do que existe.
    benefit = (
        db.session.query(Benefit)
        .filter_by(id=benefit_id)
        .with_for_update()
        .one_or_none()
    )
    if benefit is None or not benefit.is_active:
        db.session.rollback()
        return jsonify({"error": "Benefício não encontrado"}), 404
    if benefit.stock == 0:
        db.session.rollback()
        return jsonify({"error": "Sem estoque disponível"}), 409
    if benefit.stock > 0:
        benefit.stock -= 1

    try:
        wallet_svc.debit(
            user_id=user.id,
            amount_pts=benefit.cost_pts,
            tx_type=TxType.REDEEM,
            description=f"Resgate: {benefit.name}",
            reference=f"benefit:{benefit.id}",
            idempotency_key=chave,
        )
    except InsufficientBalance:
        db.session.rollback()          # explícito: não depender do teardown
        return jsonify({"error": "Saldo insuficiente"}), 402

    voucher = Voucher(
        user_id=user.id,
        benefit_id=benefit.id,
        code=Voucher.make_code(),
        points_spent=benefit.cost_pts,
        expires_at=datetime.now(timezone.utc) + timedelta(days=benefit.expires_in_days),
        idempotency_key=chave,
    )
    db.session.add(voucher)

    db.session.add(Notification(
        user_id=user.id, type="voucher",
        title="Voucher emitido",
        body=f"Seu voucher {voucher.code} está disponível. Use até {voucher.expires_at.strftime('%d/%m/%Y')}.",
        icon="✦",
        reference=voucher.id,
    ))
    db.session.commit()
    from ..services import avisos_email
    avisos_email.troca_produto(voucher, benefit)
    return jsonify(voucher.to_dict()), 201


# --------------------- Vouchers (do usuário) ---------------------- #

@bp_vouchers.get("/")
@login_required
def list_my_vouchers():
    user_id = g.current_user.id
    items = (
        db.session.query(Voucher)
        .filter_by(user_id=user_id)
        .order_by(Voucher.created_at.desc())
        .all()
    )
    return jsonify({"items": [v.to_dict() for v in items]})


@bp_vouchers.get("/<voucher_id>")
@login_required
def get_voucher(voucher_id: str):
    v = db.session.get(Voucher, voucher_id)
    if v is None or v.user_id != g.current_user.id:
        return jsonify({"error": "Voucher não encontrado"}), 404
    return jsonify(v.to_dict())
