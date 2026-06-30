"""Autenticação via JWT (Flask-JWT-Extended).

Tokens:
  - access_token: 15 minutos, usado em Authorization: Bearer <token>
  - refresh_token: 7 dias, usado para obter novo access em /auth/refresh

Migração: anterior usava Bearer = user_id direto. Mantém retrocompatibilidade
durante a transição: aceita ambos os formatos no helper `_bearer_user`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies,
    verify_jwt_in_request,
)

from ..extensions import db, limiter
from ..models import (
    User, Wallet, Notification,
    RevokedToken, PasswordResetToken, EmailVerification,
    UserConsent, SocialAccount,
)
from ..security import (
    validate_password_strength,
    generate_url_safe_token,
    generate_numeric_code,
    normalize_email,
    normalize_phone,
    is_adult,
)
from ..services.mailer import send_password_reset, send_email_verification, send_welcome
from ..services import audit as audit_svc

bp = Blueprint("auth", __name__)

# Instância global; ligada à app no factory create_app.
jwt = JWTManager()


# ----------------------- helpers de validação ---------------------------- #

_EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _valid_cpf(cpf: str) -> bool:
    digits = re.sub(r"\D", "", cpf)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(digits[j]) * ((i + 1) - j) for j in range(i))
        d = (s * 10) % 11
        if d == 10: d = 0
        if d != int(digits[i]): return False
    return True


def _normalize_cpf(cpf: str) -> str:
    return re.sub(r"\D", "", cpf)


# ----------------------- decorator login_required ------------------------ #

def _bearer_user() -> User | None:
    """Resolve o usuário a partir do JWT, vindo de cookie OU header.

    SEC-1: Flask-JWT-Extended está configurado com JWT_TOKEN_LOCATION =
    ['cookies', 'headers'], então `verify_jwt_in_request(optional=True)`
    aceita ambos automaticamente. Web SPA → cookie httpOnly `blaxx_session`;
    apps nativos iOS/Android → header `Authorization: Bearer <jwt>`. O nome
    do helper foi mantido por compat dos callers (api/*.py importa).
    """
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            return db.session.get(User, identity)
    except Exception:
        return None
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _bearer_user()
        if user is None:
            return jsonify({"error": "unauthorized"}), 401
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


# ----------------------- helpers de resposta ----------------------------- #

def _issue_tokens(user: User) -> dict:
    """Body padrão de resposta de auth.

    SEC-1: continua devolvendo `token` no body por compat com clientes
    nativos (iOS/Android/desktop) que guardam o token e mandam Authorization
    Bearer. A SPA web ignora o `token` do body e usa o cookie httpOnly que
    `_auth_response()` seta na resposta HTTP. Native apps simplesmente
    ignoram o Set-Cookie (não têm cookie jar de navegador).
    """
    access = create_access_token(identity=user.id)
    refresh = create_refresh_token(identity=user.id)
    return {
        "token": access,
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "user": user.to_dict(),
    }


def _auth_response(user: User, status: int = 200):
    """Cria Response com JSON de `_issue_tokens` + cookies httpOnly setados.

    SEC-1: substitui `jsonify(_issue_tokens(user))` nos endpoints de login.
    Web SPA → consome cookie (config.JWT_TOKEN_LOCATION inclui 'cookies').
    Apps nativos → leem `token`/`refresh_token` do body e enviam via header.
    """
    payload = _issue_tokens(user)
    resp = jsonify(payload)
    try:
        set_access_cookies(resp, payload["access_token"])
        set_refresh_cookies(resp, payload["refresh_token"])
    except Exception as exc:
        # Não derruba o login se algo falhar no cookie — apps nativos seguem
        # com o token no body. Erro fica no log pra investigar.
        current_app.logger.warning("set_jwt_cookies falhou: %s", exc)
    if status != 200:
        resp.status_code = status
    return resp


def _issue_mfa_sms_challenge(user: User):
    """Gera MfaChallenge + PhoneOtp, envia SMS, retorna challenge_token.

    Resposta consumida pelo frontend (initLogin → showMfaChallenge → POST /auth/login/2fa).
    """
    from ..models import MfaChallenge, PhoneOtp
    from ..services import sms as sms_svc
    import secrets as _secrets

    cfg = current_app.config
    code = f"{_secrets.randbelow(1_000_000):06d}"
    challenge_token = _secrets.token_urlsafe(24)

    otp = PhoneOtp(
        user_id=user.id,
        phone=user.phone or "",
        code_hash=PhoneOtp.hash_code(code),
        purpose="login_2fa",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=cfg.get("MFA_CHALLENGE_TTL", 300)),
    )
    db.session.add(otp)
    db.session.flush()  # garante otp.id

    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip() or None
    challenge = MfaChallenge(
        user_id=user.id,
        challenge_token_hash=MfaChallenge.hash_token(challenge_token),
        method="sms",
        phone_otp_id=otp.id,
        ip=ip,
        user_agent=(request.headers.get("User-Agent") or "")[:500],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=cfg.get("MFA_CHALLENGE_TTL", 300)),
    )
    db.session.add(challenge)
    db.session.commit()

    if user.phone:
        sms_svc.send_otp(user.phone, code, "login_2fa")
    audit_svc.log_event("mfa_challenge_issued", user_id=user.id,
                        extra={"method": "sms"})

    masked = "***" + user.phone[-4:] if (user.phone and len(user.phone) >= 4) else ""
    return jsonify({
        "mfa_required": True,
        "mfa_method": "sms",
        "mfa_challenge_token": challenge_token,
        "mfa_phone_hint": masked,
        "expires_in": cfg.get("MFA_CHALLENGE_TTL", 300),
    })


# ----------------------- endpoints --------------------------------------- #

@bp.post("/register")
@limiter.limit("5 per minute; 20 per hour")
def register():
    """Cadastra novo cliente B2C. Onda 2: campos completos + LGPD + audit.

    Body obrigatório:
        name, email, cpf, password, phone, birth_date (ISO YYYY-MM-DD),
        accept_terms, accept_privacy, accept_lgpd  (booleanos)

    Body opcional:
        pix_key, referral_code
    """
    data = request.get_json(silent=True) or {}
    name        = (data.get("name") or "").strip()
    email       = normalize_email(data.get("email") or "")
    cpf_raw     = (data.get("cpf") or "").strip()
    password    = data.get("password") or ""
    pix_key     = (data.get("pix_key") or "").strip() or None
    phone_raw   = (data.get("phone") or "").strip()
    birth_str   = (data.get("birth_date") or "").strip()
    accept_terms   = bool(data.get("accept_terms"))
    accept_privacy = bool(data.get("accept_privacy"))
    accept_lgpd    = bool(data.get("accept_lgpd"))

    # Nome: ≥ 2 palavras (Spec do user)
    if len(name) < 4 or len(name.split()) < 2:
        return jsonify({"error": "Informe nome completo (nome e sobrenome)"}), 400
    if not _EMAIL_RX.match(email):
        return jsonify({"error": "E-mail inválido"}), 400
    if not _valid_cpf(cpf_raw):
        return jsonify({"error": "CPF inválido"}), 400

    # Telefone E.164
    phone = normalize_phone(phone_raw) if phone_raw else None
    if phone_raw and not phone:
        return jsonify({"error": "Celular inválido. Use formato (11) 99999-9999."}), 400

    # Data de nascimento + maior de 18 anos
    birth_date = None
    if birth_str:
        try:
            from datetime import date
            birth_date = date.fromisoformat(birth_str)
        except ValueError:
            return jsonify({"error": "Data de nascimento inválida (use AAAA-MM-DD)"}), 400
        if not is_adult(birth_date):
            return jsonify({"error": "Você precisa ter 18 anos ou mais para cadastrar."}), 400

    # Aceites obrigatórios LGPD
    if not (accept_terms and accept_privacy and accept_lgpd):
        return jsonify({"error": "Você precisa aceitar os Termos, Política de Privacidade e LGPD."}), 400

    # Política de senha forte (Onda 2: passa contexto para evitar self-reference)
    if (issues := validate_password_strength(password, email=email, name=name, cpf=cpf_raw, phone=phone)):
        return jsonify({
            "error": issues[0].message,
            "issues": [{"code": i.code, "message": i.message} for i in issues],
        }), 400

    cpf = _normalize_cpf(cpf_raw)

    # Anti-enumeração + uniqueness
    if db.session.query(User).filter_by(email=email).one_or_none():
        return jsonify({"error": "Este e-mail já está cadastrado"}), 409
    if db.session.query(User).filter_by(cpf=cpf).one_or_none():
        return jsonify({"error": "Este CPF já está cadastrado"}), 409
    if phone and db.session.query(User).filter_by(phone=phone).one_or_none():
        return jsonify({"error": "Este celular já está cadastrado"}), 409

    user = User(
        name=name, email=email, cpf=cpf,
        phone=phone,
        birth_date=datetime.combine(birth_date, datetime.min.time()) if birth_date else None,
        pix_key=pix_key,
        auth_provider="email",
        terms_accepted_at=datetime.now(timezone.utc) if accept_terms else None,
        privacy_accepted_at=datetime.now(timezone.utc) if accept_privacy else None,
        lgpd_accepted_at=datetime.now(timezone.utc) if accept_lgpd else None,
        terms_accepted_version="1.0",
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    # Wallet + notificação boas-vindas
    db.session.add(Wallet(user_id=user.id, balance_pts=0, pending_pts=0))
    db.session.add(Notification(
        user_id=user.id, type="system",
        title="Bem-vindo ao BlaXx",
        body="Confirme seu e-mail para liberar todas as funcionalidades.",
        icon="★",
    ))

    # Consents auditáveis (LGPD Art. 8º §5º — versionado, com evidência)
    now = datetime.now(timezone.utc)
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip() or None
    user_agent = (request.headers.get("User-Agent") or "")[:500] or None
    # Cliente pode mandar text_hash do termo que viu (proteção contra alegação
    # de "vi outro texto"). Se não mandar, fica null e o backend assume "última versão".
    consent_hashes = data.get("consent_text_hashes") or {}
    for consent_type, accepted in [("terms", accept_terms), ("privacy", accept_privacy), ("lgpd", accept_lgpd)]:
        if accepted:
            db.session.add(UserConsent(
                user_id=user.id, type=consent_type, version="1.0",
                accepted_at=now, ip=ip, user_agent=user_agent,
                text_hash=consent_hashes.get(consent_type),
                status="accepted",
            ))

    # Cria código de verificação de e-mail (Onda 1 P0)
    code = generate_numeric_code(6)
    db.session.add(EmailVerification(
        user_id=user.id,
        code_hash=EmailVerification.hash_code(code),
        expires_at=now + timedelta(minutes=30),  # Spec: 30 min
    ))
    db.session.flush()

    # Auditoria
    audit_svc.log_event("register", user_id=user.id, status="ok", commit=False)

    db.session.commit()

    # E-mail de CONFIRMAÇÃO DE ADESÃO (boas-vindas) + código de verificação
    # no mesmo envio, pra não depender de 2 e-mails chegarem.
    try:
        send_welcome(user.email, user.name, code=code)
    except Exception as e:
        current_app.logger.warning("Falha ao enviar e-mail de adesão: %s", e)

    # Sprint 4 (S4-KYC) — valida CPF na RF (BrasilAPI) com timeout duro 3s.
    # NAO bloqueia o cadastro em downtime do provider; user fica kyc_pending=True
    # e admin pode re-validar manualmente depois (review em /admin).
    try:
        from ..services.kyc import validate_cpf_and_mark_user
        validate_cpf_and_mark_user(user)
    except Exception as e:
        current_app.logger.warning("KYC validate_cpf falhou (não bloqueia): %s", e)

    return _auth_response(user, status=201)


@bp.post("/login")
@limiter.limit("10 per minute; 60 per hour")
def login():
    """Login. Onda 2: lock progressivo + audit logs + MFA hint + status check.

    Mensagem genérica "Credenciais inválidas" pra prevenir enumeração.
    Após 5 tentativas: conta bloqueada por 15 minutos.
    Após 10: bloqueio de 1 hora.
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get("email") or data.get("cpf") or "").strip().lower()
    password = data.get("password") or ""

    if "@" in identifier:
        user = db.session.query(User).filter_by(email=identifier).one_or_none()
    else:
        cpf = _normalize_cpf(identifier)
        user = db.session.query(User).filter_by(cpf=cpf).one_or_none()

    # Lock check: conta bloqueada
    if user is not None and user.is_locked:
        audit_svc.log_login_attempt(identifier, success=False,
                                     user_id=user.id, reason="locked")
        return jsonify({
            "error": "Conta temporariamente bloqueada. Tente novamente mais tarde.",
            "locked_until": user.locked_until.isoformat() if user.locked_until else None,
        }), 423  # Locked

    # Sprint 3 (S3-4): Lock por IP em adicao ao lock por user.
    # Atacante com lista de emails poderia tentar 5 vezes em cada um sem
    # ser pego pelo lock-por-user. Aqui contamos falhas POR IP nos
    # ultimos 15 min e bloqueamos a partir de 20.
    from ..models import LoginAttempt
    from sqlalchemy import func as _sa_func
    try:
        client_ip = (request.headers.get("Fly-Client-IP", "").strip()
                     or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
                     or request.remote_addr or "unknown")
        window_start = datetime.now(timezone.utc) - timedelta(minutes=15)
        fails_by_ip = db.session.query(_sa_func.count(LoginAttempt.id)).filter(
            LoginAttempt.ip == client_ip,
            LoginAttempt.success == False,  # noqa: E712
            LoginAttempt.created_at >= window_start,
        ).scalar() or 0
        if fails_by_ip >= 20:
            audit_svc.log_event("login_blocked_by_ip",
                                user_id=user.id if user else None,
                                status="warn",
                                reason=f"{fails_by_ip} fails in last 15min",
                                extra={"fails_window": fails_by_ip,
                                       "client_ip": client_ip})
            db.session.commit()
            return jsonify({
                "error": "Muitas tentativas a partir desta rede. Aguarde 15 minutos.",
                "code": "IP_RATE_LIMITED",
            }), 429
    except Exception:
        current_app.logger.warning("S3-4 ip-lock check falhou", exc_info=True)

    # Suspensa / desativada
    if user is not None and user.status != "active":
        audit_svc.log_login_attempt(identifier, success=False,
                                     user_id=user.id, reason=f"status_{user.status}")
        return jsonify({"error": "Conta indisponível. Contate o suporte."}), 403

    if user is None or not user.check_password(password):
        # Lock progressivo (anti-brute force)
        if user is not None:
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= 10:
                user.locked_until = datetime.now(timezone.utc) + timedelta(hours=1)
                audit_svc.log_event("account_locked", user_id=user.id,
                                    status="warn", reason="10+ failed attempts", commit=False)
            elif user.failed_login_attempts >= 5:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                audit_svc.log_event("account_locked", user_id=user.id,
                                    status="warn", reason="5+ failed attempts", commit=False)
        audit_svc.log_login_attempt(identifier, success=False,
                                     user_id=user.id if user else None,
                                     reason="bad_credentials", commit=False)
        db.session.commit()
        try:
            from ..services.metrics import inc_login
            inc_login("failed")
        except Exception:
            pass
        # Mensagem GENÉRICA — não revela se email ou senha está errado
        return jsonify({"error": "Credenciais inválidas"}), 401

    # MFA: branch por método ativo
    #   - SMS  → gera challenge + manda SMS, retorna mfa_challenge_token.
    #            Frontend completa via POST /auth/login/2fa.
    #   - TOTP → exige mfa_code no body atual (compat Onda 2).
    mfa_code = (data.get("mfa_code") or "").strip()
    if user.mfa_enabled:
        method = (user.mfa_method or "totp").lower()
        if method == "sms":
            # Reset contadores ANTES (login válido — só falta 2FA)
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()
            return _issue_mfa_sms_challenge(user)

        # Fluxo TOTP existente
        from ..models import MfaSecret
        from ..security import verify_totp
        mfa = db.session.query(MfaSecret).filter_by(user_id=user.id, enabled=True).first()
        if mfa is None:
            current_app.logger.error("MFA marked enabled but no secret for user %s", user.id)
            return jsonify({"error": "MFA mal configurado. Contate suporte."}), 500
        if not mfa_code:
            return jsonify({"mfa_required": True, "mfa_method": "totp",
                            "error": "Código MFA necessário"}), 401
        if not verify_totp(mfa.secret, mfa_code):
            audit_svc.log_event("login_fail", user_id=user.id,
                                status="warn", reason="invalid_mfa", commit=False)
            db.session.commit()
            return jsonify({"error": "Código MFA inválido"}), 401
        mfa.last_used_at = datetime.now(timezone.utc)

    # Sucesso: reset contador + atualizar last_login
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)

    audit_svc.log_login_attempt(identifier, success=True, user_id=user.id, commit=False)
    audit_svc.log_event("login_success", user_id=user.id, status="ok", commit=False)
    db.session.commit()
    try:
        from ..services.metrics import inc_login
        inc_login("success")
    except Exception:
        pass

    return _auth_response(user)


# =====================================================================
# Google Sign-In · POST /auth/google
# =====================================================================
# Aceita um ID token JWT emitido pelo Google (vindo do site via Google
# Identity Services, ou do app Mac/iOS via ASWebAuthenticationSession).
# Valida assinatura + audiência, e:
#   - se já existe User com aquele google_sub → login direto
#   - se já existe User com aquele email      → faz "link" (atribui sub)
#   - senão                                   → cria User + Wallet
# E-mail vindo do Google é considerado verificado (email_verified_at = now).
# =====================================================================

@bp.post("/google")
@limiter.limit("20 per minute; 100 per hour")
def google_sign_in():
    """Login/cadastro via Google Sign-In.

    Body esperado:
      {
        "id_token": "<JWT do Google Identity Services>",
        "nonce": "<opcional — nonce gerado pelo cliente>"   # anti-replay
      }

    Validações realizadas:
      1. Assinatura RSA do ID token contra JWKS do Google (google-auth lib)
      2. Expiração (exp) — google-auth
      3. Issuer (iss = accounts.google.com)
      4. Audience (aud) bate com WEB_CLIENT_ID ou IOS_CLIENT_ID
      5. email_verified == true (atestação do Google)
      6. Se cliente enviou nonce, deve bater com payload.nonce (anti-replay)

    Em sucesso:
      - Cria/atualiza User (avatar_url, given_name, family_name)
      - Cria/atualiza SocialAccount entry (audit trail)
      - Emite par access_token + refresh_token
    """
    from ..config import Config

    data = request.get_json(silent=True) or {}
    id_token_str = (data.get("id_token") or "").strip()
    client_nonce = (data.get("nonce") or "").strip() or None
    if not id_token_str:
        return jsonify({"error": "id_token ausente"}), 400

    # Lê do current_app.config para que TestConfig.fixture possa sobrescrever
    # sem precisar de FLASK_ENV ou variáveis de ambiente globais (evita leak
    # entre testes quando Config é importado antes do módulo de teste setar os
    # os.environ).
    audiences = [
        current_app.config.get("GOOGLE_WEB_CLIENT_ID"),
        current_app.config.get("GOOGLE_IOS_CLIENT_ID"),
    ]
    audiences = [a for a in audiences if a]
    if not audiences:
        current_app.logger.error(
            "Google sign-in chamado mas GOOGLE_WEB_CLIENT_ID/GOOGLE_IOS_CLIENT_ID não configurados"
        )
        return jsonify({"error": "Google Sign-In não está configurado neste servidor"}), 503

    try:
        from google.oauth2 import id_token as google_id_token  # type: ignore
        from google.auth.transport import requests as google_requests  # type: ignore
    except ImportError as imp_err:
        current_app.logger.error("falha ao importar deps Google: %s", imp_err)
        return jsonify({
            "error": f"falha ao importar dependência Google: {imp_err}",
        }), 500

    request_adapter = google_requests.Request()
    payload: dict | None = None
    last_err: Exception | None = None
    matched_aud: str | None = None
    for aud in audiences:
        try:
            payload = google_id_token.verify_oauth2_token(
                id_token_str, request_adapter, aud
            )
            matched_aud = aud
            break
        except ValueError as e:
            last_err = e
            continue

    if payload is None:
        current_app.logger.warning("Google ID token inválido: %s", last_err)
        try:
            audit_svc.log_event("google_login_failed", status="fail",
                                reason=str(last_err)[:200])
        except Exception:
            pass
        return jsonify({"error": "Token Google inválido ou expirado"}), 401

    # Issuer obrigatório (defensive — google-auth já valida).
    if payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return jsonify({"error": "issuer inválido"}), 401

    # Nonce anti-replay: se o cliente enviou, payload deve trazer o mesmo.
    # Mitiga reuso de id_tokens capturados. Cliente DEVE gerar nonce único
    # por tentativa de login e mantê-lo só na memória da página atual.
    token_nonce = payload.get("nonce")
    if client_nonce:
        if not token_nonce or token_nonce != client_nonce:
            current_app.logger.warning(
                "Google nonce mismatch · client=%s token=%s",
                client_nonce[:8] if client_nonce else "(empty)",
                str(token_nonce)[:8] if token_nonce else "(empty)",
            )
            try:
                audit_svc.log_event("google_login_nonce_mismatch", status="fail",
                                    reason="nonce mismatch")
            except Exception:
                pass
            return jsonify({"error": "nonce inválido — possível replay"}), 401

    # email_verified obrigatório
    if not payload.get("email_verified", False):
        return jsonify({"error": "Sua conta Google não tem e-mail verificado"}), 401

    google_sub = str(payload.get("sub") or "")
    email = str(payload.get("email") or "").strip().lower()
    name = str(payload.get("name") or "").strip() or "Cliente Blaxx"
    given_name = str(payload.get("given_name") or "").strip()
    family_name = str(payload.get("family_name") or "").strip()
    picture = str(payload.get("picture") or "").strip()
    if not google_sub or not email:
        return jsonify({"error": "Token Google sem sub ou email"}), 401

    # azp (authorized party): em fluxos web, o Google emite com azp do client
    # que iniciou o login. Loga pra audit, mas não rejeita (google-auth já
    # garantiu aud bate com algum dos nossos).
    azp = str(payload.get("azp") or "")

    # 1) Já existe usuário com este sub?
    user = db.session.query(User).filter_by(google_sub=google_sub).one_or_none()

    # 2) Senão, tenta linkar pelo email.
    if user is None:
        user = db.session.query(User).filter_by(email=email).one_or_none()
        if user is not None:
            user.google_sub = google_sub

    # 3) Senão, cria conta nova.
    if user is None:
        cpf_placeholder = f"G:{google_sub[:12]}"
        user = User(
            name=name,
            email=email,
            cpf=cpf_placeholder,
            password_hash=None,
            google_sub=google_sub,
            avatar_url=picture or None,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance_pts=0, pending_pts=0))
        db.session.add(Notification(
            user_id=user.id, type="system",
            title="Bem-vindo ao BlaXx",
            body="Sua conta foi criada via Google. Complete seu CPF no Perfil para liberar resgates PIX.",
            icon="★",
        ))
    else:
        # Conta existente: garante avatar atualizado e e-mail verificado.
        if picture and not user.avatar_url:
            user.avatar_url = picture
        # confirmado o e-mail e agora entra via Google.
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(timezone.utc)

    # ---------------- Social account audit ---------------- #
    # Mantém uma entrada por (provider, provider_user_id). Útil pra:
    # - rastrear quando linkamos pela 1ª vez
    # - atualizar avatar/email do provider
    # - habilitar futuras integrações (Apple, Facebook)
    social = (
        db.session.query(SocialAccount)
        .filter_by(provider="google", provider_user_id=google_sub)
        .one_or_none()
    )
    if social is None:
        db.session.add(SocialAccount(
            user_id=user.id,
            provider="google",
            provider_user_id=google_sub,
            provider_email=email,
            avatar_url=picture or None,
        ))
    else:
        social.user_id = user.id  # garante linkage atual
        social.provider_email = email
        if picture:
            social.avatar_url = picture

    db.session.commit()
    current_app.logger.info(
        "Google sign-in OK · user=%s · sub=%s · azp=%s · aud=%s",
        user.id, google_sub[:8], azp[:20], (matched_aud or "")[:20],
    )
    try:
        audit_svc.log_event("google_login_ok", user_id=user.id, status="ok",
                            extra={"sub": google_sub[:12], "azp": azp[:40],
                                   "given_name": given_name, "family_name": family_name})
    except Exception:
        pass
    return _auth_response(user)


@bp.post("/refresh")
@jwt_required(refresh=True)
def refresh():
    """Sprint 1-2 (P0) · Refresh token ROTATION com reuse-detection.

    Em cada chamada: revoga o JTI atual e emite par novo (access + refresh).
    Cliente DEVE usar o novo refresh nas chamadas seguintes.

    Reuse-detection: ocorre dentro de `_check_revoked` (app/__init__.py).
    Se um refresh ja revogado e' reapresentado em /auth/refresh, o loader
    bumpa user.password_changed_at antes de retornar True → 401 imediato
    aqui E todos os refresh anteriormente emitidos (family kill).

    SEC-1: tambem rotaciona o cookie httpOnly da SPA web.
    """
    claims = get_jwt()
    current_jti = claims.get("jti")
    exp = claims.get("exp", 0)
    user_id = get_jwt_identity()
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404

    # Rotacao: revoga o JTI atual ANTES de emitir o novo par.
    if current_jti and exp:
        try:
            db.session.add(RevokedToken(
                jti=current_jti, user_id=user.id,
                expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("refresh: falha ao gravar RevokedToken")

    access = create_access_token(identity=user_id)
    new_refresh = create_refresh_token(identity=user_id)
    resp = jsonify({
        "access_token": access,
        "token": access,
        "refresh_token": new_refresh,
        "token_type": "Bearer",
    })
    try:
        set_access_cookies(resp, access)
        set_refresh_cookies(resp, new_refresh)
    except Exception as exc:
        current_app.logger.warning("set_jwt_cookies (refresh) falhou: %s", exc)
    return resp


@bp.get("/me")
@login_required
def me():
    return jsonify(g.current_user.to_dict())


# =====================================================================
# Onda 1 P0 — Logout + blacklist de tokens
# =====================================================================

@bp.post("/logout")
@jwt_required()
@limiter.limit("30 per minute")
def logout():
    """Revoga o JWT atual adicionando o jti à blacklist.

    SEC-1: também limpa os cookies httpOnly da SPA web (Set-Cookie com
    Max-Age=0). Apps nativos ignoram — eles apenas paravam de enviar o
    Bearer header após chamada bem-sucedida.
    """
    claims = get_jwt()
    jti = claims.get("jti")
    exp = claims.get("exp", 0)
    user_id = claims.get("sub")
    if not jti or not user_id:
        return jsonify({"error": "token inválido"}), 400

    if not db.session.get(RevokedToken, jti):
        db.session.add(RevokedToken(
            jti=jti, user_id=user_id,
            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        ))
        db.session.commit()
    resp = jsonify({"ok": True, "revoked": True})
    try:
        unset_jwt_cookies(resp)
    except Exception as exc:
        current_app.logger.warning("unset_jwt_cookies falhou: %s", exc)
    return resp


# =====================================================================
# Onda 1 P0 — Forgot / Reset password
# =====================================================================

@bp.post("/forgot-password")
@limiter.limit("3 per minute; 15 per hour")
def forgot_password():
    """Solicita reset de senha. Sempre retorna 200 (anti-enumeração)."""
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email") or "")
    if not email:
        return jsonify({"ok": True})

    user = db.session.query(User).filter_by(email=email).one_or_none()
    if user is not None:
        # Token aleatório de 32 bytes (URL-safe). Guardamos só o hash.
        raw_token = generate_url_safe_token(32)
        token_hash = PasswordResetToken.hash_token(raw_token)
        db.session.add(PasswordResetToken(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        ))
        db.session.commit()

        # URL absoluta — em prod virá do FRONTEND_URL config
        frontend = current_app.config.get(
            "FRONTEND_URL", "https://blaxx-pontos-app.netlify.app"
        )
        # Apontamos pra rota /redefinir-senha (SPA React, sem .html) — caminho
        # consistente com createBrowserRouter no web. Para EXE/Mac o front
        # trata o mesmo path internamente.
        reset_url = f"{frontend}/redefinir-senha?token={raw_token}"
        # is_first_password=True quando user e' Google-only (sem senha local).
        # Email vira "Defina sua primeira senha (login alternativo)".
        is_first = not user.has_password
        try:
            send_password_reset(user.email, user.name, reset_url,
                                is_first_password=is_first)
        except Exception as e:
            current_app.logger.warning("Falha ao enviar e-mail reset: %s", e)
        try:
            audit_svc.log_event("password_reset_request", user_id=user.id,
                                extra={"first_password": is_first})
        except Exception:
            pass

    # Sempre OK pra evitar enumeração de e-mails cadastrados
    return jsonify({"ok": True})


@bp.post("/reset-password")
@limiter.limit("5 per minute; 20 per hour")
def reset_password():
    """Aplica novo senha usando token recebido por email."""
    data = request.get_json(silent=True) or {}
    raw_token = (data.get("token") or "").strip()
    new_password = data.get("password") or ""
    if not raw_token or not new_password:
        return jsonify({"error": "token e password obrigatórios"}), 400

    if (issues := validate_password_strength(new_password)):
        return jsonify({
            "error": issues[0].message,
            "issues": [{"code": i.code, "message": i.message} for i in issues],
        }), 400

    token = db.session.get(PasswordResetToken, PasswordResetToken.hash_token(raw_token))
    if token is None:
        return jsonify({"error": "Token inválido"}), 400
    if token.used_at is not None:
        return jsonify({"error": "Token já utilizado"}), 400
    exp = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > exp:
        return jsonify({"error": "Token expirado"}), 400

    user = db.session.get(User, token.user_id)
    if user is None:
        return jsonify({"error": "Usuário não encontrado"}), 404

    user.set_password(new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    token.used_at = datetime.now(timezone.utc)
    # Boa prática: invalida outros tokens de reset pendentes do mesmo user
    pending = (
        db.session.query(PasswordResetToken)
        .filter_by(user_id=user.id, used_at=None)
        .all()
    )
    for p in pending:
        if p.token_hash != token.token_hash:
            p.used_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({"ok": True})


@bp.post("/change-password")
@login_required
@limiter.limit("10 per hour")
def change_password():
    """Troca senha estando logado. Requer senha atual."""
    data = request.get_json(silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    # User que entrou só via Google nunca teve senha local — bloqueia com
    # mensagem clara em vez de "senha atual incorreta".
    if not g.current_user.has_password:
        return jsonify({
            "error": "Sua conta entra via Google. Defina uma senha em 'Esqueci a senha' antes de trocá-la."
        }), 400
    if not g.current_user.check_password(old):
        return jsonify({"error": "Senha atual incorreta"}), 401
    if (issues := validate_password_strength(new)):
        return jsonify({
            "error": issues[0].message,
            "issues": [{"code": i.code, "message": i.message} for i in issues],
        }), 400
    g.current_user.set_password(new)
    g.current_user.password_changed_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True})


# =====================================================================
# Onda 1 P0 — Verificação de e-mail
# =====================================================================

@bp.post("/verify-email/send")
@login_required
@limiter.limit("3 per minute; 10 per hour")
def send_verification_code():
    """Re-envia código de verificação por e-mail."""
    user = g.current_user
    if user.is_email_verified:
        return jsonify({"error": "E-mail já verificado"}), 400

    # Invalida códigos pendentes
    db.session.query(EmailVerification).filter_by(
        user_id=user.id, consumed_at=None
    ).update({"consumed_at": datetime.now(timezone.utc)})

    code = generate_numeric_code(6)
    db.session.add(EmailVerification(
        user_id=user.id,
        code_hash=EmailVerification.hash_code(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    db.session.commit()

    try:
        send_email_verification(user.email, user.name, code)
    except Exception as e:
        current_app.logger.warning("Falha ao reenviar: %s", e)

    resp = {"ok": True, "expires_in_min": 10}
    # Sprint 0: _dev_code SO em dev/homolog explicito. Em prod ou sem o
    # gate ENABLE_DEV_ENDPOINTS=1, nunca devolve o codigo pra evitar
    # bypass da verificacao por API.
    from .. import _is_production, _dev_endpoints_enabled
    if not _is_production() and _dev_endpoints_enabled():
        resp["_dev_code"] = code
    return jsonify(resp)


@bp.post("/verify-email")
@login_required
@limiter.limit("5 per minute")
def verify_email():
    """Confirma o código recebido por e-mail. 3 tentativas por código."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Código obrigatório"}), 400

    user = g.current_user
    if user.is_email_verified:
        return jsonify({"ok": True, "already_verified": True})

    # Pega o código mais recente não consumido
    ev = (
        db.session.query(EmailVerification)
        .filter_by(user_id=user.id, consumed_at=None)
        .order_by(EmailVerification.created_at.desc())
        .first()
    )
    if ev is None:
        return jsonify({"error": "Nenhum código pendente. Solicite um novo."}), 400

    exp = ev.expires_at if ev.expires_at.tzinfo else ev.expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > exp:
        ev.consumed_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"error": "Código expirado. Solicite um novo."}), 400

    ev.attempts += 1
    if ev.attempts > 3:
        ev.consumed_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"error": "Muitas tentativas. Solicite um novo código."}), 429

    if ev.code_hash != EmailVerification.hash_code(code):
        db.session.commit()
        return jsonify({
            "error": "Código incorreto",
            "attempts_left": max(0, 3 - ev.attempts),
        }), 400

    ev.consumed_at = datetime.now(timezone.utc)
    user.email_verified_at = datetime.now(timezone.utc)
    db.session.add(Notification(
        user_id=user.id, type="system",
        title="E-mail verificado",
        body="Sua conta está liberada para todas as operações financeiras.",
        icon="✓",
    ))
    db.session.commit()
    return jsonify({"ok": True, "verified_at": user.email_verified_at.isoformat()})


# =====================================================================
# Dev-only: auto-verificar e-mail (homologação / MAILER != resend)
# =====================================================================

@bp.post("/dev/verify-email")
@login_required
def dev_auto_verify():
    """Auto-verifica o e-mail da conta logada (homologação).

    Sprint 0 (P0): gate por FLASK_ENV + ENABLE_DEV_ENDPOINTS=1.
    Em produção responde 404 (rota nao existe) — sem importar o MAILER.
    O gate antigo (MAILER != resend) era furado: trocar MAILER reabria.
    """
    from flask import abort
    from .. import _is_production, _dev_endpoints_enabled
    if _is_production() or not _dev_endpoints_enabled():
        abort(404)

    user = g.current_user
    if user.is_email_verified:
        return jsonify({"ok": True, "already_verified": True})

    user.email_verified_at = datetime.now(timezone.utc)
    db.session.add(Notification(
        user_id=user.id, type="system",
        title="E-mail verificado",
        body="Verificação automática (homologação).",
        icon="✓",
    ))
    db.session.commit()
    return jsonify({"ok": True, "verified_at": user.email_verified_at.isoformat()})


# =====================================================================
# Decorator pra exigir email_verified em endpoints financeiros
# =====================================================================

def email_verified_required(fn):
    """Use junto com @login_required em endpoints transacionais.

    Sequência: @bp.post / @login_required / @email_verified_required / def …
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.current_user.is_email_verified:
            return jsonify({
                "error": "E-mail não verificado",
                "code": "EMAIL_NOT_VERIFIED",
                "action": "Confirme seu e-mail antes de operar com pontos.",
            }), 403
        return fn(*args, **kwargs)
    return wrapper


# =====================================================================
# Onda 2 — MFA TOTP (RFC 6238)
# =====================================================================

@bp.post("/mfa/setup")
@login_required
@limiter.limit("5 per hour")
def mfa_setup():
    """Inicia setup MFA: gera segredo TOTP + URI para QR Code.
    Cliente mostra QR para o user escanear no Google Authenticator/Authy.
    User precisa confirmar com /mfa/enable mandando 1 código válido."""
    from ..models import MfaSecret
    from ..security import generate_totp_secret, totp_uri

    existing = db.session.query(MfaSecret).filter_by(user_id=g.current_user.id).first()
    if existing and existing.enabled:
        return jsonify({"error": "MFA já está ativado"}), 400

    secret = generate_totp_secret()
    if existing:
        existing.secret = secret
        existing.enabled = False
    else:
        db.session.add(MfaSecret(user_id=g.current_user.id, secret=secret, enabled=False))
    db.session.commit()

    uri = totp_uri(secret, g.current_user.email)
    return jsonify({"secret": secret, "uri": uri})


@bp.post("/mfa/enable")
@login_required
@limiter.limit("5 per hour")
def mfa_enable():
    """Confirma setup: user manda 1 código TOTP válido para ativar."""
    from ..models import MfaSecret
    from ..security import verify_totp

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"error": "Código TOTP de 6 dígitos obrigatório"}), 400

    mfa = db.session.query(MfaSecret).filter_by(user_id=g.current_user.id).first()
    if mfa is None:
        return jsonify({"error": "Inicie o setup MFA primeiro em /mfa/setup"}), 400
    if not verify_totp(mfa.secret, code):
        audit_svc.log_event("mfa_enable_fail", user_id=g.current_user.id,
                            status="warn", reason="invalid_code")
        return jsonify({"error": "Código inválido. Confira no app autenticador."}), 401

    mfa.enabled = True
    g.current_user.mfa_enabled = True
    audit_svc.log_event("mfa_enabled", user_id=g.current_user.id, commit=False)
    db.session.commit()
    return jsonify({"ok": True, "mfa_enabled": True})


@bp.post("/mfa/disable")
@login_required
@limiter.limit("5 per hour")
def mfa_disable():
    """Desativa MFA. Requer senha atual + código TOTP para confirmar."""
    from ..models import MfaSecret
    from ..security import verify_totp

    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    code = (data.get("code") or "").strip()

    if not g.current_user.check_password(password):
        return jsonify({"error": "Senha incorreta"}), 401

    mfa = db.session.query(MfaSecret).filter_by(user_id=g.current_user.id).first()
    if mfa and mfa.enabled and not verify_totp(mfa.secret, code):
        return jsonify({"error": "Código TOTP inválido"}), 401

    if mfa:
        mfa.enabled = False
    g.current_user.mfa_enabled = False
    audit_svc.log_event("mfa_disabled", user_id=g.current_user.id, commit=False)
    db.session.commit()
    return jsonify({"ok": True, "mfa_enabled": False})


# =====================================================================
# Onda 2 — Sessões / refresh rotation (placeholder pra Fase B)
# =====================================================================

@bp.post("/sessions/revoke-all")
@login_required
def revoke_all_sessions():
    """Logout global: revoga TODOS os tokens ativos do usuário.
    Usa flag User.password_changed_at — todos JWT emitidos antes ficam inválidos
    quando o token store comparar timestamps."""
    g.current_user.password_changed_at = datetime.now(timezone.utc)
    audit_svc.log_event("sessions_revoked_all", user_id=g.current_user.id, commit=False)
    db.session.commit()
    return jsonify({"ok": True})


# =========================================================================
# Sprint 2 (P1+P2) · LGPD endpoints (art. 18 esquecimento + portabilidade)
# =========================================================================

@bp.delete("/account")
@login_required
@limiter.limit("3 per day")
def delete_account():
    """LGPD art. 18: Direito ao esquecimento.

    Sprint 1-2 (P0): exige senha, anonimiza PII (nome/email/phone/pix_key),
    deleta notifications/social, mantem ledger imutavel (Transaction/Transfer/
    PixCharge/PixPayout — exigencia contabil/fiscal), e revoga TODAS as
    sessoes via password_changed_at (blocklist loader invalida tokens com
    iat < password_changed_at).

    CPF e' retido por 5 anos (exigencia fiscal). Audit_log preservado para
    compliance, mas sem PII direta (refs por user_id apenas).

    Body: { "password": "...", "confirm": "EXCLUIR MINHA CONTA" }
    """
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    confirm  = (data.get("confirm") or "").strip()
    user = g.current_user
    if confirm != "EXCLUIR MINHA CONTA":
        return jsonify({"error": "Confirmacao invalida"}), 400
    if not user.check_password(password):
        audit_svc.log_event("account_delete_failed", user_id=user.id,
                            extra={"reason": "wrong_password"})
        db.session.commit()
        return jsonify({"error": "Senha incorreta"}), 401

    # Snapshot pre-anonimizacao pra audit (sem PII no extra, apenas metricas)
    pre = {"cpf_kept": True,
           "balance_pts": (user.wallet.balance_pts if user.wallet else 0)}

    # Anonimizacao canonica (formato spec): deleted_<short_id>@removed.local
    short_id = (user.id or "")[:12] or "anon"
    user.email = f"deleted_{short_id}@removed.local"
    user.name = "Conta Excluida"
    user.phone = None
    user.pix_key = None
    user.password_hash = ""
    # Bump password_changed_at => `_check_revoked` invalida TODOS os JWTs
    # (access + refresh) emitidos antes deste momento. Family kill efetivo.
    user.password_changed_at = datetime.now(timezone.utc)
    if hasattr(user, "is_deleted"): user.is_deleted = True
    if hasattr(user, "deleted_at"): user.deleted_at = datetime.now(timezone.utc)
    if hasattr(user, "mfa_method"): user.mfa_method = None
    if hasattr(user, "phone_verified"): user.phone_verified = False
    if hasattr(user, "status"):
        try:
            user.status = "deleted"
        except Exception:
            pass

    # Cleanup de dados pessoais nao-ledger.
    try:
        db.session.query(SocialAccount).filter_by(user_id=user.id).delete()
    except Exception:
        current_app.logger.warning("LGPD: SocialAccount cleanup falhou", exc_info=True)
    try:
        db.session.query(Notification).filter_by(user_id=user.id).delete()
    except Exception:
        current_app.logger.warning("LGPD: Notification cleanup falhou", exc_info=True)
    # Push subscriptions (web push) — sem PII no payload mas removemos por boa pratica
    try:
        from ..models import PushSubscription  # opcional
        db.session.query(PushSubscription).filter_by(user_id=user.id).delete()
    except Exception:
        pass  # tabela pode nao existir

    audit_svc.log_event("account_deleted", user_id=user.id, extra=pre, commit=False)
    db.session.commit()

    # Limpa cookies de sessao na resposta (apps nativos seguem com tokens
    # invalidados via password_changed_at + blocklist loader).
    resp = jsonify({
        "ok": True,
        "message": "Conta excluida. Dados pessoais anonimizados. "
                   "CPF retido por exigencia fiscal (5 anos).",
    })
    try:
        unset_jwt_cookies(resp)
    except Exception:
        pass
    return resp


@bp.get("/account/export")
@login_required
@limiter.limit("3 per day")
def export_account_data():
    """LGPD art. 18: Portabilidade. JSON com todos os dados do usuario."""
    user = g.current_user
    from ..models import (
        Transaction, Voucher, Notification, LoginAttempt, AuditLog, UserConsent,
    )

    def _dump(obj):
        if obj is None: return None
        if hasattr(obj, "to_dict"):
            try: return obj.to_dict()
            except Exception: pass
        out = {}
        for col in getattr(obj, "__table__", type("X", (), {"columns": []})).columns:
            v = getattr(obj, col.name, None)
            if hasattr(v, "isoformat"): v = v.isoformat()
            elif hasattr(v, "value"): v = v.value
            out[col.name] = v
        return out

    wallet = user.wallet
    tx_list = []
    if wallet:
        rows = db.session.query(Transaction).filter_by(wallet_id=wallet.id).order_by(Transaction.created_at.asc()).all()
        tx_list = [_dump(t) for t in rows]
    vouchers = [_dump(v) for v in db.session.query(Voucher).filter_by(user_id=user.id).all()]
    notifications = [_dump(n) for n in db.session.query(Notification).filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(200).all()]
    login_attempts = [_dump(la) for la in db.session.query(LoginAttempt).filter_by(user_id=user.id).order_by(LoginAttempt.created_at.desc()).limit(50).all()]
    audit_logs = [_dump(al) for al in db.session.query(AuditLog).filter_by(user_id=user.id).order_by(AuditLog.created_at.desc()).limit(200).all()]
    consents = [_dump(c) for c in db.session.query(UserConsent).filter_by(user_id=user.id).all()]
    socials = [{"provider": s.provider, "subject": s.subject,
                "linked_at": s.created_at.isoformat() if s.created_at else None}
               for s in db.session.query(SocialAccount).filter_by(user_id=user.id).all()]

    profile = {
        "id": user.id, "name": user.name, "email": user.email, "cpf": user.cpf,
        "phone": user.phone, "pix_key": user.pix_key,
        "birth_date": user.birth_date.isoformat() if getattr(user, "birth_date", None) else None,
        "role": getattr(user, "role", "user"),
        "is_vip": bool(getattr(user, "is_vip", False)),
        "email_verified": bool(getattr(user, "email_verified", False)),
        "phone_verified": bool(getattr(user, "phone_verified", False)),
        "mfa_method": getattr(user, "mfa_method", None),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None,
    }
    export = {
        "schema_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": user.id,
        "profile": profile,
        "wallet": {
            "balance_pts": wallet.balance_pts if wallet else 0,
            "pending_pts": wallet.pending_pts if wallet else 0,
            "balance_brl_equiv": (wallet.balance_pts * current_app.config["CENTS_PER_POINT"] / 100.0) if wallet else 0.0,
        },
        "transactions": tx_list, "vouchers": vouchers,
        "notifications": notifications, "login_attempts": login_attempts,
        "audit_logs": audit_logs, "consents": consents, "social_accounts": socials,
    }
    audit_svc.log_event("account_exported", user_id=user.id,
                        extra={"records": {"transactions": len(tx_list),
                                           "vouchers": len(vouchers),
                                           "notifications": len(notifications)}})
    db.session.commit()
    from flask import Response
    import json as _json
    body = _json.dumps(export, ensure_ascii=False, indent=2)
    fname = f"blaxx-pontos-export-{user.id[:8]}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    return Response(body, mimetype="application/json; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store"})


# =========================================================================
# Sprint 4 (S4-10) · Terms versioning + re-aceite quando versao muda
# =========================================================================

@bp.get("/terms/current")
def terms_current_version():
    """Devolve a versao atual dos termos."""
    return jsonify({
        "version": current_app.config.get("TERMS_CURRENT_VERSION", "1.0"),
    })


@bp.post("/terms/reaccept")
@login_required
def terms_reaccept():
    """Usuario re-aceita os 3 documentos na versao atual."""
    data = request.get_json(silent=True) or {}
    if not (data.get("accept_terms") and data.get("accept_privacy")
            and data.get("accept_lgpd")):
        return jsonify({"error": "Aceite dos 3 documentos obrigatorio"}), 400
    user = g.current_user
    ver = current_app.config.get("TERMS_CURRENT_VERSION", "1.0")
    now = datetime.now(timezone.utc)
    user.terms_accepted_version = ver
    user.terms_accepted_at = now
    user.privacy_accepted_at = now
    user.lgpd_accepted_at = now
    ip = (request.headers.get("Fly-Client-IP", "").strip()
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    try:
        for ctype in ["terms", "privacy", "lgpd"]:
            consent = UserConsent(
                user_id=user.id, type=ctype, version=ver,
                accepted_at=now, ip=ip,
            )
            db.session.add(consent)
    except Exception:
        current_app.logger.warning("UserConsent reaccept falhou", exc_info=True)
    audit_svc.log_event("terms_reaccepted", user_id=user.id,
                        extra={"version": ver}, commit=False)
    db.session.commit()
    return jsonify({"ok": True, "version": ver})
