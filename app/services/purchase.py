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
    from sqlalchemy import func, or_

    from ..models import CardCharge, CardChargeStatus
    from .wallet import _month_start_utc

    month_start = _month_start_utc()
    pix = db.session.query(
        func.coalesce(func.sum(PixCharge.points_to_credit), 0)
    ).filter(
        PixCharge.user_id == user_id,
        # Link de cartão vale 7 dias: a charge criada no fim do mês continua
        # pagável no seguinte e precisa contar no teto dele também.
        or_(PixCharge.created_at >= month_start, PixCharge.expires_at >= month_start),
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


def _cotar_compra(
    user: User,
    package_key: str | None,
    amount_brl,
) -> tuple[int, int, str, str]:
    """Validações comuns a PIX e cartão C6: AML, pacote ou valor livre, faixa.

    Devolve (amount_cents, points_to_credit, description, stored_key).
    Ficam aqui, e não em cada fluxo, para que um meio de pagamento novo não
    nasça com regra diferente (preço, faixa ou sanção) da compra por PIX.
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
        label = pacote["label"]
        return (pacote["amount_cents"], pacote["points"],
                f"BlaXx — pacote {label}", package_key)

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
    return (amount_cents, Config.cents_to_pts(amount_cents),
            f"BlaXx — R$ {amount_brl:.2f}", "custom")


def _travar_e_checar_limite_mensal(user: User, points_to_credit: int) -> None:
    """Teto mensal de compra, checado na CRIAÇÃO (forecast), com a carteira travada.

    Sprint 1-2 (P0): limite MENSAL acumulado de compra (em pontos creditados).
    Checado na criação da charge, nao na confirmacao — evita o caso "cliente
    paga e depois nao pode creditar". VIP fica isento.
    M-1: duas requisições simultâneas liam o mesmo acumulado e ambas passavam,
    furando PURCHASE_MAX_POINTS_PER_MONTH. O lock da carteira serializa por
    usuário — não protege saldo (compra credita), protege o TETO. O lock vale
    até o commit/rollback de quem chamou.
    """
    wallet_svc.get_wallet_for_update(user.id)

    if not getattr(user, "is_vip", False):
        purchased_month = wallet_svc.credited_this_month(user.id, TxType.PURCHASE)
        pending_month = pending_purchase_points_this_month(user.id)
        if purchased_month + pending_month + points_to_credit > Config.PURCHASE_MAX_POINTS_PER_MONTH:
            remaining = Config.PURCHASE_MAX_POINTS_PER_MONTH - purchased_month - pending_month
            raise PixError(
                f"limite mensal de compra excedido — restam {max(remaining,0)} pts este mes"
            )


def _alerta_aml_pos_commit(user: User, points_to_credit: int) -> None:
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
    amount_cents, points_to_credit, description, stored_key = _cotar_compra(
        user, package_key, amount_brl)
    _travar_e_checar_limite_mensal(user, points_to_credit)

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
    _alerta_aml_pos_commit(user, points_to_credit)
    return charge


# ───────────── compra com cartão: Checkout C6 Pay (link de pagamento) ───────────── #

FLOW_C6_CARTAO = "c6-cartao"
# Validade do link no C6 quando não informada: 7 dias (a spec não deixa
# escolher). A charge local acompanha o link: com o TTL de 30 min do PIX ela
# sairia do forecast do teto mensal enquanto o link ainda aceita pagamento.
C6_CHECKOUT_TTL_SECONDS = 7 * 24 * 3600
_REF_ALFABETO = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _nova_referencia_checkout() -> str:
    """external_reference_id: 10 alfanuméricos (teto da spec), único.

    Vira o txid da charge: o UNIQUE de pix_charges.txid garante que não
    repete do nosso lado (o C6 recusa repetição), e o webhook acha a charge
    por ele. 62^10 ≈ 8e17 combinações; a consulta só cobre o azar.
    """
    import secrets

    for _ in range(5):
        ref = "".join(secrets.choice(_REF_ALFABETO) for _ in range(10))
        if db.session.query(PixCharge.id).filter_by(txid=ref).first() is None:
            return ref
    raise PixError("não foi possível gerar a referência do pagamento; tente de novo")


def _c6_provider_ou_none():
    p = current_app.extensions.get("pix_provider")
    return p if getattr(p, "name", "") == "c6" else None


def _redirect_url_checkout(charge: PixCharge) -> str:
    """Para onde o C6 devolve o cliente depois de pagar. Configurável porque o
    front que recebe ainda não existe; o default cai na área logada."""
    base = (current_app.config.get("C6_CHECKOUT_REDIRECT_URL") or "").strip()
    if not base:
        base = (current_app.config.get("FRONTEND_URL") or "").rstrip("/") + "/dashboard"
    if not base.startswith("https://"):
        return ""
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}compra_cartao={charge.id}"
    return url if len(url) <= 300 else ""


def create_card_checkout(
    user: User,
    package_key: str | None = None,
    amount_brl: float | None = None,
) -> PixCharge:
    """Compra de pontos com cartão: cria o link de pagamento no Checkout do C6.

    Mesmas regras da compra por PIX (_cotar_compra + teto mensal travado).
    A charge mora em pix_charges com flow="c6-cartao" para herdar o forecast
    do teto, o crédito idempotente de confirm_payment e a conciliação:
      · txid        = external_reference_id (10 alfanuméricos, nosso)
      · br_code     = URL do checkout (para onde o cliente vai pagar)
      · provider_ref = UUID do checkout no C6 (consulta/cancelamento)
    O crédito NUNCA acontece aqui: só depois de GET /v1/checkouts/{id} = PAID
    (webhook ou polling, ver sync_checkout).
    """
    from ..pix.c6 import CHECKOUT_MIN_CENTS

    provider = _c6_provider_ou_none()
    if provider is None:
        raise PixError("pagamento com cartão indisponível no momento")

    amount_cents, points_to_credit, description, stored_key = _cotar_compra(
        user, package_key, amount_brl)
    if amount_cents < CHECKOUT_MIN_CENTS:
        # Mínimo do Checkout do C6. Só um pacote barato (preço editável no
        # admin) chega aqui: o valor livre já exige R$ 10.
        raise PixError("valor mínimo R$ 5,00 para pagamento com cartão")
    _travar_e_checar_limite_mensal(user, points_to_credit)

    charge = PixCharge(
        user_id=user.id,
        package_key=stored_key,
        amount_cents=amount_cents,
        points_to_credit=points_to_credit,
        txid=_nova_referencia_checkout(),
        br_code="",  # URL do checkout, preenchida a seguir
        expires_at=PixCharge.make_expiry(C6_CHECKOUT_TTL_SECONDS),
        flow=FLOW_C6_CARTAO,
    )
    db.session.add(charge)
    db.session.flush()  # reserva o txid (UNIQUE) e dá o id para o redirect

    try:
        checkout = provider.create_checkout(
            amount_cents, charge.txid, description, _redirect_url_checkout(charge),
        )
        if len(checkout["url"]) > 1024:
            raise ValueError("url do checkout maior que br_code comporta")
    except Exception as exc:  # noqa: BLE001
        # Mesmo raciocínio do PIX: C6 fora do ar (sandbox só seg-sex 7h-22h)
        # é rotina. Sem a charge, nada fica no forecast do teto.
        db.session.rollback()
        current_app.logger.error(
            "C6 Checkout indisponível ao criar link de cartão: %s", exc
        )
        metrics_svc.inc_purchase("provider_error", "c6-cartao")
        raise PixError(
            "Não foi possível gerar o pagamento com cartão agora. Tente de novo em alguns minutos."
        ) from exc
    charge.br_code = checkout["url"]
    charge.provider_ref = checkout["id"]
    db.session.commit()
    metrics_svc.inc_purchase("created", "c6-cartao")
    _alerta_aml_pos_commit(user, points_to_credit)
    return charge


def sync_checkout(charge: PixCharge, checkout: dict) -> str:
    """Aplica à charge o checkout CONSULTADO NA API do C6 (nunca o do webhook).

    Credita só com status PAID, a mesma referência externa e valor >= o da
    charge. Idempotente (confirm_payment). Devolve o resultado para log/teste:
    credited | replay | pending | expirado | estornado | valor-divergente |
    referencia-divergente.
    """
    from ..pix.c6 import C6PixProvider

    if not isinstance(checkout, dict):
        checkout = {}
    ref = checkout.get("external_reference_id")
    if not isinstance(ref, str) or ref != charge.txid:
        # O id consultado é o que NÓS guardamos; referência diferente é
        # inconsistência séria, não pagamento desta compra.
        current_app.logger.error(
            "C6 checkout %s com external_reference_id divergente da charge %s; "
            "nada creditado, conciliar manualmente.", charge.provider_ref, charge.id,
        )
        return "referencia-divergente"

    status = str(checkout.get("status") or "").strip().upper()
    if status == "PAID":
        pago_cents = C6PixProvider.checkout_amount_cents(checkout)
        if pago_cents < charge.amount_cents:
            current_app.logger.error(
                "VALOR DIVERGENTE no C6 Checkout: charge=%s pago=%s cents, "
                "cobrança=%s cents. Nenhum ponto creditado; conciliação manual.",
                charge.id, pago_cents, charge.amount_cents,
            )
            return "valor-divergente"
        ja_estava_paga = charge.status == PixChargeStatus.PAID
        confirm_payment(charge.txid, provider_confirmed=True)
        return "replay" if ja_estava_paga else "credited"

    if status == "CANCELLED" and charge.status == PixChargeStatus.PAID:
        # Cancelar checkout pago = estorno no C6. Os pontos podem já ter sido
        # usados: igual ao Pix devolvido, conciliação MANUAL.
        current_app.logger.error(
            "C6 Checkout ESTORNADO após crédito: charge=%s (%s cents). "
            "Conciliação MANUAL obrigatória.", charge.id, charge.amount_cents,
        )
        return "estornado"

    if status in ("EXPIRED", "CANCELLED", "ERROR") and charge.status == PixChargeStatus.PENDING:
        # Terminal sem pagamento: tira a charge do forecast do teto mensal.
        # DECLINED fica de fora de propósito: o link aceita até 3 tentativas.
        charge.status = PixChargeStatus.EXPIRED
        db.session.commit()
        return "expirado"
    return "pending"


def refresh_card_checkout(charge: PixCharge) -> None:
    """Backstop do webhook do Checkout no polling do front (como o do PIX).

    Nunca propaga erro: falha de consulta não pode quebrar a tela.
    """
    provider = _c6_provider_ou_none()
    if provider is None or charge.flow != FLOW_C6_CARTAO or not charge.provider_ref:
        return
    if charge.status not in (PixChargeStatus.PENDING, PixChargeStatus.EXPIRED):
        return
    try:
        resultado = sync_checkout(charge, provider.get_checkout(charge.provider_ref))
        if resultado == "credited":
            current_app.logger.info(
                "compra %s (cartão C6) creditada via polling", charge.id)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.warning(
            "c6: reconsulta do checkout da charge %s falhou no polling: %s", charge.id, exc
        )


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
    from . import avisos_email
    avisos_email.compra_confirmada(
        charge, meio="Cartão" if charge.flow == "c6-cartao" else "Pix")
    return charge


def expire_if_needed(charge: PixCharge) -> PixCharge:
    if charge.status == PixChargeStatus.PENDING and charge.is_expired():
        charge.status = PixChargeStatus.EXPIRED
        db.session.commit()
    return charge
