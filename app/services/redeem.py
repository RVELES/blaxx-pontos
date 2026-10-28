"""Resgate de pontos via PIX (cashback).

Fluxo:
  1) Usuário pede resgate informando pontos + chave PIX.
  2) Sistema valida (mínimo 2.500 pts, múltiplo da conversão, limite diário).
  3) Cria PixPayout (REQUESTED), DEBITA pontos da wallet imediatamente.
  4) Chama provider.request_payout (PROCESSING).
  5) Se sucesso → PAID; se falha → FAILED + REFUND dos pontos (estorno).

Conversão: Config.CENTS_PER_POINT (default 9 cents = 1 ponto = R$ 0,09).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..config import Config
from ..extensions import db
from ..models import (
    PixPayout,
    PixPayoutStatus,
    TxType,
    User,
)
from ..pix.provider import PixPayoutRequest, PixProvider
from . import aml as aml_svc
from . import metrics as metrics_svc
from . import wallet as wallet_svc

logger = logging.getLogger(__name__)


class RedeemError(Exception):
    pass


def _provider() -> PixProvider:
    return current_app.extensions["pix_provider"]


def quote(points: int) -> dict:
    """Cota o resgate: quanto o usuário recebe em R$ por X pontos."""
    if points <= 0:
        raise RedeemError("informe um valor de pontos positivo")
    cents = Config.pts_to_cents(points)  # pts * CENTS_PER_POINT
    return {
        "points": points,
        "amount_cents": cents,
        "amount_brl": round(cents / 100, 2),
        "rate": Config.rate_label(),
        "cents_per_point": Config.CENTS_PER_POINT,
    }


def request_redeem(
    user: User,
    *,
    points: int,
    pix_key: str,
    password: str,
    mfa_code: str | None = None,
    idempotency_key: str | None = None,
) -> PixPayout:
    """Sprint 1-2 (P0): aceita `idempotency_key` (str <=64). Quando setada,
    grava na coluna `pix_payouts.idempotency_key` — UNIQUE por (user_id, key).
    A consulta de duplicata pre-request acontece em /redeem (api/redeem.py)
    pra retornar o payout original sem mover dinheiro."""
    if not user.check_password(password):
        raise RedeemError("senha incorreta")

    # B13 · step-up 2FA p/ resgates altos (só p/ quem tem 2FA ativo).
    from ..security import enforce_step_up_mfa
    enforce_step_up_mfa(user, points, mfa_code)

    # Sprint 3 (S3-10) · Gate de CPF real antes de payout PIX.
    # Usuarios que entraram so via Google recebem um placeholder do tipo
    # "G:xxxx12chars" como CPF. Esse CPF nao existe na Receita Federal
    # e o PSP vai recusar (DICT do BACEN exige titularidade). Em vez de
    # deixar quebrar la na frente, recusamos cedo com mensagem util.
    if not user.cpf or user.cpf.startswith("G:") or ":" in (user.cpf or ""):
        raise RedeemError(
            "Para resgatar via PIX, complete seu CPF real no perfil. "
            "Sua conta foi criada via Google sem CPF informado."
        )

    # O gate acima confere FORMATO, não identidade: um CPF sintaticamente
    # válido e inventado passava por ele. `services/kyc.py` já sabia validar
    # contra fonte externa e gravar `user.kyc_validated_at` — mas ninguém lia
    # esse campo, então a verificação existia e não guardava nada.
    #
    # Aqui ela passa a guardar o único caminho que tira dinheiro da conta.
    # Fail-closed de propósito: se a validação não puder ser concluída,
    # recusamos. Num trilho de saída de dinheiro, "não consegui verificar" tem
    # de ser tratado como "não verificado" — e com PAYOUT_MODE=manual a fila do
    # admin continua sendo a saída para casos legítimos travados aqui.
    if not user.kyc_validated_at:
        from .kyc import validate_cpf_and_mark_user

        try:
            resultado = validate_cpf_and_mark_user(user)
        except Exception:  # noqa: BLE001 — indisponibilidade não pode virar 500
            logger.exception("KYC indisponível no resgate · user=%s", user.id)
            raise RedeemError(
                "Não foi possível verificar seu CPF agora. Tente novamente em "
                "alguns minutos."
            )
        if not resultado.get("valid"):
            raise RedeemError(
                "Não conseguimos validar seu CPF junto à Receita Federal. "
                "Confira os dados do seu perfil ou fale com o suporte."
            )

    if points < Config.REDEEM_MIN_POINTS:
        raise RedeemError(
            f"resgate mínimo é {Config.REDEEM_MIN_POINTS} pts"
        )

    # Nao ha restricao de "multiplo" porque CENTS_PER_POINT=9 ja produz
    # valores inteiros em centavos para qualquer numero inteiro de pontos.

    if not pix_key or len(pix_key) > 180:
        raise RedeemError("chave PIX inválida")

    # Sprint 4 (S4-AML) — sanctions bloqueia, threshold/velocity registram alerta.
    try:
        aml_svc.check_sanctions_or_raise(user)
    except aml_svc.SanctionsBlock as exc:
        metrics_svc.inc_redeem("blocked_sanctions")
        raise RedeemError(str(exc)) from exc
    aml_svc.check_transaction_threshold(
        user, points, kind="redeem",
        monthly_limit_pts=Config.REDEEM_MAX_POINTS_PER_MONTH if not user.is_vip else None,
    )
    aml_svc.check_velocity(user, tx_type=TxType.REDEEM)

    # M-1 (revisão de 20/07): a soma do período rodava FORA do lock da carteira,
    # e o lock só era adquirido lá dentro do `debit`. Duas requisições simultâneas
    # liam o mesmo total, ambas passavam na comparação e ambas debitavam — furando
    # REDEEM_MAX_POINTS_PER_DAY/MONTH. O saldo nunca esteve em risco (o
    # SELECT ... FOR UPDATE serializa o débito e há CHECK(balance_pts >= 0)); o que
    # vazava era o limite regulatório de AML.
    #
    # Adquirir o lock ANTES da soma serializa por usuário: a segunda requisição
    # espera, e quando entra já enxerga o débito committado da primeira.
    wallet_svc.get_wallet_for_update(user.id)

    # Limite diario: usuarios VIP nao tem teto.
    # Demais: REDEEM_MAX_POINTS_PER_DAY (default = R$ 100.000 convertidos em pts).
    if not user.is_vip:
        # LÍQUIDO de estornos: um resgate que falhou e foi estornado devolveu os
        # pontos, então não deve pesar no teto (senão uma falha do provedor
        # bloquearia a nova tentativa legítima do cliente).
        redeemed_today = wallet_svc.net_redeemed_today(user.id)
        if redeemed_today + points > Config.REDEEM_MAX_POINTS_PER_DAY:
            remaining = Config.REDEEM_MAX_POINTS_PER_DAY - redeemed_today
            raise RedeemError(
                f"limite diário de resgate excedido — restam {max(remaining,0)} pts hoje"
            )
        # Sprint 1-2 (P0): limite MENSAL acumulado (também líquido de estornos).
        redeemed_month = wallet_svc.net_redeemed_this_month(user.id)
        if redeemed_month + points > Config.REDEEM_MAX_POINTS_PER_MONTH:
            remaining = Config.REDEEM_MAX_POINTS_PER_MONTH - redeemed_month
            raise RedeemError(
                f"limite mensal de resgate excedido — restam {max(remaining,0)} pts este mes"
            )

    quoted = quote(points)
    idem = (idempotency_key or None)
    if idem:
        idem = idem.strip()[:64] or None
    payout = PixPayout(
        user_id=user.id,
        points_debited=points,
        amount_cents=quoted["amount_cents"],
        pix_key=pix_key,
        idempotency_key=idem,
    )
    db.session.add(payout)
    try:
        db.session.flush()
    except IntegrityError as exc:
        # Corrida: 2 retries simultaneos com a mesma idem_key — UNIQUE
        # barra o segundo INSERT. Devolve o payout original.
        db.session.rollback()
        if idem:
            existing = (
                db.session.query(PixPayout)
                .filter_by(user_id=user.id, idempotency_key=idem)
                .one_or_none()
            )
            if existing is not None:
                return existing
        raise RedeemError("resgate duplicado detectado") from exc

    # 1) Debita pontos ANTES de chamar o provedor — protege contra duplicidade
    try:
        wallet_svc.debit(
            user_id=user.id,
            amount_pts=points,
            tx_type=TxType.REDEEM,
            description=f"Resgate via PIX → {pix_key}",
            reference=payout.id,
            idempotency_key=f"redeem-debit:{payout.id}",
        )
    except wallet_svc.InsufficientBalance as exc:
        db.session.rollback()
        raise RedeemError(str(exc)) from exc

    # 2) Pede o pagamento ao provedor
    payout.status = PixPayoutStatus.PROCESSING
    db.session.flush()

    # Modo manual (produção sem provider de payout integrado): o débito fica
    # retido em PROCESSING, admins recebem notificação, executam o PIX no
    # banco e confirmam em POST /admin/payouts/<id>/confirm (ou /fail, que
    # estorna). Evita o comportamento antigo: MP sem payout_provider
    # retornava 'failed' na hora e TODA venda era estornada.
    # Modo efetivo resolvido no boot (app/__init__.py): se o provider PIX não
    # sabe fazer payout, o efetivo vira "manual" mesmo sem a env var, evitando
    # que todo resgate falhe e estorne. Fallback pro Config se não resolvido.
    # B-9: o fallback era `Config.PAYOUT_MODE`, que default-ava "auto" — ou
    # seja, o caminho de erro escolhia o modo que faz o resgate falhar e
    # estornar. Agora o fallback e "manual": se por algum motivo o factory
    # nao resolveu o modo, o resgate espera numa fila (recuperavel) em vez
    # de tentar pagar sem provider (nao e).
    payout_mode = current_app.config.get("PAYOUT_MODE_EFFECTIVE", "manual")
    if payout_mode == "manual":
        from ..models import Notification
        admins = db.session.query(User).filter_by(role="admin").all()
        for admin in admins:
            db.session.add(Notification(
                user_id=admin.id, type="system",
                title="Payout PIX aguardando execução",
                body=(f"{user.name} resgatou {points} pts — transferir "
                      f"R$ {payout.amount_cents/100:.2f} para a chave "
                      f"{pix_key} e confirmar no painel admin."),
                icon="💸",
                reference=payout.id,
            ))
        db.session.commit()
        metrics_svc.inc_redeem(payout.status.value)
        try:
            from . import push as push_svc
            push_svc.send_to_user(
                user.id, "Resgate em processamento",
                f"R$ {payout.amount_cents/100:.2f} via PIX em até 1 dia útil.",
                data={"payout_id": payout.id, "status": payout.status.value},
            )
        except Exception:
            pass
        from . import avisos_email
        avisos_email.venda(payout)
        return payout

    resp = _provider().request_payout(
        PixPayoutRequest(
            txid=payout.txid,
            amount_cents=payout.amount_cents,
            pix_key=pix_key,
            description="BlaXx — resgate",
        )
    )

    # Guarda o id da transferência no provedor ANTES de qualquer decisão: é o
    # único caminho confiável de reconsulta/conciliação (o Asaas não documenta
    # filtro por externalReference na listagem de transferências).
    provider_ref = getattr(resp, "provider_transfer_id", None)
    if provider_ref:
        payout.provider_transfer_id = provider_ref
        db.session.flush()

    if resp.status == "paid":
        payout.status = PixPayoutStatus.PAID
        payout.end_to_end_id = resp.end_to_end_id
        payout.paid_at = datetime.now(timezone.utc)
    elif resp.status == "processing":
        # Provedor confirma depois via callback — débito permanece
        payout.end_to_end_id = resp.end_to_end_id or None
    else:
        # Falha no provedor → estorna pontos (refund)
        payout.status = PixPayoutStatus.FAILED
        payout.failure_reason = resp.failure_reason or "falha no provedor PIX"
        wallet_svc.credit(
            user_id=user.id,
            amount_pts=points,
            tx_type=TxType.REFUND,
            description=f"Estorno de resgate falho — {payout.failure_reason}",
            reference=payout.id,
            idempotency_key=f"redeem-refund:{payout.id}",
        )

    db.session.commit()
    metrics_svc.inc_redeem(payout.status.value)
    # Sprint 7 — notifica user quando payout finaliza (incluindo failed)
    try:
        from . import push as push_svc
        title = "Resgate processado" if payout.status == PixPayoutStatus.PAID else \
                "Resgate em processamento" if payout.status == PixPayoutStatus.PROCESSING else \
                "Falha no resgate"
        body = (f"R$ {payout.amount_cents/100:.2f} via PIX "
                f"({payout.status.value}).")
        push_svc.send_to_user(user.id, title, body,
                              data={"payout_id": payout.id, "status": payout.status.value})
    except Exception:
        pass
    from . import avisos_email
    avisos_email.venda(payout)
    return payout


def confirm_payout(txid: str, *, end_to_end_id: str | None = None) -> PixPayout:
    """Para provedores que confirmam por callback (status assíncrono) e para
    a confirmação manual do admin (PAYOUT_MODE=manual)."""
    payout = db.session.query(PixPayout).filter_by(txid=txid).one_or_none()
    if payout is None:
        raise RedeemError(f"payout não encontrado: txid={txid}")

    if payout.status == PixPayoutStatus.PAID:
        return payout
    if payout.status == PixPayoutStatus.FAILED:
        # Já estornado — confirmar agora creditaria o user duas vezes
        # (estorno + PIX). Exige intervenção manual consciente.
        raise RedeemError(
            "payout já falhou e foi estornado — não pode ser confirmado"
        )

    payout.status = PixPayoutStatus.PAID
    payout.paid_at = datetime.now(timezone.utc)
    if end_to_end_id:
        payout.end_to_end_id = end_to_end_id
    db.session.commit()
    from . import avisos_email
    avisos_email.venda(payout)
    return payout


def fail_payout(txid: str, *, reason: str) -> PixPayout:
    """Marca payout como FAILED e estorna os pontos debitados.

    Usado pelo admin (PAYOUT_MODE=manual, ex.: chave PIX inexistente) e por
    callbacks de provider assíncrono. Idempotente: repetir não estorna 2x
    (idempotency_key do crédito) e payout já FAILED retorna direto.
    """
    payout = db.session.query(PixPayout).filter_by(txid=txid).one_or_none()
    if payout is None:
        raise RedeemError(f"payout não encontrado: txid={txid}")

    if payout.status == PixPayoutStatus.FAILED:
        return payout
    if payout.status == PixPayoutStatus.PAID:
        raise RedeemError("payout já foi pago — não pode ser marcado como falho")

    payout.status = PixPayoutStatus.FAILED
    payout.failure_reason = (reason or "falha no payout")[:255]
    wallet_svc.credit(
        user_id=payout.user_id,
        amount_pts=payout.points_debited,
        tx_type=TxType.REFUND,
        description=f"Estorno de resgate falho — {payout.failure_reason}",
        reference=payout.id,
        idempotency_key=f"redeem-refund:{payout.id}",
    )
    db.session.commit()
    metrics_svc.inc_redeem(payout.status.value)
    try:
        from . import push as push_svc
        push_svc.send_to_user(
            payout.user_id, "Falha no resgate",
            f"R$ {payout.amount_cents/100:.2f} não pôde ser transferido: "
            f"{payout.failure_reason}. Seus pontos foram estornados.",
            data={"payout_id": payout.id, "status": payout.status.value},
        )
    except Exception:
        pass
    from . import avisos_email
    avisos_email.venda(payout)
    return payout
