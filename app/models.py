"""Modelos de domínio do Blaxx Pontos.

Convenções:
  * Pontos são INTEIROS (nunca float). Saldo nunca pode ficar negativo.
  * Valores em R$ ficam em centavos (Integer) para evitar erro de ponto flutuante.
  * Cada movimentação de saldo tem 1 Transaction correspondente (ledger imutável).
"""

from __future__ import annotations

import enum
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import Config as _Config  # display rate (CENTS_PER_POINT)
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return uuid.uuid4().hex


def _new_txid() -> str:
    """txid no padrão Bacen para PIX: [a-zA-Z0-9]{26,35}."""
    return secrets.token_hex(16)  # 32 chars hex


# --------------------------------------------------------------------------- #
# Enums                                                                       #
# --------------------------------------------------------------------------- #
class TxType(str, enum.Enum):
    PURCHASE = "purchase"        # crédito por compra de pontos via PIX
    TRANSFER_OUT = "transfer_out"  # débito ao enviar pontos para outro user
    TRANSFER_IN = "transfer_in"   # crédito ao receber pontos
    REDEEM = "redeem"            # débito por resgate via PIX
    REFUND = "refund"            # estorno (ex.: payout PIX falhou)
    BONUS = "bonus"              # boas-vindas, indicação, etc.
    EXPIRE = "expire"            # Sprint 2: expiração FIFO de pontos > 24 meses


class TxStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REVERSED = "reversed"


class PixChargeStatus(str, enum.Enum):
    PENDING = "pending"
    # Cliente clicou "já paguei", aguardando admin confirmar (PIX manual)
    PENDING_CONFIRMATION = "pending_confirmation"
    PAID = "paid"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    REJECTED = "rejected"  # admin rejeitou (não recebeu o PIX)


class PixPayoutStatus(str, enum.Enum):
    REQUESTED = "requested"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# User + Wallet                                                               #
# --------------------------------------------------------------------------- #
class User(db.Model):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    cpf: Mapped[str] = mapped_column(String(14), unique=True, nullable=False)
    # Nullable: usuários que entram só via Google podem não ter senha local.
    # Sempre que nullable, set_password() vira opcional e check_password()
    # devolve False (forçando o user a entrar via Google).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pix_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    # Google OAuth: "sub" claim do ID token. Único por conta Google.
    # Permite linkar conta existente (mesmo email) ao Google sem duplicar usuário.
    google_sub: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    # Papel: 'user' (default) | 'admin' (acesso ao módulo /admin)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    # VIP = sem limites diários (compra >R$ 100.000/dia, transferência sem teto).
    # Toggleável pelo admin via PATCH /admin/users/{id}/vip.
    is_vip: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Onda 2 — Auth refactor completo (Spec do user, seções 1-7)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    birth_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    # status: 'active' (default) | 'suspended' | 'closed'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Lock progressivo contra brute force
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # MFA TOTP (RFC 6238) + SMS (RFC 4226 OTP via PhoneOtp)
    mfa_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Onda 3 — método ativo do MFA: 'totp' (Google Authenticator) ou 'sms'.
    # null = MFA desativado (consistente com mfa_enabled=False).
    mfa_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Onda 3 — telefone verificado por OTP via SMS (pré-requisito do MFA SMS)
    phone_verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Onda 1 P0: email verification gate em operações financeiras
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Sprint 4 (S4-KYC): validação remota do CPF na Receita Federal (via BrasilAPI).
    # `kyc_validated_at` setado quando BrasilAPI retornou valid=True; provider
    # registra o serviço usado ('brasilapi' por padrão). Se downtime no provider,
    # ficam null e o user é tratado como kyc_pending=True via property.
    kyc_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    kyc_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Aceite LGPD versionado — agora também rastreado em user_consents
    terms_accepted_version: Mapped[str | None] = mapped_column(String(10), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lgpd_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Última troca de senha (pra forçar reauth quando admin alterar)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Sprint 2 (LGPD): soft-delete pra atender art. 18 sem quebrar
    # integridade do ledger imutavel.
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def kyc_pending(self) -> bool:
        """Sprint 4: True quando CPF ainda não foi validado na RF (BrasilAPI)."""
        return self.kyc_validated_at is None

    @property
    def has_password(self) -> bool:
        """False quando o usuário só entra via Google (sem senha local)."""
        return bool(self.password_hash)

    wallet: Mapped["Wallet"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, raw: str) -> None:
        """Hash com Argon2id (com fallback bcrypt se a lib não estiver disponível)."""
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            self.password_hash = ph.hash(raw)
        except ImportError:
            # Fallback bcrypt (Werkzeug) — usado se argon2-cffi não instalado
            self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        """Aceita hashes Argon2id ($argon2id$) e bcrypt legacy ($2b$).
        Auto-upgrade: se for bcrypt e login OK, re-hash com Argon2id."""
        if not self.password_hash:
            return False
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError, InvalidHash
            ph = PasswordHasher()
            if self.password_hash.startswith("$argon2"):
                try:
                    ph.verify(self.password_hash, raw)
                    # Re-hash automático se parâmetros do hasher mudaram
                    if ph.check_needs_rehash(self.password_hash):
                        self.password_hash = ph.hash(raw)
                    return True
                except (VerifyMismatchError, InvalidHash):
                    return False
        except ImportError:
            pass
        # Hash bcrypt legado (formato werkzeug): valida e migra pra Argon2id
        if check_password_hash(self.password_hash, raw):
            # Auto-upgrade transparente: próxima validação será Argon2id
            try:
                self.set_password(raw)
            except Exception:
                pass
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "cpf": self.cpf,
            "phone": self.phone,
            "pix_key": self.pix_key,
            "avatar_url": self.avatar_url,
            "auth_provider": self.auth_provider,
            "email_verified_at": self.email_verified_at.isoformat() if self.email_verified_at else None,
            "role": self.role,
            "is_vip": self.is_vip,
            "mfa_enabled": self.mfa_enabled,
            "status": self.status,
            # Onda 3 — pra UI mostrar "Definir senha" pra users Google-only.
            # Não expõe o hash em si; só se existe.
            "has_password": self.has_password,
            "google_linked": bool(self.google_sub),
        }

    def to_admin_dict(self) -> dict:
        """Versão detalhada usada no painel /admin (inclui timestamps + flags)."""
        wallet_pts = self.wallet.balance_pts if self.wallet else 0
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "cpf": self.cpf,
            "phone": self.phone,
            "pix_key": self.pix_key,
            "avatar_url": self.avatar_url,
            "auth_provider": self.auth_provider,
            "role": self.role,
            "is_vip": self.is_vip,
            "mfa_enabled": self.mfa_enabled,
            "status": self.status,
            "email_verified": self.is_email_verified,
            "email_verified_at": self.email_verified_at.isoformat() if self.email_verified_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "password_changed_at": self.password_changed_at.isoformat() if self.password_changed_at else None,
            "google_linked": bool(self.google_sub),
            "failed_login_attempts": self.failed_login_attempts,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "balance_pts": wallet_pts,
        }

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        # A coluna é DateTime "naive" (sem tz); _utcnow() é aware. Em SQLite e
        # mesmo no Postgres (coluna sem timezone) o valor volta naive, e
        # comparar naive > aware lança TypeError → login 500 p/ conta bloqueada.
        # Normaliza ambos para aware (UTC) antes de comparar.
        lu = self.locked_until
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        return lu > _utcnow()


class Wallet(db.Model):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance_pts >= 0", name="ck_wallet_balance_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    balance_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="wallet")
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan",
        order_by="Transaction.created_at.desc()",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "balance_pts": self.balance_pts,
            "pending_pts": self.pending_pts,
            "balance_brl_equiv": round(
                self.balance_pts * _Config.CENTS_PER_POINT / 100, 2
            ),
        }


# --------------------------------------------------------------------------- #
# Ledger                                                                      #
# --------------------------------------------------------------------------- #
class Transaction(db.Model):
    """Ledger: cada movimentação de saldo gera 1 linha imutável."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    # index=True: extrato filtra por wallet_id direto
    wallet_id: Mapped[str] = mapped_column(
        ForeignKey("wallets.id"), nullable=False, index=True
    )
    # index=True: filtros do extrato/admin frequentemente por tipo
    type: Mapped[TxType] = mapped_column(Enum(TxType), nullable=False, index=True)
    status: Mapped[TxStatus] = mapped_column(
        Enum(TxStatus), nullable=False, default=TxStatus.CONFIRMED
    )
    # positivo = crédito; negativo = débito
    amount_pts: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # ID externo (charge, payout, transfer) para rastreabilidade
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # chave de idempotência (por usuário) — evita débito/crédito duplicado
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # index=True: ORDER BY created_at DESC + range queries no extrato
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")

    __table_args__ = (
        UniqueConstraint(
            "wallet_id", "idempotency_key",
            name="uq_tx_idempotency",
        ),
        # Indice composto pra paginacao keyset do extrato (Sprint 1)
        db.Index("ix_tx_wallet_created", "wallet_id", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "amount_pts": self.amount_pts,
            "description": self.description,
            "reference": self.reference,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# PIX — cobrança (entrada de dinheiro → vira pontos)                          #
# --------------------------------------------------------------------------- #
class PixCharge(db.Model):
    __tablename__ = "pix_charges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    # index=True: "minhas charges" filtra por user_id direto
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    package_key: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    points_to_credit: Mapped[int] = mapped_column(Integer, nullable=False)
    txid: Mapped[str] = mapped_column(String(64), unique=True, default=_new_txid)
    br_code: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Base64 PNG do QR Code (data URI). Opcional — depende do provider.
    # Pode ser grande (~3-10KB) então usa Text em vez de String fixa.
    qr_code_image: Mapped[str | None] = mapped_column(String(50000), nullable=True)
    # index=True: admin filtra pending; user filtra paid/pending
    status: Mapped[PixChargeStatus] = mapped_column(
        Enum(PixChargeStatus), default=PixChargeStatus.PENDING, nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Onda 2 — fluxo PIX manual (QR estático):
    # cliente clica "já paguei" → claimed_paid_at preenchido + status=PENDING_CONFIRMATION
    # admin confirma → paid_at preenchido + status=PAID + pontos liberados
    claimed_paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 'mp' (Mercado Pago automático) | 'manual' (admin confirma)
    flow: Mapped[str] = mapped_column(String(16), nullable=False, default="mp")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        # Composto pra "minhas charges ordenadas por data" (Sprint 1)
        db.Index("ix_pix_charge_user_created", "user_id", "created_at"),
    )

    @classmethod
    def make_expiry(cls, ttl_seconds: int) -> datetime:
        return _utcnow() + timedelta(seconds=ttl_seconds)

    def is_expired(self) -> bool:
        # Trata datetimes naïve vindos do SQLite como UTC
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return _utcnow() > exp

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "package": self.package_key,
            "amount_brl": round(self.amount_cents / 100, 2),
            "points_to_credit": self.points_to_credit,
            "txid": self.txid,
            "br_code": self.br_code,
            "qr_code_image": self.qr_code_image,
            "status": self.status.value,
            "flow": self.flow,
            "expires_at": self.expires_at.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "claimed_paid_at": self.claimed_paid_at.isoformat() if self.claimed_paid_at else None,
        }


# --------------------------------------------------------------------------- #
# PIX — payout (resgate: pontos saem → vira R$ na conta do usuário)           #
# --------------------------------------------------------------------------- #
class PixPayout(db.Model):
    __tablename__ = "pix_payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    points_debited: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    pix_key: Mapped[str] = mapped_column(String(180), nullable=False)
    txid: Mapped[str] = mapped_column(String(64), unique=True, default=_new_txid)
    end_to_end_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[PixPayoutStatus] = mapped_column(
        Enum(PixPayoutStatus), default=PixPayoutStatus.REQUESTED, nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Sprint 1-2 (P0): chave de idempotencia por usuario. UNIQUE
    # (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL — retries
    # do cliente com a mesma key NAO geram segundo payout/debito.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        db.Index(
            "uq_pixpayout_user_idem",
            "user_id", "idempotency_key",
            unique=True,
            postgresql_where=db.text("idempotency_key IS NOT NULL"),
            sqlite_where=db.text("idempotency_key IS NOT NULL"),
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "points_debited": self.points_debited,
            "amount_brl": round(self.amount_cents / 100, 2),
            "pix_key": self.pix_key,
            "txid": self.txid,
            "end_to_end_id": self.end_to_end_id,
            "status": self.status.value,
            "failure_reason": self.failure_reason,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
        }


# --------------------------------------------------------------------------- #
# Transfer P2P                                                                #
# --------------------------------------------------------------------------- #
class Transfer(db.Model):
    __tablename__ = "transfers"

    # Estados — ToS Seção 9.2 (janela de 60s):
    #   pending:   acabou de ser criada, dentro da janela de cancelamento
    #   committed: já passou da janela ou foi promovida; recipient creditado
    #   cancelled: cancelada pelo sender antes da janela fechar
    STATUS_PENDING = "pending"
    STATUS_COMMITTED = "committed"
    STATUS_CANCELLED = "cancelled"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount_pts: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str | None] = mapped_column(String(140), nullable=True)
    receipt_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Janela de 60s — ToS Seção 9.2
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_COMMITTED)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @staticmethod
    def make_receipt() -> str:
        # ENV-2026-XXXX-XXXX (espelha o padrão das telas de envio-concluido)
        year = _utcnow().year
        suffix = secrets.token_hex(4).upper()
        mid = secrets.token_hex(2).upper()
        return f"ENV-{year}-{mid}-{suffix}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING

    @property
    def is_cancellable(self) -> bool:
        """True se ainda dentro da janela e ainda no estado pending."""
        if not self.is_pending:
            return False
        if self.committed_at is None:
            return True  # sem deadline = aceita cancel (não deveria acontecer)
        now = datetime.now(timezone.utc)
        target = self.committed_at
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return now < target

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "amount_pts": self.amount_pts,
            "message": self.message,
            "receipt_code": self.receipt_code,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "is_cancellable": self.is_cancellable,
        }


# =========================================================================== #
# Catálogo: Parceiros e Benefícios                                            #
# =========================================================================== #

class Partner(db.Model):
    __tablename__ = "partners"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)
    accrual_rule: Mapped[str | None] = mapped_column(String(120), nullable=True)  # ex: "5 pts a cada R$ 1"
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "description": self.description, "logo_emoji": self.logo_emoji,
            "accrual_rule": self.accrual_rule, "city": self.city,
            "is_active": self.is_active,
        }


class Benefit(db.Model):
    """Catálogo de benefícios resgatáveis (vouchers, descontos, experiências)."""
    __tablename__ = "benefits"
    __table_args__ = (
        CheckConstraint("cost_pts >= 0", name="ck_benefit_cost_nonneg"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    partner_id: Mapped[str | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="voucher")
    cost_pts: Mapped[int] = mapped_column(Integer, nullable=False)
    image_emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)  # -1 = ilimitado
    expires_in_days: Mapped[int] = mapped_column(Integer, default=180, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    tag: Mapped[str | None] = mapped_column(String(40), nullable=True)  # "Mais resgatado", "Premium" etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    partner: Mapped[Partner | None] = relationship()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "partner_id": self.partner_id,
            "partner_name": self.partner.name if self.partner else None,
            "name": self.name, "description": self.description,
            "category": self.category, "cost_pts": self.cost_pts,
            "image_emoji": self.image_emoji, "stock": self.stock,
            "expires_in_days": self.expires_in_days,
            "is_active": self.is_active, "tag": self.tag,
        }


class Voucher(db.Model):
    """Voucher emitido quando o usuário resgata um Benefit."""
    __tablename__ = "vouchers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    # index=True: "meus vouchers" filtra por user_id (Sprint 1)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    benefit_id: Mapped[str] = mapped_column(ForeignKey("benefits.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    points_spent: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    benefit: Mapped[Benefit] = relationship()

    @staticmethod
    def make_code() -> str:
        return f"BLAXX-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"

    @property
    def status(self) -> str:
        if self.used_at is not None: return "used"
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return "expired" if _utcnow() > exp else "active"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "user_id": self.user_id, "benefit_id": self.benefit_id,
            "benefit_name": self.benefit.name if self.benefit else None,
            "code": self.code, "points_spent": self.points_spent,
            "expires_at": self.expires_at.isoformat(),
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


# =========================================================================== #
# Campanhas                                                                   #
# =========================================================================== #

class Campaign(db.Model):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mechanic: Mapped[str] = mapped_column(String(200), nullable=False, default="")  # texto explicativo
    target_brl: Mapped[int] = mapped_column(Integer, default=50000, nullable=False)  # meta em centavos
    reward_pts: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "mechanic": self.mechanic,
            "target_brl": round(self.target_brl / 100, 2),
            "reward_pts": self.reward_pts,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "is_active": self.is_active,
        }


class UserCampaign(db.Model):
    """Adesão de um usuário a uma campanha."""
    __tablename__ = "user_campaigns"
    __table_args__ = (
        UniqueConstraint("user_id", "campaign_id", name="uq_user_campaign"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    progress_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    campaign: Mapped[Campaign] = relationship()

    def to_dict(self) -> dict:
        target = self.campaign.target_brl if self.campaign else 50000
        pct = min(100, int(self.progress_cents * 100 / target)) if target > 0 else 0
        return {
            "id": self.id, "user_id": self.user_id, "campaign_id": self.campaign_id,
            "campaign": self.campaign.to_dict() if self.campaign else None,
            "progress_brl": round(self.progress_cents / 100, 2),
            "progress_pct": pct,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "joined_at": self.joined_at.isoformat(),
        }


# =========================================================================== #
# Notificações                                                                #
# =========================================================================== #

# =========================================================================== #
# Onda 1 P0 — Segurança e autenticação                                        #
# =========================================================================== #

class RevokedToken(db.Model):
    """JWTs que foram explicitamente revogados (logout, troca de senha)."""
    __tablename__ = "revoked_tokens"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # TTL pra purge: pode apagar a linha depois de expires_at
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PasswordResetToken(db.Model):
    """Token único para reset de senha. Token é hash; expira em 30 min."""
    __tablename__ = "password_reset_tokens"
    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib
        return hashlib.sha256(raw.encode()).hexdigest()


class EmailVerification(db.Model):
    """Código de 6 dígitos para verificação de e-mail. 3 tentativas, TTL 10 min."""
    __tablename__ = "email_verifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    @staticmethod
    def hash_code(code: str) -> str:
        import hashlib
        return hashlib.sha256(code.encode()).hexdigest()


class Notification(db.Model):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    # index=True: listagem de notificacoes do user (filtro principal)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # purchase, expiration, campaign, transfer, system
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str | None] = mapped_column(String(500), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(8), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        # Composto pra unread-count e listagem ordenada por data (Sprint 1)
        db.Index("ix_notif_user_read_created", "user_id", "read_at", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "user_id": self.user_id, "type": self.type,
            "title": self.title, "body": self.body, "icon": self.icon,
            "reference": self.reference,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "is_read": self.read_at is not None,
            "created_at": self.created_at.isoformat(),
        }


# =========================================================================
# Onda 2 — Auth refactor completo · novas tabelas
# =========================================================================

class LoginAttempt(db.Model):
    """Auditoria de tentativas de login (sucesso e falha).

    Usado para: detectar brute force, monitorar credential stuffing,
    relatórios de segurança. Anti-enumeração: registramos pelo email TENTADO
    mesmo se o usuário não existir (user_id pode ser null).
    """
    __tablename__ = "login_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # index=True: filtros antifraude por email tentado e por IP
    email_attempted: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    success: Mapped[bool] = mapped_column(nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class AuditLog(db.Model):
    """Log de auditoria de eventos de segurança/conta.

    Eventos: register, login_success, login_fail, logout, password_change,
    password_reset_request, email_verified, token_revoked, account_locked,
    account_unlocked, vip_changed, role_changed, mfa_enabled, mfa_disabled,
    consent_accepted, profile_updated.
    """
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    # index=True: timeline por user, busca por evento e correlation
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    extra_data: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # JSON serializado
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        # Auditoria por user ordenada por data (Sprint 1)
        db.Index("ix_audit_user_created", "user_id", "created_at"),
    )


class TrustedDevice(db.Model):
    """Dispositivos confiáveis (skip MFA por 30 dias)."""
    __tablename__ = "trusted_devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_trusted_device"),
    )


class RefreshTokenDB(db.Model):
    """Refresh tokens persistidos com rotação.

    Cada login emite um refresh token novo. Refresh token usado é marcado como
    revogado e um NOVO é emitido. parent_id rastreia a cadeia (detecta reuso).
    """
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 do refresh token (nunca armazenamos o token cru)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # token pai na cadeia de rotação
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class UserConsent(db.Model):
    """Histórico versionado de consentimentos (LGPD/Termos/Privacidade).

    Cumpre o **Art. 8º §5º LGPD** (consentimento granular, revogável, com
    evidência). Cada aceite gera uma linha imutável — quando a versão do
    termo muda, o usuário é solicitado a aceitar de novo e nova linha é
    criada (a anterior fica como histórico).

    Campos de evidência:
      - `accepted_at` + `ip` + `user_agent` → onde/quando/como o aceite foi feito
      - `text_hash` → SHA-256 do texto do termo que o usuário viu nessa data
        (prova que o texto não foi alterado retroativamente)
    """
    __tablename__ = "user_consents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # type: 'terms' | 'privacy' | 'lgpd' | 'marketing' | 'cookies_analytics' | 'cookies_marketing'
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # SHA-256 hex (64 chars) do texto do termo aceito — prova de imutabilidade
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 'accepted' | 'revoked' — revogação cria nova linha com este status
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    # Para consentimentos revogados, aponta para o consent original revogado
    revokes_consent_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_consents.id", ondelete="SET NULL"), nullable=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "status": self.status,
            "ip": self.ip,
            "user_agent": (self.user_agent or "")[:200],
            "text_hash": self.text_hash,
            "revokes_consent_id": self.revokes_consent_id,
        }


class SocialAccount(db.Model):
    """Vinculação com providers externos (Google hoje; futuro: Apple, etc).

    Permite múltiplos providers por user e rastreia avatar + provider_user_id.
    """
    __tablename__ = "social_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # 'google'|'apple'
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(180), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_social_provider_user"),
    )


class MfaSecret(db.Model):
    """Segredo TOTP do MFA. Sprint 2 (P5): cifrado em repouso com Fernet.

    A coluna `secret` armazena CIPHERTEXT Fernet (base64-url, ~200 chars).
    Use get_secret()/set_secret() para encrypt/decrypt transparente.
    Acesso direto em `secret` ainda funciona pra compat com codigo antigo,
    mas devolve ciphertext — sempre use get_secret() pra TOTP verification.
    """
    __tablename__ = "mfa_secrets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Ciphertext Fernet — base64-url, ~180-250 chars dependendo do plaintext.
    # Sprint 2: aumentado de 128 -> 512 pra acomodar Fernet token.
    secret: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def set_secret(self, plaintext: str) -> None:
        """Cifra e armazena. Use SEMPRE este em vez de atribuir secret direto."""
        from .security import encrypt_secret
        self.secret = encrypt_secret(plaintext) or ""

    def get_secret(self) -> str:
        """Devolve o secret TOTP em texto plano (decifrado). Compat com legacy."""
        from .security import decrypt_secret
        return decrypt_secret(self.secret) or ""


class PhoneOtp(db.Model):
    """OTP de telefone — verificar cadastro de telefone OU completar login 2FA SMS.

    purpose = 'verify_phone' | 'login_2fa'
    Sempre armazena HASH SHA-256 do código (nunca o código em claro).
    Max 5 tentativas. TTL configurável por purpose.
    """
    __tablename__ = "phone_otps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    @staticmethod
    def hash_code(code: str) -> str:
        import hashlib
        return hashlib.sha256(code.encode()).hexdigest()

    def is_valid(self) -> bool:
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return self.used_at is None and exp > _utcnow() and self.attempts < 5


class MfaChallenge(db.Model):
    """Challenge intermediário do login com MFA SMS.

    Quando /auth/login bate certo e o user tem mfa_method='sms', criamos um
    MfaChallenge curto-prazo (5 min) com token público e mandamos SMS.
    /auth/login/2fa consome esse challenge.
    """
    __tablename__ = "mfa_challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    challenge_token_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)  # 'sms' (futuro: 'totp')
    phone_otp_id: Mapped[str | None] = mapped_column(
        ForeignKey("phone_otps.id", ondelete="SET NULL"), nullable=True
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib
        return hashlib.sha256(raw.encode()).hexdigest()

    def is_valid(self) -> bool:
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return self.used_at is None and exp > _utcnow()


class UserProfile(db.Model):
    """Dados de perfil estendidos (endereço, bio, indicação, etc).

    Separado de User pra manter o login leve. Lazy load via relationship.
    """
    __tablename__ = "user_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    referred_by_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address_line: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    zipcode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# =========================================================================== #
# Sprint 4 — KYC / AML / Push / MercadoPago replay store                      #
# =========================================================================== #


class CpfValidation(db.Model):
    """Cache de validação remota do CPF (Sprint 4 / S4-KYC).

    Quando consultamos BrasilAPI (ou outro provider), gravamos o resultado
    aqui pra:
      1. Evitar consultas duplicadas (cache 30d)
      2. Auditar quem foi validado, quando, por qual provider
      3. Permitir re-validação manual via admin
    """
    __tablename__ = "cpf_validations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    cpf: Mapped[str] = mapped_column(String(14), nullable=False, index=True)
    valid: Mapped[bool | None] = mapped_column(nullable=True)  # None = provider offline
    validated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="brasilapi")
    # SHA-256 do payload bruto pra rastreabilidade sem armazenar nome completo
    raw_response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AmlAlert(db.Model):
    """Alerta de AML/PLD (Sprint 4 / S4-AML).

    Disparado pelos checks em app/services/aml.py durante transfer/redeem/purchase.
    `severity`: 'low' (info), 'medium' (review), 'high' (escalonar).
    `kind`: 'threshold', 'velocity', 'sanctions', 'pattern'.
    """
    __tablename__ = "aml_alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", index=True)
    # JSON serializado com detalhes (amount, recipient, threshold, etc.)
    payload: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def to_dict(self) -> dict:
        import json as _json
        try:
            payload = _json.loads(self.payload) if self.payload else None
        except Exception:
            payload = self.payload
        return {
            "id": self.id,
            "user_id": self.user_id,
            "kind": self.kind,
            "severity": self.severity,
            "payload": payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
        }


class MpWebhookEvent(db.Model):
    """Replay-store de webhooks do Mercado Pago (Sprint 4 / S4-MP).

    UNIQUE(event_id) garante que o mesmo evento (mesmo id na URL `?id=`) só
    seja processado UMA vez — segunda chamada idêntica retorna 200 sem efeito.
    """
    __tablename__ = "mp_webhook_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    action: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PushDevice(db.Model):
    """Dispositivo registrado pra push notification (Sprint 7).

    UNIQUE(token) — mesmo token nunca registrado pra 2 users (usuário trocou
    de conta no mesmo device; o último registro vence).
    """
    __tablename__ = "push_devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    # 'ios' | 'android' | 'web'
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "app_version": self.app_version,
            "registered_at": self.registered_at.isoformat() if self.registered_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked": self.revoked_at is not None,
        }
