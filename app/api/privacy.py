"""Endpoints de privacidade / LGPD.

Cobre:
  - GET  /privacy/consents          — lista consentimentos do user logado
  - POST /privacy/consents          — registra novo consentimento (re-aceite,
                                       opt-in marketing, etc) com evidência
  - DELETE /privacy/consents/<id>   — revoga consentimento (cria linha
                                       'revoked' apontando para o original)

Cumpre o **Art. 8º §5º LGPD** (consentimento versionado e revogável).

Consultas reaproveitadas:
  - /privacy/request  já existe em auth.py (Art. 18 — exercício de direitos)
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import UserConsent
from ..services import audit as audit_svc
from .auth import login_required


bp = Blueprint("privacy", __name__)


def _client_ip() -> str | None:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr


def _user_agent() -> str | None:
    return (request.headers.get("User-Agent") or "")[:500] or None


@bp.get("/consents")
@login_required
def list_consents():
    """Lista o histórico completo de consentimentos do user logado.

    Resposta: lista ordenada por accepted_at desc.
    Útil para o titular comprovar quais versões aceitou e quando.
    """
    rows = (
        db.session.query(UserConsent)
        .filter_by(user_id=g.current_user.id)
        .order_by(UserConsent.accepted_at.desc())
        .all()
    )
    return jsonify({"items": [c.to_dict() for c in rows]})


@bp.post("/consents")
@login_required
def add_consent():
    """Registra novo consentimento (re-aceite de termos, opt-in marketing, etc).

    Body:
      {
        "type": "terms" | "privacy" | "lgpd" | "marketing" |
                "cookies_analytics" | "cookies_marketing",
        "version": "2.0",
        "text_hash": "<sha256 hex do texto que o usuário viu>"  (opcional)
      }
    """
    data = request.get_json(silent=True) or {}
    consent_type = (data.get("type") or "").strip()
    version = (data.get("version") or "").strip()
    text_hash = (data.get("text_hash") or "").strip() or None

    valid_types = {
        "terms", "privacy", "lgpd", "marketing",
        "cookies_analytics", "cookies_marketing",
    }
    if consent_type not in valid_types:
        return jsonify({"error": f"type inválido — use um de: {sorted(valid_types)}"}), 400
    if not version:
        return jsonify({"error": "version é obrigatório (ex: '2.0')"}), 400
    if text_hash and len(text_hash) != 64:
        return jsonify({"error": "text_hash deve ser SHA-256 hex (64 chars)"}), 400

    consent = UserConsent(
        user_id=g.current_user.id,
        type=consent_type,
        version=version,
        accepted_at=datetime.now(timezone.utc),
        ip=_client_ip(),
        user_agent=_user_agent(),
        text_hash=text_hash,
        status="accepted",
    )
    db.session.add(consent)
    db.session.flush()

    audit_svc.log_event(
        "consent_accepted",
        user_id=g.current_user.id,
        status="ok",
        extra={"type": consent_type, "version": version, "text_hash": text_hash},
        commit=False,
    )
    db.session.commit()
    return jsonify(consent.to_dict()), 201


@bp.delete("/consents/<consent_id>")
@login_required
def revoke_consent(consent_id: str):
    """Revoga consentimento (Art. 8º §5º LGPD).

    NÃO deleta a linha original (audit trail imutável). Cria nova linha com
    status='revoked' apontando para a original.

    Tipos como 'terms' e 'privacy' não podem ser revogados sem fechar a conta —
    se você não aceita os termos, encerra a conta. Apenas tipos opcionais
    (marketing, cookies não-essenciais) aceitam revogação isolada.
    """
    original = db.session.get(UserConsent, consent_id)
    if original is None or original.user_id != g.current_user.id:
        return jsonify({"error": "consentimento não encontrado"}), 404

    if original.status == "revoked":
        return jsonify({"error": "consentimento já foi revogado"}), 400

    REVOCABLE = {"marketing", "cookies_analytics", "cookies_marketing"}
    if original.type not in REVOCABLE:
        return jsonify({
            "error": (
                f"o consentimento '{original.type}' é necessário para o uso da "
                "Plataforma. Para revogá-lo, encerre sua conta em "
                "/auth/account (LGPD Art. 18 VI)."
            ),
        }), 400

    revocation = UserConsent(
        user_id=g.current_user.id,
        type=original.type,
        version=original.version,
        accepted_at=datetime.now(timezone.utc),
        ip=_client_ip(),
        user_agent=_user_agent(),
        text_hash=original.text_hash,
        status="revoked",
        revokes_consent_id=original.id,
    )
    db.session.add(revocation)
    db.session.flush()

    audit_svc.log_event(
        "consent_revoked",
        user_id=g.current_user.id,
        status="ok",
        extra={
            "type": original.type,
            "version": original.version,
            "revoked_consent_id": original.id,
        },
        commit=False,
    )
    db.session.commit()
    return jsonify({"original": original.to_dict(), "revocation": revocation.to_dict()}), 200
