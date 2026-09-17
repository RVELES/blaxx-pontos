"""Compra de pontos via PIX.

Fluxo:
  1) Usuário escolhe um pacote → POST /pix/charge
  2) Sistema cria PixCharge (PENDING), pede BR Code ao provider, devolve QR.
  3) Usuário paga no banco. Provedor envia webhook → POST /payouts/asaas/webhook
  4) Webhook marca PixCharge como PAID e credita os pontos na carteira.
     A operação é idempotente (mesmo webhook duas vezes não dobra pontos).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from ..config import Config
from ..extensions import db
from ..models import (
    PixCharge,
    PixChargeStatus,
    PointPackage,
    TxType,
    User,
)
from ..pix.provider import PixChargeRequest, PixProvider
from . import aml as aml_svc
from . import metrics as metrics_svc
from . import wallet as wallet_svc


class PixError(Exception):
    pass


def seed_default_packages() -> int:
    """Idempotente: popula point_packages a partir de Config.POINT_PACKAGES se
    a tabela estiver vazia. Chamado no boot. Retorna quantos inseriu."""
    if db.session.query(PointPackage.key).first() is not None:
        return 0
    inserted = 0
    for i, (key, pkg) in enumerate(Config.POINT_PACKAGES.items()):
        db.session.add(PointPackage(
            key=key,
            label=pkg["label"],
            points=int(pkg["points"]),
            price_cents=int(round(pkg["price_brl"] * 100)),
            active=True,
            sort_order=i,
        ))
        inserted += 1
    db.session.commit()
    return inserted


def _package_row(key: str) -> PointPackage | None:
    return db.session.get(PointPackage, key)


def resolve_package(key: str) -> dict | None:
    """Preço e pontos de um pacote — FONTE ÚNICA para PIX e cartão.

    Achado M-3 (revisão de 20/07): o fluxo PIX lia a tabela `point_packages`,
    editável pelo admin e servida em GET /pix/packages; o fluxo de CARTÃO lia
    `Config.POINT_PACKAGES`, hardcoded. Publicar um preço novo mudava o que o
    cliente via e não mudava o que o cartão cobrava — divergência silenciosa
    entre preço exibido e preço cobrado, nos dois sentidos.

    O DB manda. `Config` é fallback só para banco ainda sem seed.
    Devolve None se o pacote não existe em lugar nenhum.
    """
    row = _package_row(key)
    if row is not None and row.active:
        return {"amount_cents": row.price_cents, "points": row.points,
                "label": row.label, "fonte": "db"}
    pkg = Config.POINT_PACKAGES.get(key)
    if pkg is None:
        return None
    return {"amount_cents": int(round(pkg["price_brl"] * 100)),
            "points": pkg["points"], "label": pkg["label"], "fonte": "config"}


def pending_purchase_points_this_month(user_id: str) -> int:
    """Soma dos pontos de compras AINDA NÃO creditadas (charges abertas)
    criadas no mês corrente — PIX pending/pending_confirmation + cartão
    pending/in_process.

    Entra no forecast do teto mensal junto com o já creditado: sem isso, várias
    charges pendentes passavam individualmente na checagem e, ao pagar todas,
    furavam PURCHASE_MAX_POINTS_PER_MONTH (que também é controle AML). Cobre PIX
    e cartão porque ambos creditam TxType.PURCHASE no mesmo teto.
    """
    from sqlalchemy import func

    from ..models import CardCharge, CardChargeStatus
    from .wallet import _month_start_utc

    month_start = _month_start_utc()
    pix = db.session.query(
        func.coalesce(func.sum(PixCharge.points_to_credit), 0)
    ).filter(
        PixCharge.user_id == user_id,
        PixCharge.created_at >= month_start,
        PixCharge.status.in_((
            PixChargeStatus.PENDING, PixChargeStatus.PENDING_CONFIRMATION,
        )),
    ).scalar() or 0
    card = db.session.query(
        func.coalesce(func.sum(CardCharge.points_to_credit), 0)
    ).filter(
        CardCharge.user_id == user_id,
        CardCharge.created_at >= month_start,
        CardCharge.status.in_((
            CardChargeStatus.PENDING, CardChargeStatus.IN_PROCESS,
        )),
    ).scalar() or 0
    return int(pix) + int(card)


def list_packages() -> dict:
    """Pacotes ativos, do DB (fonte única). Fallback pra Config se tabela vazia
    (DB virgem antes do seed) — nunca deixa o endpoint/landing sem pacotes."""
    rows = (
        db.session.query(PointPackage)
        .filter_by(active=True)
        .order_by(PointPackage.sort_order, PointPackage.key)
        .all()
    )
    if not rows:
        return dict(Config.POINT_PACKAGES)
    return {
        r.key: {"price_brl": round(r.price_cents / 100, 2), "points": r.points, "label": r.label}
        for r in rows
    }


def _provider() -> PixProvider:
    return current_app.extensions["pix_provider"]


def create_charge(
    user: User,
    package_key: str | None = None,
    amount_brl: float | None = None,
) -> PixCharge:
    """Cria charge PIX via MP. Aceita pacote pré-definido OU valor livre.

    Exatamente UMA das duas opções deve ser fornecida:
      - package_key: chave de Config.POINT_PACKAGES (start/plus/prime/black/...)
      - amount_brl: valor livre em reais (mínimo R$ 10, máximo R$ 100k não-VIP)

    Em ambos os casos a charge resultante usa o provider PIX configurado
    (Asaas em prod), gerando QR Code real do provedor.
    """
    if package_key and amount_brl is not None:
        raise PixError("informe package_key OU amount_brl, não os dois")
    if not package_key and amount_brl is None:
        raise PixError("informe package_key ou amount_brl")

    # Sprint 4 (S4-AML) — sanctions bloqueia, threshold/velocity registram.
    try:
        aml_svc.check_sanctions_or_raise(user)
    except aml_svc.SanctionsBlock as exc:
        metrics_svc.inc_purchase("blocked_sanctions", _provider().name)
        raise PixError(str(exc)) from exc

    if package_key:
        # Fonte ÚNICA: o mesmo preço do DB que o cliente viu em /pix/packages.
        # Fallback pra Config só se a linha não existir (DB antes do seed).
        pacote = resolve_package(package_key)
        if pacote is None:
            raise PixError(f"pacote desconhecido: {package_key}")
        amount_cents = pacote["amount_cents"]
        points_to_credit = pacote["points"]
        label = pacote["label"]
        description = f"BlaXx — pacote {label}"
        stored_key = package_key
    else:
        # Valor livre — validação de faixa
        try:
            amount_brl = float(amount_brl)
        except (TypeError, ValueError):
            raise PixError("amount_brl inválido")
        if amount_brl < 10:
            raise PixError("valor mínimo R$ 10,00")
        if not getattr(user, "is_vip", False) and amount_brl > 100_000:
            raise PixError("valor máximo R$ 100.000 por compra (VIP não tem limite)")
        amount_cents = int(round(amount_brl * 100))
        # Conversao via Config.CENTS_PER_POINT (default: 1 pt = 9 cents = R$ 0,09)
        points_to_credit = Config.cents_to_pts(amount_cents)
        description = f"BlaXx — R$ {amount_brl:.2f}"
        stored_key = "custom"

    # Sprint 1-2 (P0): limite MENSAL acumulado de compra (em pontos creditados).
    # Checado na CRIACAO da charge (forecast), nao na confirmacao — evita o
    # caso "cliente paga e depois nao pode creditar". VIP fica isento.
    # M-1: mesma corrida do resgate/transferência, aqui na CRIAÇÃO da charge.
    # Duas requisições simultâneas liam o mesmo acumulado e ambas passavam,
    # furando PURCHASE_MAX_POINTS_PER_MONTH. O lock da carteira serializa por
    # usuário — não protege saldo (compra credita), protege o TETO.
    wallet_svc.get_wallet_for_update(user.id)

    if not getattr(user, "is_vip", False):
        purchased_month = wallet_svc.credited_this_month(user.id, TxType.PURCHASE)
        pending_month = pending_purchase_points_this_month(user.id)
        if purchased_month + pending_month + points_to_credit > Config.PURCHASE_MAX_POINTS_PER_MONTH:
            remaining = Config.PURCHASE_MAX_POINTS_PER_MONTH - purchased_month - pending_month
            raise PixError(
                f"limite mensal de compra excedido — restam {max(remaining,0)} pts este mes"
            )

    charge = PixCharge(
        user_id=user.id,
        package_key=stored_key,
        amount_cents=amount_cents,
        points_to_credit=points_to_credit,
        br_code="",  # será preenchido a seguir
        expires_at=PixCharge.make_expiry(Config.PIX_CHARGE_TTL_SECONDS),
    )
    db.session.add(charge)
    db.session.flush()  # garante txid

    from ..pix.provider import PayerDataError
    try:
        resp = _provider().create_charge(
            PixChargeRequest(
                txid=charge.txid,
                amount_cents=amount_cents,
                description=description[:255],
                payer_name=user.name,
                payer_cpf=user.cpf,
                payer_email=user.email,    # MP exige email válido
                expires_in_seconds=Config.PIX_CHARGE_TTL_SECONDS,
            )
        )
    except PayerDataError as exc:
        # CPF/e-mail inválidos pra cobrança real (strict_payer em prod):
        # descarta a charge pendente e devolve erro amigável (HTTP 400).
        db.session.rollback()
        raise PixError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Provider fora do ar é ROTINA, não exceção: o sandbox do C6 só
        # atende seg a sex das 7h às 22h, e qualquer 5xx cairia aqui. Sem
        # este tratamento o cliente recebe o 500 HTML do Werkzeug e o Sentry
        # ganha um evento por tentativa de compra.
        db.session.rollback()
        nome = _provider().name
        current_app.logger.error(
            "PIX provider %s indisponível ao criar cobrança: %s", nome, exc
        )
        metrics_svc.inc_purchase("provider_error", nome)
        raise PixError(
            "Não foi possível gerar a cobrança PIX agora. Tente de novo em alguns minutos."
        ) from exc
    charge.br_code = resp.br_code
    charge.qr_code_image = resp.qr_code_image or None
    db.session.commit()
    metrics_svc.inc_purchase("created", _provider().name)
    # Threshold check pós-commit (não bloqueia, só registra alerta)
    try:
        aml_svc.check_transaction_threshold(
            user, points_to_credit, kind="purchase",
            monthly_limit_pts=Config.PURCHASE_MAX_POINTS_PER_MONTH
            if not getattr(user, "is_vip", False) else None,
        )
        # Commit isolado do alerta
        db.session.commit()
    except Exception:
        db.session.rollback()
    return charge


def confirm_payment(txid: str, *, provider_confirmed: bool = False) -> PixCharge:
    """Chamado pelo webhook do provedor PIX quando o pagamento é confirmado.

    Idempotente: chamadas repetidas com o mesmo txid não creditam de novo.

    provider_confirmed=True (webhook MP após get_payment=approved): o dinheiro
    ENTROU — credita mesmo que a charge tenha expirado localmente (corrida na
    borda do TTL). False (mock/simulate): mantém a recusa por expiração.
    """
    charge = db.session.query(PixCharge).filter_by(txid=txid).one_or_none()
    if charge is None:
        raise PixError(f"charge não encontrada: txid={txid}")

    if charge.status == PixChargeStatus.PAID:
        return charge  # já foi processada

    if charge.status == PixChargeStatus.EXPIRED or charge.is_expired():
        if not provider_confirmed:
            charge.status = PixChargeStatus.EXPIRED
            db.session.commit()
            raise PixError("charge expirada")
        current_app.logger.warning(
            "charge %s expirada localmente mas pagamento confirmado pelo "
            "provider — creditando mesmo assim", charge.id,
        )

    # O crédito vem ANTES de marcar PAID: se `credit` levantar (carteira
    # ausente, erro de DB), a sessão nunca chega a carregar uma charge PAID
    # sem lançamento no ledger. Um commit posterior de outro handler no mesmo
    # request persistiria esse estado, e o retry morreria no early-return de
    # `status == PAID` acima — cliente paga e nunca recebe.
    wallet_svc.credit(
        user_id=charge.user_id,
        amount_pts=charge.points_to_credit,
        tx_type=TxType.PURCHASE,
        description=f"Compra de pontos — pacote {charge.package_key}",
        reference=charge.id,
        idempotency_key=f"charge:{charge.id}",  # blinda contra webhook duplicado
    )
    charge.status = PixChargeStatus.PAID
    charge.paid_at = datetime.now(timezone.utc)
    db.session.commit()
    metrics_svc.inc_purchase("paid", _provider().name)
    # Sprint 7 — push pro user confirmando crédito
    try:
        from . import push as push_svc
        push_svc.send_to_user(
            charge.user_id,
            "Pagamento confirmado",
            f"+{charge.points_to_credit} pts creditados na sua carteira.",
            data={"charge_id": charge.id, "amount_brl": charge.amount_cents/100},
        )
    except Exception:
        pass
    return charge


def expire_if_needed(charge: PixCharge) -> PixCharge:
    if charge.status == PixChargeStatus.PENDING and charge.is_expired():
        charge.status = PixChargeStatus.EXPIRED
        db.session.commit()
    return charge
