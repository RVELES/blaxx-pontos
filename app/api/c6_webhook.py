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

Checkout (cartão / link de pagamento) usa OUTRO formato, o das notificações
do C6 (`POST /v1/webhooks` com `service: "CHECKOUT"`): `{external_id,
date_time, status, client_id, partner_id, service, information}`, em que
`information` é o checkout serializado em STRING. Ele chega na mesma URL ou
em `.../checkout`. Todo evento é registrado (homologação C_0501/C_0502); só
PAID pode creditar, e com as mesmas regras do Pix: o corpo é gatilho, a
charge (flow "c6-cartao") é achada pela `external_reference_id` e o checkout
é reconsultado em `GET /v1/checkouts/{id}` pelo id guardado na criação antes
de creditar (ver `_checkout_pago` e `purchase.sync_checkout`). O `information`
traz nome, CPF/CNPJ, e-mail e endereço do pagador e o token do cartão;
nada disso vai para log (ver `_resumo_checkout`).
"""

from __future__ import annotations

import hmac
import json
import re

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
    return hmac.compare_digest(expected.encode("utf-8"), got.encode("utf-8"))


@bp.post("/webhook/<token>")
@bp.post("/webhook/<token>/pix")
@bp.post("/webhook/<token>/checkout")
@limiter.limit("120 per minute")
def c6_webhook(token: str):
    if not _verify_token(token):
        current_app.logger.warning("c6 webhook: token inválido")
        return jsonify({"error": "unauthorized"}), 401

    # force=True: as notificações do C6 não garantem o Content-Type.
    try:
        payload = request.get_json(silent=True, force=True) or {}
    except RecursionError:
        payload = None
    if not isinstance(payload, dict):
        return jsonify({"ok": True, "ignored": "payload"}), 200
    if "pix" not in payload and payload.get("service"):
        if _limpo(payload.get("service"), 24).strip().upper() != "PIX":
            return _notificacao_c6(payload)
        # Pix no envelope do C6: a lista `pix` do Bacen vem DENTRO de
        # `information`, como string. Sem desembrulhar, a compra paga só
        # seria creditada pelo polling.
        info = _decodifica_information(payload.get("information"))
        if not info or not isinstance(info.get("pix"), list):
            current_app.logger.warning(
                "c6 webhook: notificação PIX sem lista pix legível (status=%s); "
                "conciliar pelo polling.", _limpo(payload.get("status"), 24),
            )
            return jsonify({"ok": True, "results": [], "ignored": "information"}), 200
        payload = info
    items = payload.get("pix")
    if not isinstance(items, list):
        # Tolera um único objeto de Pix no corpo.
        items = [payload] if payload.get("txid") else []

    results = []
    deferred = False
    for item in items:
        if not isinstance(item, dict):
            continue
        txid = item.get("txid") if isinstance(item.get("txid"), str) else ""
        txid = txid.strip()
        e2e = item.get("endToEndId") if isinstance(item.get("endToEndId"), str) else ""
        e2e = e2e.strip()
        if not txid and e2e:
            # O exemplo oficial da notificação PIX do C6 não traz txid: o
            # txid vem da API autenticada (GET /pix/{e2eid}), nunca do corpo.
            try:
                txid = _txid_por_e2e(e2e)
            except Exception as exc:  # noqa: BLE001
                deferred = True
                current_app.logger.exception(
                    "c6 webhook: falha ao consultar e2e=%s: %s", _limpo(e2e), exc
                )
                results.append({"e2e": e2e, "result": "deferred"})
                continue
        if not txid:
            # Pix avulso (sem cobrança) não tem como amarrar a uma compra.
            current_app.logger.warning(
                "c6 webhook: pix sem txid (e2e=%s, valor=%s); conciliar manualmente.",
                _limpo(e2e), _limpo(item.get("valor"), 20),
            )
            results.append({"e2e": e2e, "result": "sem-txid"})
            continue
        try:
            results.append({"txid": txid, "result": _process(txid, _limpo(e2e))})
        except Exception as exc:  # noqa: BLE001
            # rollback OBRIGATÓRIO: sem ele a sessão fica suja e o commit do
            # PRÓXIMO item persiste o estado parcial deste (charge marcada
            # PAID sem crédito no ledger, e o retry morre no early-return de
            # confirm_payment). Regressão coberta em tests/test_c6_cobranca.py.
            db.session.rollback()
            deferred = True
            current_app.logger.exception(
                "c6 webhook: falha ao processar txid=%s e2e=%s: %s",
                _limpo(txid), _limpo(e2e), exc,
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
            _limpo(txid), e2e,
        )
        return "desconhecido"
    if charge.flow == purchase_svc.FLOW_C6_CARTAO:
        # txid de compra com cartão (referência do Checkout) num corpo de Pix:
        # não existe cob para ele, e GET /cob falharia em loop de reenvio.
        current_app.logger.warning(
            "c6 webhook: txid=%s é de compra com cartão, não de Pix; ignorado.",
            _limpo(txid),
        )
        return "fluxo-cartao"

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


_E2E_RE = re.compile(r"^[a-zA-Z0-9]{32}$")
_TXID_RE = re.compile(r"^[a-zA-Z0-9]{26,35}$")


def _txid_por_e2e(e2e: str) -> str:
    """Qual cobrança este Pix pagou, segundo a API. "" = Pix avulso ou
    desconhecido (404). Rede/5xx sobe: o chamador devolve 503 e o C6 reenvia."""
    from ..pix.c6 import C6Error

    provider = _c6_provider()
    if provider is None or not _E2E_RE.match(e2e):
        return ""
    try:
        pix = provider.get_pix(e2e)
    except C6Error as exc:
        st = getattr(exc, "status", None)
        if st == 404:
            return ""
        if st is not None and 400 <= st < 500 and st not in (408, 429):
            # 4xx não se resolve com reenvio (ex.: credencial sem pix.read).
            # 503 aqui faria o C6 reenviar para sempre e desligar o webhook.
            current_app.logger.error(
                "c6 webhook: GET /pix/%s devolveu HTTP %s; conciliar manualmente.",
                e2e, st,
            )
            return ""
        raise
    bruto = pix.get("txid") if isinstance(pix, dict) else None
    txid = bruto.strip() if isinstance(bruto, str) else ""
    # As nossas cobranças têm txid 26-35 (PUT /cob/{txid}); fora disso o Pix
    # não é de compra nossa (QR estático, "***") e get_cob recusaria em loop.
    return txid if _TXID_RE.match(txid) else ""


# ───────────── notificações no formato do C6 (Checkout, Boleto) ───────────── #

_SEGURO = re.compile(r"[^A-Za-z0-9_.:+\- ]")


def _limpo(valor, limite: int = 64) -> str:
    """Texto vindo de fora, pronto para log: sem quebra de linha nem controle."""
    if valor is None:
        return "-"
    return _SEGURO.sub("", str(valor))[:limite] or "-"


def _decodifica_information(bruto):
    """`information` é o objeto serializado em string. Os exemplos da spec às
    vezes trazem o ENVELOPE inteiro serializado ali dentro; desembrulha esse
    envelope uma vez. Devolve dict ou None."""
    obj = bruto
    for _ in range(3):
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except (ValueError, TypeError, RecursionError):
                return None
            continue
        if isinstance(obj, dict) and "information" in obj and "service" in obj:
            obj = obj.get("information")
            continue
        break
    return obj if isinstance(obj, dict) else None


def _resumo_checkout(info: dict | None) -> dict:
    """Só o que serve para conciliar. Fica DE FORA: payer (nome, CPF/CNPJ,
    e-mail, endereço), payment.card.token, url, description."""
    if not info:
        return {}
    pagamento = info.get("payment") if isinstance(info.get("payment"), dict) else {}
    cartao = pagamento.get("card") if isinstance(pagamento.get("card"), dict) else {}
    fraude = info.get("fraud_analysis") if isinstance(info.get("fraud_analysis"), dict) else {}
    resumo = {
        "id": _limpo(info.get("id")),
        "status": _limpo(info.get("status"), 24),
        "referencia": _limpo(info.get("external_reference_id")),
        "valor": _limpo(info.get("amount"), 16),
        "tipo": _limpo(info.get("transaction_type") or cartao.get("type"), 16),
    }
    if cartao.get("installments") is not None:
        resumo["parcelas"] = _limpo(cartao.get("installments"), 4)
    if cartao.get("return_code") is not None or cartao.get("message"):
        resumo["recusa"] = f"{_limpo(cartao.get('return_code'), 8)} {_limpo(cartao.get('message'), 60)}"
    if fraude.get("recommendation"):
        resumo["antifraude"] = _limpo(fraude.get("recommendation"), 16)
    return resumo


def _notificacao_c6(payload: dict):
    servico = _limpo(payload.get("service"), 24).strip().upper()
    status = _limpo(payload.get("status"), 24).upper()
    external_id = _limpo(payload.get("external_id"))

    if servico != "CHECKOUT":
        current_app.logger.info(
            "c6 notificação %s ignorada (status=%s, external_id=%s)",
            servico, status, external_id,
        )
        return jsonify({"ok": True, "service": servico, "result": "ignorado"}), 200

    info = _decodifica_information(payload.get("information"))
    if info is None:
        current_app.logger.warning(
            "c6 checkout: information ausente ou ilegível (status=%s, external_id=%s)",
            status, external_id,
        )
    resumo = _resumo_checkout(info)
    current_app.logger.info(
        "c6 checkout %s: external_id=%s date_time=%s %s",
        status, external_id, _limpo(payload.get("date_time"), 40),
        " ".join(f"{k}={v}" for k, v in resumo.items()),
    )
    corpo = {
        "ok": True, "service": "CHECKOUT", "status": status,
        "external_id": external_id, "result": "registrado",
    }
    status_info = _limpo((info or {}).get("status"), 24).upper()
    if info is None or not ({"PAID", "CANCELLED"} & {status, status_info}):
        # Só PAID move ponto; CANCELLED reconsulta para flagrar estorno de charge
        # já paga. CREATED/DECLINED/EXPIRED ficam no log; a charge expira pelo
        # TTL ou pelo polling, que consulta a API.
        return jsonify(corpo), 200

    credito, http = _checkout_pago(info)
    corpo["credito"] = credito
    if http != 200:
        corpo["ok"] = False
    return jsonify(corpo), http


_REF_RE = re.compile(r"^[a-zA-Z0-9]{1,10}$")


def _checkout_pago(info: dict) -> tuple[str, int]:
    """Gatilho de pagamento de cartão. O corpo só diz QUAL charge olhar.

    A charge é achada pela external_reference_id (o nosso txid de 10
    caracteres) e o checkout reconsultado na API pelo id que NÓS guardamos
    na criação; sync_checkout exige PAID, mesma referência e valor cheio.
    Rede/5xx → 503 (o C6 reenvia); 4xx permanente → ERROR + 200.
    """
    from ..models import PixCharge
    from ..pix.c6 import C6Error
    from ..services import purchase as purchase_svc

    ref = info.get("external_reference_id")
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        current_app.logger.warning("c6 checkout PAID sem external_reference_id válida; ignorado.")
        return "sem-referencia", 200
    charge = db.session.query(PixCharge).filter_by(
        txid=ref, flow=purchase_svc.FLOW_C6_CARTAO).one_or_none()
    if charge is None:
        current_app.logger.warning(
            "c6 checkout PAID: referencia=%s não é compra nossa; ignorado.", _limpo(ref))
        return "desconhecido", 200

    provider = _c6_provider()
    if provider is None or not charge.provider_ref:
        current_app.logger.error(
            "c6 checkout PAID da charge %s, mas sem provider C6 ativo ou sem id do "
            "checkout guardado; conciliar manualmente.", charge.id)
        return "ignored", 200
    if info.get("id") != charge.provider_ref:
        current_app.logger.warning(
            "c6 checkout PAID: id do corpo (%s) difere do guardado na charge %s; "
            "consultando o guardado.", _limpo(info.get("id")), charge.id)

    try:
        checkout = provider.get_checkout(charge.provider_ref)
    except C6Error as exc:
        st = getattr(exc, "status", None)
        if st is not None and 400 <= st < 500 and st not in (408, 429):
            # Reenviar não resolve 4xx (ex.: credencial sem checkout.read) e
            # 503 eterno faria o C6 desligar o webhook.
            current_app.logger.error(
                "c6 checkout: GET /v1/checkouts/%s devolveu HTTP %s (charge %s); "
                "conciliar manualmente.", charge.provider_ref, st, charge.id)
            return "erro-permanente", 200
        db.session.rollback()
        current_app.logger.exception(
            "c6 checkout: falha ao consultar a charge %s: %s", charge.id, exc)
        return "deferred", 503
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception(
            "c6 checkout: falha ao consultar a charge %s: %s", charge.id, exc)
        return "deferred", 503

    try:
        resultado = purchase_svc.sync_checkout(charge, checkout)
    except Exception as exc:  # noqa: BLE001
        # Mesmo motivo do Pix: sessão suja persistiria estado parcial.
        db.session.rollback()
        current_app.logger.exception(
            "c6 checkout: falha ao creditar a charge %s: %s", charge.id, exc)
        return "deferred", 503
    if resultado == "credited":
        current_app.logger.info("compra %s (cartão C6) creditada via webhook", charge.id)
    return resultado, 200
