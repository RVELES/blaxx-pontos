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
from html import escape
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
           EMAIL_FROM="BlaXx <noreply@blaxxpontos.com.br>"
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
        self.from_addr = from_addr or "BlaXx <no-reply@blaxxpontos.com.br>"

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
                # Sem User-Agent o urllib manda "Python-urllib/3.x", que o
                # Cloudflare do Resend bloqueia com 403 "error code: 1010".
                "User-Agent": "blaxx-pontos-backend/1.0",
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
                "EMAIL_FROM", "BlaXx <no-reply@blaxxpontos.com.br>"
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


# ---------------------------------------------------------------------
# Layout HTML da marca (tema Embarque: papel, cartão claro, preto, neon).
# Tabelas + estilo inline porque Gmail/Outlook ignoram <style> e flexbox.
# ---------------------------------------------------------------------

_LOGO_URL = "https://blaxxpontos.com.br/assets/blaxx-logo.png"
_SITE_URL = "https://blaxxpontos.com.br"
_FONTE = "'Helvetica Neue',Helvetica,Arial,sans-serif"


def _layout_html(*, preheader: str, titulo: str, paragrafos: list[str],
                 botao: tuple[str, str] | None = None,
                 codigo: str | None = None,
                 nota: str | None = None) -> str:
    """Monta o e-mail com a identidade da BlaXx.

    `paragrafos` e `nota` já devem vir escapados (podem conter <b>).
    `botao` = (texto, url). `codigo` aparece em destaque, fonte mono.
    """
    p_html = "".join(
        f'<p style="margin:0 0 16px;font:16px/1.55 {_FONTE};color:#0A0B0E">{p}</p>'
        for p in paragrafos
    )
    botao_html = ""
    if botao:
        texto, url = botao
        url = escape(url, quote=True)
        botao_html = (
            '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 20px">'
            '<tr><td style="background:#59FD27;border:2px solid #0A0B0E;border-radius:14px">'
            f'<a href="{url}" style="display:inline-block;padding:14px 26px;font:bold 16px {_FONTE};'
            f'color:#0A0B0E;text-decoration:none">{escape(texto)} &rarr;</a>'
            '</td></tr></table>'
            f'<p style="margin:0 0 16px;font:13px/1.5 {_FONTE};color:#555860">'
            'Se o botão não abrir, copie e cole este endereço no navegador:<br>'
            f'<a href="{url}" style="color:#1F6B0B;word-break:break-all">{url}</a></p>'
        )
    codigo_html = ""
    if codigo:
        codigo_html = (
            '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 20px">'
            '<tr><td style="background:#0A0B0E;border-radius:14px;padding:16px 26px">'
            "<span style=\"font:bold 30px 'Space Mono',Menlo,'Courier New',monospace;"
            f'letter-spacing:8px;color:#59FD27">{escape(codigo)}</span>'
            '</td></tr></table>'
        )
    nota_html = (
        f'<p style="margin:8px 0 0;padding-top:16px;border-top:1px dashed #B9B6AE;'
        f'font:13px/1.5 {_FONTE};color:#555860">{nota}</p>'
        if nota else ""
    )
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{escape(titulo)}</title></head>
<body style="margin:0;padding:0;background:#E4E1D9">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#E4E1D9">
<tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:560px;background:#FBFAF7;border:2px solid #0A0B0E;border-radius:18px;overflow:hidden">
  <tr><td align="center" style="background:#0A0B0E;padding:22px">
    <a href="{_SITE_URL}"><img src="{_LOGO_URL}" width="112" height="112" alt="BlaXx Rewards"
       style="display:block;border:0;width:112px;height:112px;color:#59FD27;font:bold 24px {_FONTE}"></a>
  </td></tr>
  <tr><td style="height:6px;background:#59FD27;font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:30px 30px 26px">
    <h1 style="margin:0 0 18px;font:bold 24px/1.25 {_FONTE};color:#0A0B0E">{escape(titulo)}</h1>
    {p_html}{codigo_html}{botao_html}
    <p style="margin:0;font:16px/1.55 {_FONTE};color:#0A0B0E">Um abraço,<br><b>Equipe BlaXx</b></p>
    {nota_html}
  </td></tr>
</table>
<p style="margin:16px 0 0;font:12px/1.5 {_FONTE};color:#555860">
  BlaXx Rewards &middot; <a href="{_SITE_URL}" style="color:#555860">blaxxpontos.com.br</a><br>
  Este é um e-mail automático. Precisa de ajuda? Fale com a gente no WhatsApp (11) 92470-6095.
</p>
</td></tr></table></body></html>"""


def _primeiro_nome(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] or "tudo bem"


def send_password_reset(to_email: str, name: str, reset_url: str,
                        is_first_password: bool = False) -> bool:
    """Email de definição/recuperação de senha.

    is_first_password=True quando o usuário entrou só via Google (sem senha
    local) e está definindo a primeira. Texto e assunto ficam adaptados pra
    não parecer estranho ("recuperar" algo que nunca teve).
    """
    nome = _primeiro_nome(name)
    if is_first_password:
        subject = "BlaXx · Crie sua senha"
        titulo = "Vamos criar sua senha?"
        paragrafos = [
            f"Oi, {escape(nome)}!",
            "Você entra na BlaXx com o Google, e agora pode ter também uma senha "
            "própria. Assim você acessa com e-mail e senha em qualquer aparelho.",
            "É a <b>mesma conta</b>, com o <b>mesmo saldo de pontos</b>. "
            "O login com o Google continua funcionando normalmente.",
        ]
        botao_txt = "Criar minha senha"
        texto = (
            f"Oi, {nome}!\n\n"
            f"Você entra na BlaXx com o Google, e agora pode ter também uma senha própria.\n"
            f"É a mesma conta, com o mesmo saldo de pontos. O login com o Google continua valendo.\n\n"
            f"Crie sua senha neste link (vale por 30 minutos):\n{reset_url}\n\n"
        )
    else:
        subject = "BlaXx · Redefina sua senha"
        titulo = "Bora criar uma senha nova?"
        paragrafos = [
            f"Oi, {escape(nome)}!",
            "Recebemos um pedido para redefinir a senha da sua conta BlaXx. "
            "É rapidinho: toque no botão abaixo e escolha a nova senha.",
            "Seus pontos continuam guardados, do jeitinho que estavam.",
        ]
        botao_txt = "Criar nova senha"
        texto = (
            f"Oi, {nome}!\n\n"
            f"Recebemos um pedido para redefinir a senha da sua conta BlaXx.\n"
            f"Seus pontos continuam guardados, do jeitinho que estavam.\n\n"
            f"Crie a nova senha neste link (vale por 30 minutos):\n{reset_url}\n\n"
        )
    nota = ("O link vale por 30 minutos e só pode ser usado uma vez. "
            "Não foi você? Pode ignorar este e-mail: sua senha continua a mesma.")
    html = _layout_html(preheader="Seu link vale por 30 minutos.", titulo=titulo,
                        paragrafos=paragrafos, botao=(botao_txt, reset_url), nota=nota)
    texto += f"{nota}\n\nUm abraço,\nEquipe BlaXx"
    return get_mailer().send(EmailMessage(to=to_email, subject=subject,
                                          body_text=texto, body_html=html))


def send_welcome(to_email: str, name: str, code: str | None = None) -> bool:
    """E-mail de CONFIRMAÇÃO DE ADESÃO enviado ao concluir o cadastro.

    Diferente do e-mail de verificação (que carrega o código), este confirma
    de forma calorosa que a conta foi criada e a carteira de pontos está
    ativa. Se `code` for passado, reforça o passo de confirmação de e-mail
    no mesmo envio (evita depender de 2 e-mails chegarem).
    """
    nome = _primeiro_nome(name)
    paragrafos = [
        f"Oi, {escape(nome)}!",
        "Que bom ter você com a gente! Sua conta está criada e sua carteira "
        "de pontos já está ativa.",
        "A partir de agora é só juntar pontos no dia a dia e trocar por vouchers, "
        "milhas e cashback no Pix.",
    ]
    texto = (
        f"Oi, {nome}!\n\n"
        f"Que bom ter você com a gente! Sua conta está criada e sua carteira de pontos\n"
        f"já está ativa.\n\n"
        f"A partir de agora é só juntar pontos no dia a dia e trocar por vouchers,\n"
        f"milhas e cashback no Pix.\n\n"
    )
    if code:
        paragrafos.append("Falta só um passo: confirme seu e-mail com o código abaixo "
                          "para liberar compra, envio e resgate de pontos.")
        texto += f"Falta só um passo: confirme seu e-mail com o código {code} (vale por 30 minutos).\n\n"
    html = _layout_html(
        preheader="Sua carteira de pontos já está ativa.",
        titulo="Boas-vindas à BlaXx!",
        paragrafos=paragrafos, codigo=code,
        botao=("Abrir a BlaXx", _SITE_URL),
        nota="O código vale por 30 minutos." if code else None,
    )
    texto += "Bons pontos!\n\nUm abraço,\nEquipe BlaXx"
    return get_mailer().send(EmailMessage(
        to=to_email,
        subject="BlaXx · Boas-vindas! Sua conta está pronta",
        body_text=texto, body_html=html,
    ))


def send_email_verification(to_email: str, name: str, code: str) -> bool:
    nome = _primeiro_nome(name)
    nota = "O código vale por 10 minutos. Não criou uma conta na BlaXx? Pode ignorar este e-mail."
    html = _layout_html(
        preheader=f"Seu código é {code}.",
        titulo="Confirme seu e-mail",
        paragrafos=[
            f"Oi, {escape(nome)}!",
            "Falta só um passo para liberar tudo na sua conta: compra, envio e "
            "resgate de pontos. Digite este código no app:",
        ],
        codigo=code, nota=nota,
    )
    texto = (
        f"Oi, {nome}!\n\n"
        f"Falta só um passo para liberar tudo na sua conta: compra, envio e resgate de pontos.\n"
        f"Digite este código no app:\n\n  {code}\n\n{nota}\n\nUm abraço,\nEquipe BlaXx"
    )
    return get_mailer().send(EmailMessage(
        to=to_email, subject=f"BlaXx · Seu código: {code}",
        body_text=texto, body_html=html,
    ))
