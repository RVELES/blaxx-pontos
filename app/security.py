"""Utilitários de segurança usados pelos endpoints de autenticação.

Onda 1: política de senha forte, geração e validação de tokens.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from typing import NamedTuple


# Top 50 senhas mais comuns (subset prático) — bloqueia ataques de dicionário básicos
COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "qwerty123", "11111111",
    "abc12345", "senha123", "blaxx123", "admin123", "iloveyou", "welcome1",
    "monkey123", "dragon123", "passw0rd", "letmein1", "12abc345", "qwertyui",
    "p@ssw0rd", "p@ssword", "987654321", "1q2w3e4r", "qaz12345", "00000000",
    "aaaaaaaa", "asdfasdf", "trustno1", "master12", "shadow12", "michael1",
    "football", "baseball", "superman", "batman12", "princess", "sunshine",
    "starwars", "computer", "internet", "freedom1", "whatever", "qwerty12",
    "abcd1234", "1234abcd", "asd123456", "qwer1234", "123qweasd",
    "mariana123", "lucas1234", "blaxxblaxx",
}


class PasswordIssue(NamedTuple):
    code: str
    message: str


PASSWORD_MIN_LENGTH = 7  # Spec do user: mínimo 7 caracteres, qualquer formato


def validate_password_strength(
    password: str,
    *,
    email: str | None = None,
    name: str | None = None,
    cpf: str | None = None,
    phone: str | None = None,
) -> list[PasswordIssue]:
    """Retorna lista de problemas. Lista vazia = senha aceita.

    Política (spec do user): senha livre, no formato que o cliente quiser,
    bastando ter no mínimo 7 caracteres. Os demais parâmetros são mantidos
    por compatibilidade de assinatura, mas não impõem mais restrições.
    """
    issues: list[PasswordIssue] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        issues.append(PasswordIssue(
            "too_short", f"Senha precisa ter no mínimo {PASSWORD_MIN_LENGTH} caracteres."
        ))
    return issues


def _has_trivial_sequence(s: str) -> bool:
    """Detecta sequências como 1234, abcd, qwer (4+ chars consecutivos crescentes)."""
    s = s.lower()
    for i in range(len(s) - 3):
        chunk = s[i:i + 4]
        if not chunk.isalnum():
            continue
        if all(ord(chunk[j + 1]) - ord(chunk[j]) == 1 for j in range(3)):
            return True
    return False


def password_strength_score(password: str) -> int:
    """Score 0-100. Útil pro frontend mostrar barra de progresso."""
    issues = validate_password_strength(password)
    # 8 regras → cada uma vale 12.5 pontos
    return max(0, 100 - len(issues) * 13)


# ---------------------------- Token helpers ---------------------------- #

def generate_url_safe_token(n_bytes: int = 32) -> str:
    """Token criptograficamente seguro para reset de senha (URL safe)."""
    return secrets.token_urlsafe(n_bytes)


def generate_numeric_code(digits: int = 6) -> str:
    """Código numérico (verificação de e-mail). Padding com zero à esquerda."""
    return f"{secrets.randbelow(10 ** digits):0{digits}d}"


def normalize_email(email: str) -> str:
    """Normaliza email removendo case + espaços + diacríticos comuns."""
    return unicodedata.normalize("NFKC", email).strip().lower()


# ---------------------------- Phone E.164 ---------------------------- #

def normalize_phone(raw: str, default_region: str = "BR") -> str | None:
    """Normaliza telefone para E.164 (+5511987654321). None se inválido.
    Usa lib `phonenumbers` (Google) — mesma do iOS/Android."""
    if not raw:
        return None
    try:
        import phonenumbers
        parsed = phonenumbers.parse(raw, default_region)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        # Fallback básico se a lib não estiver disponível
        import re as _re
        digits = _re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith(("11","21","31","41","51","61","71","81","91")):
            return "+55" + digits
        if len(digits) == 13 and digits.startswith("55"):
            return "+" + digits
        return None


# ---------------------------- Idade ---------------------------- #

def is_adult(birth_date) -> bool:
    """True se a pessoa tem >= 18 anos completos.
    Aceita date ou datetime."""
    from datetime import date, datetime as _dt
    if isinstance(birth_date, _dt):
        birth_date = birth_date.date()
    today = date.today()
    years = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
    return years >= 18


# ---------------------------- Hash refresh tokens ---------------------------- #

def hash_refresh_token(token: str) -> str:
    """SHA-256 hex do refresh token. Nunca armazenamos o token cru no banco."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------- Device fingerprint ---------------------------- #

def device_fingerprint(user_agent: str | None, accept_lang: str | None) -> str:
    """Hash determinístico de UA + Accept-Language para identificar dispositivo.
    Não é único garantido, mas serve pra agrupar sessões e flagging."""
    import hashlib
    base = (user_agent or "") + "|" + (accept_lang or "")
    return hashlib.sha256(base.encode()).hexdigest()[:32]


# ---------------------------- TOTP / MFA ---------------------------- #

def generate_totp_secret() -> str:
    """Segredo TOTP (32 chars base32) compatível com Google Authenticator."""
    try:
        import pyotp
        return pyotp.random_base32()
    except ImportError:
        # Fallback se pyotp não disponível ainda
        import base64
        return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_uri(secret: str, email: str, issuer: str = "Blaxx Pontos") -> str:
    """URI otpauth:// para gerar QR Code (Google/MS/Authy authenticators)."""
    try:
        import pyotp
        return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)
    except ImportError:
        return f"otpauth://totp/{issuer}:{email}?secret={secret}&issuer={issuer}"


def verify_totp(secret: str, code: str) -> bool:
    """Verifica código TOTP de 6 dígitos. Tolerância ±30s."""
    if not secret or not code:
        return False
    try:
        import pyotp
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except ImportError:
        return False


# =========================================================================
# Sprint 2 (P5) · Cifrar/decifrar secrets do DB com Fernet
# =========================================================================

def _derive_fernet_key(master: str, *, info: bytes = b"blaxx-mfa-secret") -> bytes:
    """Deriva 32 bytes (base64-url Fernet key) a partir de SECRET_KEY via HKDF.

    HKDF-SHA256 com salt fixo (info) — deterministico por config. Cada
    `info` diferente da uma key diferente (permite separar dominios).
    """
    import base64
    import hashlib
    import hmac as _hmac
    if not master:
        raise ValueError("SECRET_KEY vazio — nao da pra derivar key de crypto")
    master_b = master.encode("utf-8") if isinstance(master, str) else master
    prk = _hmac.new(info, master_b, hashlib.sha256).digest()
    okm = _hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(okm)


def _get_fernet():
    from cryptography.fernet import Fernet
    from flask import current_app
    master = current_app.config.get("SECRET_KEY", "") or ""
    return Fernet(_derive_fernet_key(master))


def encrypt_secret(plaintext):
    """Cifra string em ciphertext Fernet. None/'' passa intacto."""
    if plaintext is None or plaintext == "":
        return plaintext
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext):
    """Decifra ciphertext Fernet → plaintext.

    Compat: se receber valor que NAO eh Fernet (secret legado em texto
    claro de antes do Sprint 2), devolve como veio + log warning.
    Permite migrar sem quebrar usuarios existentes — eles re-cifram no
    proximo setup_mfa. Pra forcar re-cifra global, rode script de migration.
    """
    if ciphertext is None or ciphertext == "":
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception as e:
        try:
            from flask import current_app
            current_app.logger.warning(
                "decrypt_secret: token invalido — assumindo legacy plaintext (%s)",
                type(e).__name__,
            )
        except Exception:
            pass
        return ciphertext
