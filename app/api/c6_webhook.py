"""Webhook de Pix RECEBIDO do C6 Bank (cobrança, compra de pontos).

Segurança, leia antes de mexer:

O C6 chama a URL cadastrada em `PUT /webhook/{chave}` (acrescida de `/pix`,
como manda o padrão Bacen) com `{"pix": [{endToEndId, txid, valor, ...}]}`.
A autenticação de origem do Bacen é mTLS do PSP, que o Render termina antes
de chegar aqui, então não dá para verificá-la. O desenho compensa isso:

  1. A URL registrada carrega um segredo no caminho
     (`/payments/c6/webhook/<C6_WEBHOOK_TOKEN>`), comparado em tempo
     constante. Sem o token certo, 401.
  2. O payload é GATILHO, nunca verdade: antes de creditar, reconsultamos
     `GET /cob/{txid}` na API do C6 (autenticada com o nosso certificado).
     Quem tiver só o token não consegue creditar uma cobrança não paga.
  3. Crédito idempotente: `purchase.confirm_payment` ignora charge já paga e
     o ledger tem idempotency_key por charge. Entrega duplicada não dobra
     pontos.

Devolvemos 200 em quase tudo (o C6 pode desligar o webhook após falhas
seguidas); erro de processamento é logado como ERROR para conciliação.
"""

from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db, limiter

bp = Blueprint("c6_webhook", __name__)


def _verify_token(got: str) -> bool:
    """Fail-closed: sem C6_WEBHOOK_TOKEN configurado, rejeita fora de dev/test."""
    expected = (current_app.config.get("C6_WEBHOOK_TOKEN") or "").strip()
    got = (got or "").strip()
    if not expected:
        if current_app.debug or current_app.config.get("TESTING"):
            return True
        current_app.logger.error(
            "C6_WEBHOOK_TOKEN não configurado; rejeitando webhook do C6."
        )
        return False
    if not got:
        return False
    return hmac.compare_digest(expected, got)


@bp.post("/webhook/<token>")
@bp.post("/webhook/<token>/pix")
@limiter.limit("120 per minute")
def c6_webhook(token: str):
    if not _verify_token(token):
        current_app.logger.warning("c6 webhook: token inválido")
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": True, "ignored": "payload"}), 200
    items = payload.get("pix")
    if not isinstance(items, list):
        # Tolera um único objeto de Pix no corpo.
        items = [payload] if payload.get("txid") else []

    results = []
    deferred = False
    for item in items:
        if not isinstance(item, dict):
            continue
        txid = (item.get("txid") or "").strip()
        e2e = (item.get("endToEndId") or "").strip()
        if not txid:
            # Pix avulso (sem cobrança) não tem como amarrar a uma compra.
            current_app.logger.warning(
                "c6 webhook: pix sem txid (e2e=%s, valor=%s); conciliar manualmente.",
                e2e, item.get("valor"),
            )
            results.append({"e2e": e2e, "result": "sem-txid"})
            continue
        try:
            results.append({"txid": txid, "result": _process(txid, e2e)})
        except Exception as exc:  # noqa: BLE001
            # rollback OBRIGATÓRIO: sem ele a sessão fica suja e o commit do
            # PRÓXIMO item persiste o estado parcial deste (charge marcada
            # PAID sem crédito no ledger, e o retry morre no early-return de
            # confirm_payment). Regressão coberta em tests/test_c6_cobranca.py.
            db.session.rollback()
            deferred = True
            current_app.logger.exception(
                "c6 webhook: falha ao processar txid=%s e2e=%s: %s", txid, e2e, exc
            )
            results.append({"txid": txid, "result": "deferred"})

    # Falha de infraestrutura devolve 5xx de propósito: 200 diria ao C6 que a
    # notificação foi entregue e o gatilho se perderia (o dinheiro já entrou,
    # a charge expiraria sozinha e nada reconsultaria). Com 5xx o PSP reenvia.
    # O caminho de polling em GET /pix/charge/<id> é o segundo backstop.
    if deferred:
        return jsonify({"ok": False, "results": results}), 503
    return jsonify({"ok": True, "results": results}), 200


def _process(txid: str, e2e: str) -> str:
    from ..models import PixCharge, PixChargeStatus
    from ..services import purchase as purchase_svc

    provider = _c6_provider()
    if provider is None:
        current_app.logger.error(
            "c6 webhook recebido (txid=%s) mas o provider PIX ativo não é o C6; ignorando.",
            txid,
        )
        return "ignored"

    # A charge é carregada ANTES da API: é ela quem diz quanto deveria entrar.
    charge = db.session.query(PixCharge).filter_by(txid=txid).one_or_none()
    if charge is None:
        current_app.logger.warning(
            "c6 webhook: txid=%s não corresponde a nenhuma cobrança nossa (e2e=%s).",
            txid, e2e,
        )
        return "desconhecido"

    # Fonte de verdade: a API, não o corpo do webhook.
    cob = provider.get_cob(txid)
    pago_cents = provider.cob_paid_cents(cob)
    tem_devolucao = provider.cob_has_refund(cob)

    if tem_devolucao:
        # A cobrança segue CONCLUIDA depois de devolvida; creditar aqui daria
        # pontos por dinheiro que já saiu da conta.
        current_app.logger.error(
            "PIX DEVOLVIDO no C6: txid=%s e2e=%s (liquidado=%s cents, charge=%s cents, "
            "status da charge=%s). Se já havia crédito, os pontos podem ter sido "
            "resgatados: conciliação MANUAL obrigatória.",
            txid, e2e, pago_cents, charge.amount_cents, charge.status.value,
        )
        if pago_cents < charge.amount_cents:
            return "devolvido"

    if pago_cents <= 0:
        current_app.logger.info(
            "c6 webhook: cobrança txid=%s ainda não liquidada (status=%s)",
            txid, cob.get("status"),
        )
        return "pending"

    if pago_cents < charge.amount_cents:
        # Pagamento parcial: creditar os pontos cheios seria prejuízo direto.
        current_app.logger.error(
            "VALOR DIVERGENTE no C6: txid=%s pago=%s cents, cobrança=%s cents. "
            "Nenhum ponto creditado; conciliação manual.",
            txid, pago_cents, charge.amount_cents,
        )
        return "valor-divergente"

    ja_estava_paga = charge.status == PixChargeStatus.PAID
    purchase_svc.confirm_payment(txid, provider_confirmed=True)
    if ja_estava_paga:
        current_app.logger.info("c6 webhook: txid=%s já creditada (reentrega)", txid)
        return "replay"
    current_app.logger.info("compra %s creditada via C6 (e2e=%s)", txid, e2e)
    return "credited"


def _c6_provider():
    pix = current_app.extensions.get("pix_provider")
    return pix if getattr(pix, "name", "") == "c6" else None
