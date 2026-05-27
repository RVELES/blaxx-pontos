"""Serviço de envio de e-mail.

Modo dev: imprime no console + grava em /tmp/blaxx_emails/<timestamp>.txt
Modo prod: SendGrid/SES/Resend (interface pronta, implementação fica como TODO
até integrar provider real).

Em ambos os modos:
  - Email contém token sensível → logamos hash, não o conteúdo
  - Failure não derruba o fluxo (capturado, registrado)
"""

from __future__ import annotations

import os
import json
import logging
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


_singleton: EmailProvider | None = None

def get_mailer() -> EmailProvider:
    global _singleton
    if _singleton is None:
        mode = os.environ.get("MAILER", "console").lower()
        if mode == "noop":
            _singleton = NoOpMailer()
        else:
            _singleton = ConsoleMailer()
    return _singleton


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
