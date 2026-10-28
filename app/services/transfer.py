"""Envio de pontos entre clientes inscritos (P2P interno).

Regras (espelham as telas enviar-pontos.html / confirmar-envio.html):
  * Mínimo 100 pts, máximo 50.000 pts/dia (somando todos os envios).
  * Sem taxa.
  * Identifica destinatário por e-mail OU CPF.
  * Exige senha do remetente para confirmar.
  * Não pode enviar para si mesmo.
  * Gera receipt_code (ENV-2026-XXXX-XXXX) — comprovante.
  * Atômico: se algo falhar, nem débito nem crédito acontecem.

Anti-duplicidade (Sprint QA — bug A1):
  * Idempotência exata: se o cliente mandar uma chave (`idempotency_key`,
    via header Idempotency-Key ou body request_id), um reenvio com a mesma
    chave NÃO gera segundo débito/crédito — devolve a transferência original.
    Implementado sobre Transaction.idempotency_key (já tem UNIQUE
    (wallet_id, idempotency_key)), sem mexer no schema.
  * Rede de segurança sem chave: detecta duplicata acidental (mesmo
    remetente→destinatário→valor) dentro de uma janela curta e devolve a
    transferência original em vez de debitar de novo (combate double-submit /
    retry de rede mesmo em clientes que ainda não mandam chave).

Notificação (bug A2) e auditoria (bug A3) acontecem dentro da mesma
transação do débito/crédito — tudo confirma junto ou nada confirma.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..config import Config
from ..extensions import db
from ..models import Notification, Transaction, Transfer, TxType, User, Wallet
from . import aml as aml_svc
from . import audit as audit_svc
from . import metrics as metrics_svc
from . import wallet as wallet_svc


class TransferError(Exception):
    pass


class DuplicateSuspected(TransferError):
    """Envio idêntico dentro da janela anti-double-submit, SEM chave do cliente.

    B-5: antes isto devolvia a transferência anterior como se fosse sucesso. O
    duplo-clique ficava resolvido, mas quem quisesse mesmo enviar duas vezes o
    mesmo valor à mesma pessoa — dividir uma conta em duas parcelas iguais, por
    exemplo — via "enviado" duas vezes e só uma acontecia. Silêncio é a pior
    resposta: o remetente acha que pagou.

    Agora vira 409 com `code: DUPLICATE_SUSPECTED`, e o cliente reenvia com
    `Idempotency-Key` para dizer "é outro envio mesmo".
    """

    def __init__(self, transfer_id: str):
        super().__init__(
            "Você enviou o mesmo valor para esta pessoa há instantes. "
            "Se for um novo envio, confirme para prosseguir."
        )
        self.transfer_id = transfer_id


_CPF_RE = re.compile(r"\D+")
_CARD_ID_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)

# Janela curta p/ deduplicar reenvios acidentais quando o cliente NÃO mandou
# idempotency_key (double-tap no botão, retry automático de rede).
_DUP_WINDOW_SECONDS = 15

# Janela de cancelamento P2P — ToS Seção 9.2 promete 60s.
# Durante essa janela: pontos ficam em pending_pts do sender, recipient não vê.
# Após: promote → credit recipient + notify + push.
P2P_CANCEL_WINDOW_SECONDS = 60


def _normalize_cpf(value: str) -> str:
    return _CPF_RE.sub("", value)


# ────────────────────────────────── B-4 · anti-sondagem de diretório ─────────
# "destinatário não encontrado" é uma resposta honesta e necessária — quem
# digita o e-mail errado precisa saber. Mas ela também confirma, uma consulta
# por vez, quem NÃO é cliente da BlaXx e, por complemento, quem é. Com uma conta
# válida dá para transformar uma lista de CPFs ou e-mails em uma lista de
# clientes, sem tocar em saldo nenhum: a resolução do destinatário acontece
# antes de qualquer checagem de valor.
#
# A defesa não é esconder a mensagem (isso quebraria o uso legítimo), é limitar
# quantos ALVOS DISTINTOS uma conta pode testar por hora. Errar o mesmo e-mail
# cinco vezes é um alvo; testar cinco e-mails diferentes é uma varredura.
_JANELA_SONDAGEM_MIN = 60
_MAX_ALVOS_DISTINTOS = 5


class RecipientProbeBlocked(TransferError):
    """Alvos inexistentes distintos demais na janela — cheira a varredura."""


def _digest_alvo(identifier: str) -> str:
    """Hash curto do identificador procurado.

    Guardamos o hash, NUNCA o valor. Registrar o e-mail/CPF tentado faria a
    trilha de auditoria virar exatamente o diretório que este controle existe
    para proteger — e um dump do audit_logs entregaria de graça o que o atacante
    tentou montar. O hash ainda cumpre o papel: separa "errou o mesmo alvo
    várias vezes" de "testou vários alvos".
    """
    base = f"blaxx-transfer-alvo:{(identifier or '').strip().lower()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _checar_sondagem(sender: User, identifier: str) -> None:
    """Registra o alvo inexistente e barra a conta que está varrendo."""
    from ..models import AuditLog

    audit_svc.log_event(
        "transfer_recipient_miss",
        user_id=sender.id,
        status="fail",
        extra={"alvo": _digest_alvo(identifier)},
    )

    corte = datetime.now(timezone.utc) - timedelta(minutes=_JANELA_SONDAGEM_MIN)
    distintos = (
        db.session.query(AuditLog.extra_data)
        .filter(
            AuditLog.user_id == sender.id,
            AuditLog.event == "transfer_recipient_miss",
            AuditLog.created_at >= corte,
        )
        .distinct()
        .count()
    )
    if distintos > _MAX_ALVOS_DISTINTOS:
        audit_svc.log_event(
            "transfer_probe_blocked",
            user_id=sender.id,
            status="fail",
            extra={"alvos_distintos": distintos},
        )
        raise RecipientProbeBlocked(
            "Muitas tentativas para destinatários que não existem. "
            "Aguarde alguns minutos antes de tentar de novo."
        )


def find_recipient(identifier: str) -> User | None:
    """Aceita e-mail, CPF (com ou sem máscara) ou Cartão BlaXx (8 hex)."""
    identifier = (identifier or "").strip().lower()
    if "@" in identifier:
        return db.session.query(User).filter_by(email=identifier).one_or_none()
    cpf = _normalize_cpf(identifier)
    if cpf:
        user = db.session.query(User).filter_by(cpf=cpf).one_or_none()
        if user:
            return user
    card_id = re.sub(r"[\s\-]", "", identifier)
    if _CARD_ID_RE.match(card_id):
        # B-1: `.one_or_none()` levantava MultipleResultsFound (500 não tratado)
        # quando dois usuários compartilham o prefixo de 8 hex. Não é hipótese
        # remota: é aniversário sobre 32 bits — com dezenas de milhares de
        # contas a colisão fica provável. Preferimos recusar a transferência a
        # creditar a pessoa errada.
        achados = db.session.query(User).filter(
            User.id.like(f"{card_id}%")
        ).limit(2).all()
        if len(achados) == 1:
            return achados[0]
        return None
    return None


def _out_key(sender_id: str, client_key: str) -> str:
    """Chave de idempotência do débito do remetente derivada da chave do cliente."""
    return f"transfer-out:{sender_id}:{client_key}"


def _in_key(sender_id: str, client_key: str) -> str:
    return f"transfer-in:{sender_id}:{client_key}"


def _transfer_for_debit_key(sender_id: str, out_key: str) -> Transfer | None:
    """Acha a Transfer original a partir do débito idempotente já registrado."""
    tx = (
        db.session.query(Transaction)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .filter(
            Wallet.user_id == sender_id,
            Transaction.idempotency_key == out_key,
        )
        .one_or_none()
    )
    if tx is None or not tx.reference:
        return None
    return db.session.get(Transfer, tx.reference)


def _recent_duplicate(
    sender_id: str, recipient_id: str, amount_pts: int
) -> Transfer | None:
    """Última transferência idêntica dentro da janela anti-double-submit."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DUP_WINDOW_SECONDS)
    return (
        db.session.query(Transfer)
        .filter(
            Transfer.sender_id == sender_id,
            Transfer.recipient_id == recipient_id,
            Transfer.amount_pts == amount_pts,
            Transfer.created_at >= cutoff,
        )
        .order_by(Transfer.created_at.desc())
        .first()
    )


def send(
    sender: User,
    *,
    recipient_identifier: str,
    amount_pts: int,
    password: str,
    message: str | None = None,
    idempotency_key: str | None = None,
    device_id: str | None = None,
    platform: str | None = None,
    mfa_code: str | None = None,
) -> Transfer:
    if not sender.check_password(password):
        raise TransferError("senha incorreta")

    if amount_pts < Config.TRANSFER_MIN_POINTS:
        raise TransferError(
            f"valor mínimo é {Config.TRANSFER_MIN_POINTS} pts"
        )

    # B13 · step-up 2FA p/ valores altos (só p/ quem tem 2FA ativo).
    from ..security import enforce_step_up_mfa
    enforce_step_up_mfa(sender, amount_pts, mfa_code)

    recipient = find_recipient(recipient_identifier)
    if recipient is None:
        # B-4: mantém a mensagem útil, mas conta alvos distintos por hora.
        # Levanta RecipientProbeBlocked (→ 429) quando vira varredura.
        _checar_sondagem(sender, recipient_identifier)
        raise TransferError("destinatário não encontrado")

    if recipient.id == sender.id:
        raise TransferError("não é possível enviar pontos para si mesmo")

    # Sprint 4 (S4-AML) — sanctions check bloqueia (raise SanctionsBlock → 403 no api).
    # Threshold/velocity registram alerta mas NÃO bloqueiam.
    try:
        aml_svc.check_sanctions_or_raise(sender)
        aml_svc.check_sanctions_or_raise(recipient)
    except aml_svc.SanctionsBlock as exc:
        metrics_svc.inc_transfer("blocked_sanctions")
        raise TransferError(str(exc)) from exc
    aml_svc.check_transaction_threshold(
        sender, amount_pts, kind="transfer",
        monthly_limit_pts=Config.TRANSFER_MAX_POINTS_PER_MONTH if not sender.is_vip else None,
    )
    aml_svc.check_velocity(sender, tx_type=TxType.TRANSFER_OUT)

    client_key = (idempotency_key or "").strip()[:64] or None

    # (A1) Idempotência exata: reenvio com a mesma chave devolve o original.
    if client_key:
        existing = _transfer_for_debit_key(sender.id, _out_key(sender.id, client_key))
        if existing is not None:
            return existing
    else:
        # (A1) Rede de segurança contra double-submit sem chave do cliente.
        dup = _recent_duplicate(sender.id, recipient.id, amount_pts)
        if dup is not None:
            raise DuplicateSuspected(dup.id)

    # M-1 (revisão de 20/07): a soma do período rodava FORA do lock da carteira,
    # e o lock só era adquirido lá dentro do `debit`. Duas requisições simultâneas
    # liam o mesmo total, ambas passavam na comparação e ambas debitavam — furando
    # o teto TRANSFER_MAX_POINTS_PER_DAY/MONTH. O saldo nunca esteve em risco (o
    # SELECT ... FOR UPDATE serializa o débito e há CHECK(balance_pts >= 0)); o que
    # vazava era o limite regulatório.
    #
    # Adquirir o lock ANTES da soma serializa por usuário: a segunda requisição
    # espera, e quando entra já enxerga o débito committado da primeira.
    wallet_svc.get_wallet_for_update(sender.id)

    # VIP: usuarios marcados pelo admin podem transferir sem limite diario/mensal.
    if not sender.is_vip:
        sent_today = wallet_svc.debited_today(sender.id, TxType.TRANSFER_OUT)
        if sent_today + amount_pts > Config.TRANSFER_MAX_POINTS_PER_DAY:
            remaining = Config.TRANSFER_MAX_POINTS_PER_DAY - sent_today
            raise TransferError(
                f"limite diário excedido — restam {max(remaining,0)} pts hoje"
            )
        # Sprint 1-2 (P0): limite MENSAL acumulado.
        sent_month = wallet_svc.debited_this_month(sender.id, TxType.TRANSFER_OUT)
        if sent_month + amount_pts > Config.TRANSFER_MAX_POINTS_PER_MONTH:
            remaining = Config.TRANSFER_MAX_POINTS_PER_MONTH - sent_month
            raise TransferError(
                f"limite mensal de envio excedido — restam {max(remaining,0)} pts este mes"
            )

    if message and len(message) > 140:
        raise TransferError("mensagem com mais de 140 caracteres")

    now = datetime.now(timezone.utc)
    transfer = Transfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        amount_pts=amount_pts,
        message=message,
        receipt_code=Transfer.make_receipt(),
        # ToS Seção 9.2 — janela de 60s antes de creditar o destinatário
        status=Transfer.STATUS_PENDING,
        committed_at=now + timedelta(seconds=P2P_CANCEL_WINDOW_SECONDS),
    )
    db.session.add(transfer)
    db.session.flush()

    # Chave de idempotência do débito (recipient só credita após promote).
    out_key = _out_key(sender.id, client_key) if client_key else f"transfer-out:{transfer.id}"

    # Durante a janela: debita do sender mas SEM creditar o recipient.
    # O débito SAI do balance_pts e ENTRA no pending_pts (mesmo usuário, segura).
    # Se cancelar dentro da janela: reversão. Se passar: promote credita o recipient.
    try:
        wallet_svc.debit(
            user_id=sender.id,
            amount_pts=amount_pts,
            tx_type=TxType.TRANSFER_OUT,
            description=f"Envio (pending) para {recipient.name}",
            reference=transfer.id,
            idempotency_key=out_key,
        )
        # Move o valor debitado pra pending_pts do sender (não some, fica retido)
        sender_wallet = wallet_svc.get_wallet_for_update(sender.id)
        sender_wallet.pending_pts = (sender_wallet.pending_pts or 0) + amount_pts

        # (A3) Auditoria do envio em estado pending.
        audit_svc.log_event(
            "transfer_pending",
            user_id=sender.id,
            status="ok",
            device_id=device_id,
            extra={
                "transfer_id": transfer.id,
                "receipt_code": transfer.receipt_code,
                "recipient_id": recipient.id,
                "amount_pts": amount_pts,
                "platform": platform,
                "idempotent": bool(client_key),
                "cancel_window_seconds": P2P_CANCEL_WINDOW_SECONDS,
            },
            commit=False,
        )

        # (B14) Detecção de transação suspeita — alerta admin (não bloqueia).
        from . import fraud as fraud_svc
        fraud_svc.evaluate_transfer(sender.id, recipient.id, amount_pts)
    except wallet_svc.InsufficientBalance as exc:
        db.session.rollback()
        raise TransferError(str(exc)) from exc
    except IntegrityError as exc:
        # Corrida: dois reenvios simultâneos com a mesma idempotency_key —
        # o UNIQUE (wallet_id, idempotency_key) barra o segundo débito.
        db.session.rollback()
        if client_key:
            existing = _transfer_for_debit_key(sender.id, _out_key(sender.id, client_key))
            if existing is not None:
                return existing
        raise TransferError("transferência duplicada detectada") from exc

    db.session.commit()
    metrics_svc.inc_transfer("pending")
    # NOTA: push/notification do recipient acontece em promote_pending() — não aqui.
    return transfer


# =========================================================================== #
# Cancelamento e promoção — ToS Seção 9.2                                     #
# =========================================================================== #

def cancel(transfer_id: str, *, sender: User) -> Transfer:
    """Cancela uma transfer pending dentro da janela de 60s.

    Reverte: pending_pts → balance_pts do sender. Recipient nunca viu os pontos.
    Idempotente: chamar duas vezes na mesma transfer cancelada é ok.
    """
    transfer = db.session.get(Transfer, transfer_id)
    if transfer is None:
        raise TransferError("transferência não encontrada")
    if transfer.sender_id != sender.id:
        raise TransferError("apenas o remetente pode cancelar")
    if transfer.status == Transfer.STATUS_CANCELLED:
        return transfer  # idempotente
    if transfer.status == Transfer.STATUS_COMMITTED:
        raise TransferError(
            "transferência já efetivada (janela de 60s expirada) — "
            "se foi erro material, peça a devolução ao destinatário"
        )
    if not transfer.is_cancellable:
        # status='pending' mas committed_at já passou — promove primeiro
        promote_pending([transfer])
        raise TransferError(
            "transferência já efetivada (janela de 60s expirada) — "
            "se foi erro material, peça a devolução ao destinatário"
        )

    # Reverte: move pending_pts → balance_pts
    sender_wallet = wallet_svc.get_wallet_for_update(sender.id)
    sender_wallet.pending_pts = max(0, (sender_wallet.pending_pts or 0) - transfer.amount_pts)
    # Re-credita o balance via wallet_svc (cria Transaction de reversão para audit trail)
    wallet_svc.credit(
        user_id=sender.id,
        amount_pts=transfer.amount_pts,
        tx_type=TxType.TRANSFER_IN,  # reuso o tipo de crédito (técnica de estorno)
        description=f"Reversão de envio cancelado #{transfer.receipt_code}",
        reference=transfer.id,
        idempotency_key=f"transfer-cancel:{transfer.id}",
    )

    transfer.status = Transfer.STATUS_CANCELLED
    transfer.cancelled_at = datetime.now(timezone.utc)

    audit_svc.log_event(
        "transfer_cancelled",
        user_id=sender.id,
        status="ok",
        extra={
            "transfer_id": transfer.id,
            "receipt_code": transfer.receipt_code,
            "amount_pts": transfer.amount_pts,
        },
        commit=False,
    )

    db.session.commit()
    metrics_svc.inc_transfer("cancelled")
    return transfer


def promote_pending(transfers: list[Transfer] | None = None) -> int:
    """Promove transfers pending cujo committed_at já passou.

    Chamado lazy nos endpoints de leitura (GET /wallet/, /transactions, etc).
    Pode ser chamado com uma lista específica (acelera caso conhecido) ou
    sem argumento (varre todas as pending vencidas — usado por cron opcional).

    Retorna número de transfers promovidas.
    """
    now = datetime.now(timezone.utc)

    if transfers is None:
        transfers = (
            db.session.query(Transfer)
            .filter(
                Transfer.status == Transfer.STATUS_PENDING,
                Transfer.committed_at <= now,
            )
            .all()
        )
    else:
        # Filtra só as que realmente venceram
        transfers = [t for t in transfers if t.is_pending and not t.is_cancellable]

    promoted = 0
    promovidas: list[Transfer] = []  # só as que ESTA chamada promoveu (e-mail sem duplicar)
    for t in transfers:
        try:
            # Trava a transfer e revalida o estado SOB lock antes de mexer no
            # saldo: o crédito do recipient é idempotente, mas o -= de
            # pending_pts não. Sem esta trava, dois promotes concorrentes da
            # mesma transfer decrementariam pending_pts duas vezes (drift).
            t = (
                db.session.query(Transfer)
                .filter_by(id=t.id)
                .with_for_update()
                .one_or_none()
            )
            if t is None or t.status != Transfer.STATUS_PENDING:
                continue

            # Move pending_pts → fora (já saiu do sender, vai pro recipient)
            sender_wallet = wallet_svc.get_wallet_for_update(t.sender_id)
            sender_wallet.pending_pts = max(
                0, (sender_wallet.pending_pts or 0) - t.amount_pts
            )

            # Credita o recipient (agora visível)
            wallet_svc.credit(
                user_id=t.recipient_id,
                amount_pts=t.amount_pts,
                tx_type=TxType.TRANSFER_IN,
                description=f"Recebido (cód. {t.receipt_code})",
                reference=t.id,
                idempotency_key=_in_key(t.sender_id, f"promote:{t.id}"),
            )

            # Notifica recipient — agora pode
            recipient = db.session.get(User, t.recipient_id)
            sender = db.session.get(User, t.sender_id)
            if recipient and sender:
                db.session.add(Notification(
                    user_id=t.recipient_id,
                    type="transfer",
                    title="Você recebeu pontos",
                    body=(
                        f"{sender.name} te enviou {t.amount_pts} pts."
                        + (f' "{t.message}"' if t.message else "")
                    ),
                    icon="↪",
                    reference=t.id,
                ))

            t.status = Transfer.STATUS_COMMITTED
            audit_svc.log_event(
                "transfer_committed",
                user_id=t.sender_id,
                status="ok",
                extra={
                    "transfer_id": t.id,
                    "receipt_code": t.receipt_code,
                    "recipient_id": t.recipient_id,
                    "amount_pts": t.amount_pts,
                },
                commit=False,
            )
            promoted += 1
            promovidas.append(t)

            # Push best-effort
            try:
                from . import push as push_svc
                push_svc.send_to_user(
                    t.recipient_id,
                    "Você recebeu pontos",
                    f"{sender.name if sender else 'Alguém'} te enviou {t.amount_pts} pts.",
                    data={"transfer_id": t.id, "receipt": t.receipt_code},
                )
            except Exception:
                pass

        except Exception:
            # Não deixa uma transfer ruim quebrar as outras — log e segue.
            from flask import current_app
            current_app.logger.exception(
                "promote_pending: falha ao promover transfer %s", t.id
            )
            continue

    if promoted:
        db.session.commit()
        from . import avisos_email
        for t in promovidas:
            avisos_email.transferencia_concluida(t)
        for _ in range(promoted):
            metrics_svc.inc_transfer("committed")

    return promoted


def promote_pending_for_user(user_id: str) -> int:
    """Conveniência: promove transfers pending do user (sender OU recipient)
    cujo committed_at já passou. Chamado por /wallet, /transactions.
    """
    now = datetime.now(timezone.utc)
    pending = (
        db.session.query(Transfer)
        .filter(
            Transfer.status == Transfer.STATUS_PENDING,
            Transfer.committed_at <= now,
            (Transfer.sender_id == user_id) | (Transfer.recipient_id == user_id),
        )
        .all()
    )
    return promote_pending(pending) if pending else 0
