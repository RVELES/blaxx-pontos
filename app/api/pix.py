"""Endpoints de PIX — compra de pontos.

Endpoints:
  GET  /pix/packages              → lista de pacotes disponíveis
  POST /pix/charge                → cria cobrança (BR Code) para comprar pontos
  GET  /pix/charge/<id>           → consulta status
  POST /pix/simulate-payment      → SOMENTE no mock: força pagamento de uma charge
"""

from __future__ import annotations

import os
import time

from flask import Blueprint, abort, current_app, g, jsonify, request

from ..extensions import db, limiter
from ..models import PixCharge
from ..services import purchase as purchase_svc
from .auth import login_required

bp = Blueprint("pix", __name__)


@bp.get("/packages")
def packages():
    return jsonify(purchase_svc.list_packages())


@bp.get("/provider")
def provider_info():
    """Identifica qual provider está ativo. Usado pelo frontend pra
    decidir se mostra o botão 'Simular pagamento' (só faz sentido em mock).
    """
    p = current_app.extensions["pix_provider"]
    return jsonify({
        "name": p.name,
        "is_mock": p.name == "mock",
    })


@bp.post("/charge")
@login_required
# Rate limit por usuário: cada charge dispara um POST /v1/payments real no MP.
# Alinhado a /pix/custom-charge, /payments/card/charge e /redeem (10/h).
@limiter.limit("10 per hour",
               key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
# Compra de pontos não exige mais e-mail verificado (decisão de produto).
def create_charge():
    """Cria charge PIX via provider configurado (Asaas em prod).

    Body aceita uma das duas formas:
      - {"package": "plus"}            → pacote pré-definido em Config.POINT_PACKAGES
      - {"amount_brl": 50.00}          → valor livre (R$ 10 a R$ 100k não-VIP)
    """
    data = request.get_json(silent=True) or {}
    package_key = (data.get("package") or "").strip().lower() or None
    amount_brl = data.get("amount_brl")

    try:
        charge = purchase_svc.create_charge(
            g.current_user,
            package_key=package_key,
            amount_brl=amount_brl,
        )
    except purchase_svc.PixError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(charge.to_dict()), 201


@bp.get("/charge/<charge_id>")
@login_required
def get_charge(charge_id: str):
    charge = db.session.get(PixCharge, charge_id)
    if charge is None or charge.user_id != g.current_user.id:
        return jsonify({"error": "not found"}), 404
    _reconsultar_c6_se_pendente(charge)
    purchase_svc.expire_if_needed(charge)
    return jsonify(charge.to_dict())


def _reconsultar_c6_se_pendente(charge) -> None:
    """Backstop do webhook do C6: confirma pelo polling que o frontend já faz.

    O webhook é a via normal, mas ele pode se perder (C6 fora do ar na hora
    da entrega, sandbox fechado, hiccup de rede). Sem isto o dinheiro entra,
    a charge expira sozinha e nada reconsulta. Aqui a consulta sai de graça:
    a tela de pagamento já faz polling neste endpoint.

    Nunca propaga erro: falha de consulta não pode quebrar a tela.
    """
    from ..models import PixChargeStatus

    provider = current_app.extensions.get("pix_provider")
    if getattr(provider, "name", "") != "c6":
        return
    if charge.status not in (PixChargeStatus.PENDING, PixChargeStatus.EXPIRED):
        return
    try:
        cob = provider.get_cob(charge.txid)
        if provider.cob_has_refund(cob):
            return
        if provider.cob_paid_cents(cob) >= charge.amount_cents:
            purchase_svc.confirm_payment(charge.txid, provider_confirmed=True)
            current_app.logger.info(
                "compra %s creditada via polling (webhook do C6 não chegou)",
                charge.txid,
            )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.warning(
            "c6: reconsulta de %s falhou no polling: %s", charge.txid, exc
        )


# NOTA: o endpoint /pix/webhook (MercadoPago) foi REMOVIDO em 2026-08-01.
# Os providers ativos têm handler próprio:
#   · Asaas  → /payouts/asaas/webhook   (token estático + reconsulta na API)
#   · Stripe → /payments/stripe/webhook (corpo ASSINADO, HMAC + anti-replay)


@bp.post("/custom-charge")
@login_required
# Compra de pontos (valor livre) não exige mais e-mail verificado.
@limiter.limit("10 per hour")
def create_custom_charge():
    """Cria charge com valor livre apontando para o QR PIX estático Blaxx.

    Body: { "amount_brl": 50.00 }  (mínimo R$ 10, máximo R$ 100.000)

    Conversão: Config.CENTS_PER_POINT (default: 1 pt = R$ 0,09). VIPs sem teto.
    """
    from ..config import Config
    from ..models import PixCharge, PixChargeStatus

    data = request.get_json(silent=True) or {}
    try:
        amount_brl = float(data.get("amount_brl") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "valor inválido"}), 400

    if amount_brl < 10:
        return jsonify({"error": "Valor mínimo: R$ 10,00"}), 400

    # VIPs podem comprar acima de R$ 100k/dia (Spec do user)
    if not g.current_user.is_vip and amount_brl > 100_000:
        return jsonify({"error": "Valor máximo R$ 100.000 por compra (VIP não tem limite)"}), 400

    amount_cents = int(round(amount_brl * 100))
    points_to_credit = Config.cents_to_pts(amount_cents)  # via CENTS_PER_POINT

    # BR Code do QR estático Blaxx — EMV BR Code da conta PJ verificada.
    # Sprint 3 (S3-3): em prod, abortar se ainda for o placeholder.
    # Cliente "pagaria" pra um codigo invalido sem nunca chegar pra ninguem.
    _PLACEHOLDER = (
        "00020126360014BR.GOV.BCB.PIX0114blaxxpontos5204000053039865802BR"
        "5908Blaxx Pontos6009SAO PAULO63041234"
    )
    br_code = current_app.config.get("BLAXX_STATIC_PIX_BRCODE", _PLACEHOLDER)
    _is_dev = bool(current_app.debug) or current_app.config.get("TESTING") \
              or os.environ.get("FLASK_ENV") == "development"
    if br_code == _PLACEHOLDER and not _is_dev:
        current_app.logger.error(
            "BLAXX_STATIC_PIX_BRCODE = placeholder em PRODUCAO. "
            "Charge recusada — configure o EMV BR Code real da conta PJ."
        )
        return jsonify({
            "error": "Cobranca PIX manual temporariamente indisponivel. "
                     "Equipe tecnica notificada.",
            "code": "BRCODE_NOT_CONFIGURED",
        }), 503

    charge = PixCharge(
        user_id=g.current_user.id,
        package_key="custom",
        amount_cents=amount_cents,
        points_to_credit=points_to_credit,
        br_code=br_code,
        # Frontend monta o caminho para a imagem estática /static/pix-qr-blaxx.png
        qr_code_image=None,
        expires_at=PixCharge.make_expiry(Config.PIX_CHARGE_TTL_SECONDS),
        flow="manual",
    )
    db.session.add(charge)
    db.session.commit()

    return jsonify({
        **charge.to_dict(),
        "qr_image_url": "/static/pix-qr-blaxx.png",
        "instructions": "Abra o app do seu banco, escolha PIX → ler QR Code, escaneie a imagem e digite o valor EXATO indicado.",
    }), 201


@bp.post("/custom-charge/<charge_id>/claim-paid")
@login_required
def claim_paid(charge_id: str):
    """Cliente avisa que pagou. Charge vai para PENDING_CONFIRMATION."""
    from datetime import datetime, timezone
    from ..models import PixCharge, PixChargeStatus, Notification

    charge = db.session.get(PixCharge, charge_id)
    if charge is None or charge.user_id != g.current_user.id:
        return jsonify({"error": "charge não encontrada"}), 404
    if charge.flow != "manual":
        return jsonify({"error": "essa charge não é do fluxo manual"}), 400
    if charge.status in (PixChargeStatus.PAID, PixChargeStatus.REJECTED,
                          PixChargeStatus.EXPIRED, PixChargeStatus.REFUNDED):
        return jsonify({"error": f"charge já está em status final ({charge.status.value})"}), 400

    charge.status = PixChargeStatus.PENDING_CONFIRMATION
    charge.claimed_paid_at = datetime.now(timezone.utc)

    # Notifica todos os admins (lista do banco — caro mas raro)
    from ..models import User
    admins = db.session.query(User).filter_by(role="admin").all()
    for admin in admins:
        db.session.add(Notification(
            user_id=admin.id, type="system",
            title="Pagamento PIX para conferir",
            body=f"{g.current_user.name} avisou que pagou R$ {charge.amount_cents/100:.2f}.",
            icon="💸",
            reference=charge.id,
        ))

    db.session.commit()
    return jsonify({"ok": True, "status": charge.status.value})


@bp.get("/charge/<charge_id>/events")
@login_required
@limiter.limit("2 per minute",
               key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
def charge_events_sse(charge_id: str):
    # B-10: cada conexão segura um worker por até 10 min fazendo SELECT a cada
    # 2s. Com `--workers 2 --threads 4` são 8 slots no total: 8 clientes com o
    # SSE aberto derrubavam o serviço inteiro. O achado estava marcado como
    # BAIXO, mas é negação de serviço com 8 usuários — e nem precisa de
    # má-fé, basta gente esperando o PIX cair.
    #
    # Duas travas: 2 aberturas por minuto por usuário, e o deadline caiu de
    # 10 para 3 minutos. PIX que não cai em 3 min não vai cair por SSE — o
    # cliente reconecta ou usa o botão "Já paguei".
    """Sprint 4 (S4-6) · Server-Sent Events de status de uma charge.

    Substitui o polling client-side a cada 5s. O client abre uma conexao
    EventSource que recebe push imediato quando o status muda.

    Cliente:
        const ev = new EventSource('/pix/charge/{id}/events');
        ev.addEventListener('status', e => { ... });

    Servidor:
        Sondamos o DB a cada 2s (cheap, indexado) e mandamos um event
        somente quando muda. Encerra ao ficar PAID/REJECTED/EXPIRED ou
        apos 10 min (timeout de seguranca).
    """
    import time
    from flask import Response, stream_with_context

    charge_id_local = charge_id
    user_id_local = g.current_user.id

    def gen():
        last_status = None
        deadline = time.time() + 180  # 3 min — ver B-10 acima
        # Heartbeat inicial pro client saber que abriu OK
        yield ": connected\n\n"
        while time.time() < deadline:
            charge = db.session.query(PixCharge).filter_by(
                id=charge_id_local, user_id=user_id_local
            ).first()
            if not charge:
                yield "event: error\ndata: {\"error\":\"not_found\"}\n\n"
                return
            cur = charge.status.value if charge.status else "unknown"
            if cur != last_status:
                last_status = cur
                payload = (
                    '{"status":"' + cur + '","charge_id":"' + charge.id + '",'
                    '"amount_brl":' + str(charge.amount_cents / 100) + ',"points_to_credit":'
                    + str(charge.points_to_credit) + '}'
                )
                yield "event: status\ndata: " + payload + "\n\n"
            # Estados terminais — encerra a stream
            if cur in ("paid", "expired", "rejected", "refunded"):
                return
            # Heartbeat a cada loop pra manter conexao viva atras de proxy
            yield ": ping\n\n"
            time.sleep(2)
        yield "event: timeout\ndata: {}\n\n"

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",   # desliga buffering em nginx/render
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)


@bp.get("/my-charges")
@login_required
def my_charges():
    """Lista as charges do próprio usuário (status, valor, paid_at)."""
    rows = (
        db.session.query(PixCharge)
        .filter_by(user_id=g.current_user.id)
        .order_by(PixCharge.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({"items": [c.to_dict() for c in rows]})


@bp.post("/simulate-payment")
@login_required
def simulate_payment():
    """Atalho de demonstração: simula que o usuário pagou o PIX agora.

    SOMENTE no provider mock E fora de produção. Em produção real, o webhook
    do provedor (Asaas) é o caminho oficial de confirmação.

    Gate duplo: o gate por provider sozinho não bastava — se o app subisse em
    prod com o MockPixProvider (drift de env var, rollback de config, serviço
    novo criado pelo render.yaml), qualquer usuário autenticado creditaria a
    si mesmo o valor integral de uma charge sem pagar nada.
    """
    from .. import _is_production, _dev_endpoints_enabled
    if _is_production() and not _dev_endpoints_enabled():
        abort(404)
    if current_app.extensions["pix_provider"].name != "mock":
        return jsonify({"error": "endpoint só está disponível no provider mock"}), 403

    data = request.get_json(silent=True) or {}
    charge_id = data.get("charge_id")
    charge = db.session.get(PixCharge, charge_id)
    if charge is None or charge.user_id != g.current_user.id:
        return jsonify({"error": "charge não encontrada"}), 404

    try:
        charge = purchase_svc.confirm_payment(charge.txid)
    except purchase_svc.PixError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"ok": True, "charge": charge.to_dict()})
