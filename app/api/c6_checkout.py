"""Compra de pontos com CARTÃO pelo Checkout do C6 (link de pagamento C6 Pay).

Endpoints (prefixo /payments/c6, o mesmo do webhook do C6):
  POST /payments/c6/card/checkout              → cria o link; 201 {id, url, status, ...}
  GET  /payments/c6/card/checkout/<charge_id>  → status (reconsulta o C6 se pendente)

O cliente paga numa página do C6: o número do cartão nunca passa por aqui.
O crédito acontece só depois de GET /v1/checkouts/{id} = PAID, pelo webhook
(app/api/c6_webhook.py) ou, se ele se perder, pelo polling deste GET.

Gate: C6_CHECKOUT_ENABLED=1 e provider PIX ativo = c6. Fora disso, 503.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from ..extensions import db, limiter
from ..models import PixCharge
from ..services import purchase as purchase_svc
from .auth import login_required

bp = Blueprint("c6_checkout", __name__)


def _habilitado() -> bool:
    provider = current_app.extensions.get("pix_provider")
    return bool(current_app.config.get("C6_CHECKOUT_ENABLED")) and getattr(provider, "name", "") == "c6"


def _resposta(charge: PixCharge) -> dict:
    return {**charge.to_dict(), "url": charge.br_code, "meio": "cartao"}


@bp.post("/card/checkout")
@login_required
# Mesmo limite da compra por PIX: cada chamada cria um checkout real no C6.
@limiter.limit("10 per hour",
               key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
def criar_checkout():
    """Body: {"package": "plus"} OU {"amount_brl": 50.00}."""
    if not _habilitado():
        return jsonify({
            "error": "pagamento com cartão indisponível no momento",
            "code": "C6_CHECKOUT_DISABLED",
        }), 503
    data = request.get_json(silent=True) or {}
    package_key = (data.get("package") or "").strip().lower() or None
    try:
        charge = purchase_svc.create_card_checkout(
            g.current_user, package_key=package_key, amount_brl=data.get("amount_brl"),
        )
    except purchase_svc.PixError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_resposta(charge)), 201


@bp.get("/card/checkout/<charge_id>")
@login_required
# Cada GET pode reconsultar o C6: o polling do front faz 20 em 1 minuto.
@limiter.limit("30 per minute",
               key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
def consultar_checkout(charge_id: str):
    charge = db.session.get(PixCharge, charge_id)
    if (charge is None or charge.user_id != g.current_user.id
            or charge.flow != purchase_svc.FLOW_C6_CARTAO):
        return jsonify({"error": "not found"}), 404
    purchase_svc.refresh_card_checkout(charge)
    purchase_svc.expire_if_needed(charge)
    return jsonify(_resposta(charge))
