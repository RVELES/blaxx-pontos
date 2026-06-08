"""Endpoints de PIX — compra de pontos.

Endpoints:
  GET  /pix/packages              → lista de pacotes disponíveis
  POST /pix/charge                → cria cobrança (BR Code) para comprar pontos
  GET  /pix/charge/<id>           → consulta status
  POST /pix/webhook               → callback do provedor (não exige auth)
  POST /pix/simulate-payment      → SOMENTE no mock: força pagamento de uma charge
"""

from __future__ import annotations

import hmac
import hashlib
import time

from flask import Blueprint, abort, current_app, g, jsonify, request

from ..extensions import db, limiter
from ..models import PixCharge
from ..services import purchase as purchase_svc
from .auth import login_required

bp = Blueprint("pix", __name__)


# -------------------- HMAC / IP whitelist do webhook -------------------- #

def _client_ip() -> str:
    """Pega o IP real do cliente respeitando o proxy (Fly.io)."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def _verify_webhook_signature(raw_body: bytes) -> bool:
    """Valida assinatura do webhook.

    Suporta 2 formatos:
      1. Mercado Pago — headers x-signature + x-request-id (algoritmo deles)
      2. Genérico — header X-Blaxx-Signature: sha256=<hex>
    """
    # Tenta Mercado Pago primeiro (se headers existem)
    mp_sig = request.headers.get("x-signature", "")
    if mp_sig:
        return _verify_mp_signature(mp_sig)

    # Fallback: HMAC genérico
    secret = current_app.config.get("PIX_WEBHOOK_SECRET", "")
    if not secret:
        # Sem segredo configurado → em DEV passa direto, em PROD bloqueia
        return current_app.debug or current_app.config.get("TESTING", False)
    received = request.headers.get("X-Blaxx-Signature", "")
    if not received.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def _verify_mp_signature(x_signature: str) -> bool:
    """Valida assinatura do webhook do Mercado Pago.

    Formato do header x-signature:
        ts=1704067200,v1=abc123...
    Algoritmo (do doc do MP):
        manifest = "id:<data.id>;request-id:<x-request-id>;ts:<ts>;"
        expected = HMAC-SHA256(MP_WEBHOOK_SECRET, manifest).hex()

    Anti-replay: valida que `ts` está dentro de uma janela razoável (±5 min)
    em relação ao agora. Sem isso, um atacante que captura um webhook válido
    poderia replayá-lo indefinidamente — a idempotência via external_reference
    impede crédito duplo, mas ainda permitiria floodar a API MP via get_payment.
    """
    secret = current_app.config.get("MP_WEBHOOK_SECRET", "")
    if not secret:
        return current_app.debug or current_app.config.get("TESTING", False)

    parts = {p.split("=", 1)[0]: p.split("=", 1)[1]
             for p in x_signature.split(",") if "=" in p}
    ts = parts.get("ts", "")
    sig = parts.get("v1", "")
    if not ts or not sig:
        return False

    # Anti-replay: rejeita webhooks com timestamp fora de ±5 minutos.
    # Toleramos um pouco mais (10 min) se a flag de tolerância foi
    # configurada — útil em janelas de manutenção.
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    max_skew = int(current_app.config.get("MP_WEBHOOK_MAX_CLOCK_SKEW", 300))
    if abs(now - ts_int) > max_skew:
        current_app.logger.warning(
            "pix webhook: timestamp fora da janela (ts=%s, now=%s, skew_max=%s)",
            ts, now, max_skew,
        )
        return False

    body = request.get_json(silent=True) or {}
    data_id = (body.get("data") or {}).get("id", "")
    request_id = request.headers.get("x-request-id", "")
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _check_ip_whitelist() -> bool:
    allowed = current_app.config.get("PIX_WEBHOOK_ALLOWED_IPS") or []
    if not allowed:
        return True  # whitelist vazia = permite tudo (DEV)
    return _client_ip() in allowed


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
# Compra de pontos não exige mais e-mail verificado (decisão de produto).
def create_charge():
    """Cria charge PIX via provider configurado (MP em prod).

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
    purchase_svc.expire_if_needed(charge)
    return jsonify(charge.to_dict())


@bp.post("/webhook")
@limiter.limit("60 per minute")
def webhook():
    """Endpoint público — provedor PIX bate aqui ao confirmar pagamento.

    Segurança (Sprint 2):
      1. IP whitelist (PIX_WEBHOOK_ALLOWED_IPS).
      2. HMAC-SHA256 do body com PIX_WEBHOOK_SECRET.
      3. Rate limit por IP (60/min) pra evitar flood.

    Em produção real cada provedor (Mercado Pago, Efí, Stark) tem o seu
    cabeçalho de assinatura — aqui temos o esquema genérico X-Blaxx-Signature.
    """
    if not _check_ip_whitelist():
        current_app.logger.warning("pix webhook: IP não permitido: %s", _client_ip())
        return jsonify({"error": "forbidden"}), 403

    raw = request.get_data() or b""
    if not _verify_webhook_signature(raw):
        current_app.logger.warning("pix webhook: HMAC inválido")
        return jsonify({"error": "invalid signature"}), 401

    data = request.get_json(silent=True) or {}
    provider = current_app.extensions["pix_provider"]

    # ------- Resolve o txid de acordo com o provider -------
    if provider.name == "mercadopago":
        # MP envia {"action": "payment.updated", "data": {"id": "<payment_id>"}}
        # Precisamos buscar o pagamento na API pra pegar status e external_reference.
        action = data.get("action", "")
        if not action.startswith("payment"):
            return jsonify({"ok": True, "ignored": "type != payment"}), 200
        mp_payment_id = (data.get("data") or {}).get("id")
        if not mp_payment_id:
            return jsonify({"error": "data.id ausente"}), 400
        try:
            payment = provider.get_payment(str(mp_payment_id))
        except Exception as e:
            current_app.logger.error("MP webhook: falha ao buscar payment %s: %s",
                                     mp_payment_id, e)
            return jsonify({"error": "erro ao consultar MP"}), 502

        # Só processamos se aprovado de fato
        if payment.get("status") != "approved":
            return jsonify({"ok": True, "status": payment.get("status")}), 200

        # external_reference é o nosso txid original (passado em create_charge)
        txid = payment.get("external_reference") or ""
        if not txid:
            return jsonify({"error": "external_reference ausente no payment"}), 400
    else:
        # Webhook genérico (Mock, ou outros providers que usam txid direto)
        txid = data.get("txid") or data.get("id") or ""
        if not txid:
            return jsonify({"error": "txid ausente"}), 400

    try:
        charge = purchase_svc.confirm_payment(txid)
    except purchase_svc.PixError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"received": True, "charge": charge.to_dict()})


# =====================================================================
# PIX manual (QR estático) · Onda 2
# =====================================================================
# Fluxo:
#   1. Cliente informa valor em R$ → POST /pix/custom-charge
#      → backend cria PixCharge com flow='manual', valor exato e QR estático
#   2. Frontend mostra o QR + valor pra digitar no banco
#   3. Cliente paga via app do banco
#   4. Cliente clica "Já paguei" → POST /pix/custom-charge/<id>/claim-paid
#      → charge.status = PENDING_CONFIRMATION
#   5. Admin recebe na fila → POST /admin/charges/<id>/confirm
#      → wallet_svc.credit() libera os pontos


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
def charge_events_sse(charge_id: str):
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
        deadline = time.time() + 600  # 10 min
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
