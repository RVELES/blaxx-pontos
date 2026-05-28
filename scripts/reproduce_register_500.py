"""Reproduz o cadastro do user diretamente via SQLAlchemy.

Conecta no Postgres com DATABASE_URL, faz INSERT de User + Wallet +
Notification + UserConsent + EmailVerification + AuditLog exatamente
como o /auth/register faria. Captura o stack trace exato do erro.

Uso:
    $env:DATABASE_URL = "postgresql://user:pass@xxx.neon.tech/blaxx"
    python scripts/reproduce_register_500.py
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone, timedelta


def main() -> int:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("ERRO: DATABASE_URL nao setada")
        return 1

    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    # Garante import dos models
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("Step 1: importando models...")
    from app.extensions import db
    from app.models import (
        User, Wallet, Notification, UserConsent, EmailVerification, AuditLog,
        TxType,
    )

    print("Step 2: conectando no Postgres...")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url, echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Detalhes do cadastro
    name = "Diagnostico Teste"
    email = "diag-test@blaxx.test"
    cpf = "11144477735"  # CPF de teste valido (passa DV)
    password_hash = "$argon2id$v=19$m=65536,t=3,p=4$FAKE$FAKE"  # mock
    now = datetime.now(timezone.utc)

    # Limpa cadastros anteriores deste teste
    try:
        existing = session.query(User).filter_by(email=email).one_or_none()
        if existing:
            print(f"  cleanup: deletando user existente {existing.id}")
            session.delete(existing)
            session.commit()
    except Exception:
        session.rollback()

    print()
    print("Step 3: tentando INSERT do User...")
    try:
        user = User(
            name=name, email=email, cpf=cpf,
            phone=None,
            birth_date=None,
            pix_key=None,
            auth_provider="email",
            terms_accepted_at=now,
            privacy_accepted_at=now,
            lgpd_accepted_at=now,
            terms_accepted_version="1.0",
            password_hash=password_hash,
        )
        session.add(user)
        session.flush()
        print(f"  [OK] user.id = {user.id}")
    except Exception as e:
        print(f"  [ERRO] User INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    print()
    print("Step 4: tentando INSERT do Wallet...")
    try:
        session.add(Wallet(user_id=user.id, balance_pts=0, pending_pts=0))
        session.flush()
        print("  [OK] Wallet")
    except Exception as e:
        print(f"  [ERRO] Wallet INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2

    print()
    print("Step 5: tentando INSERT da Notification...")
    try:
        session.add(Notification(
            user_id=user.id, type="system",
            title="Bem-vindo ao Blaxx Pontos",
            body="Teste de diag",
            icon="*",
        ))
        session.flush()
        print("  [OK] Notification")
    except Exception as e:
        print(f"  [ERRO] Notification INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 3

    print()
    print("Step 6: tentando INSERT de 3 UserConsents...")
    try:
        for ctype in ["terms", "privacy", "lgpd"]:
            session.add(UserConsent(
                user_id=user.id, type=ctype, version="1.0",
                accepted_at=now, ip="127.0.0.1",
            ))
        session.flush()
        print("  [OK] UserConsent x3")
    except Exception as e:
        print(f"  [ERRO] UserConsent INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 4

    print()
    print("Step 7: tentando INSERT do EmailVerification...")
    try:
        session.add(EmailVerification(
            user_id=user.id,
            code_hash=EmailVerification.hash_code("123456"),
            expires_at=now + timedelta(minutes=30),
        ))
        session.flush()
        print("  [OK] EmailVerification")
    except Exception as e:
        print(f"  [ERRO] EmailVerification INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 5

    print()
    print("Step 8: tentando INSERT do AuditLog...")
    try:
        session.add(AuditLog(
            user_id=user.id,
            event="register",
            ip="127.0.0.1",
            user_agent="diag-test",
            status="ok",
        ))
        session.flush()
        print("  [OK] AuditLog")
    except Exception as e:
        print(f"  [ERRO] AuditLog INSERT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 6

    print()
    print("Step 9: COMMIT...")
    try:
        session.commit()
        print("  [OK] COMMIT")
    except Exception as e:
        print(f"  [ERRO] COMMIT: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 7

    print()
    print("=== SUCESSO: o register flow funciona no Postgres ===")
    print(f"User criado: {user.id}")
    print(f"Email: {email}")
    print()
    print("Limpando o user de teste...")
    session.delete(user)
    session.commit()
    print("OK. Se /auth/register continua dando 500, problema NAO e' schema.")
    print("Investigue Sentry ou logs do Render diretamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
