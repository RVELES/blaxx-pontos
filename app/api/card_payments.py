"""Endpoints de pagamento com CARTÃO DE CRÉDITO — compra de pontos (Stripe).

Prefixo /payments/card (NÃO confundir com /card, que é o cartão-fidelidade
BlaXx / loyalty tiers / Apple Wallet — blueprint app/api/card.py).

Endpoints:
  GET  /payments/card/config       → público: flag + public key pro SDK JS
  POST /payments/card/charge       → cria e processa pagamento (token do Elements)
  GET  /payments/card/charge/<id>  → consulta status (polling do in_process)
  GET  /payments/card/my-charges   → histórico do próprio usuário

Gate: CARD_ENABLED=1 (config). Com a flag off, /config responde
{enabled: false} e os demais endpoints retornam 503 — deploy incremental
e rollback instantâneo sem afetar o PIX.

PCI (SAQ-A): o backend só aceita card_token (single-use, gerado pelo SDK
JS com a STRIPE_PUBLISHABLE_KEY). Payloads com número de cartão/CVV são
recusados na entrada, por defesa em profundidade.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from ..extensions import db, limiter
from ..models import CardCharge
from ..services import card_purchase as card_svc
from .auth import login_required

bp = Blueprint("card_payments", __name__)

# Campos que NUNCA podem chegar aqui — presença indica integração errada
# (PAN/CVV devem virar token no frontend, nunca transitar pro backend).
_FORBIDDEN_FIELDS = frozenset({
    "card_number", "cardnumber", "pan", "number",
    "cvv", "cvc", "ccv", "security_code", "securitycode", "code",
    "expiration_month", "expiration_year", "exp_month", "exp_year",
    "expiry", "expiration", "expiration_date", "card_expiry",
})


def _scan_forbidden(payload, _depth: int = 0) -> set[str]:
    """Varre recursivamente as chaves do payload procurando campos brutos de
    cartão — cobre também aninhamento (ex.: {"card": {"number": ...}}), não só
    o topo. Limita profundidade pra não abrir DoS por payload aninhado.
    """
    found: set[str] = set()
    if _depth > 6:
        return found
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str) and k.lower() in _FORBIDDEN_FIELDS:
                found.add(k.lower())
            found |= _scan_forbidden(v, _depth + 1)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= _scan_forbidden(item, _depth + 1)
    return found


def _card_enabled() -> bool:
    return bool(current_app.config.get("CARD_ENABLED"))


@bp.get("/config")
def card_config():
    """Config pública pro frontend montar o checkout.

    Devolve `provider` para o front saber qual SDK carregar:
      · "stripe"       → Stripe.js + Elements (arquitetura atual)

    A chave publicável é pública por design — quem tokeniza é o browser, e o
    número do cartão nunca chega a este backend.
    """
    # Checkout do C6 (link de pagamento) vence o Stripe quando ligado: é o
    # cartão nacional da conta PJ. O front só redireciona para a página do C6,
    # então não há chave pública nem SDK.
    pix = current_app.extensions.get("pix_provider")
    if current_app.config.get("C6_CHECKOUT_ENABLED") and getattr(pix, "name", "") == "c6":
        return jsonify({
            "enabled": True,
            "provider": "c6",
            "public_key": "",
            "max_installments": 1,
            "checkout_path": "/payments/c6/card/checkout",
        })

    enabled = _card_enabled()
    stripe = current_app.extensions.get("card_intl_provider")

    if stripe is not None:
        pk = current_app.config.get("STRIPE_PUBLISHABLE_KEY", "")
        return jsonify({
            "enabled": enabled and bool(pk),
            "provider": "stripe",
            "public_key": pk if enabled else "",
            # Stripe não oferece parcelamento no Brasil — cartão é à vista.
            "max_installments": 1,
            "currency": current_app.config.get("STRIPE_CURRENCY", "brl"),
        })

    # Sem Stripe configurado não há checkout de cartão possível.
    return jsonify({
        "enabled": False,
        "provider": "none",
        "public_key": "",
        "max_installments": 1,
    })


@bp.post("/charge")
@login_required
@limiter.limit("10 per hour")
def create_charge():
    """Cria pagamento com cartão. Fluxo síncrono (aprova/recusa na hora;
    in_process confirma depois via webhook).

    Body:
      {"package": "plus"} OU {"amount_brl": 50.0}
      + "card_token", "payment_method_id", "installments"?, "issuer_id"?
    Header opcional: Idempotency-Key (retry seguro — não cobra 2x).
    """
    if not _card_enabled():
        return jsonify({
            "error": "pagamento com cartão indisponível no momento",
            "code": "CARD_DISABLED",
        }), 503

    data = request.get_json(silent=True) or {}

    # Defesa em profundidade: recusa payloads com dados brutos de cartão
    # (varredura recursiva — cobre aninhamento, não só chaves de topo).
    leaked = _scan_forbidden(data)
    if leaked:
        current_app.logger.error(
            "POST /payments/card/charge com campos proibidos (%s) — "
            "integração do cliente está mandando dados brutos de cartão",
            ", ".join(sorted(leaked)),
        )
        return jsonify({
            "error": "dados brutos de cartão não são aceitos — use o token "
                     "gerado pelo SDK de pagamento",
            "code": "RAW_CARD_DATA",
        }), 400

    try:
        charge = card_svc.create_card_charge(
            g.current_user,
            package_key=(data.get("package") or "").strip().lower() or None,
            amount_brl=data.get("amount_brl"),
            card_token=data.get("card_token") or "",
            payment_method_id=data.get("payment_method_id") or "",
            installments=data.get("installments") or 1,
            issuer_id=data.get("issuer_id") or "",
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except card_svc.CardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(charge.to_dict()), 201


@bp.get("/charge/<charge_id>")
@login_required
def get_charge(charge_id: str):
    charge = db.session.get(CardCharge, charge_id)
    if charge is None or charge.user_id != g.current_user.id:
        return jsonify({"error": "not found"}), 404
    return jsonify(charge.to_dict())


@bp.get("/my-charges")
@login_required
def my_charges():
    rows = (
        db.session.query(CardCharge)
        .filter_by(user_id=g.current_user.id)
        .order_by(CardCharge.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({"items": [c.to_dict() for c in rows]})
