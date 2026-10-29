"""Módulo Administrador.

Endpoints (todos exigem login + role='admin'):

  GET   /admin/users                  → paginado, busca por nome/email/cpf
  GET   /admin/users/<id>             → detalhe + perfil + métricas
  PATCH /admin/users/<id>/vip         → seta is_vip (corpo obrigatório)
  PATCH /admin/users/<id>/role        → promove/rebaixa admin (cuidado)
  GET   /admin/transactions           → todas as transações do sistema
  GET   /admin/transactions/<user_id> → transações de um user específico
  GET   /admin/stats                  → totais agregados (users, balance, vol PIX)

Segurança:
  - Acesso restrito por role='admin'
  - Toda ação é logada (futuro audit_logs)
  - PATCH /role retorna 403 se admin tentar rebaixar a si mesmo (evita lock-out)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import or_, func, select

from ..config import Config
from ..extensions import db, limiter
from ..models import PointPackage, Transaction, TxType, User, Wallet
from .auth import login_required

bp = Blueprint("admin", __name__)


# ─────────────────────────── decorator ─────────────────────────── #

def admin_required(fn):
    """Garante que o usuário autenticado tem role='admin'.

    Use SEMPRE depois de @login_required.
    Retorna 403 com mensagem genérica (não revela que o endpoint existe).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = getattr(g, "current_user", None)
        if u is None or u.role != "admin":
            return jsonify({"error": "acesso restrito"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ─────────────────────────── /users ─────────────────────────── #

@bp.get("/users")
@login_required
@admin_required
def list_users():
    """Lista usuários paginados. Aceita ?q=, ?limit=, ?offset=, ?role=, ?vip=true."""
    q = (request.args.get("q") or "").strip().lower()
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    role_filter = request.args.get("role")
    vip_filter = request.args.get("vip")

    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(User.name.ilike(like), User.email.ilike(like), User.cpf.ilike(like))
        )
    if role_filter in ("user", "admin"):
        stmt = stmt.where(User.role == role_filter)
    if vip_filter == "true":
        stmt = stmt.where(User.is_vip.is_(True))
    elif vip_filter == "false":
        stmt = stmt.where(User.is_vip.is_(False))

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    users = db.session.execute(stmt).scalars().all()

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [u.to_admin_dict() for u in users],
    })


@bp.get("/users/<user_id>")
@login_required
@admin_required
def get_user(user_id: str):
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    data = u.to_admin_dict()
    # Adiciona últimas 20 transações pra contexto
    if u.wallet:
        recent = (
            db.session.query(Transaction)
            .filter_by(wallet_id=u.wallet.id)
            .order_by(Transaction.created_at.desc())
            .limit(20)
            .all()
        )
        data["recent_transactions"] = [t.to_dict() for t in recent]
    return jsonify(data)


@bp.patch("/users/<user_id>/vip")
@login_required
@admin_required
def set_vip(user_id: str):
    """Seta is_vip. Body OBRIGATÓRIO: {"is_vip": true/false}.

    S13: corpo vazio alternava o valor. Uma sondagem de varredura administrativa
    inverteu o is_vip de uma conta real em produção — sem intenção, só de bater
    no endpoint para ver o status HTTP. is_vip governa o teto diário de resgate;
    uma rota que muda esse teto não pode mudar estado sem instrução explícita.
    Nenhum chamador real (PWA, iOS) depende do toggle — ambos sempre mandam o
    valor.
    """
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    body = request.get_json(silent=True) or {}
    if "is_vip" not in body:
        return jsonify({"error": "corpo deve conter is_vip (true/false)"}), 400
    u.is_vip = bool(body["is_vip"])
    db.session.commit()
    return jsonify({"id": u.id, "is_vip": u.is_vip})


@bp.patch("/users/<user_id>/role")
@login_required
@admin_required
def set_role(user_id: str):
    """Promove/rebaixa role. Body: {"role": "admin"|"user"}.
    Bloqueia o admin de rebaixar a si mesmo (proteção contra lock-out)."""
    body = request.get_json(silent=True) or {}
    new_role = (body.get("role") or "").lower()
    if new_role not in ("user", "admin"):
        return jsonify({"error": "role inválido (use 'user' ou 'admin')"}), 400
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    if u.id == g.current_user.id and new_role != "admin":
        return jsonify({"error": "você não pode remover seu próprio acesso admin"}), 403
    u.role = new_role
    db.session.commit()
    return jsonify({"id": u.id, "role": u.role})


# ─────────────────────────── /transactions ─────────────────────────── #

@bp.get("/transactions")
@login_required
@admin_required
def all_transactions():
    """Todas as transações do sistema, paginadas. Filtros: ?type=, ?user_id=."""
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    tx_type = request.args.get("type")
    user_id = request.args.get("user_id")

    stmt = select(Transaction).join(Wallet, Wallet.id == Transaction.wallet_id)
    if tx_type:
        try:
            stmt = stmt.where(Transaction.type == TxType(tx_type))
        except ValueError:
            return jsonify({"error": f"type inválido: {tx_type}"}), 400
    if user_id:
        stmt = stmt.where(Wallet.user_id == user_id)

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    stmt = stmt.order_by(Transaction.created_at.desc()).limit(limit).offset(offset)
    txs = db.session.execute(stmt).scalars().all()

    # Enriquece com nome do user
    items = []
    for t in txs:
        d = t.to_dict()
        w = db.session.get(Wallet, t.wallet_id)
        if w:
            u = db.session.get(User, w.user_id)
            d["user_name"] = u.name if u else "—"
            d["user_email"] = u.email if u else "—"
        items.append(d)

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": items})


# ─────────────────────────── /stats ─────────────────────────── #

@bp.get("/stats")
@login_required
@admin_required
def stats():
    """Totais agregados do sistema. Útil pra dashboard do admin."""
    total_users = db.session.execute(
        select(func.count()).select_from(User)
    ).scalar_one()
    total_admins = db.session.execute(
        select(func.count()).select_from(User).where(User.role == "admin")
    ).scalar_one()
    total_vips = db.session.execute(
        select(func.count()).select_from(User).where(User.is_vip.is_(True))
    ).scalar_one()
    verified = db.session.execute(
        select(func.count()).select_from(User).where(User.email_verified_at.is_not(None))
    ).scalar_one()

    # Saldo total em pontos no sistema
    total_balance = db.session.execute(
        select(func.coalesce(func.sum(Wallet.balance_pts), 0))
    ).scalar_one() or 0

    # Volume por tipo de transação (últimos 30 dias)
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    vol_by_type = {}
    for tx_type in TxType:
        v = db.session.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_pts)), 0))
            .where(Transaction.type == tx_type)
            .where(Transaction.created_at >= cutoff)
        ).scalar_one() or 0
        vol_by_type[tx_type.value] = int(v)

    # Pagamentos PIX pendentes de confirmação manual
    from ..models import PixCharge, PixChargeStatus
    pending_payments = db.session.execute(
        select(func.count()).select_from(PixCharge).where(
            PixCharge.status == PixChargeStatus.PENDING_CONFIRMATION
        )
    ).scalar_one() or 0

    return jsonify({
        "total_users": total_users,
        "total_admins": total_admins,
        "total_vips": total_vips,
        "email_verified_users": verified,
        "total_balance_pts": int(total_balance),
        "volume_last_30d_by_type": vol_by_type,
        "pending_payments": int(pending_payments),
    })


# ─────────────────────── PIX manual · confirmação ─────────────────────── #

@bp.get("/charges/pending")
@login_required
@admin_required
def list_pending_charges():
    """Lista charges aguardando admin confirmar (PENDING_CONFIRMATION).

    Sprint 4 (S4-7): JOIN unico com User (era N+1 — 1 query base + N
    db.session.get(User) no loop). Pra 200 rows isso era 201 queries.
    """
    from ..models import PixCharge, PixChargeStatus
    rows = (
        db.session.query(PixCharge, User)
        .join(User, PixCharge.user_id == User.id)
        .filter(PixCharge.status == PixChargeStatus.PENDING_CONFIRMATION)
        .order_by(PixCharge.claimed_paid_at.asc())
        .limit(200)
        .all()
    )
    items = []
    for c, u in rows:
        d = c.to_dict()
        d["user_name"] = u.name if u else "—"
        d["user_email"] = u.email if u else "—"
        items.append(d)
    return jsonify({"items": items, "total": len(items)})


@bp.post("/charges/<charge_id>/confirm")
@login_required
@admin_required
def admin_confirm_charge(charge_id: str):
    """Admin confirma o recebimento do PIX → libera os pontos."""
    from datetime import datetime, timezone
    from ..models import PixCharge, PixChargeStatus, TxType, Notification
    from ..services import wallet as wallet_svc

    charge = db.session.get(PixCharge, charge_id)
    if charge is None:
        return jsonify({"error": "charge não encontrada"}), 404
    # Confirmação manual é só para o fluxo manual (QR estático "Já paguei").
    # Charges automáticas (flow="mp") são creditadas exclusivamente pelo
    # webhook do provedor — confirmá-las na mão creditaria antes do pagamento.
    if charge.flow != "manual":
        return jsonify({"error": "apenas charges do fluxo manual podem ser confirmadas aqui"}), 400
    if charge.status not in (PixChargeStatus.PENDING_CONFIRMATION,
                              PixChargeStatus.PENDING):
        return jsonify({"error": f"charge não pode ser confirmada (status atual: {charge.status.value})"}), 400

    charge.status = PixChargeStatus.PAID
    charge.paid_at = datetime.now(timezone.utc)
    charge.confirmed_by_user_id = g.current_user.id

    wallet_svc.credit(
        user_id=charge.user_id,
        amount_pts=charge.points_to_credit,
        tx_type=TxType.PURCHASE,
        description=f"Compra de pontos PIX (manual) — R$ {charge.amount_cents/100:.2f}",
        reference=charge.id,
        idempotency_key=f"charge:{charge.id}",
    )

    # Notifica o cliente
    db.session.add(Notification(
        user_id=charge.user_id, type="system",
        title="Pontos liberados!",
        body=f"Recebemos seu PIX de R$ {charge.amount_cents/100:.2f} · "
             f"{charge.points_to_credit} pts creditados.",
        icon="✓",
        reference=charge.id,
    ))
    db.session.commit()
    return jsonify({"ok": True, "charge": charge.to_dict()})


@bp.post("/charges/<charge_id>/reject")
@login_required
@admin_required
def admin_reject_charge(charge_id: str):
    """Admin rejeita (não recebeu o PIX)."""
    from ..models import PixCharge, PixChargeStatus, Notification

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "PIX não recebido").strip()

    charge = db.session.get(PixCharge, charge_id)
    if charge is None:
        return jsonify({"error": "charge não encontrada"}), 404
    if charge.flow != "manual":
        return jsonify({"error": "apenas charges do fluxo manual podem ser rejeitadas aqui"}), 400
    if charge.status not in (PixChargeStatus.PENDING_CONFIRMATION,
                              PixChargeStatus.PENDING):
        return jsonify({"error": "charge já está em status final"}), 400

    charge.status = PixChargeStatus.REJECTED
    charge.confirmed_by_user_id = g.current_user.id

    db.session.add(Notification(
        user_id=charge.user_id, type="system",
        title="Pagamento PIX não confirmado",
        body=f"Sua compra de R$ {charge.amount_cents/100:.2f} não foi creditada: {reason}. "
             f"Se você realmente pagou, mostre o comprovante no suporte.",
        icon="⚠",
        reference=charge.id,
    ))
    db.session.commit()
    return jsonify({"ok": True, "status": charge.status.value, "reason": reason})


# =========================================================================
# Payouts PIX (venda/resgate) — fila manual · PAYOUT_MODE=manual
# =========================================================================
# Enquanto não há provider de payout integrado (Efí/Stark/MP Money Out),
# o resgate fica PROCESSING com os pontos debitados. O admin executa a
# transferência PIX no banco e confirma aqui (ou marca falha → estorno).


@bp.get("/payouts/processing")
@login_required
@admin_required
def list_processing_payouts():
    """Lista payouts aguardando execução manual (PROCESSING/REQUESTED)."""
    from ..models import PixPayout, PixPayoutStatus
    rows = (
        db.session.query(PixPayout, User)
        .join(User, PixPayout.user_id == User.id)
        .filter(PixPayout.status.in_(
            (PixPayoutStatus.PROCESSING, PixPayoutStatus.REQUESTED)))
        .order_by(PixPayout.created_at.asc())
        .limit(200)
        .all()
    )
    items = []
    for p, u in rows:
        d = p.to_dict()
        d["user_name"] = u.name if u else "—"
        d["user_email"] = u.email if u else "—"
        d["user_cpf"] = u.cpf if u else "—"
        items.append(d)
    return jsonify({"items": items, "total": len(items)})


@bp.post("/payouts/<payout_id>/confirm")
@login_required
@admin_required
def admin_confirm_payout(payout_id: str):
    """Admin confirma que o PIX foi transferido → payout vira PAID.

    Body opcional: {"end_to_end_id": "E123..."} (comprovante do banco).
    """
    from ..models import PixPayout, Notification
    from ..services import redeem as redeem_svc

    payout = db.session.get(PixPayout, payout_id)
    if payout is None:
        return jsonify({"error": "payout não encontrado"}), 404

    data = request.get_json(silent=True) or {}
    try:
        payout = redeem_svc.confirm_payout(
            payout.txid,
            end_to_end_id=(data.get("end_to_end_id") or "").strip() or None,
        )
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400

    db.session.add(Notification(
        user_id=payout.user_id, type="system",
        title="Resgate concluído!",
        body=f"R$ {payout.amount_cents/100:.2f} transferidos via PIX "
             f"para {payout.pix_key}.",
        icon="✓",
        reference=payout.id,
    ))
    db.session.commit()
    return jsonify({"ok": True, "payout": payout.to_dict()})


@bp.post("/payouts/<payout_id>/fail")
@login_required
@admin_required
def admin_fail_payout(payout_id: str):
    """Admin marca o payout como falho (ex.: chave PIX inexistente) → estorno."""
    from ..models import PixPayout
    from ..services import redeem as redeem_svc

    payout = db.session.get(PixPayout, payout_id)
    if payout is None:
        return jsonify({"error": "payout não encontrado"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "transferência PIX não pôde ser executada").strip()
    try:
        payout = redeem_svc.fail_payout(payout.txid, reason=reason)
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "payout": payout.to_dict()})


# =========================================================================
# Sprint 2 · Expiracao de pontos (cron mensal disparado por endpoint admin)
# =========================================================================

@bp.post("/expire-points")
@login_required
@admin_required
def admin_expire_points():
    """Dispara varredura de expiracao de pontos > 24 meses.

    Body opcional:
        { "dry_run": true }  # so calcula, nao commita

    Para uso em cron mensal: configure um Render Cron Service ou GitHub
    Actions chamando POST /admin/expire-points com Authorization Bearer
    de um admin tecnico (idealmente um service account dedicado).

    Retorna estatisticas + lista de erros (se houver).
    """
    from ..services.expiration import expire_old_points_all
    data = request.get_json(silent=True) or {}
    dry = bool(data.get("dry_run"))
    result = expire_old_points_all(dry_run=dry)
    return jsonify(result)


# =========================================================================
# Sprint 5 (S5-6) · A/B testing
# =========================================================================

@bp.get("/experiments")
@login_required
@admin_required
def list_experiments():
    """Lista experimentos registrados."""
    from ..services.experiments import list_active
    return jsonify({"items": list_active()})


# ───────────────── Moderação: bloquear / desbloquear usuário ───────────────── #

@bp.patch("/users/<user_id>/status")
@login_required
@admin_required
def set_user_status(user_id: str):
    """Bloqueia (suspended) ou reativa (active) um usuário.

    Body: {"status": "active"|"suspended", "reason": "..."}
    Usuário suspenso é barrado no /auth/login (status != active → 403).
    """
    from ..services import audit as audit_svc
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").lower().strip()
    if new_status not in ("active", "suspended"):
        return jsonify({"error": "status inválido (use 'active' ou 'suspended')"}), 400
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    if u.id == g.current_user.id and new_status != "active":
        return jsonify({"error": "você não pode suspender a si mesmo"}), 403
    u.status = new_status
    audit_svc.log_event(
        "admin_user_status", user_id=g.current_user.id, status="ok",
        reason=(body.get("reason") or None),
        extra={"target_user": u.id, "new_status": new_status}, commit=False,
    )
    db.session.commit()
    return jsonify({"id": u.id, "status": u.status})


@bp.post("/users/<user_id>/liberar-identidade")
@login_required
@admin_required
def liberar_identidade(user_id: str):
    """Fecha uma conta de TESTE e libera e-mail, CPF, celular e vínculo Google
    para um cadastro novo (ex.: gravar o vídeo de cadastro pedido pela Apple).

    Diferente de `DELETE /auth/account` (LGPD), que guarda o CPF por 5 anos:
    aqui o CPF também sai, então é só para contas de teste do próprio dono.
    O ledger fica intacto e o auditor guarda só hashes do que foi liberado.

    Body: {"confirm": "LIBERAR", "reason": "..."}
    """
    import hashlib
    from ..models import SocialAccount
    from ..services import audit as audit_svc

    body = request.get_json(silent=True) or {}
    if (body.get("confirm") or "").strip() != "LIBERAR":
        return jsonify({"error": 'confirmação inválida (envie confirm: "LIBERAR")'}), 400
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    if u.id == g.current_user.id:
        return jsonify({"error": "você não pode liberar a própria conta"}), 403
    if u.role == "admin":
        return jsonify({"error": "conta de administrador não pode ser liberada"}), 403

    def _h(v):
        return hashlib.sha256((v or "").encode()).hexdigest()[:16] if v else None

    antes = {"email_sha": _h(u.email), "cpf_sha": _h(u.cpf), "phone_sha": _h(u.phone),
             "google": bool(u.google_sub),
             "balance_pts": u.wallet.balance_pts if u.wallet else 0}
    curto = (u.id or "")[:12]
    u.email = f"liberado_{curto}@removed.local"
    u.cpf = f"L:{curto}"
    u.phone = None
    u.google_sub = None
    u.pix_key = None
    u.name = "Conta Liberada"
    u.password_hash = ""
    u.password_changed_at = datetime.now(timezone.utc)  # derruba todas as sessões
    u.status = "closed"
    db.session.query(SocialAccount).filter_by(user_id=u.id).delete()
    audit_svc.log_event(
        "admin_liberar_identidade", user_id=g.current_user.id, status="ok",
        reason=(body.get("reason") or None),
        extra={"target_user": u.id, **antes}, commit=False,
    )
    db.session.commit()
    return jsonify({"id": u.id, "status": u.status, "liberado": ["email", "cpf", "phone", "google"]})


# ───────────────── Estorno de transferência P2P ───────────────── #

@bp.post("/transfers/<transfer_id>/reverse")
@login_required
@admin_required
def reverse_transfer(transfer_id: str):
    """Estorna uma transferência P2P: debita o destinatário e devolve ao remetente.

    Body: {"reason": "justificativa obrigatória"}.
    Idempotente (não estorna duas vezes). Atômico. Auditado.
    """
    from ..models import Transfer, Transaction, Notification, TxType
    from ..services import wallet as wallet_svc, audit as audit_svc

    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    if len(reason) < 5:
        return jsonify({"error": "justificativa obrigatória (mín. 5 caracteres)"}), 400

    t = db.session.get(Transfer, transfer_id)
    if t is None:
        return jsonify({"error": "transferência não encontrada"}), 404

    out_key = f"transfer-reverse-out:{t.id}"   # débito do destinatário
    in_key = f"transfer-reverse-in:{t.id}"     # crédito de volta ao remetente

    # Idempotência: se já há crédito de estorno para o remetente, devolve.
    already = (
        db.session.query(Transaction)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .filter(Wallet.user_id == t.sender_id, Transaction.idempotency_key == in_key)
        .one_or_none()
    )
    if already is not None:
        return jsonify({"ok": True, "already_reversed": True, "transfer_id": t.id}), 200

    try:
        wallet_svc.debit(
            user_id=t.recipient_id, amount_pts=t.amount_pts, tx_type=TxType.REFUND,
            description=f"Estorno da transferência {t.receipt_code}",
            reference=t.id, idempotency_key=out_key,
        )
        wallet_svc.credit(
            user_id=t.sender_id, amount_pts=t.amount_pts, tx_type=TxType.REFUND,
            description=f"Estorno recebido — {t.receipt_code}",
            reference=t.id, idempotency_key=in_key,
        )
        db.session.add(Notification(
            user_id=t.recipient_id, type="system", title="Transferência estornada",
            body=f"{t.amount_pts} pts foram estornados. Motivo: {reason}",
            icon="↩", reference=t.id,
        ))
        audit_svc.log_event(
            "admin_transfer_reverse", user_id=g.current_user.id, status="ok", reason=reason,
            extra={"transfer_id": t.id, "amount_pts": t.amount_pts,
                   "sender_id": t.sender_id, "recipient_id": t.recipient_id}, commit=False,
        )
    except wallet_svc.InsufficientBalance:
        db.session.rollback()
        return jsonify({"error": "destinatário não tem saldo suficiente para o estorno"}), 409

    db.session.commit()
    return jsonify({"ok": True, "reversed": True, "transfer_id": t.id, "amount_pts": t.amount_pts}), 200


# ───────────────── Exportação CSV (transações) ───────────────── #

@bp.get("/export/transactions.csv")
@login_required
@admin_required
def export_transactions_csv():
    """Exporta as transações do sistema em CSV (últimas 5000)."""
    import csv
    import io
    from flask import Response

    rows = (
        db.session.query(Transaction, User)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .join(User, User.id == Wallet.user_id)
        .order_by(Transaction.created_at.desc())
        .limit(5000)
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "user_email", "type", "status", "amount_pts",
                     "description", "reference", "tx_id"])
    for tx, u in rows:
        writer.writerow([
            tx.created_at.isoformat() if tx.created_at else "",
            u.email,
            tx.type.value if hasattr(tx.type, "value") else tx.type,
            tx.status.value if hasattr(tx.status, "value") else tx.status,
            tx.amount_pts,
            (tx.description or "").replace("\n", " "),
            tx.reference or "",
            tx.id,
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=blaxx-transactions.csv"},
    )


# ───────────────── Alertas de transações suspeitas (B14) ───────────────── #

@bp.get("/alerts")
@login_required
@admin_required
def list_alerts():
    """Lista alertas de segurança/fraude (eventos de auditoria nível 'warn').

    Inclui: suspicious_transfer (B14), login_blocked_by_ip, account_locked, etc.
    """
    import json as _json
    from ..models import AuditLog
    try:
        limit = min(int(request.args.get("limit", 100) or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    rows = (
        db.session.query(AuditLog)
        .filter(AuditLog.status == "warn")
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for a in rows:
        extra = None
        if a.extra_data:
            try:
                extra = _json.loads(a.extra_data)
            except Exception:
                extra = a.extra_data
        items.append({
            "id": a.id,
            "event": a.event,
            "status": a.status,
            "reason": a.reason,
            "user_id": a.user_id,
            "ip": a.ip,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "extra": extra,
        })
    return jsonify({"items": items, "count": len(items)})

# ============================================================================
# Sprint 4 (S4-AML) · AML alerts review
# ============================================================================

@bp.get("/aml/alerts")
@login_required
@admin_required
def list_aml_alerts():
    """Lista AmlAlerts paginados. Filtros: ?severity=, ?kind=, ?resolved=true|false."""
    from ..models import AmlAlert

    try:
        limit = min(int(request.args.get("limit", 50) or 50), 200)
        offset = max(int(request.args.get("offset", 0) or 0), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    severity = (request.args.get("severity") or "").strip().lower() or None
    kind = (request.args.get("kind") or "").strip().lower() or None
    resolved = (request.args.get("resolved") or "").strip().lower()

    stmt = select(AmlAlert)
    if severity in ("low", "medium", "high"):
        stmt = stmt.where(AmlAlert.severity == severity)
    if kind:
        stmt = stmt.where(AmlAlert.kind == kind)
    if resolved == "true":
        stmt = stmt.where(AmlAlert.resolved_at.is_not(None))
    elif resolved == "false":
        stmt = stmt.where(AmlAlert.resolved_at.is_(None))

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    stmt = stmt.order_by(AmlAlert.created_at.desc()).limit(limit).offset(offset)
    rows = db.session.execute(stmt).scalars().all()

    return jsonify({
        "total": total, "limit": limit, "offset": offset,
        "items": [a.to_dict() for a in rows],
    })


@bp.post("/aml/alerts/<alert_id>/resolve")
@login_required
@admin_required
def resolve_aml_alert(alert_id: str):
    """Marca alerta como resolvido. Body: { \"note\": \"...\" }"""
    from datetime import datetime, timezone
    from ..models import AmlAlert

    alert = db.session.get(AmlAlert, alert_id)
    if alert is None:
        return jsonify({"error": "alert não encontrado"}), 404
    if alert.resolved_at is not None:
        return jsonify({"error": "já resolvido", "resolved_at": alert.resolved_at.isoformat()}), 400
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()[:500] or None
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolved_by = g.current_user.id
    alert.resolution_note = note
    db.session.commit()
    return jsonify(alert.to_dict())


# ─────────────────────────── /packages (preços editáveis) ─────────────────────────── #
# SENSÍVEL: price_cents é o valor cobrado do cliente no PIX. É a MESMA fonte que
# alimenta GET /pix/packages (o que o cliente vê) e create_charge (o que paga).

# Faixas de guarda — o preço cobra dinheiro real; validar sempre.
_PKG_PRICE_CENTS_MIN = 100            # R$ 1,00
_PKG_PRICE_CENTS_MAX = 100_000_00     # R$ 100.000,00
_PKG_POINTS_MIN = 1
_PKG_POINTS_MAX = 100_000_000


@bp.get("/packages")
@login_required
@admin_required
def list_all_packages():
    """Todos os pacotes (inclui inativos), ordenados — para o Admin editar."""
    rows = (
        db.session.query(PointPackage)
        .order_by(PointPackage.sort_order, PointPackage.key)
        .all()
    )
    return jsonify({"items": [p.to_dict() for p in rows]})


@bp.put("/packages/<key>")
@login_required
@admin_required
def update_package(key: str):
    """Edita um pacote EXISTENTE (preço/pontos/label/active/sort_order).

    Body aceita `price_brl` (reais) OU `price_cents` (inteiro). Não cria
    pacote novo via PUT. Valida faixa e registra quem alterou (updated_by).
    """
    pkg = db.session.get(PointPackage, key)
    if pkg is None:
        return jsonify({"error": f"pacote desconhecido: {key}"}), 404
    body = request.get_json(silent=True) or {}

    # base = rascunho atual (se houver) senão o valor publicado — assim editar
    # só o preço não descarta um rascunho de pontos já em andamento.
    base_price = pkg.draft_price_cents if pkg.draft_price_cents is not None else pkg.price_cents
    base_points = pkg.draft_points if pkg.draft_points is not None else pkg.points
    base_label = pkg.draft_label if pkg.draft_label is not None else pkg.label
    base_active = pkg.draft_active if pkg.draft_active is not None else pkg.active

    # preço — aceita price_cents (int) ou price_brl (reais → cents)
    if "price_cents" in body:
        try:
            price_cents = int(body["price_cents"])
        except (TypeError, ValueError):
            return jsonify({"error": "price_cents inválido"}), 400
    elif "price_brl" in body:
        try:
            price_cents = int(round(float(body["price_brl"]) * 100))
        except (TypeError, ValueError):
            return jsonify({"error": "price_brl inválido"}), 400
    else:
        price_cents = base_price
    if not (_PKG_PRICE_CENTS_MIN <= price_cents <= _PKG_PRICE_CENTS_MAX):
        return jsonify({"error": "preço fora da faixa (R$ 1,00 a R$ 100.000,00)"}), 400

    # pontos
    if "points" in body:
        try:
            points = int(body["points"])
        except (TypeError, ValueError):
            return jsonify({"error": "points inválido"}), 400
    else:
        points = base_points
    if not (_PKG_POINTS_MIN <= points <= _PKG_POINTS_MAX):
        return jsonify({"error": "points fora da faixa permitida"}), 400

    label = base_label
    if "label" in body:
        label = (str(body["label"]).strip() or base_label)[:64]
    active = base_active
    if "active" in body:
        active = bool(body["active"])

    # grava no RASCUNHO — NÃO afeta /pix/packages nem a cobrança até publicar.
    pkg.draft_price_cents = price_cents
    pkg.draft_points = points
    pkg.draft_label = label
    pkg.draft_active = active
    pkg.draft_updated_at = datetime.now(timezone.utc)
    pkg.draft_by = g.current_user.id
    db.session.commit()
    current_app.logger.info(
        "admin %s salvou RASCUNHO do pacote '%s' → %d pts / R$ %.2f (não publicado)",
        g.current_user.id, key, points, price_cents / 100,
    )
    return jsonify(pkg.to_dict())


@bp.post("/packages/publish")
@login_required
@admin_required
def publish_packages():
    """Promove RASCUNHO → PUBLICADO. Body opcional {"keys":[...]} publica só
    esses; sem body, publica TODOS os pacotes com rascunho pendente. A partir
    daqui /pix/packages e a cobrança PIX passam a usar o novo valor."""
    body = request.get_json(silent=True) or {}
    keys = body.get("keys")
    q = db.session.query(PointPackage).filter(PointPackage.draft_updated_at.isnot(None))
    if keys:
        q = q.filter(PointPackage.key.in_(list(keys)))
    now = datetime.now(timezone.utc)
    published = []
    for pkg in q.all():
        if pkg.draft_price_cents is not None:
            pkg.price_cents = pkg.draft_price_cents
        if pkg.draft_points is not None:
            pkg.points = pkg.draft_points
        if pkg.draft_label is not None:
            pkg.label = pkg.draft_label
        if pkg.draft_active is not None:
            pkg.active = pkg.draft_active
        pkg.updated_at = now
        pkg.updated_by = g.current_user.id
        pkg.draft_price_cents = None
        pkg.draft_points = None
        pkg.draft_label = None
        pkg.draft_active = None
        pkg.draft_updated_at = None
        pkg.draft_by = None
        published.append(pkg.key)
    db.session.commit()
    current_app.logger.info("admin %s PUBLICOU pacotes: %s", g.current_user.id, published)
    return jsonify({"published": published, "count": len(published)})


@bp.post("/packages/discard")
@login_required
@admin_required
def discard_package_drafts():
    """Descarta rascunhos não publicados. Body opcional {"keys":[...]}."""
    body = request.get_json(silent=True) or {}
    keys = body.get("keys")
    q = db.session.query(PointPackage).filter(PointPackage.draft_updated_at.isnot(None))
    if keys:
        q = q.filter(PointPackage.key.in_(list(keys)))
    discarded = []
    for pkg in q.all():
        pkg.draft_price_cents = None
        pkg.draft_points = None
        pkg.draft_label = None
        pkg.draft_active = None
        pkg.draft_updated_at = None
        pkg.draft_by = None
        discarded.append(pkg.key)
    db.session.commit()
    return jsonify({"discarded": discarded, "count": len(discarded)})



# =========================================================================== #
# B2B — gestão das redes parceiras                                            #
# =========================================================================== #
#
# É daqui que sai o número que mais importa no B2B: quanto cada rede DEVE.
# `bill_cents_per_point` abaixo de `CENTS_PER_POINT` é destacado como
# insolvente — a emissão já é recusada em runtime, mas o painel precisa mostrar
# a causa, senão o operador vê "rede parou de pontuar" sem saber por quê.

from ..models import (  # noqa: E402
    Merchant, MerchantAccrual, MerchantApiKey, MerchantUser, MerchantVertical,
)
from ..services import b2b as b2b_svc  # noqa: E402


def _merchant_row(m: Merchant) -> dict:
    tot = db.session.execute(
        select(
            func.count(MerchantAccrual.id),
            func.coalesce(func.sum(MerchantAccrual.points_awarded), 0),
            func.coalesce(func.sum(MerchantAccrual.bill_cents), 0),
            func.coalesce(func.sum(MerchantAccrual.amount_cents), 0),
        ).where(MerchantAccrual.merchant_id == m.id)
    ).one()
    d = m.to_dict()
    d.update({
        "transactions": int(tot[0]),
        "points_issued": int(tot[1]),
        "amount_due_brl": round(int(tot[2]) / 100, 2),
        "gmv_brl": round(int(tot[3]) / 100, 2),
        "solvent": m.bill_cents_per_point >= Config.CENTS_PER_POINT,
        "active_keys": db.session.query(MerchantApiKey).filter_by(
            merchant_id=m.id, is_active=True).count(),
        "panel_users": db.session.query(MerchantUser).filter_by(
            merchant_id=m.id).count(),
    })
    return d


@bp.get("/merchants")
@login_required
@admin_required
def admin_list_merchants():
    redes = db.session.query(Merchant).order_by(Merchant.name).all()
    linhas = [_merchant_row(m) for m in redes]
    return jsonify({
        "items": linhas,
        "totals": {
            "merchants": len(linhas),
            "points_issued": sum(x["points_issued"] for x in linhas),
            "gmv_brl": round(sum(x["gmv_brl"] for x in linhas), 2),
            "receivable_brl": round(sum(x["amount_due_brl"] for x in linhas), 2),
            "insolvent": sum(1 for x in linhas if not x["solvent"]),
        },
        "redemption_cost_cents": Config.CENTS_PER_POINT,
    })


@bp.post("/merchants")
@login_required
@admin_required
def admin_create_merchant():
    d = request.get_json(silent=True) or {}
    obrigatorios = ("name", "cnpj", "vertical", "accrual_cents_per_point",
                    "bill_cents_per_point")
    faltando = [k for k in obrigatorios if not d.get(k)]
    if faltando:
        return jsonify({"error": f"campos obrigatórios: {', '.join(faltando)}"}), 400

    try:
        vertical = MerchantVertical(str(d["vertical"]).lower())
    except ValueError:
        return jsonify({"error": "vertical deve ser posto, supermercado ou farmacia"}), 400

    cnpj = "".join(ch for ch in str(d["cnpj"]) if ch.isdigit())
    if len(cnpj) != 14:
        return jsonify({"error": "CNPJ deve ter 14 dígitos"}), 400
    if db.session.query(Merchant).filter_by(cnpj=cnpj).first() is not None:
        return jsonify({"error": "já existe rede com este CNPJ"}), 409

    try:
        acumulo = int(d["accrual_cents_per_point"])
        cobranca = int(d["bill_cents_per_point"])
    except (TypeError, ValueError):
        return jsonify({"error": "valores de contrato devem ser inteiros (centavos)"}), 400
    if acumulo <= 0:
        return jsonify({"error": "acúmulo deve ser positivo"}), 400
    # Não bloqueia — o admin pode cadastrar rede em negociação — mas devolve o
    # aviso para a UI destacar. A emissão é que fica travada.
    insolvente = cobranca < Config.CENTS_PER_POINT

    m = Merchant(
        name=str(d["name"])[:120],
        legal_name=(d.get("legal_name") or None),
        cnpj=cnpj,
        vertical=vertical,
        accrual_cents_per_point=acumulo,
        bill_cents_per_point=cobranca,
        max_points_per_tx=int(d.get("max_points_per_tx") or 10_000),
    )
    db.session.add(m)
    db.session.commit()
    return jsonify({"merchant": _merchant_row(m), "insolvent": insolvente}), 201


@bp.patch("/merchants/<merchant_id>")
@login_required
@admin_required
def admin_update_merchant(merchant_id: str):
    m = db.session.get(Merchant, merchant_id)
    if m is None:
        return jsonify({"error": "rede não encontrada"}), 404
    d = request.get_json(silent=True) or {}

    for campo in ("accrual_cents_per_point", "bill_cents_per_point",
                  "max_points_per_tx"):
        if campo in d:
            try:
                valor = int(d[campo])
            except (TypeError, ValueError):
                return jsonify({"error": f"{campo} inválido"}), 400
            if valor <= 0:
                return jsonify({"error": f"{campo} deve ser positivo"}), 400
            setattr(m, campo, valor)
    if "is_active" in d:
        m.is_active = bool(d["is_active"])
    if "name" in d and d["name"]:
        m.name = str(d["name"])[:120]

    db.session.commit()
    return jsonify(_merchant_row(m))


@bp.post("/merchants/<merchant_id>/keys")
@login_required
@admin_required
def admin_issue_merchant_key(merchant_id: str):
    m = db.session.get(Merchant, merchant_id)
    if m is None:
        return jsonify({"error": "rede não encontrada"}), 404
    label = (request.get_json(silent=True) or {}).get("label") or "emitida pelo admin"
    _, raw = b2b_svc.issue_api_key(m, label=str(label)[:80])
    db.session.commit()
    return jsonify({"key": raw, "warning": "guarde agora; não é recuperável"}), 201


@bp.post("/merchants/<merchant_id>/users")
@login_required
@admin_required
def admin_create_merchant_user(merchant_id: str):
    """Cria o acesso ao painel da rede."""
    m = db.session.get(Merchant, merchant_id)
    if m is None:
        return jsonify({"error": "rede não encontrada"}), 404
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    senha = d.get("password") or ""
    if not email or len(senha) < 8:
        return jsonify({"error": "email e senha (mín. 8) são obrigatórios"}), 400
    if db.session.query(MerchantUser).filter_by(email=email).first() is not None:
        return jsonify({"error": "e-mail já cadastrado"}), 409

    u = MerchantUser(
        merchant_id=m.id, name=str(d.get("name") or email)[:120],
        email=email, role=("staff" if d.get("role") == "staff" else "owner"),
    )
    u.set_password(senha)
    db.session.add(u)
    db.session.commit()
    return jsonify(u.to_dict()), 201


@bp.get("/merchants/<merchant_id>/accruals")
@login_required
@admin_required
def admin_merchant_accruals(merchant_id: str):
    try:
        limit = min(int(request.args.get("limit") or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    linhas = (
        db.session.query(MerchantAccrual)
        .filter_by(merchant_id=merchant_id)
        .order_by(MerchantAccrual.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [x.to_dict() for x in linhas]})


# ─────────────────── Faturamento das redes ─────────────────── #

from ..models import InvoiceStatus, MerchantInvoice  # noqa: E402


def _parse_dt(valor, padrao=None):
    if not valor:
        return padrao
    try:
        return datetime.fromisoformat(str(valor)).replace(tzinfo=None)
    except ValueError:
        return padrao


@bp.get("/invoices")
@login_required
@admin_required
def admin_list_invoices():
    """Todas as faturas, filtráveis por status. O total em aberto é o que a
    BlaXx tem a receber das redes — é o número que fecha com o contábil."""
    q = db.session.query(MerchantInvoice)
    status = (request.args.get("status") or "").lower()
    if status in ("open", "paid", "void"):
        q = q.filter(MerchantInvoice.status == InvoiceStatus(status))
    linhas = q.order_by(MerchantInvoice.created_at.desc()).limit(200).all()

    em_aberto = int(db.session.execute(
        select(func.coalesce(func.sum(MerchantInvoice.amount_cents), 0))
        .where(MerchantInvoice.status == InvoiceStatus.OPEN)
    ).scalar() or 0)
    return jsonify({
        "items": [f.to_dict() for f in linhas],
        "totals": {"unpaid_brl": round(em_aberto / 100, 2), "count": len(linhas)},
    })


@bp.get("/merchants/<merchant_id>/invoices")
@login_required
@admin_required
def admin_merchant_invoices(merchant_id: str):
    m = db.session.get(Merchant, merchant_id)
    if m is None:
        return jsonify({"error": "rede não encontrada"}), 404
    linhas = (
        db.session.query(MerchantInvoice)
        .filter_by(merchant_id=merchant_id)
        .order_by(MerchantInvoice.created_at.desc())
        .all()
    )
    return jsonify({
        "items": [f.to_dict() for f in linhas],
        "open_balance": b2b_svc.open_balance(m),
    })


@bp.post("/merchants/<merchant_id>/invoices/close")
@login_required
@admin_required
def admin_close_invoice(merchant_id: str):
    """Fecha o período. Sem datas, fecha o MÊS ANTERIOR inteiro — que é o
    fechamento normal; passar datas é a exceção."""
    m = db.session.get(Merchant, merchant_id)
    if m is None:
        return jsonify({"error": "rede não encontrada"}), 404
    d = request.get_json(silent=True) or {}

    hoje = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                              second=0, microsecond=0)
    inicio_mes = hoje.replace(day=1)
    padrao_fim = inicio_mes
    padrao_inicio = (inicio_mes - timedelta(days=1)).replace(day=1)

    inicio = _parse_dt(d.get("period_start"), padrao_inicio)
    fim = _parse_dt(d.get("period_end"), padrao_fim)
    vencimento = _parse_dt(d.get("due_date"), fim + timedelta(days=10))

    try:
        f = b2b_svc.close_invoice(m, period_start=inicio, period_end=fim,
                                  due_date=vencimento)
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), exc.status
    db.session.commit()
    return jsonify(f.to_dict()), 201


@bp.post("/invoices/<invoice_id>/pay")
@login_required
@admin_required
def admin_pay_invoice(invoice_id: str):
    f = db.session.get(MerchantInvoice, invoice_id)
    if f is None:
        return jsonify({"error": "fatura não encontrada"}), 404
    try:
        b2b_svc.mark_invoice_paid(f, note=(request.get_json(silent=True) or {}).get("note"))
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), exc.status
    db.session.commit()
    return jsonify(f.to_dict())


@bp.post("/invoices/<invoice_id>/void")
@login_required
@admin_required
def admin_void_invoice(invoice_id: str):
    f = db.session.get(MerchantInvoice, invoice_id)
    if f is None:
        return jsonify({"error": "fatura não encontrada"}), 404
    try:
        b2b_svc.void_invoice(f, note=(request.get_json(silent=True) or {}).get("note"))
    except b2b_svc.B2BError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), exc.status
    db.session.commit()
    return jsonify(f.to_dict())


# ─────────────────── Catálogo: parceiros, benefícios, campanhas ─────────────────── #
#
# Existem porque o serviço roda no plano free do Render, que não tem Shell: sem
# estes endpoints, popular o catálogo exigiria acesso direto ao banco, e o
# DATABASE_URL de produção só vive nas env vars do Render.
#
# Todos recusam nome duplicado com 409 em vez de criar cópia — assim o script de
# carga pode ser reexecutado sem sujar o catálogo.

from ..models import Benefit, Campaign, Partner  # noqa: E402


def _duplicado(modelo, nome: str):
    return db.session.query(modelo).filter_by(name=nome).first() is not None


@bp.post("/partners")
@login_required
@admin_required
def admin_create_partner():
    d = request.get_json(silent=True) or {}
    nome = (d.get("name") or "").strip()
    if not nome or not (d.get("category") or "").strip():
        return jsonify({"error": "name e category são obrigatórios"}), 400
    if _duplicado(Partner, nome):
        return jsonify({"error": "já existe parceiro com este nome",
                        "code": "duplicate"}), 409
    p = Partner(
        name=nome[:120], category=str(d["category"])[:60],
        description=(d.get("description") or None),
        logo_emoji=(d.get("logo_emoji") or None),
        accrual_rule=(d.get("accrual_rule") or None),
        city=(d.get("city") or None),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201


@bp.post("/benefits")
@login_required
@admin_required
def admin_create_benefit():
    d = request.get_json(silent=True) or {}
    nome = (d.get("name") or "").strip()
    if not nome:
        return jsonify({"error": "name é obrigatório"}), 400
    if _duplicado(Benefit, nome):
        return jsonify({"error": "já existe benefício com este nome",
                        "code": "duplicate"}), 409
    try:
        custo = int(d.get("cost_pts") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "cost_pts inválido"}), 400
    if custo <= 0:
        return jsonify({"error": "cost_pts deve ser positivo"}), 400

    # Aceita o parceiro por NOME: quem carrega o catálogo pensa em "Pão & Cia",
    # não em uuid. Nome que não existe é erro, não silêncio — benefício órfão
    # aparece na tela sem dizer de quem é.
    partner_id = d.get("partner_id")
    if not partner_id and d.get("partner_name"):
        p = db.session.query(Partner).filter_by(name=str(d["partner_name"])).one_or_none()
        if p is None:
            return jsonify({"error": f"parceiro '{d['partner_name']}' não existe",
                            "code": "partner_not_found"}), 400
        partner_id = p.id

    b = Benefit(
        partner_id=partner_id, name=nome[:120],
        description=(d.get("description") or None),
        category=str(d.get("category") or "voucher")[:60],
        cost_pts=custo,
        image_emoji=(d.get("image_emoji") or None),
        stock=int(d.get("stock", -1)),
        expires_in_days=int(d.get("expires_in_days") or 180),
        tag=(d.get("tag") or None),
    )
    db.session.add(b)
    db.session.commit()
    return jsonify(b.to_dict()), 201


@bp.post("/campaigns")
@login_required
@admin_required
def admin_create_campaign():
    d = request.get_json(silent=True) or {}
    nome = (d.get("name") or "").strip()
    if not nome:
        return jsonify({"error": "name é obrigatório"}), 400
    if _duplicado(Campaign, nome):
        return jsonify({"error": "já existe campanha com este nome",
                        "code": "duplicate"}), 409
    try:
        # target_brl chega em CENTAVOS (é como a coluna guarda); reward_pts em pontos.
        alvo = int(d.get("target_brl") or 0)
        premio = int(d.get("reward_pts") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "target_brl e reward_pts devem ser inteiros"}), 400
    if alvo <= 0 or premio <= 0:
        return jsonify({"error": "target_brl e reward_pts devem ser positivos"}), 400

    c = Campaign(
        name=nome[:120], description=(d.get("description") or None),
        mechanic=str(d.get("mechanic") or "")[:200],
        target_brl=alvo, reward_pts=premio,
        period_end=_parse_dt(d.get("period_end")),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201


@bp.patch("/campaigns/<campaign_id>")
@login_required
@admin_required
def admin_update_campaign(campaign_id: str):
    c = db.session.get(Campaign, campaign_id)
    if c is None:
        return jsonify({"error": "campanha não encontrada"}), 404
    d = request.get_json(silent=True) or {}
    if "is_active" in d:
        c.is_active = bool(d["is_active"])
    for campo in ("target_brl", "reward_pts"):
        if campo in d:
            try:
                v = int(d[campo])
            except (TypeError, ValueError):
                return jsonify({"error": f"{campo} inválido"}), 400
            if v <= 0:
                return jsonify({"error": f"{campo} deve ser positivo"}), 400
            setattr(c, campo, v)
    db.session.commit()
    return jsonify(c.to_dict())


@bp.patch("/partners/<partner_id>")
@login_required
@admin_required
def admin_update_partner(partner_id: str):
    p = db.session.get(Partner, partner_id)
    if p is None:
        return jsonify({"error": "parceiro não encontrado"}), 404
    d = request.get_json(silent=True) or {}
    if "is_active" in d:
        p.is_active = bool(d["is_active"])
    db.session.commit()
    return jsonify(p.to_dict())


@bp.patch("/benefits/<benefit_id>")
@login_required
@admin_required
def admin_update_benefit(benefit_id: str):
    b = db.session.get(Benefit, benefit_id)
    if b is None:
        return jsonify({"error": "benefício não encontrado"}), 404
    d = request.get_json(silent=True) or {}
    if "is_active" in d:
        b.is_active = bool(d["is_active"])
    if "stock" in d:
        b.stock = int(d["stock"])
    if "cost_pts" in d:
        try:
            v = int(d["cost_pts"])
        except (TypeError, ValueError):
            return jsonify({"error": "cost_pts inválido"}), 400
        if v <= 0:
            return jsonify({"error": "cost_pts deve ser positivo"}), 400
        b.cost_pts = v
    db.session.commit()
    return jsonify(b.to_dict())
