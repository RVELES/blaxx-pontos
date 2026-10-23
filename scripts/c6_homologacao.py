#!/usr/bin/env python3
"""Executor da homologação da API Pix do C6 Bank (Roteiro de Testes v3.0).

Por que existe: em 22/10/2026 o C6 cancelou a homologação porque o roteiro foi
sem os retornos reais. O provider de produção (`app/pix/c6.py`) não serve para
isso: só faz 6 das 25 chamadas, engole o status no sucesso e não mascara nada
(lacunas L1 a L8 de `docs/c6-homologacao/01-mapa-roteiro.md`). Este script faz
as 25, do jeito que o roteiro pede, numa execução só.

O que ele faz, nesta ordem:
  1. recusa produção sem `--producao` explícito (e C6_ENV=producao, os dois);
  2. confere a janela do sandbox (seg-sex, 7h-22h, horário de Brasília) e sai
     fora dela: fora da janela o C6 responde 503 e o teste não vale nada;
  3. confere as variáveis de ambiente e o par certificado/chave sem mostrar
     nenhum valor (reaproveita a normalização e o diagnóstico de PEM do
     provider, que dizem por extenso o que está errado);
  4. roda AT_01 e os 24 testes Pix (P_01_01 a P_06_03) NA ORDEM do roteiro,
     encadeando txid, id do lote, id da location, e2eid e id da devolução;
  5. grava `docs/c6-homologacao/resultados/<data-hora>.json` (por teste: id,
     método, endpoint, status, corpo JSON indentado, horário, x-request-id) e
     um resumo `.md` com o bloco pronto para colar em cada tabela do .docx;
  6. sai com código != 0 se algum status diferir do esperado.

Segredos: client_secret, access_token, chave privada e o token do webhook
nunca vão para arquivo nem para a tela; a chave Pix e o client_id aparecem só
com o final; CPF/CNPJ só com os 2 últimos dígitos. Credencial SÓ por variável
de ambiente (ou caminho de arquivo informado nela). Passo a passo do dia em
`docs/c6-homologacao/03-executor.md`.

Uso:
    python3 scripts/c6_homologacao.py --simulador   # ensaio local, sem rede e sem credencial
    python3 scripts/c6_homologacao.py --checar      # só confere a configuração, não chama o C6
    python3 scripts/c6_homologacao.py               # sandbox, de verdade

Códigos de saída:
    0  todos os 25 status conferiram
    1  algum status divergente, erro de rede ou teste pulado
    2  fora da janela do sandbox
    3  configuração incompleta ou execução recusada (produção sem --producao etc.)
    4  AT_01 falhou ou a credencial veio sem escopo (nenhum teste Pix rodou)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

AQUI = Path(__file__).resolve().parent
BACKEND = AQUI.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Host, caminhos e o tratamento de PEM vêm do provider: uma fonte só.
from app.pix.c6 import (  # noqa: E402
    API_HOST_PROD,
    API_HOST_SANDBOX,
    AUTH_PATH,
    PIX_PATH,
    C6Error,
    _TXID_RE,
    _normalize_pem,
    conferir_par,
    resolve_cert_paths,
)

FUSO = ZoneInfo("America/Sao_Paulo")
PASTA_RESULTADOS = BACKEND / "docs" / "c6-homologacao" / "resultados"
VERSAO = "1.0"

# Os 10 escopos que o roteiro lista para Pix + os 2 de webhook que a spec exige (L8).
ESCOPOS_EXIGIDOS = (
    "cob.read", "cob.write", "cobv.read", "cobv.write",
    "lotecobv.read", "lotecobv.write",
    "payloadlocation.read", "payloadlocation.write",
    "pix.read", "pix.write", "webhook.read", "webhook.write",
)

AMBIENTES = {
    "sandbox": "sandbox", "homologacao": "sandbox",
    "producao": "producao", "produção": "producao", "prod": "producao", "production": "producao",
}

# Valores de teste: entre R$ 0,01 e R$ 1,00, todos diferentes para achar cada
# cobrança no extrato do sandbox sem ambiguidade.
VALORES = {
    "P_01_01": "0.10", "P_01_03": "0.11", "P_01_02": "0.12",
    "P_02_01": "0.20", "P_02_02": "0.21",
    "LOTE_1": "0.30", "LOTE_2": "0.31", "P_03_02": "0.32",
    "COB_LOC": "0.40",
    "DEVOLUCAO": "0.01",
}
SOLICITACAO = "Homologacao BlaXx Pontos - teste {}"  # ASCII de propósito: nada de encoding para gastar a janela

_E2E_RE = re.compile(r"^[a-zA-Z0-9]{32}$")
_ID_LOC_RE = re.compile(r"^\d{1,19}$")
_ID_LIVRE_RE = re.compile(r"^[a-zA-Z0-9]{1,35}$")

# Modo simulador: tudo fictício e fixo. As variáveis C6_* do ambiente são
# IGNORADAS, então uma credencial real nunca vai parar no servidor local.
SIM_CLIENT_ID = "client-id-ficticio-do-simulador"
SIM_CLIENT_SECRET = "segredo-ficticio-do-simulador-0000"
SIM_PIX_KEY = "11111111-2222-4333-8444-555555555555"
SIM_WEBHOOK_TOKEN = "tok-ficticio-do-simulador-" + "x" * 16
SIM_WEBHOOK_URL = "https://exemplo.invalid/payments/c6/webhook/" + SIM_WEBHOOK_TOKEN
SIM_DEVEDOR = {
    "nome": "Pessoa Ficticia do Simulador",
    "cpf": "52998224725",  # CPF de teste (dígitos válidos) já usado em tests/test_c6_cobranca.py
    "logradouro": "Rua Ficticia do Simulador, 1",
    "cidade": "Sao Paulo",
    "uf": "SP",
    "cep": "01001000",
}

HEADERS_RELEVANTES = (
    "x-request-id", "x-correlation-id", "correlation-id", "x-amzn-requestid",
    "location", "content-type", "date", "retry-after",
)


# ═════════════════════════════ janela do sandbox ═════════════════════════════ #

def dentro_da_janela(agora: datetime, *, margem_min: int = 5) -> tuple[bool, str]:
    """Sandbox do C6: segunda a sexta, das 7h às 22h, horário de Brasília.

    `agora` sem fuso é tratado como horário de Brasília; com fuso, é
    convertido. Recusa também quando faltam menos de `margem_min` minutos para
    as 22h: os 25 testes não cabem e um 503 no meio estraga a rodada.
    """
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=FUSO)
    local = agora.astimezone(FUSO)
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    quando = f"{dias[local.weekday()]}, {local:%H:%M} (Brasília)"
    if local.weekday() >= 5:
        return False, f"{quando}: o sandbox só atende de segunda a sexta"
    if local.hour < 7:
        return False, f"{quando}: o sandbox abre às 7h"
    fecha = local.replace(hour=22, minute=0, second=0, microsecond=0)
    if local >= fecha:
        return False, f"{quando}: o sandbox fecha às 22h"
    if fecha - local < timedelta(minutes=margem_min):
        resto = int((fecha - local).total_seconds() // 60)
        return False, f"{quando}: faltam {resto} min para as 22h, não dá tempo de rodar os 25 testes"
    return True, f"{quando}: dentro da janela"


# ═════════════════════════════ mascaramento ═════════════════════════════ #

CAMPOS_SECRETOS = {
    "access_token", "refresh_token", "id_token", "client_secret",
    "password", "senha", "private_key", "secret",
}
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)
_BEARER_RE = re.compile(r"(Bearer\s+)(?!<)([A-Za-z0-9\-._~+/=]{8,})")
_URL_WEBHOOK_RE = re.compile(r"(/webhook/)([^/?#\s\"'<]+)")


def mascarar_documento(valor: str) -> str:
    """CPF/CNPJ com só os 2 últimos dígitos: '52998224725' → '*********25'."""
    pos = [i for i, ch in enumerate(valor) if ch.isdigit()]
    manter = set(pos[-2:])
    return "".join("*" if (ch.isdigit() and i not in manter) else ch for i, ch in enumerate(valor))


def mascarar_url_webhook(url: str) -> str:
    """Troca o segmento depois de /webhook/ por <TOKEN:Nch>, como o registrar_webhook_c6.py."""
    return _URL_WEBHOOK_RE.sub(lambda m: f"{m.group(1)}<TOKEN:{len(m.group(2))}ch>", url)


class Mascarador:
    """Tira segredo de tudo o que vai para a tela ou para arquivo.

    Duas camadas: por NOME de campo (access_token, client_secret, cpf,
    webhookUrl...) e por VALOR conhecido (o client_secret, o token emitido, a
    chave Pix, o token do webhook), em qualquer lugar de qualquer string,
    inclusive URL-encoded e escapado em JSON.
    """

    def __init__(self, *, expor_chave_pix: bool = False):
        self.expor_chave_pix = expor_chave_pix
        self._pares: list[tuple[str, str]] = []

    def segredo(self, valor: str, marcador: str | None = None) -> None:
        v = (valor or "").strip()
        if len(v) < 4:
            return
        marcador = marcador or f"<mascarado, {len(v)} caracteres>"
        variantes = {
            v,
            urllib.parse.quote(v, safe=""),
            urllib.parse.quote_plus(v),
            json.dumps(v, ensure_ascii=False)[1:-1],
            json.dumps(v)[1:-1],
        }
        conhecidos = {p for p, _ in self._pares}
        for var in variantes:
            if len(var) >= 4 and var not in conhecidos:
                self._pares.append((var, marcador))
        # O mais longo primeiro: um segredo que contém outro sai inteiro.
        self._pares.sort(key=lambda par: len(par[0]), reverse=True)

    def parcial(self, valor: str, rotulo: str) -> None:
        v = (valor or "").strip()
        if v:
            self.segredo(v, f"<{rotulo}:…{v[-4:]}>")

    def chave_pix(self, valor: str) -> None:
        if not self.expor_chave_pix:
            self.parcial(valor, "chave-pix")

    def texto(self, s):
        if not isinstance(s, str) or not s:
            return s
        for valor, marcador in self._pares:
            if valor in s:
                s = s.replace(valor, marcador)
        s = _PEM_RE.sub("<PEM removido>", s)
        return _BEARER_RE.sub(lambda m: f"{m.group(1)}<mascarado, {len(m.group(2))} caracteres>", s)

    def objeto(self, obj):
        if isinstance(obj, dict):
            saida = {}
            for k, v in obj.items():
                nome = str(k).lower()
                if nome in CAMPOS_SECRETOS and isinstance(v, str) and v:
                    saida[k] = f"<mascarado, {len(v)} caracteres>"
                elif nome in ("cpf", "cnpj") and isinstance(v, (str, int)):
                    saida[k] = mascarar_documento(str(v))
                elif nome == "webhookurl" and isinstance(v, str):
                    saida[k] = mascarar_url_webhook(self.texto(v))
                else:
                    saida[k] = self.objeto(v)
            return saida
        if isinstance(obj, list):
            return [self.objeto(v) for v in obj]
        if isinstance(obj, str):
            return self.texto(obj)
        return obj

    def vazamentos(self, texto: str) -> list[str]:
        """O que sobrou de segredo num texto já mascarado (só rótulos, nunca o valor)."""
        achados = [marcador for valor, marcador in self._pares if valor in texto]
        if "PRIVATE KEY" in texto or "BEGIN CERTIFICATE" in texto:
            achados.append("bloco PEM")
        if _BEARER_RE.search(texto):
            achados.append("Bearer sem máscara")
        return sorted(set(achados))


# ═════════════════════════════ configuração ═════════════════════════════ #

@dataclass
class Config:
    modo: str  # "sandbox" | "producao" | "simulador"
    host: str
    client_id: str = ""
    client_secret: str = ""
    chave_pix: str = ""
    webhook_url: str = ""
    webhook_token_extra: str = ""
    cert_path: str = ""
    key_path: str = ""
    cert_pem: str = ""
    key_pem: str = ""
    devedor: dict = field(default_factory=dict)
    e2eid: str = ""


def _so_digitos(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def cpf_valido(cpf: str) -> bool:
    d = _so_digitos(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    d = _so_digitos(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    for n, pesos in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]), (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        resto = sum(int(d[i]) * pesos[i] for i in range(n)) % 11
        if (0 if resto < 2 else 11 - resto) != int(d[n]):
            return False
    return True


def token_da_url_webhook(url: str) -> str:
    m = _URL_WEBHOOK_RE.search(url or "")
    return m.group(2) if m else ""


def escolher_modo(args, env) -> tuple[str | None, str]:
    """Devolve (modo, erro). Produção exige C6_ENV=producao E --producao."""
    bruto = (env.get("C6_ENV") or "sandbox").strip().lower()
    if args.simulador or args.simulador_url:
        if args.producao:
            return None, "--simulador e --producao juntos não fazem sentido"
        if args.simulador_url:
            alvo = urllib.parse.urlsplit(args.simulador_url)
            if alvo.scheme != "http" or alvo.hostname not in ("127.0.0.1", "localhost"):
                return None, ("--simulador-url só aceita http://127.0.0.1:<porta> ou http://localhost:<porta>: "
                              "o modo simulador não usa mTLS e não pode apontar para fora da máquina")
        return "simulador", ""
    ambiente = AMBIENTES.get(bruto)
    if ambiente is None:
        return None, f"C6_ENV={bruto!r} não é reconhecido; use sandbox (padrão) ou producao"
    if ambiente == "producao" and not args.producao:
        return None, ("C6_ENV=producao, mas sem --producao. A homologação é no SANDBOX; produção cria "
                      "cobrança e devolução de verdade. Se é isso mesmo, rode de novo com --producao")
    if args.producao and ambiente != "producao":
        return None, ("--producao só vale junto com C6_ENV=producao (as duas coisas, de propósito, "
                      "para nenhum engano sozinho apontar para produção)")
    return ambiente, ""


def config_de_env(env, modo: str) -> Config:
    def g(nome: str) -> str:
        return (env.get(nome) or "").strip()

    devedor = {
        "nome": g("C6_DEVEDOR_NOME"),
        "logradouro": g("C6_DEVEDOR_LOGRADOURO"),
        "cidade": g("C6_DEVEDOR_CIDADE"),
        "uf": g("C6_DEVEDOR_UF").upper(),
        "cep": _so_digitos(g("C6_DEVEDOR_CEP")),
    }
    if g("C6_DEVEDOR_CPF"):
        devedor["cpf"] = _so_digitos(g("C6_DEVEDOR_CPF"))
    elif g("C6_DEVEDOR_CNPJ"):
        devedor["cnpj"] = _so_digitos(g("C6_DEVEDOR_CNPJ"))
    return Config(
        modo=modo,
        host=API_HOST_PROD if modo == "producao" else API_HOST_SANDBOX,
        client_id=g("C6_CLIENT_ID"),
        client_secret=g("C6_CLIENT_SECRET"),
        chave_pix=g("C6_PIX_KEY"),
        webhook_url=g("C6_WEBHOOK_URL"),
        webhook_token_extra=g("C6_WEBHOOK_TOKEN"),
        cert_path=os.path.expanduser(g("C6_CERT_PATH")),
        key_path=os.path.expanduser(g("C6_KEY_PATH")),
        cert_pem=env.get("C6_CERT_PEM") or "",
        key_pem=env.get("C6_KEY_PEM") or "",
        devedor=devedor,
        e2eid=g("C6_E2EID"),
    )


def config_simulador(url: str) -> Config:
    return Config(
        modo="simulador", host=url.rstrip("/"),
        client_id=SIM_CLIENT_ID, client_secret=SIM_CLIENT_SECRET,
        chave_pix=SIM_PIX_KEY, webhook_url=SIM_WEBHOOK_URL,
        devedor=dict(SIM_DEVEDOR),
    )


def conferir_config(cfg: Config) -> list[tuple[str, bool, str]]:
    """(variável, ok, o que falta). Nunca devolve valor de variável."""
    itens: list[tuple[str, bool, str]] = [
        ("C6_CLIENT_ID", bool(cfg.client_id), "client_id da credencial nova"),
        ("C6_CLIENT_SECRET", bool(cfg.client_secret), "client_secret da credencial nova"),
        ("C6_PIX_KEY", 0 < len(cfg.chave_pix) <= 77, "chave Pix da conta sandbox (até 77 caracteres)"),
    ]
    url = urllib.parse.urlsplit(cfg.webhook_url) if cfg.webhook_url else None
    itens.append((
        "C6_WEBHOOK_URL",
        bool(url and url.scheme == "https" and url.hostname and token_da_url_webhook(cfg.webhook_url)),
        "https://<host>/payments/c6/webhook/<token> (P_06_01 cadastra esta URL)",
    ))
    if cfg.modo != "simulador":
        if cfg.cert_path or cfg.key_path or not (cfg.cert_pem or cfg.key_pem):
            for nome, p in (("C6_CERT_PATH", cfg.cert_path), ("C6_KEY_PATH", cfg.key_path)):
                ok = bool(p) and os.path.isfile(p) and os.path.getsize(p) > 100
                falta = "caminho do arquivo" if not p else f"arquivo ausente ou vazio: {p}"
                itens.append((nome, ok, falta))
        else:
            itens.append(("C6_CERT_PEM", bool(cfg.cert_pem.strip()), "conteúdo do .crt"))
            itens.append(("C6_KEY_PEM", bool(cfg.key_pem.strip()), "conteúdo do .key"))
    d = cfg.devedor
    itens.append(("C6_DEVEDOR_NOME", bool(d.get("nome")), "nome do devedor de teste"))
    if "cpf" in d:
        itens.append(("C6_DEVEDOR_CPF", cpf_valido(d["cpf"]), "CPF de 11 dígitos com dígitos verificadores válidos"))
    elif "cnpj" in d:
        itens.append(("C6_DEVEDOR_CNPJ", cnpj_valido(d["cnpj"]), "CNPJ de 14 dígitos com dígitos verificadores válidos"))
    else:
        itens.append(("C6_DEVEDOR_CPF (ou C6_DEVEDOR_CNPJ)", False, "documento do devedor de teste"))
    itens.append(("C6_DEVEDOR_LOGRADOURO", bool(d.get("logradouro")), "endereço (exigido em P_02_01 e P_03_01)"))
    itens.append(("C6_DEVEDOR_CIDADE", bool(d.get("cidade")), "cidade"))
    itens.append(("C6_DEVEDOR_UF", bool(re.fullmatch(r"[A-Z]{2}", d.get("uf") or "")), "UF com 2 letras"))
    itens.append(("C6_DEVEDOR_CEP", bool(re.fullmatch(r"\d{8}", d.get("cep") or "")), "CEP com 8 dígitos"))
    if cfg.e2eid:
        itens.append(("C6_E2EID / --e2eid", bool(_E2E_RE.match(cfg.e2eid)), "32 caracteres alfanuméricos"))
    return itens


def montar_mtls(cfg: Config, agora: datetime) -> tuple[ssl.SSLContext, str]:
    """Contexto TLS com o certificado de cliente. Levanta C6Error com a causa por extenso.

    Lê o arquivo, normaliza o PEM (achatado, com aspas, com \\n literal: tudo o
    que já quebrou o deploy) e confere que a chave é do certificado ANTES de
    gastar a janela do sandbox.
    """
    if cfg.cert_path and cfg.key_path:
        try:
            cert_pem = Path(cfg.cert_path).read_text(encoding="utf-8", errors="replace")
            key_pem = Path(cfg.key_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise C6Error(f"não consegui ler o certificado/chave: {exc.strerror} ({exc.filename})") from exc
    else:
        cert_pem, key_pem = cfg.cert_pem, cfg.key_pem
    try:
        crt, key = resolve_cert_paths(cert_pem=cert_pem, key_pem=key_pem)
    except C6Error as exc:
        msg = str(exc)
        if cfg.cert_path and cfg.key_path:  # o diagnóstico do provider fala em *_PEM; aqui são arquivos
            msg = msg.replace("C6_CERT_PEM", "C6_CERT_PATH").replace("C6_KEY_PEM", "C6_KEY_PATH")
        raise C6Error(msg) from exc
    try:
        resumo = conferir_par(_normalize_pem(cert_pem), _normalize_pem(key_pem))
        from cryptography.x509 import load_pem_x509_certificate

        cert = load_pem_x509_certificate(_normalize_pem(cert_pem).encode())
        if cert.not_valid_after_utc < agora.astimezone(timezone.utc):
            raise C6Error(f"o certificado venceu em {cert.not_valid_after_utc:%d/%m/%Y}; peça um novo ao C6")
        contexto = ssl.create_default_context()
        try:
            contexto.load_cert_chain(certfile=crt, keyfile=key)
        except ssl.SSLError as exc:
            raise C6Error(f"o OpenSSL recusou o par certificado/chave: {exc.reason or exc}") from exc
    finally:
        # Arquivos temporários 0600 do resolve_cert_paths: o contexto já carregou.
        for caminho in (crt, key):
            try:
                os.unlink(caminho)
            except OSError:
                pass
    return contexto, resumo


# ═════════════════════════════ HTTP ═════════════════════════════ #

class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """3xx vira HTTPError: o Bearer nunca segue um redirect para outro host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


@dataclass
class Resposta:
    status: int | None
    headers: dict
    texto: str
    erro: str = ""
    duracao_ms: int = 0
    horario: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def json(self):
        if not (self.texto or "").strip():
            return None
        try:
            return json.loads(self.texto)
        except ValueError:
            return None


class ClienteHttp:
    def __init__(self, host: str, contexto_ssl: ssl.SSLContext | None, *, timeout: float = 30.0,
                 sem_proxy: bool = False):
        self.host = host.rstrip("/")
        self.timeout = timeout
        handlers: list = [_SemRedirecionamento()]
        if sem_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        if contexto_ssl is not None:
            handlers.append(urllib.request.HTTPSHandler(context=contexto_ssl))
        self._opener = urllib.request.build_opener(*handlers)

    def enviar(self, metodo: str, caminho: str, *, query: dict | None = None, corpo=None,
               form: dict | None = None, token: str = "") -> tuple[Resposta, str, dict]:
        url = self.host + caminho
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json, application/problem+json", "User-Agent": f"blaxx-homologacao-c6/{VERSAO}"}
        dados = None
        if form is not None:
            dados = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif corpo is not None:
            dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=dados, method=metodo, headers=headers)
        horario = datetime.now(timezone.utc)
        t0 = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                resp = Resposta(r.status, _headers(r.headers), r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                texto = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                texto = ""
            resp = Resposta(exc.code, _headers(exc.headers), texto)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
            motivo = getattr(exc, "reason", None) or exc
            resp = Resposta(None, {}, "", erro=f"{type(exc).__name__}: {motivo}")
        resp.duracao_ms = int((time.monotonic() - t0) * 1000)
        resp.horario = horario
        return resp, url, headers


def _headers(msg) -> dict:
    if msg is None:
        return {}
    return {k.lower(): v for k, v in msg.items()}


def _request_id(resp: Resposta) -> str:
    for nome in ("x-request-id", "x-correlation-id", "correlation-id", "x-amzn-requestid"):
        if resp.headers.get(nome):
            return resp.headers[nome]
    corpo = resp.json()
    if isinstance(corpo, dict):
        return str(corpo.get("correlationId") or corpo.get("correlation_id") or "")
    return ""


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _indentar(corpo) -> str:
    if corpo is None:
        return ""
    if isinstance(corpo, str):
        return corpo
    return json.dumps(corpo, ensure_ascii=False, indent=2)


def novo_txid() -> str:
    """32 alfanuméricos ([a-zA-Z0-9]{26,35}). Sempre novo: txid repetido mascara o 201 (L3)."""
    return "blxhml" + secrets.token_hex(13)


def novo_id_lote() -> str:
    return str(secrets.randbelow(9 * 10 ** 11) + 10 ** 11)  # 12 dígitos: serve se o C6 tratar como inteiro


def novo_id_devolucao() -> str:
    return "blxdev" + secrets.token_hex(6)


# ═════════════════════════════ plano (ordem do roteiro) ═════════════════════════════ #

class Pulado(Exception):
    """O teste não pode rodar; a mensagem diz por quê e o que fazer."""


@dataclass(frozen=True)
class Teste:
    id: str
    titulo: str
    metodo: str
    endpoint_roteiro: str
    status_roteiro: int
    status_spec: int
    tabela_docx: int  # índice 0-based de w:tbl em word/document.xml (01-mapa-roteiro.md)
    funcao: Callable
    depende: tuple[str, ...] = ()
    rotulo_docx: str = ""  # quando o .docx rotula a tabela com outro id (erro de cópia do C6)


def _devedor_simples(ctx) -> dict:
    d = ctx.cfg.devedor
    doc = {"cpf": d["cpf"]} if "cpf" in d else {"cnpj": d["cnpj"]}
    return {**doc, "nome": d["nome"]}


def _devedor_completo(ctx) -> dict:
    d = ctx.cfg.devedor
    return {
        "logradouro": d["logradouro"], "cidade": d["cidade"], "uf": d["uf"], "cep": d["cep"],
        **({"cpf": d["cpf"]} if "cpf" in d else {"cnpj": d["cnpj"]}),
        "nome": d["nome"],
    }


def _no_caminho(valor: str, regra: re.Pattern, nome: str) -> str:
    """Valida um id antes de ele virar caminho da URL (L4): nada de '../outro-endpoint'."""
    valor = str(valor or "")
    if not regra.match(valor):
        raise Pulado(f"{nome} fora do padrão esperado ({regra.pattern}); não vai para a URL")
    return valor


def _at_01(ctx) -> None:
    ctx.autenticar_principal()
    if ctx.escopos_faltando:
        ctx.nota("escopos FALTANDO na credencial: " + " ".join(ctx.escopos_faltando))
    else:
        ctx.nota(f"os {len(ESCOPOS_EXIGIDOS)} escopos exigidos vieram no scope")
    corpo = ctx.ultima_json()
    if isinstance(corpo, dict) and corpo.get("access_token"):
        ctx.nota_roteiro("O access_token foi mascarado neste documento.")
    if isinstance(corpo, dict):
        if corpo.get("token_type") != "Bearer":
            ctx.nota(f"token_type={corpo.get('token_type')!r} (o roteiro espera 'Bearer')")
        if corpo.get("expires_in") != 300:
            ctx.nota(f"expires_in={corpo.get('expires_in')!r} (o roteiro espera 300)")


def _p_01_01(ctx) -> None:
    txid = novo_txid()
    ctx.vars["txid_cob"] = txid
    ctx.principal("PUT", f"/cob/{txid}", corpo={
        "calendario": {"expiracao": 3600},
        "devedor": _devedor_simples(ctx),
        # modalidadeAlteracao 0: o pagador não altera o valor (mesma regra do provider).
        "valor": {"original": VALORES["P_01_01"], "modalidadeAlteracao": 0},
        "chave": ctx.cfg.chave_pix,
        "solicitacaoPagador": SOLICITACAO.format("P_01_01"),
    })


def _p_01_02(ctx) -> None:
    resp = ctx.principal("POST", "/cob", corpo={
        "calendario": {"expiracao": 3600},
        "devedor": _devedor_simples(ctx),
        "valor": {"original": VALORES["P_01_02"], "modalidadeAlteracao": 0},
        "chave": ctx.cfg.chave_pix,
        "solicitacaoPagador": SOLICITACAO.format("P_01_02"),
    })
    corpo = resp.json()
    if isinstance(corpo, dict) and corpo.get("txid"):
        ctx.vars["txid_cob_gerado"] = str(corpo["txid"])
        ctx.nota(f"txid gerado pelo C6: {corpo['txid']}")


def _p_01_03(ctx) -> None:
    txid = _no_caminho(ctx.vars.get("txid_cob"), _TXID_RE, "txid do P_01_01")
    ctx.nota(f"revisa a cobrança ATIVA do P_01_01 (txid {txid})")
    ctx.principal("PATCH", f"/cob/{txid}", corpo={
        "valor": {"original": VALORES["P_01_03"]},
        "solicitacaoPagador": SOLICITACAO.format("P_01_03 (cobranca revisada)"),
    })


def _p_01_04(ctx) -> None:
    txid = _no_caminho(ctx.vars.get("txid_cob"), _TXID_RE, "txid do P_01_01")
    ctx.principal("GET", f"/cob/{txid}")


def _p_01_05(ctx) -> None:
    ctx.principal("GET", "/cob", query=ctx.periodo())


def _p_02_01(ctx) -> None:
    txid = novo_txid()
    ctx.vars["txid_cobv"] = txid
    ctx.principal("PUT", f"/cobv/{txid}", corpo={
        "calendario": {"dataDeVencimento": ctx.vencimento(), "validadeAposVencimento": 30},
        "devedor": _devedor_completo(ctx),
        "valor": {"original": VALORES["P_02_01"]},
        "chave": ctx.cfg.chave_pix,
        "solicitacaoPagador": SOLICITACAO.format("P_02_01"),
    })


def _p_02_02(ctx) -> None:
    txid = _no_caminho(ctx.vars.get("txid_cobv"), _TXID_RE, "txid do P_02_01")
    ctx.principal("PATCH", f"/cobv/{txid}", corpo={
        "valor": {"original": VALORES["P_02_02"]},
        "solicitacaoPagador": SOLICITACAO.format("P_02_02 (cobranca revisada)"),
    })


def _p_02_03(ctx) -> None:
    txid = _no_caminho(ctx.vars.get("txid_cobv"), _TXID_RE, "txid do P_02_01")
    ctx.principal("GET", f"/cobv/{txid}")


def _p_02_04(ctx) -> None:
    ctx.principal("GET", "/cobv", query=ctx.periodo())


def _p_03_01(ctx) -> None:
    lote = novo_id_lote()
    txids = [novo_txid(), novo_txid()]
    venc = ctx.vencimento()
    ctx.vars.update(lote_id=lote, txids_lote=txids, vencimento_lote=venc)
    cobsv = [
        {
            "calendario": {"dataDeVencimento": venc, "validadeAposVencimento": 30},
            "txid": txid,
            "devedor": _devedor_completo(ctx),
            "valor": {"original": valor},
            "chave": ctx.cfg.chave_pix,
            "solicitacaoPagador": SOLICITACAO.format(f"P_03_01, item {i}"),
        }
        for i, (txid, valor) in enumerate(zip(txids, (VALORES["LOTE_1"], VALORES["LOTE_2"])), 1)
    ]
    resp = ctx.principal("PUT", f"/lotecobv/{lote}", corpo={
        "descricao": SOLICITACAO.format("P_03_01 (lote)"),
        "cobsv": cobsv,
    })
    if resp.status is not None and 200 <= resp.status < 300:
        ctx.aguardar_lote(lote)


def _p_03_02(ctx) -> None:
    lote = _no_caminho(ctx.vars.get("lote_id"), _ID_LIVRE_RE, "id do lote")
    primeiro = ctx.vars["txids_lote"][0]
    ctx.nota(f"revisa só a 1ª cobrança do lote (txid {primeiro}); o conjunto original fica igual")
    resp = ctx.principal("PATCH", f"/lotecobv/{lote}", corpo={
        "cobsv": [{
            "calendario": {"dataDeVencimento": ctx.vars["vencimento_lote"]},
            "txid": primeiro,
            "valor": {"original": VALORES["P_03_02"]},
        }],
    })
    if resp.status is not None and 200 <= resp.status < 300:
        ctx.aguardar_lote(lote)


def _p_03_03(ctx) -> None:
    lote = _no_caminho(ctx.vars.get("lote_id"), _ID_LIVRE_RE, "id do lote")
    ctx.principal("GET", f"/lotecobv/{lote}")


def _p_03_04(ctx) -> None:
    ctx.principal("GET", "/lotecobv", query=ctx.periodo())


def _p_04_01(ctx) -> None:
    resp = ctx.principal("POST", "/loc", corpo={"tipoCob": "cob"})
    corpo = resp.json()
    lid = str(corpo.get("id")) if isinstance(corpo, dict) and corpo.get("id") is not None else ""
    if not (resp.status is not None and 200 <= resp.status < 300):
        return
    if not _ID_LOC_RE.match(lid):
        ctx.nota("a resposta não trouxe um id numérico de location; P_04_03 e P_04_04 vão ser pulados")
        return
    ctx.vars["loc_id"] = lid
    if resp.headers.get("location"):
        ctx.nota(f"header location: {resp.headers['location']}")
    # O P_04_04 desvincula uma cobrança da location: então vinculamos uma agora.
    txid = novo_txid()
    vinc = ctx.auxiliar("criar cobrança vinculada à location (pré-requisito do P_04_04)",
                        "PUT", f"/cob/{txid}", corpo={
                            "calendario": {"expiracao": 3600},
                            "devedor": _devedor_simples(ctx),
                            "loc": {"id": int(lid)},
                            "valor": {"original": VALORES["COB_LOC"], "modalidadeAlteracao": 0},
                            "chave": ctx.cfg.chave_pix,
                            "solicitacaoPagador": SOLICITACAO.format("P_04 (cobranca vinculada a location)"),
                        })
    if vinc.status is not None and 200 <= vinc.status < 300:
        ctx.vars["txid_loc"] = txid
        ctx.nota(f"cobrança {txid} vinculada à location {lid} (chamada auxiliar)")
    else:
        ctx.nota(f"não consegui vincular uma cobrança à location (HTTP {vinc.status}); o P_04_04 vai ser pulado")


def _p_04_02(ctx) -> None:
    ctx.nota("o roteiro não cita query; a spec exige inicio e fim, então vão junto")
    ctx.nota_roteiro("Enviados os parâmetros inicio e fim, obrigatórios na especificação.")
    ctx.principal("GET", "/loc", query=ctx.periodo())


def _p_04_03(ctx) -> None:
    lid = _no_caminho(ctx.vars.get("loc_id"), _ID_LOC_RE, "id da location do P_04_01")
    ctx.principal("GET", f"/loc/{lid}")


def _p_04_04(ctx) -> None:
    lid = _no_caminho(ctx.vars.get("loc_id"), _ID_LOC_RE, "id da location do P_04_01")
    if not ctx.vars.get("txid_loc"):
        raise Pulado("nenhuma cobrança ficou vinculada à location (ver a chamada auxiliar do P_04_01); "
                     "sem vínculo o DELETE só devolveria 404")
    ctx.principal("DELETE", f"/loc/{lid}/txid")


def _p_05_01(ctx) -> None:
    e2e = ctx.cfg.e2eid
    if not e2e:
        # O P_05_01 vem antes do P_05_02 no roteiro, então a lista vai como auxiliar.
        resp = ctx.auxiliar("procurar um Pix recebido no período para obter o e2eid",
                            "GET", "/pix", query=ctx.periodo())
        corpo = resp.json()
        lista = corpo.get("pix") if isinstance(corpo, dict) else None
        candidatos = [p for p in (lista or []) if isinstance(p, dict) and _E2E_RE.match(str(p.get("endToEndId") or ""))]
        candidatos.sort(key=lambda p: str(p.get("horario") or ""), reverse=True)
        if candidatos:
            e2e = str(candidatos[0]["endToEndId"])
            ctx.nota("e2eid obtido da lista de Pix recebidos (chamada auxiliar)")
    if not e2e:
        raise Pulado(
            "nenhum Pix pago no período, então não há e2eid (bloqueador L7 do mapa). Pague uma cobrança "
            "no sandbox e rode de novo com C6_E2EID=<endToEndId>, ou envie ao C6 explicando que o sandbox "
            "não documenta como simular pagamento")
    ctx.vars["e2eid"] = _no_caminho(e2e, _E2E_RE, "e2eid")
    ctx.principal("GET", f"/pix/{ctx.vars['e2eid']}")


def _p_05_02(ctx) -> None:
    ctx.principal("GET", "/pix", query=ctx.periodo())


def _p_05_03(ctx) -> None:
    e2e = _no_caminho(ctx.vars.get("e2eid"), _E2E_RE, "e2eid")
    did = novo_id_devolucao()
    ctx.vars["devolucao_id"] = did
    ctx.nota(f"devolução parcial de R$ {VALORES['DEVOLUCAO']} com id nosso {did}; "
             "o roteiro não cita body, a spec exige valor")
    ctx.nota_roteiro(f"Devolução parcial de R$ {VALORES['DEVOLUCAO'].replace('.', ',')} "
                     "(campo valor, obrigatório na especificação).")
    ctx.principal("PUT", f"/pix/{e2e}/devolucao/{did}", corpo={
        "valor": VALORES["DEVOLUCAO"],
        "natureza": "ORIGINAL",
        "descricao": SOLICITACAO.format("P_05_03 (devolucao parcial)"),
    })


def _p_05_04(ctx) -> None:
    e2e = _no_caminho(ctx.vars.get("e2eid"), _E2E_RE, "e2eid")
    did = _no_caminho(ctx.vars.get("devolucao_id"), _ID_LIVRE_RE, "id da devolução")
    ctx.principal("GET", f"/pix/{e2e}/devolucao/{did}")


def _p_06_01(ctx) -> None:
    ctx.nota("o roteiro não cita body, a spec exige webhookUrl (lida de C6_WEBHOOK_URL, token mascarado)")
    ctx.nota_roteiro("Body enviado: webhookUrl, obrigatório na especificação.")
    ctx.principal("PUT", f"/webhook/{ctx.chave_no_caminho()}", corpo={"webhookUrl": ctx.cfg.webhook_url})


def _p_06_02(ctx) -> None:
    resp = ctx.principal("GET", f"/webhook/{ctx.chave_no_caminho()}")
    corpo = resp.json()
    if isinstance(corpo, dict) and "webhookUrl" in corpo:
        ctx.nota_roteiro("O token no caminho da webhookUrl foi mascarado neste documento.")
        igual = str(corpo["webhookUrl"]).strip() == ctx.cfg.webhook_url
        ctx.nota("webhookUrl gravado confere com o enviado" if igual
                 else "ATENÇÃO: webhookUrl gravado é DIFERENTE do enviado no P_06_01")


def _p_06_03(ctx) -> None:
    resp = ctx.principal("DELETE", f"/webhook/{ctx.chave_no_caminho()}")
    if ctx.args.manter_webhook and resp.status is not None and 200 <= resp.status < 300:
        novo = ctx.auxiliar("recadastrar o webhook depois do P_06_03 (--manter-webhook)",
                            "PUT", f"/webhook/{ctx.chave_no_caminho()}", corpo={"webhookUrl": ctx.cfg.webhook_url})
        ctx.nota(f"webhook recadastrado após o teste (HTTP {novo.status}, chamada auxiliar)")


PLANO: tuple[Teste, ...] = (
    Teste("AT_01", "Geração do token de sessão", "POST", "/auth", 200, 200, 4, _at_01),
    Teste("P_01_01", "Criar cobrança imediata com txid", "PUT", "/pix/cob/{txid}", 201, 201, 53, _p_01_01),
    Teste("P_01_02", "Criar cobrança imediata sem txid", "POST", "/pix/cob", 200, 201, 55, _p_01_02),
    Teste("P_01_03", "Revisar cobrança imediata", "PATCH", "/pix/cob/{txid}", 201, 200, 57, _p_01_03, ("P_01_01",)),
    Teste("P_01_04", "Consultar cobrança imediata", "GET", "/pix/cob/{txid}", 200, 200, 59, _p_01_04, ("P_01_01",)),
    Teste("P_01_05", "Consultar lista de cobranças imediatas", "GET", "/pix/cob?inicio={data}&fim={data}",
          200, 200, 61, _p_01_05),
    Teste("P_02_01", "Criar cobrança com vencimento com txid", "PUT", "/pix/cobv/{txid}", 201, 201, 63, _p_02_01),
    Teste("P_02_02", "Revisar cobrança com vencimento", "PATCH", "/pix/cobv/{txid}", 200, 200, 65, _p_02_02,
          ("P_02_01",)),
    Teste("P_02_03", "Consultar cobrança com vencimento", "GET", "/pix/cobv/{txid}", 200, 200, 67, _p_02_03,
          ("P_02_01",)),
    Teste("P_02_04", "Consultar lista de cobranças com vencimento", "GET", "/pix/cobv?inicio={data}&fim={data}",
          200, 200, 69, _p_02_04),
    Teste("P_03_01", "Criar lote de cobranças com vencimento", "PUT", "/pix/lotecobv/{id}", 201, 202, 71, _p_03_01),
    Teste("P_03_02", "Revisar cobranças dentro do lote", "PATCH", "/pix/lotecobv/{id}", 200, 202, 73, _p_03_02,
          ("P_03_01",)),
    Teste("P_03_03", "Consultar lote específico", "GET", "/pix/lotecobv/{id}", 200, 200, 75, _p_03_03,
          ("P_03_01",), "P_02_03"),
    Teste("P_03_04", "Consultar lista de lotes", "GET", "/pix/lotecobv?inicio={data}&fim={data}",
          200, 200, 77, _p_03_04),
    Teste("P_04_01", "Criar location do payload", "POST", "/pix/loc", 201, 201, 79, _p_04_01),
    Teste("P_04_02", "Consultar locations cadastradas", "GET", "/pix/loc", 200, 200, 81, _p_04_02),
    Teste("P_04_03", "Recuperar location específica", "GET", "/pix/loc/{id}", 200, 200, 83, _p_04_03, ("P_04_01",)),
    Teste("P_04_04", "Desvincular cobrança de uma location", "DELETE", "/pix/loc/{id}/txid", 200, 200, 85,
          _p_04_04, ("P_04_01",), "P_02_04"),
    Teste("P_05_01", "Consultar Pix recebido específico", "GET", "/pix/pix/{e2eid}", 200, 200, 87, _p_05_01),
    Teste("P_05_02", "Consultar lista de Pix recebidos", "GET", "/pix/pix?inicio={data}&fim={data}",
          200, 200, 89, _p_05_02),
    Teste("P_05_03", "Solicitar devolução", "PUT", "/pix/pix/{e2eid}/devolucao/{id}", 201, 201, 91, _p_05_03,
          ("P_05_01",)),
    Teste("P_05_04", "Consultar devolução", "GET", "/pix/pix/{e2eid}/devolucao/{id}", 200, 200, 93, _p_05_04,
          ("P_05_03",)),
    Teste("P_06_01", "Cadastrar webhook Pix", "PUT", "/pix/webhook/{chave}", 200, 200, 95, _p_06_01),
    Teste("P_06_02", "Consultar webhook", "GET", "/pix/webhook/{chave}", 200, 200, 97, _p_06_02, ("P_06_01",)),
    Teste("P_06_03", "Excluir webhook", "DELETE", "/pix/webhook/{chave}", 204, 204, 99, _p_06_03, ("P_06_01",)),
)


# ═════════════════════════════ execução ═════════════════════════════ #

class Execucao:
    def __init__(self, cfg: Config, cliente: ClienteHttp, masc: Mascarador, args,
                 relogio: Callable[[], datetime], dormir: Callable[[float], None], janela: str = ""):
        self.cfg = cfg
        self.cliente = cliente
        self.masc = masc
        self.args = args
        self.relogio = relogio
        self.dormir = dormir
        self.janela = janela
        self.inicio = relogio()
        self.fim: datetime | None = None
        self.token = ""
        self._token_vale_ate = 0.0
        self.escopos: list[str] = []
        self.vars: dict = {}
        self.sucesso: dict[str, bool] = {}
        self.testes: list[dict] = []
        self.auxiliares: list[dict] = []
        self.codigo = 0
        self._teste: Teste | None = None
        self._atual: dict | None = None
        self._notas: list[str] = []
        self._notas_roteiro: list[str] = []

    # ───────── utilidades para os testes ───────── #

    @property
    def escopos_faltando(self) -> list[str]:
        return [e for e in ESCOPOS_EXIGIDOS if e not in self.escopos]

    def nota(self, texto: str) -> None:
        """Observação interna: vai para o .json/.md, nunca para o roteiro."""
        self._notas.append(texto)

    def nota_roteiro(self, texto: str) -> None:
        """Observação para quem lê o roteiro no C6 (vira `nota`, que o preenchedor põe no .docx)."""
        self._notas_roteiro.append(texto)

    def periodo(self) -> dict:
        """inicio/fim (RFC 3339, UTC) cobrindo as últimas N horas até agora."""
        inicio = self.inicio - timedelta(hours=self.args.horas_lista)
        return {"inicio": _rfc3339(inicio), "fim": _rfc3339(self.relogio())}

    def vencimento(self) -> str:
        return (self.relogio().astimezone(FUSO).date() + timedelta(days=7)).isoformat()

    def chave_no_caminho(self) -> str:
        return urllib.parse.quote(self.cfg.chave_pix, safe="")

    def ultima_json(self):
        return self._atual["resp"].json() if self._atual else None

    # ───────── token ───────── #

    def _autenticar(self) -> tuple[Resposta, str, dict, dict]:
        form = {"client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
                "grant_type": "client_credentials"}
        resp, url, headers = self.cliente.enviar("POST", AUTH_PATH, form=form)
        corpo = resp.json()
        if resp.status == 200 and isinstance(corpo, dict) and str(corpo.get("access_token") or "").strip():
            self.token = str(corpo["access_token"]).strip()
            self.masc.segredo(self.token)  # antes de qualquer registro
            try:
                ttl = int(corpo.get("expires_in") or 300)
            except (TypeError, ValueError):
                ttl = 300
            self._token_vale_ate = time.monotonic() + max(ttl - 30, 30)
            self.escopos = str(corpo.get("scope") or "").split()
        return resp, url, headers, form

    def autenticar_principal(self) -> Resposta:
        resp, url, headers, form = self._autenticar()
        self._atual = {"metodo": "POST", "url": url, "headers": headers, "corpo": form, "resp": resp}
        return resp

    def _bearer(self) -> str:
        if not self.token or time.monotonic() >= self._token_vale_ate:
            resp, url, headers, form = self._autenticar()
            self.auxiliares.append(self._montar("aux", "renovar o token (expires_in)", "POST",
                                                url, headers, form, resp))
            if resp.status != 200:
                raise Pulado(f"não consegui renovar o token (HTTP {resp.status}); ver chamadas auxiliares")
        return self.token

    def _enviar_pix(self, metodo: str, caminho: str, *, corpo=None, query=None):
        token = self._bearer()
        resp, url, headers = self.cliente.enviar(metodo, PIX_PATH + caminho, query=query, corpo=corpo, token=token)
        if resp.status == 401:
            self.auxiliares.append(self._montar(
                "aux", "401 na 1ª tentativa; o token foi renovado e a chamada repetida",
                metodo, url, headers, corpo, resp))
            self.token = ""
            token = self._bearer()
            resp, url, headers = self.cliente.enviar(metodo, PIX_PATH + caminho, query=query, corpo=corpo,
                                                     token=token)
            self.nota("repetido uma vez depois de um 401 (token renovado)")
        return resp, url, headers

    def principal(self, metodo: str, caminho: str, *, corpo=None, query=None) -> Resposta:
        resp, url, headers = self._enviar_pix(metodo, caminho, corpo=corpo, query=query)
        self._atual = {"metodo": metodo, "url": url, "headers": headers, "corpo": corpo, "resp": resp}
        return resp

    def auxiliar(self, descricao: str, metodo: str, caminho: str, *, corpo=None, query=None) -> Resposta:
        resp, url, headers = self._enviar_pix(metodo, caminho, corpo=corpo, query=query)
        dono = self._teste.id if self._teste else "-"
        self.auxiliares.append(self._montar(f"aux:{dono}", descricao, metodo, url, headers, corpo, resp))
        return resp

    def aguardar_lote(self, lote: str) -> bool:
        """O lote é assíncrono (202): consulta até sair de EM_PROCESSAMENTO."""
        for n in range(1, self.args.tentativas_lote + 1):
            self.dormir(self.args.intervalo_lote)
            resp = self.auxiliar(f"aguardar o processamento do lote (consulta {n})", "GET", f"/lotecobv/{lote}")
            corpo = resp.json()
            itens = corpo.get("cobsv") if resp.status == 200 and isinstance(corpo, dict) else None
            if itens and not any(isinstance(i, dict) and i.get("status") == "EM_PROCESSAMENTO" for i in itens):
                negadas = [str(i.get("txid")) for i in itens if isinstance(i, dict) and i.get("status") == "NEGADA"]
                self.nota(f"lote processado após {n} consulta(s)"
                          + (f"; NEGADAS: {', '.join(negadas)} (ver problema no corpo)" if negadas else ""))
                return True
        self.nota("o lote ainda estava em processamento ao fim da espera; os passos seguintes rodaram assim mesmo")
        return False

    # ───────── registro ───────── #

    def _montar(self, id_: str, titulo: str, metodo: str, url: str, headers: dict, corpo_enviado,
                resp: Resposta) -> dict:
        corpo = resp.json()
        if corpo is None and (resp.texto or "").strip():
            corpo = resp.texto[:20000]  # não-JSON (HTML de proxy, por exemplo): guarda o texto
        corpo_m = self.masc.objeto(corpo)
        partes = urllib.parse.urlsplit(url)
        return {
            "id": id_,
            "titulo": titulo,
            "metodo": metodo,
            # Legível (query decodificada); a `url` abaixo fica exatamente como foi enviada.
            "endpoint": self.masc.texto(urllib.parse.unquote(
                partes.path + (f"?{partes.query}" if partes.query else ""))),
            "url": self.masc.texto(url),
            "horario": resp.horario.astimezone(FUSO).isoformat(timespec="milliseconds"),
            "duracao_ms": resp.duracao_ms,
            "status_retornado": resp.status,
            "x_request_id": self.masc.texto(_request_id(resp)),
            "headers_relevantes": {k: self.masc.texto(v) for k, v in resp.headers.items() if k in HEADERS_RELEVANTES},
            "headers_enviados": self.masc.objeto(headers),
            "corpo_enviado": self.masc.objeto(corpo_enviado),
            "corpo_retornado": corpo_m,
            "corpo_retornado_indentado": _indentar(corpo_m),
            "erro_de_rede": self.masc.texto(resp.erro),
        }

    def _base(self, t: Teste) -> dict:
        return {
            "id": t.id,
            "titulo": t.titulo,
            "metodo": t.metodo,
            "endpoint_roteiro": t.endpoint_roteiro,
            "tabela_docx": t.tabela_docx,
            "rotulo_docx": t.rotulo_docx or t.id,
            "status_esperado": {"roteiro": t.status_roteiro, "spec": t.status_spec},
        }

    def _registrar_pulado(self, t: Teste, motivo: str) -> dict:
        reg = {
            **self._base(t),
            "status_retornado": None,
            "confere": False,
            "situacao": "pulado",
            "motivo": self.masc.texto(motivo),
            "observacoes": [self.masc.texto(n) for n in self._notas],
            "nota_roteiro": "",
            "horario": self.relogio().astimezone(FUSO).isoformat(timespec="milliseconds"),
            "corpo_retornado": None,
            "corpo_retornado_indentado": "",
            "x_request_id": "",
        }
        self.testes.append(reg)
        self.sucesso[t.id] = False
        self._imprimir(reg)
        return reg

    def executar_teste(self, t: Teste) -> dict:
        self._teste, self._atual, self._notas, self._notas_roteiro = t, None, [], []
        faltam = [d for d in t.depende if not self.sucesso.get(d)]
        if faltam:
            return self._registrar_pulado(t, f"depende de {', '.join(faltam)}, que não teve sucesso nesta execução")
        try:
            t.funcao(self)
        except Pulado as exc:
            if self._atual is None:
                return self._registrar_pulado(t, str(exc))
            self.nota(str(exc))
        if self._atual is None:  # pragma: no cover - todo teste chama principal() ou levanta Pulado
            return self._registrar_pulado(t, "o teste não fez a chamada principal")
        a = self._atual
        reg = {**self._base(t), **self._montar(t.id, t.titulo, a["metodo"], a["url"], a["headers"],
                                               a["corpo"], a["resp"])}
        status = a["resp"].status
        self.sucesso[t.id] = status is not None and 200 <= status < 300
        aceitos = {t.status_roteiro} if self.args.estrito else {t.status_roteiro, t.status_spec}
        reg["confere"] = status in aceitos
        if status is None:
            reg["situacao"] = "erro_de_rede"
        else:
            reg["situacao"] = "ok" if reg["confere"] else "divergente"
        if status is not None and status != t.status_roteiro and status == t.status_spec:
            self.nota(f"HTTP {status} é o status da spec; o roteiro pede {t.status_roteiro} "
                      "(divergência conhecida, ver 01-mapa-roteiro.md)"
                      + (" e --estrito não aceita" if self.args.estrito else ""))
            self.nota_roteiro(f"Status {status} conforme a especificação da API Pix (pix-api.yaml v3.0.1) "
                              f"para este endpoint; o roteiro indica {t.status_roteiro}.")
        reg["motivo"] = ""
        reg["observacoes"] = [self.masc.texto(n) for n in self._notas]
        reg["nota_roteiro"] = self.masc.texto(" ".join(self._notas_roteiro))
        self.testes.append(reg)
        self._imprimir(reg)
        return reg

    def _imprimir(self, reg: dict) -> None:
        esperado = reg["status_esperado"]
        esp = str(esperado["roteiro"]) if esperado["roteiro"] == esperado["spec"] else \
            f"{esperado['roteiro']}/{esperado['spec']}"
        situacao = {"ok": "ok", "divergente": "DIVERGENTE", "pulado": "PULADO", "erro_de_rede": "ERRO DE REDE"}
        linha = (f"[{reg['id']:<7}] {reg['metodo']:<6} {reg.get('endpoint') or reg['endpoint_roteiro']}"
                 f"  -> {reg['status_retornado'] if reg['status_retornado'] is not None else '---'}"
                 f" (esperado {esp})  {situacao[reg['situacao']]}")
        if reg.get("x_request_id"):
            linha += f"  x-request-id={reg['x_request_id']}"
        print(self.masc.texto(linha))
        if reg.get("motivo"):
            print(f"           motivo: {reg['motivo']}")
        if reg.get("erro_de_rede"):
            print(f"           erro: {reg['erro_de_rede']}")

    # ───────── laço principal ───────── #

    def rodar(self) -> int:
        abortado = ""
        for t in PLANO:
            if abortado:
                self._teste, self._notas, self._notas_roteiro = t, [], []
                self._registrar_pulado(t, abortado)
                continue
            self.executar_teste(t)
            if t.id == "AT_01":
                if not self.sucesso.get("AT_01"):
                    abortado = "AT_01 falhou: sem token não há teste Pix (ver o corpo retornado do AT_01)"
                elif self.escopos_faltando and not self.args.ignorar_escopos:
                    abortado = ("a credencial veio sem os escopos " + " ".join(self.escopos_faltando)
                                + "; peça ao C6 antes de gastar a janela (ou rode com --ignorar-escopos)")
        self.fim = self.relogio()
        if abortado:
            self.codigo = 4
        else:
            self.codigo = 0 if all(r["confere"] for r in self.testes) else 1
        return self.codigo

    # ───────── saída ───────── #

    @staticmethod
    def _item_contrato(t: dict) -> dict:
        """Um item {id, status, corpo, nota, erro} do formato-resultados.md."""
        item = {"id": t["id"], "status": t["status_retornado"], "corpo": t["corpo_retornado"]}
        if t.get("nota_roteiro"):
            item["nota"] = t["nota_roteiro"]
        if t["status_retornado"] is None:
            item["erro"] = t.get("motivo") or t.get("erro_de_rede") or "sem resposta HTTP"
        item.update({
            "metodo": t["metodo"],
            "url": t.get("endpoint") or t["endpoint_roteiro"],  # sem host; segredos já mascarados
            "executado_em": t["horario"],
            "duracao_ms": t.get("duracao_ms"),
            "x_request_id": t.get("x_request_id") or "",
        })
        return item

    def documento(self) -> dict:
        contagem = {s: sum(1 for r in self.testes if r["situacao"] == s)
                    for s in ("ok", "divergente", "pulado", "erro_de_rede")}
        aviso = ""
        if self.cfg.modo == "simulador":
            aviso = "SIMULADOR LOCAL: estes retornos NÃO são do C6 e NÃO servem para o roteiro."
        elif self.cfg.modo == "producao":
            aviso = "PRODUÇÃO: chamadas na API de produção do C6."
        inicio = self.inicio.astimezone(FUSO).isoformat(timespec="seconds")
        return {
            # ── contrato com scripts/c6_preencher_roteiro.py (docs/c6-homologacao/formato-resultados.md) ──
            "executado_em": inicio,
            "ambiente": self.cfg.modo,
            "simulado": self.cfg.modo == "simulador",
            "host": self.cfg.host,
            "executor": f"scripts/c6_homologacao.py v{VERSAO}",
            "resultados": [self._item_contrato(t) for t in self.testes],
            # ── detalhe completo deste executor ──
            "roteiro": "Roteiro de Testes - C6 Developers v3.0 (AT_01 + P_01_01..P_06_03)",
            "modo": self.cfg.modo,
            "aviso": aviso,
            "inicio": inicio,
            "fim": (self.fim or self.relogio()).astimezone(FUSO).isoformat(timespec="seconds"),
            "janela_sandbox": self.janela,
            "estrito": bool(self.args.estrito),
            "chave_pix": self.masc.texto(self.cfg.chave_pix),
            "escopos_recebidos": self.escopos,
            "escopos_faltando": self.escopos_faltando if self.sucesso.get("AT_01") else list(ESCOPOS_EXIGIDOS),
            "resumo": {"total": len(self.testes), **contagem, "codigo_de_saida": self.codigo},
            "testes": self.testes,
            "auxiliares": self.auxiliares,
        }


def resumo_md(doc: dict) -> str:
    r = doc["resumo"]
    linhas = [
        f"# Homologação C6: execução de {doc['inicio']} ({doc['modo']})",
        "",
    ]
    if doc["aviso"]:
        linhas += [f"> **{doc['aviso']}**", ""]
    linhas += [
        f"- Host: `{doc['host']}`",
        f"- Início: {doc['inicio']} · fim: {doc['fim']}",
        f"- Janela do sandbox: {doc['janela_sandbox'] or 'não se aplica'}",
        f"- Chave Pix: `{doc['chave_pix']}`",
        f"- Escopos recebidos: {len(doc['escopos_recebidos'])} de {len(ESCOPOS_EXIGIDOS)}"
        + (f" (faltando: {' '.join(doc['escopos_faltando'])})" if doc["escopos_faltando"] else ""),
        f"- Resultado: **{r['ok']} ok, {r['divergente']} divergentes, {r['pulado']} pulados, "
        f"{r['erro_de_rede']} erros de rede**. Código de saída: {r['codigo_de_saida']}"
        + (" (modo --estrito: só o status do roteiro conta)" if doc["estrito"] else
           " (aceita o status do roteiro ou o da spec, ver 01-mapa-roteiro.md)"),
        "",
        "| Teste | Método | Endpoint chamado | Esperado (roteiro/spec) | Retornado | Situação | x-request-id |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in doc["testes"]:
        esp = t["status_esperado"]
        linhas.append(
            f"| {t['id']} | {t['metodo']} | `{t.get('endpoint') or t['endpoint_roteiro']}` | "
            f"{esp['roteiro']}/{esp['spec']} | {t['status_retornado'] if t['status_retornado'] is not None else '-'} | "
            f"{t['situacao']} | {t.get('x_request_id') or '-'} |"
        )
    linhas += [
        "",
        "## Para colar no roteiro",
        "",
        "Cada bloco vai na linha R2 da tabela \"Status Code - Retornado | Response Body - Retornado\" do teste. "
        "Localize a tabela pela POSIÇÃO (nº = índice 0-based de `w:tbl` em `word/document.xml`), nunca pelo "
        "rótulo: o .docx do C6 tem rótulos trocados. Trabalhe numa CÓPIA do .docx.",
        "",
    ]
    for t in doc["testes"]:
        rotulo = f" · rotulado \"TESTE: {t['rotulo_docx']}\" no .docx" if t["rotulo_docx"] != t["id"] else ""
        linhas += [f"### {t['id']} · {t['titulo']} · tabela nº {t['tabela_docx']}{rotulo}", ""]
        if t["situacao"] == "pulado":
            linhas += [f"**Não executado.** {t['motivo']}", ""]
            continue
        corpo = t["corpo_retornado_indentado"] or "(sem corpo)"
        if len(corpo) > 6000:
            corpo = corpo[:6000] + "\n... (truncado aqui; o corpo inteiro está no .json)"
        linhas += [
            f"**Status Code - Retornado:** {t['status_retornado'] if t['status_retornado'] is not None else 'sem resposta'}",
            "",
            f"**Response Body - Retornado** ({t['horario']}, x-request-id {t.get('x_request_id') or '-'}):",
            "",
            "```json",
            corpo,
            "```",
            "",
        ]
        if t.get("nota_roteiro"):
            linhas += [f"**Obs. (vai para o roteiro):** {t['nota_roteiro']}", ""]
        for obs in t.get("observacoes") or []:
            linhas.append(f"- {obs}")
        if t.get("observacoes"):
            linhas.append("")
    pend = [t for t in doc["testes"] if t["situacao"] != "ok"]
    linhas += ["## Pendências", ""]
    if pend:
        for t in pend:
            detalhe = t.get("motivo") or (t.get("erro_de_rede") or f"retornou {t['status_retornado']}")
            linhas.append(f"- **{t['id']}** ({t['situacao']}): {detalhe}")
    else:
        linhas.append("- Nenhuma: os 25 status conferiram.")
    linhas += ["", "## Chamadas auxiliares (não entram no roteiro)", ""]
    if doc["auxiliares"]:
        linhas += ["| Horário | Do teste | O quê | Método | Endpoint | Status | x-request-id |", "|---|---|---|---|---|---|---|"]
        for a in doc["auxiliares"]:
            linhas.append(f"| {a['horario']} | {a['id']} | {a['titulo']} | {a['metodo']} | `{a['endpoint']}` | "
                          f"{a['status_retornado'] if a['status_retornado'] is not None else '-'} | "
                          f"{a.get('x_request_id') or '-'} |")
    else:
        linhas.append("- Nenhuma.")
    linhas.append("")
    return "\n".join(linhas)


def gravar_resultados(ex: Execucao, pasta: Path) -> tuple[Path, Path]:
    doc = ex.documento()
    texto_json = ex.masc.texto(json.dumps(doc, ensure_ascii=False, indent=2))
    texto_md = ex.masc.texto(resumo_md(doc))
    vazou = ex.masc.vazamentos(texto_json) + ex.masc.vazamentos(texto_md)
    if vazou:
        # Nunca deveria acontecer; se acontecer, melhor não gravar do que gravar segredo.
        raise RuntimeError("recusei gravar os resultados: sobrou segredo sem máscara ("
                           + ", ".join(sorted(set(vazou))) + ")")
    pasta.mkdir(parents=True, exist_ok=True)
    carimbo = ex.inicio.astimezone(FUSO).strftime("%Y-%m-%d_%H%M%S")
    sufixo = {"simulador": "-simulador", "producao": "-producao"}.get(ex.cfg.modo, "")
    base = pasta / f"{carimbo}{sufixo}"
    n = 2
    while base.with_suffix(".json").exists():
        base = pasta / f"{carimbo}{sufixo}-{n}"
        n += 1
    caminho_json, caminho_md = base.with_suffix(".json"), base.with_suffix(".md")
    caminho_json.write_text(texto_json + "\n", encoding="utf-8")
    caminho_md.write_text(texto_md, encoding="utf-8")
    return caminho_json, caminho_md


# ═════════════════════════════ main ═════════════════════════════ #

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Executor da homologação Pix do C6 (Roteiro v3.0). Ver docs/c6-homologacao/03-executor.md.")
    ap.add_argument("--simulador", action="store_true",
                    help="sobe o simulador local e roda contra ele (sem rede, sem credencial, sem mTLS)")
    ap.add_argument("--simulador-url", metavar="URL",
                    help="usa um simulador já rodando (só http://127.0.0.1 ou http://localhost)")
    ap.add_argument("--producao", action="store_true",
                    help="permite produção; exige também C6_ENV=producao")
    ap.add_argument("--checar", action="store_true",
                    help="só confere janela, variáveis e certificado; não chama o C6")
    ap.add_argument("--estrito", action="store_true",
                    help="só o status do roteiro conta (por padrão o da spec também é aceito nos 4 divergentes)")
    ap.add_argument("--ignorar-janela", action="store_true",
                    help="não confere seg-sex 7h-22h (só se o C6 informar outro horário)")
    ap.add_argument("--ignorar-escopos", action="store_true",
                    help="segue mesmo se o token vier sem algum dos 12 escopos")
    ap.add_argument("--manter-webhook", action="store_true",
                    help="recadastra o webhook depois do P_06_03 (chamada auxiliar)")
    ap.add_argument("--expor-chave-pix", action="store_true",
                    help="não mascara a chave Pix (decisão do dono, L6 do mapa)")
    ap.add_argument("--e2eid", default="", help="endToEndId de um Pix pago no sandbox (P_05); ou C6_E2EID")
    ap.add_argument("--saida", type=Path, default=PASTA_RESULTADOS, help="pasta dos resultados")
    ap.add_argument("--horas-lista", type=int, default=24,
                    help="as consultas de lista cobrem as últimas N horas (padrão 24)")
    ap.add_argument("--intervalo-lote", type=float, default=None,
                    help="segundos entre consultas do lote (padrão 3; 0.2 no simulador)")
    ap.add_argument("--tentativas-lote", type=int, default=10, help="consultas do lote antes de seguir (padrão 10)")
    ap.add_argument("--timeout", type=float, default=30.0, help="timeout de cada chamada, em segundos")
    return ap


def _iniciar_simulador():
    if str(AQUI) not in sys.path:
        sys.path.insert(0, str(AQUI))
    import c6_simulador  # noqa: PLC0415 - só no modo simulador

    return c6_simulador.Simulador(0).iniciar()


def main(argv=None, *, env=None, relogio: Callable[[], datetime] | None = None,
         dormir: Callable[[float], None] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    relogio = relogio or (lambda: datetime.now(timezone.utc))
    dormir = dormir or time.sleep

    print("=" * 78)
    print("HOMOLOGAÇÃO C6 BANK · Roteiro v3.0 · AT_01 + P_01_01..P_06_03")
    print("=" * 78)

    modo, erro = escolher_modo(args, env)
    if erro:
        print(f"RECUSADO: {erro}.")
        return 3
    if args.intervalo_lote is None:
        args.intervalo_lote = 0.2 if modo == "simulador" else 3.0

    simulador = None
    try:
        if modo == "simulador":
            if args.simulador_url:
                url = args.simulador_url
            else:
                simulador = _iniciar_simulador()
                url = simulador.url
            cfg = config_simulador(url)
            print(f"[modo   ] SIMULADOR LOCAL em {cfg.host}: dados e credenciais fictícios; "
                  "as variáveis C6_* do ambiente são ignoradas")
        else:
            cfg = config_de_env(env, modo)
            print(f"[modo   ] {modo.upper()} em {cfg.host}")
            if modo == "producao":
                print("[modo   ] ATENÇÃO: produção cria cobranças e devolução de verdade")
        if args.e2eid:
            cfg.e2eid = args.e2eid.strip()

        masc = Mascarador(expor_chave_pix=args.expor_chave_pix)
        masc.segredo(cfg.client_secret)
        masc.parcial(cfg.client_id, "client_id")
        masc.chave_pix(cfg.chave_pix)
        for tok in {token_da_url_webhook(cfg.webhook_url), cfg.webhook_token_extra}:
            if tok:
                masc.segredo(tok, f"<TOKEN:{len(tok)}ch>")
        for pem in (cfg.cert_pem, cfg.key_pem):
            masc.segredo(pem.strip(), "<PEM removido>")

        janela = ""
        if modo == "sandbox":
            ok, janela = dentro_da_janela(relogio())
            print(f"[janela ] {janela}")
            if not ok and not args.ignorar_janela:
                print("\nSAINDO: fora da janela do sandbox o C6 responde 503 e o resultado não vale para o roteiro.")
                print("Rode de novo de segunda a sexta, entre 7h e 22h (Brasília).")
                return 2
            if not ok:
                print("[janela ] --ignorar-janela: seguindo mesmo assim")

        itens = conferir_config(cfg)
        faltando = [(n, d) for n, ok, d in itens if not ok]
        print("[config ] " + "  ".join(f"{n}={'ok' if ok else 'FALTA'}" for n, ok, _ in itens))
        if faltando:
            print("\nSAINDO: configuração incompleta. Falta ou está inválido:")
            for nome, detalhe in faltando:
                print(f"  - {nome}: {detalhe}")
            print("\nExporte as variáveis no terminal (ver docs/c6-homologacao/03-executor.md). "
                  "Não cole segredo em chat nem em arquivo versionado.")
            return 3

        contexto = None
        if modo != "simulador":
            try:
                contexto, resumo_cert = montar_mtls(cfg, relogio())
            except C6Error as exc:
                print(f"\nSAINDO: certificado/chave com problema: {masc.texto(str(exc))}")
                return 3
            print(f"[mTLS   ] par certificado/chave confere: {resumo_cert}")

        if args.checar:
            print("\n--checar: configuração OK. Nenhuma chamada ao C6 foi feita.")
            return 0

        cliente = ClienteHttp(cfg.host, contexto, timeout=args.timeout, sem_proxy=(modo == "simulador"))
        execucao = Execucao(cfg, cliente, masc, args, relogio, dormir, janela)
        print()
        codigo = execucao.rodar()
        try:
            caminho_json, caminho_md = gravar_resultados(execucao, args.saida)
        except RuntimeError as exc:
            print(f"\nERRO: {exc}")
            return 3
        r = execucao.documento()["resumo"]
        print()
        print(f"Resultado: {r['ok']} ok, {r['divergente']} divergentes, {r['pulado']} pulados, "
              f"{r['erro_de_rede']} erros de rede, de {r['total']}.")
        print(f"JSON:   {caminho_json}")
        print(f"Resumo: {caminho_md}")
        if modo == "simulador":
            print("Lembrete: execução contra o SIMULADOR. Nada disto vai para o roteiro.")
        print(f"Código de saída: {codigo}")
        return codigo
    finally:
        if simulador is not None:
            simulador.parar()


if __name__ == "__main__":
    sys.exit(main())
