"""Provider de PIX de ENTRADA (cobrança) via C6 Bank, API Pix no padrão Bacen.

Por que existe: o Asaas parou de responder e a BlaXx tem conta PJ no C6, que
expõe a API Pix do arranjo (`PUT /cob/{txid}`) sob OAuth2 client_credentials
com mTLS. Este provider cobre SÓ a entrada de dinheiro. Payout (PIX de saída,
resgate) não é implementado: com PIX_PROVIDER=c6 o factory força
PAYOUT_MODE=manual (fila admin), ver app/__init__.py.

── Autenticação ────────────────────────────────────────────────────────────
`POST {host}/v1/auth/` (form: client_id, client_secret,
grant_type=client_credentials), obrigatoriamente com o certificado de cliente
(.crt/.key) emitido pelo C6, porque a API exige TLS mútuo. Devolve
`access_token` Bearer, `expires_in` (300 s) e os escopos da credencial.
O token fica em memória, é renovado 30 s antes de expirar e, se a API
responder 401, renovamos uma vez e repetimos a chamada: tudo o que este
provider faz é idempotente, então repetir é seguro.

── Idempotência (o ponto que protege o caixa) ──────────────────────────────
A cobrança é criada com `PUT /cob/{txid}`, onde txid é o NOSSO id (UNIQUE em
pix_charges). Repetir o PUT com o mesmo txid não cobra o cliente duas vezes:
o C6 recusa a duplicata e nós recuperamos a cobrança com `GET /cob/{txid}`.
Timeout no PUT nunca vira erro definitivo antes de uma consulta.

── Webhook ─────────────────────────────────────────────────────────────────
`PUT /webhook/{chave}` registra a URL; o C6 chama `{url}/pix` com
`{"pix": [{endToEndId, txid, valor, horario, ...}]}` quando um Pix com txid
é recebido. O handler (app/api/c6_webhook.py) trata o payload como GATILHO e
reconsulta `GET /cob/{txid}` antes de creditar.

Docs: https://developers.c6bank.com.br/apis/auth
      https://developers.c6bank.com.br/apis/pix
      https://developers.c6bank.com.br/apis/webhook
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from .mock import _render_qr_data_uri
from .provider import (
    PixChargeRequest,
    PixChargeResponse,
    PixPayoutRequest,
    PixPayoutResponse,
    PixProvider,
)

log = logging.getLogger("blaxx.pix.c6")

API_HOST_PROD = "https://baas-api.c6bank.info"
API_HOST_SANDBOX = "https://baas-api-sandbox.c6bank.info"
AUTH_PATH = "/v1/auth/"
PIX_PATH = "/v2/pix"

# Status da cobrança no C6 (padrão Bacen) → vocabulário interno.
_STATUS_MAP = {
    "ATIVA": "pending",
    "CONCLUIDA": "paid",
    "REMOVIDA_PELO_USUARIO_RECEBEDOR": "cancelled",
    "REMOVIDA_PELO_PSP": "cancelled",
}

_ONLY_DIGITS = re.compile(r"\D")

# txid do padrão Bacen. Validado ANTES de entrar no path da API: o txid do
# corpo do webhook é entrada externa, e sem esta checagem um valor como
# "../webhook/{chave}" faria o nosso backend chamar outro endpoint do C6
# usando o nosso certificado mTLS.
_TXID_RE = re.compile(r"^[a-zA-Z0-9]{26,35}$")

# Renova o token com esta folga antes do expires_in informado pelo C6.
_TOKEN_SAFETY_SECONDS = 30


class C6Error(RuntimeError):
    """Erro de comunicação/negócio com o C6."""


class _IndeterminateError(RuntimeError):
    """Timeout/rede: não sabemos se a requisição chegou ao C6."""


def resolve_cert_paths(
    cert_path: str = "",
    key_path: str = "",
    cert_pem: str = "",
    key_pem: str = "",
) -> tuple[str, str]:
    """Devolve (cert_path, key_path) prontos para `load_cert_chain`.

    Aceita caminhos de arquivo OU o conteúdo PEM inline (é assim que o Render
    entrega segredos multilinha). PEM inline vai para arquivo temporário com
    permissão 0600, porque `ssl` só carrega certificado de arquivo.
    """
    cert_path = (cert_path or "").strip()
    key_path = (key_path or "").strip()
    if cert_path and key_path:
        for p in (cert_path, key_path):
            if not os.path.isfile(p):
                raise C6Error(f"C6: arquivo de certificado não encontrado: {p}")
        return cert_path, key_path

    cert_pem = _normalize_pem(cert_pem)
    key_pem = _normalize_pem(key_pem)
    if not (cert_pem and key_pem):
        raise C6Error(
            "C6: informe C6_CERT_PATH+C6_KEY_PATH ou C6_CERT_PEM+C6_KEY_PEM "
            "(mTLS é obrigatório na API do C6)."
        )
    return _pem_to_tempfile(cert_pem, ".crt"), _pem_to_tempfile(key_pem, ".key")


def _normalize_pem(value: str) -> str:
    """Devolve um PEM válido a partir do que quer que o painel tenha guardado.

    Um PEM colado num dashboard chega estragado de várias formas, e todas dão
    o mesmo erro opaco (`[SSL] PEM lib`) lá na frente, no load_cert_chain:
    campo de linha única transforma as quebras em espaço, alguns painéis
    envolvem o valor em aspas, outros indentam, e o shell costuma entregar
    "\\n" literal. Reconstruir aqui custa nada e evita um deploy quebrado com
    mensagem que não explica a causa.
    """
    v = (value or "").strip()
    if not v:
        return ""
    # Aspas que o painel (ou o shell) colocou em volta do valor inteiro.
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    v = v.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")

    # Reconstrói cada bloco: cabeçalho, corpo base64 em linhas de 64, rodapé.
    # Serve tanto para o PEM achatado quanto para o indentado, e preserva
    # cadeias com mais de um bloco (certificado + intermediários).
    blocos = re.findall(
        r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", v, re.S
    )
    if not blocos:
        # Sem marcadores não há o que reconstruir: devolve como veio e deixa
        # o OpenSSL recusar, com o arquivo intacto para inspeção.
        return v if v.endswith("\n") else v + "\n"

    saida = []
    for rotulo, corpo in blocos:
        b64 = "".join(corpo.split())
        linhas = [b64[i:i + 64] for i in range(0, len(b64), 64)]
        saida.append(
            f"-----BEGIN {rotulo}-----\n" + "\n".join(linhas) + f"\n-----END {rotulo}-----\n"
        )
    return "".join(saida)


def _pem_to_tempfile(pem: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="c6-", suffix=suffix)
    with os.fdopen(fd, "w") as fh:
        fh.write(pem)
    os.chmod(path, 0o600)
    return path


class C6PixProvider(PixProvider):
    """PIX de entrada (cobrança imediata) no C6 Bank."""

    name = "c6"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        pix_key: str,
        *,
        cert_path: str,
        key_path: str,
        sandbox: bool = False,
        timeout: int = 30,
        user_agent: str = "blaxx-pontos-backend",
    ):
        if not (client_id and client_secret):
            raise ValueError("C6: client_id e client_secret obrigatórios")
        if not pix_key:
            raise ValueError("C6: pix_key (chave DICT do recebedor) obrigatória")
        self.client_id = client_id
        self.client_secret = client_secret
        self.pix_key = pix_key.strip()
        self.sandbox = sandbox
        self.host = API_HOST_SANDBOX if sandbox else API_HOST_PROD
        self.timeout = timeout
        self.user_agent = user_agent
        self._ssl = ssl.create_default_context()
        self._ssl.load_cert_chain(certfile=cert_path, keyfile=key_path)
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    # ---------------- HTTP de baixo nível ---------------- #

    def _http(self, method: str, url: str, data: bytes | None, headers: dict) -> tuple[int, str]:
        """Uma requisição HTTPS com o certificado de cliente. Devolve (status, corpo).

        Único ponto que toca a rede: os testes substituem este método.
        """
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
            return exc.code, body
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
            raise _IndeterminateError(str(exc)) from exc

    # ---------------- OAuth2 client_credentials ---------------- #

    def _authenticate(self) -> None:
        form = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }).encode()
        status, body = self._http("POST", self.host + AUTH_PATH, form, {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        })
        if status != 200:
            raise C6Error(f"C6 auth HTTP {status}: {_describe_problem(body)}")
        try:
            data = json.loads(body or "{}")
        except ValueError as exc:
            raise C6Error("C6 auth: resposta não é JSON") from exc
        token = (data.get("access_token") or "").strip()
        if not token:
            raise C6Error("C6 auth: resposta sem access_token")
        ttl = int(data.get("expires_in") or 300)
        self._token = token
        self._token_expires_at = time.monotonic() + max(ttl - _TOKEN_SAFETY_SECONDS, 30)
        scopes = data.get("scope") or ""
        if scopes and "cob.write" not in scopes.split():
            log.error("C6: credencial sem escopo cob.write (escopos: %s); criar cobrança vai falhar", scopes)

    def _bearer(self, *, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() >= self._token_expires_at:
                self._authenticate()
            return self._token

    # ---------------- API Pix ---------------- #

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Chama {host}/v2/pix{path} com Bearer; em 401 renova o token e repete uma vez."""
        url = self.host + PIX_PATH + path
        data = json.dumps(body).encode() if body is not None else None
        token = self._bearer()
        for attempt in (1, 2):
            status, raw = self._http(method, url, data, {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            })
            if status == 401 and attempt == 1:
                token = self._bearer(force=True)
                continue
            if 200 <= status < 300:
                try:
                    return json.loads(raw or "{}")
                except ValueError as exc:
                    raise C6Error(f"C6 {method} {path}: resposta não é JSON") from exc
            raise C6Error(f"HTTP {status}: {_describe_problem(raw)}")
        raise C6Error("C6: não autenticou após renovar o token")  # pragma: no cover

    # ---------------- Cobrança (entrada) ---------------- #

    def create_charge(self, req: PixChargeRequest) -> PixChargeResponse:
        """`PUT /cob/{txid}`: cobrança imediata com o nosso txid, idempotente por construção."""
        _validate_txid(req.txid)
        payload: dict = {
            "calendario": {"expiracao": max(60, int(req.expires_in_seconds or 3600))},
            # modalidadeAlteracao 0 explícito: o pagador NÃO pode alterar o
            # valor. Sem isso dependemos do default do PSP, e um valor menor
            # pago creditaria os pontos cheios.
            "valor": {"original": _brl(req.amount_cents), "modalidadeAlteracao": 0},
            "chave": self.pix_key,
            "solicitacaoPagador": (req.description or "BlaXx, compra de pontos")[:140],
        }
        devedor = _devedor(req.payer_name, req.payer_cpf)
        if devedor:
            payload["devedor"] = devedor

        try:
            cob = self._request("PUT", f"/cob/{req.txid}", payload)
        except _IndeterminateError as exc:
            # A cobrança pode ter sido criada; antes de desistir, consulta.
            log.error("C6 PUT /cob INDETERMINADO txid=%s (%s); consultando", req.txid, exc)
            cob = self._get_cob_or_none(req.txid)
            if cob is None:
                raise C6Error(f"C6: sem resposta ao criar a cobrança ({exc})") from exc
        except C6Error as exc:
            # Duplicata (retry de um PUT que já pegou): reaproveita a existente.
            cob = self._get_cob_or_none(req.txid)
            if cob is None:
                raise exc
            log.warning("C6 txid=%s já existia (%s); reaproveitando", req.txid, exc)

        return self._charge_response(req.txid, cob)

    def _charge_response(self, txid: str, cob: dict) -> PixChargeResponse:
        br_code = (cob.get("pixCopiaECola") or "").strip()
        if not br_code:
            raise C6Error(
                f"C6 não devolveu pixCopiaECola (txid={txid}, status={cob.get('status')}). "
                "A chave PIX configurada pertence à conta da credencial?"
            )
        return PixChargeResponse(
            txid=txid,
            br_code=br_code,
            qr_code_image=_render_qr_data_uri(br_code),
        )

    def _get_cob_or_none(self, txid: str) -> dict | None:
        try:
            return self.get_cob(txid)
        except (C6Error, _IndeterminateError):
            return None

    # ---------------- Consultas ---------------- #

    def get_cob(self, txid: str) -> dict:
        """`GET /cob/{txid}`: fonte de verdade depois do webhook."""
        _validate_txid(txid)
        return self._request("GET", f"/cob/{txid}")

    def get_charge_status(self, txid: str) -> str:
        try:
            cob = self.get_cob(txid)
        except (C6Error, _IndeterminateError) as exc:
            log.warning("C6 consulta txid=%s falhou: %s", txid, exc)
            return "unknown"
        # Devolvida: nunca reportar como paga, mesmo com status CONCLUIDA.
        if self.cob_has_refund(cob) and self.cob_paid_cents(cob) <= 0:
            return "cancelled"
        return _STATUS_MAP.get((cob.get("status") or "").upper(), "unknown")

    @staticmethod
    def cob_paid_cents(cob: dict) -> int:
        """Quanto LIQUIDOU nesta cobrança, em centavos, já descontada devolução.

        Por que não basta `status == CONCLUIDA`: no padrão Bacen a cobrança
        continua CONCLUIDA depois de o Pix ser devolvido (devolução pelo
        recebedor ou MED). Creditar com base no status daria pontos por
        dinheiro que já saiu da conta. Devolução `EM_PROCESSAMENTO` também
        não conta como liquidada: ainda pode virar DEVOLVIDO.
        """
        total = 0
        for item in cob.get("pix") or []:
            if not isinstance(item, dict):
                continue
            total += _cents(item.get("valor"))
            for dev in item.get("devolucoes") or []:
                if isinstance(dev, dict) and (dev.get("status") or "").upper() in (
                    "DEVOLVIDO", "EM_PROCESSAMENTO"
                ):
                    total -= _cents(dev.get("valor"))
        if total <= 0 and (cob.get("status") or "").upper() == "CONCLUIDA" and not cob.get("pix"):
            # CONCLUIDA sem a lista de Pix (alguns PSPs só a trazem sob
            # consulta): cai no valor original da cobrança.
            return _cents((cob.get("valor") or {}).get("original"))
        return max(total, 0)

    @staticmethod
    def cob_has_refund(cob: dict) -> bool:
        """Há devolução registrada (qualquer status que não NAO_REALIZADO)."""
        for item in cob.get("pix") or []:
            if not isinstance(item, dict):
                continue
            for dev in item.get("devolucoes") or []:
                if isinstance(dev, dict) and (dev.get("status") or "").upper() != "NAO_REALIZADO":
                    return True
        return False

    # ---------------- Webhook (registro) ---------------- #

    def register_webhook(self, url: str) -> dict:
        """`PUT /webhook/{chave}`: o C6 passa a chamar `{url}/pix` a cada Pix recebido."""
        return self._request("PUT", f"/webhook/{urllib.parse.quote(self.pix_key)}", {"webhookUrl": url})

    def get_webhook(self) -> dict:
        return self._request("GET", f"/webhook/{urllib.parse.quote(self.pix_key)}")

    def delete_webhook(self) -> dict:
        return self._request("DELETE", f"/webhook/{urllib.parse.quote(self.pix_key)}")

    # ---------------- Payout (não suportado) ---------------- #

    def request_payout(self, req: PixPayoutRequest) -> PixPayoutResponse:
        raise NotImplementedError(
            "C6PixProvider só faz cobrança (entrada). Resgate exige PAYOUT_MODE=manual "
            "ou um payout_provider (Asaas)."
        )


def _validate_txid(txid: str) -> None:
    """Recusa txid fora do padrão Bacen ANTES de ele virar path de URL."""
    if not _TXID_RE.match(txid or ""):
        raise C6Error(f"txid fora do padrão Bacen [a-zA-Z0-9]{{26,35}}: {txid!r}")


def _brl(amount_cents: int) -> str:
    """Valor no formato do Bacen: string decimal com 2 casas ("37.00")."""
    return f"{Decimal(int(amount_cents)) / Decimal(100):.2f}"


def _cents(valor) -> int:
    """"37.00" → 3700. Decimal, nunca float: centavo perdido é dinheiro."""
    try:
        return int((Decimal(str(valor)) * 100).to_integral_value())
    except Exception:  # noqa: BLE001
        return 0


def _devedor(name: str, cpf_cnpj: str) -> dict | None:
    """CPF (11) ou CNPJ (14) só dígitos; nome exige documento e vice-versa."""
    digits = _ONLY_DIGITS.sub("", cpf_cnpj or "")
    nome = (name or "").strip()[:200]
    if not nome:
        return None
    if len(digits) == 11:
        return {"cpf": digits, "nome": nome}
    if len(digits) == 14:
        return {"cnpj": digits, "nome": nome}
    return None


def _describe_problem(raw: str) -> str:
    """Extrai title/detail/violacoes do application/problem+json do C6."""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return (raw or "")[:200]
    if not isinstance(parsed, dict):
        return (raw or "")[:200]
    parts = [str(parsed.get(k)) for k in ("title", "detail") if parsed.get(k)]
    for v in parsed.get("violacoes") or []:
        if isinstance(v, dict):
            parts.append(f"{v.get('propriedade', '?')}: {v.get('razao', '')}")
    if parsed.get("correlation_id"):
        parts.append(f"correlation_id={parsed['correlation_id']}")
    return ("; ".join(parts) or (raw or ""))[:300]
