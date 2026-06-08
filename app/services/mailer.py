"""Serviço de envio de e-mail.

Providers disponiveis (selecionados via env var MAILER):
  - "console" (default): grava em /tmp/ + loga corpo em logger.info
  - "resend": Resend.com API (free tier 3k/mes, dominios proprios)
  - "noop": nao envia nada (uso em testes)

Em todos os modos:
  - Failure nao derruba o fluxo de auth (capturado, registrado)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    body_text: str
    body_html: str | None = None


class EmailProvider(Protocol):
    name: str
    def send(self, msg: EmailMessage) -> bool: ...


class ConsoleMailer:
    """Provider de dev. Imprime e salva em disco.

    Em ambientes sem acesso ao filesystem (ex: Render free tier sem SSH), o
    arquivo em /tmp/ é inacessivel. Por isso ALEM de salvar o arquivo,
    logamos o corpo INTEIRO no logger.info — assim o codigo aparece no
    Render Dashboard → Logs e o admin/dev pode pegar dali pra testar fluxo.

    JAMAIS use ConsoleMailer em producao real — ele expoe senhas/codigos
    em logs. Pra producao real use SendGrid/Resend/SES (TODO).
    """
    name = "console"

    def __init__(self, outdir: str = "/tmp/blaxx_emails"):
        self.outdir = outdir
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception:
            self.outdir = None  # filesystem read-only → desabilita gravacao

    def send(self, msg: EmailMessage) -> bool:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if self.outdir:
            path = os.path.join(self.outdir, f"{ts}_{msg.to.replace('@', '_at_')}.txt")
            try:
                with open(path, "w") as f:
                    f.write(
                        f"From: noreply@blaxxpontos.com.br\n"
                        f"To: {msg.to}\n"
                        f"Subject: {msg.subject}\n"
                        f"Date: {ts}\n\n{msg.body_text}\n"
                    )
            except Exception as e:
                logger.warning("ConsoleMailer: falha ao salvar arquivo: %s", e)
        # Log do corpo inteiro pra Render Logs (acessivel via dashboard)
        logger.info(
            "[MAIL DEV] To=%s · Subject=%s\n----- BODY -----\n%s\n----- END -----",
            msg.to, msg.subject, msg.body_text,
        )
        return True


class NoOpMailer:
    """Provider null. Usa em testes ou quando email está desabilitado."""
    name = "noop"
    def send(self, msg: EmailMessage) -> bool:
        return True


class ResendMailer:
    """Envia emails via Resend.com API (HTTPS REST).

    Setup:
      1. Crie conta em https://resend.com
      2. Pegue API key em Dashboard → API Keys
      3. Configure env vars no Render:
           MAILER=resend
           RESEND_API_KEY=re_xxxxx
           EMAIL_FROM="Blaxx Pontos <noreply@blaxxpontos.com.br>"
                  ↑ pro dominio precisa estar verificado no Resend
                  ↑ pra testar: use "Blaxx <onboarding@resend.dev>"

    Free tier: 3000 emails/mes, 100/dia. Sem cartao de credito.
    Sem dependencia externa (usa urllib stdlib).
    """
    name = "resend"
    API_URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, from_addr: str):
        if not api_key:
            raise ValueError("RESEND_API_KEY não configurado")
        self.api_key = api_key
        self.from_addr = from_addr or "onboarding@resend.dev"

    def send(self, msg: EmailMessage) -> bool:
        # Resend aceita JSON com "text" e/ou "html". Mandamos text por enquanto;
        # se body_html estiver populado, mandamos os dois.
        body = {
            "from": self.from_addr,
            "to": [msg.to],
            "subject": msg.subject,
            "text": msg.body_text,
        }
        if msg.body_html:
            body["html"] = msg.body_html

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.API_URL,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                if resp.status >= 300:
                    logger.error("Resend %s: %s", resp.status, payload)
                    return False
                logger.info("[MAIL Resend] enviado para %s · id=%s",
                            msg.to, payload.get("id"))
                return True
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            logger.error("Resend HTTP %s: %s", e.code, err_body[:300])
            return False
        except Exception:
            logger.exception("Resend: falha ao enviar para %s", msg.to)
            return False


_singleton: EmailProvider | None = None

def get_mailer() -> EmailProvider:
    global _singleton
    if _singleton is None:
        mode = (os.environ.get("MAILER", "console") or "").lower().strip()
        if mode == "noop":
            _singleton = NoOpMailer()
        elif mode == "resend":
            api_key = os.environ.get("RESEND_API_KEY", "")
            from_addr = os.environ.get(
                "EMAIL_FROM", "Blaxx Pontos <onboarding@resend.dev>"
            )
            if not api_key:
                logger.error(
                    "[MAILER] MAILER=resend mas RESEND_API_KEY NAO esta setada — "
                    "fallback ConsoleMailer. Configure a env var no Render."
                )
                _singleton = ConsoleMailer()
            else:
                try:
                    _singleton = ResendMailer(api_key=api_key, from_addr=from_addr)
                    # Mascara API key no log pra debugar config sem expor secret
                    masked = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
                    logger.info(
                        "[MAILER] Inicializado: ResendMailer · from=%s · key=%s",
                        from_addr, masked,
                    )
                except ValueError as e:
                    logger.error("[MAILER] ResendMailer config invalida (%s) — fallback Console", e)
                    _singleton = ConsoleMailer()
        else:
            _singleton = ConsoleMailer()
            logger.info("[MAILER] Inicializado: ConsoleMailer (MAILER=%s) — emails ficam só em log/disco", mode or "console")
    return _singleton


def reset_mailer():
    """Forca re-leitura das env vars (util em testes ou apos config change)."""
    global _singleton
    _singleton = None


def send_password_reset(to_email: str, name: str, reset_url: str,
                        is_first_password: bool = False) -> bool:
    """Email de definição/recuperação de senha.

    is_first_password=True quando o usuário entrou só via Google (sem senha
    local) e está definindo a primeira. Texto e assunto ficam adaptados pra
    não parecer estranho ("recuperar" algo que nunca teve).
    """
    if is_first_password:
        subject = "Blaxx Pontos · Defina sua senha (login alternativo)"
        body = (
            f"Olá {name},\n\n"
            f"Sua conta no Blaxx Pontos foi criada via Google e ainda não tem senha local.\n"
            f"Definir uma senha permite que você acesse também por e-mail e senha — útil\n"
            f"no app de Windows ou em dispositivos onde você prefere não usar o Google.\n\n"
            f"Para escolher sua senha, abra o link abaixo (válido por 30 minutos):\n\n"
            f"  {reset_url}\n\n"
            f"Você continuará podendo entrar via Google normalmente. O Google e a senha\n"
            f"levam à MESMA conta com o MESMO saldo de pontos.\n\n"
            f"Se não foi você que solicitou, ignore este e-mail.\n\n"
            f"— Equipe Blaxx Pontos"
        )
    else:
        subject = "Blaxx Pontos · Recuperação de senha"
        body = (
            f"Olá {name},\n\n"
            f"Você (ou alguém) solicitou recuperação de senha da sua conta Blaxx Pontos.\n\n"
            f"Para criar uma nova senha, acesse o link abaixo (válido por 30 minutos):\n\n"
            f"  {reset_url}\n\n"
            f"Se não foi você, ignore este e-mail. Sua senha permanece a mesma.\n\n"
            f"— Equipe Blaxx Pontos"
        )
    return get_mailer().send(EmailMessage(to=to_email, subject=subject, body_text=body))


def send_welcome(to_email: str, name: str, code: str | None = None) -> bool:
    """E-mail de CONFIRMAÇÃO DE ADESÃO enviado ao concluir o cadastro.

    Diferente do e-mail de verificação (que carrega o código), este confirma
    de forma calorosa que a conta foi criada e a carteira de pontos está
    ativa. Se `code` for passado, reforça o passo de confirmação de e-mail
    no mesmo envio (evita depender de 2 e-mails chegarem).
    """
    first = (name or "").split(" ")[0] or "cliente"
    extra_code = (
        f"\nPara liberar compra, envio e resgate de pontos, confirme seu e-mail\n"
        f"com o código abaixo (válido por 30 minutos):\n\n  Código: {code}\n"
        if code else ""
    )
    body = (
        f"Olá {first},\n\n"
        f"Sua adesão ao Blaxx Pontos foi confirmada! 🎉\n\n"
        f"Sua carteira de pontos já está ativa e você ganhou 500 pontos de\n"
        f"boas-vindas. A partir de agora você pode acumular pontos no dia a dia\n"
        f"e trocar por vouchers, milhas e cashback no Pix.\n"
        f"{extra_code}\n"
        f"Bons pontos!\n\n"
        f"— Equipe Blaxx Pontos"
    )
    return get_mailer().send(EmailMessage(
        to=to_email,
        subject="Blaxx Pontos · Adesão confirmada — bem-vindo!",
        body_text=body,
    ))


def send_email_verification(to_email: str, name: str, code: str) -> bool:
    msg = EmailMessage(
        to=to_email,
        subject="Blaxx Pontos · Confirme seu e-mail",
        body_text=(
            f"Olá {name},\n\n"
            f"Bem-vindo ao Blaxx Pontos! Para liberar todas as funcionalidades\n"
            f"da sua conta (compra, envio e resgate de pontos), confirme seu e-mail\n"
            f"informando o código abaixo dentro de 10 minutos:\n\n"
            f"  Código: {code}\n\n"
            f"Se não foi você que criou esta conta, ignore este e-mail.\n\n"
            f"— Equipe Blaxx Pontos"
        ),
    )
    return get_mailer().send(msg)
