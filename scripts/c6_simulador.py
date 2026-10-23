#!/usr/bin/env python3
"""Simulador LOCAL da API do C6 Bank (auth + Pix padrão Bacen) para ensaiar a homologação.

NÃO é o C6 e NÃO serve para preencher o roteiro. Existe para provar, sem
credencial e sem rede, que o executor (`scripts/c6_homologacao.py`) encadeia
os 25 testes do Roteiro v3.0 (AT_01 + P_01_01..P_06_03), lê os ids certos de
cada resposta e grava tudo mascarado.

As respostas seguem o formato das specs oficiais baixadas em 22/10/2026
(`authentication-api.yaml`, `pix-api.yaml` v3.0.1):

  · mesmos campos dos exemplos da spec (cob, cobv, lote, loc, pix, devolução,
    webhook) e listas com `parametros.paginacao`;
  · os status da SPEC, que divergem do roteiro em P_01_02 (201), P_01_03 (200),
    P_03_01 (202) e P_03_02 (202);
  · erros em application/problem+json no padrão Bacen
    (`https://pix.bcb.gov.br/api/v2/error/...`, `violacoes`, `correlationId`);
  · header `x-request-id` em toda resposta;
  · escopo conferido por recurso (403 sem ele), token de 300 s (401 sem ele).

Diferenças de propósito para o C6 real: sem TLS/mTLS, qualquer client_id e
client_secret são aceitos, o lote fica pronto depois de um atraso fixo e há
um Pix PAGO semeado (no sandbox real não existe forma documentada de pagar
uma cobrança; é o bloqueador L7 do mapa). Todos os dados são fictícios.

Escuta só em 127.0.0.1.

Uso:
    python3 scripts/c6_simulador.py                    # 127.0.0.1:8765
    python3 scripts/c6_simulador.py --porta 0          # porta livre qualquer
    python3 scripts/c6_simulador.py --fora-da-janela   # 503 em tudo, como o sandbox fora do horário
    python3 scripts/c6_simulador.py --sem-pix-pago     # sem e2eid: P_05_01/03/04 ficam sem Pix

    # em outro terminal:
    python3 scripts/c6_homologacao.py --simulador-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import secrets
import string
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ESCOPOS_PADRAO = (
    "cob.read", "cob.write", "cobv.read", "cobv.write",
    "lotecobv.read", "lotecobv.write",
    "payloadlocation.read", "payloadlocation.write",
    "pix.read", "pix.write", "webhook.read", "webhook.write",
)

_TXID = re.compile(r"^[a-zA-Z0-9]{26,35}$")
_E2E = re.compile(r"^[a-zA-Z0-9]{32}$")
_ID_LIVRE = re.compile(r"^[a-zA-Z0-9]{1,35}$")
_VALOR = re.compile(r"^\d{1,10}\.\d{2}$")
_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ERRO = "https://pix.bcb.gov.br/api/v2/error/"
_DOMINIO_QR = "qr.simulador-c6.invalid"
HOSTS_LOCAIS = ("127.0.0.1", "localhost")

# Recebedor de mentira: o C6 real devolve os dados da conta da credencial.
RECEBEDOR_FICTICIO = {
    "logradouro": "Endereco ficticio do simulador, 0",
    "cidade": "Sao Paulo",
    "uf": "SP",
    "cep": "00000000",
    "cnpj": "00000000000000",
    "nome": "RECEBEDOR FICTICIO DO SIMULADOR",
}

# Recurso da URL → prefixo do escopo exigido (spec: security.OAuth2 de cada rota).
_ESCOPO_DO_RECURSO = {
    "cob": "cob", "cobv": "cobv", "lotecobv": "lotecobv",
    "loc": "payloadlocation", "pix": "pix", "webhook": "webhook",
}


class ErroApi(Exception):
    """Erro no formato problem+json do Bacen."""

    def __init__(self, status: int, tipo: str, titulo: str, detalhe: str = "",
                 violacoes: list[tuple[str, str]] | None = None):
        super().__init__(titulo)
        self.status = status
        self.tipo = tipo
        self.titulo = titulo
        self.detalhe = detalhe
        self.violacoes = violacoes or []

    def corpo(self, correlacao: str | None = None) -> dict:
        c: dict = {"type": _ERRO + self.tipo, "title": self.titulo, "status": self.status}
        if self.detalhe:
            c["detail"] = self.detalhe
        if correlacao:
            c["correlationId"] = correlacao
        if self.violacoes:
            c["violacoes"] = [{"razao": r, "propriedade": p} for p, r in self.violacoes]
        return c


def _nao_encontrado(detalhe: str = "Entidade não encontrada.") -> ErroApi:
    return ErroApi(404, "NaoEncontrado", "Não Encontrado", detalhe)


def _invalida(detalhe: str, violacoes=None) -> ErroApi:
    return ErroApi(400, "RequisicaoInvalida", "Requisição inválida.", detalhe, violacoes)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ler_data_hora(valor: str) -> datetime:
    """RFC 3339 com fuso obrigatório (o `Z` vira +00:00)."""
    v = (valor or "").strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError("sem fuso")
    return dt


def _alnum(n: int) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _e2e(prefixo: str = "E", quando: datetime | None = None) -> str:
    """E2E/rtrId do Bacen: letra + ISPB (8) + AAAAMMDDHHMM (12) + 11 alfanuméricos = 32."""
    q = (quando or _agora()).astimezone(timezone.utc)
    return prefixo + "00000000" + q.strftime("%Y%m%d%H%M") + _alnum(11)


def _crc16(s: str) -> str:
    crc = 0xFFFF
    for b in s.encode("utf-8"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _br_code(location: str, valor: str) -> str:
    """BR Code FICTÍCIO no formato EMV do Pix dinâmico (URL do payload no campo 26)."""
    mai = "0014br.gov.bcb.pix" + f"25{len(location):02d}{location}"
    nome = "RECEBEDOR FICTICIO"
    corpo = (
        "000201" "010212"
        f"26{len(mai):02d}{mai}"
        "52040000" "5303986"
        f"54{len(valor):02d}{valor}"
        "5802BR" f"59{len(nome):02d}{nome}" "6009SAO PAULO"
        "62070503***" "6304"
    )
    return corpo + _crc16(corpo)


def _publico(obj):
    """Cópia sem as chaves internas (prefixo `_`), segura para serializar fora do lock."""
    if isinstance(obj, dict):
        return {k: _publico(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_publico(v) for v in obj]
    return copy.deepcopy(obj)


def _valor_ok(v) -> bool:
    if not (isinstance(v, str) and _VALOR.match(v)):
        return False
    try:
        return Decimal(v) > 0
    except InvalidOperation:
        return False


def _data_ok(v) -> bool:
    if not (isinstance(v, str) and _DATA.match(v)):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def _validar_devedor(d, v: list, prefixo: str, *, endereco: bool) -> None:
    if not isinstance(d, dict):
        v.append((f"{prefixo}.devedor", f"O objeto {prefixo}.devedor não respeita o _schema_."))
        return
    if not (isinstance(d.get("nome"), str) and d["nome"].strip()):
        v.append((f"{prefixo}.devedor.nome", "O campo nome é obrigatório."))
    docs = [k for k in ("cpf", "cnpj") if k in d]
    if len(docs) != 1:
        v.append((f"{prefixo}.devedor", "Informe cpf OU cnpj (exatamente um)."))
    elif docs[0] == "cpf" and not re.fullmatch(r"\d{11}", str(d["cpf"])):
        v.append((f"{prefixo}.devedor.cpf", "O campo cpf deve ter 11 dígitos."))
    elif docs[0] == "cnpj" and not re.fullmatch(r"\d{14}", str(d["cnpj"])):
        v.append((f"{prefixo}.devedor.cnpj", "O campo cnpj deve ter 14 dígitos."))
    if endereco:
        for campo in ("logradouro", "cidade"):
            if not (isinstance(d.get(campo), str) and d[campo].strip()):
                v.append((f"{prefixo}.devedor.{campo}", f"O campo {campo} é obrigatório."))
        if not re.fullmatch(r"[A-Z]{2}", str(d.get("uf") or "")):
            v.append((f"{prefixo}.devedor.uf", "O campo uf deve ter 2 letras maiúsculas."))
        if not re.fullmatch(r"\d{8}", str(d.get("cep") or "")):
            v.append((f"{prefixo}.devedor.cep", "O campo cep deve ter 8 dígitos."))


class EstadoC6:
    """Tudo em memória, protegido por um lock (o servidor é multi-thread)."""

    def __init__(self, *, escopos=ESCOPOS_PADRAO, atraso: float = 0.3,
                 semear_pix: bool = True, fora_da_janela: bool = False,
                 ttl_token: int = 300):
        self._lock = threading.RLock()
        self.escopos = escopos.split() if isinstance(escopos, str) else list(escopos)
        self.atraso = atraso
        self.fora_da_janela = fora_da_janela
        self.ttl_token = ttl_token
        self.tokens: dict[str, float] = {}
        self.cobs: dict[str, dict] = {}
        self.cobvs: dict[str, dict] = {}
        self.lotes: dict[str, dict] = {}
        self.locs: dict[int, dict] = {}
        self.pix: dict[str, dict] = {}
        self.webhooks: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self._prox_loc = 7000
        if semear_pix:
            self._semear_pix_pago()

    # ───────────── semente ───────────── #

    def _semear_pix_pago(self) -> None:
        """Uma cobrança CONCLUIDA com um Pix pago há 1 h: é o e2eid do P_05."""
        quando = _agora() - timedelta(hours=1)
        txid = "simuladorpago" + secrets.token_hex(9)
        loc = self._nova_loc("cob", quando)
        loc["txid"] = txid
        pix = {
            "endToEndId": _e2e("E", quando),
            "txid": txid,
            "valor": "0.50",
            "horario": _iso(quando),
            "infoPagador": "Pagamento ficticio semeado pelo simulador",
        }
        self.pix[pix["endToEndId"]] = pix
        self.cobs[txid] = {
            "calendario": {"criacao": _iso(quando - timedelta(minutes=5)), "expiracao": 3600},
            "txid": txid,
            "revisao": 0,
            "loc": {"id": loc["id"], "location": loc["location"], "tipoCob": "cob"},
            "location": loc["location"],
            "status": "CONCLUIDA",
            "valor": {"original": "0.50", "modalidadeAlteracao": 0},
            "chave": "chave-ficticia-da-semente",
            "pixCopiaECola": _br_code(loc["location"], "0.50"),
            "pix": [pix],
        }

    # ───────────── entrada ───────────── #

    def tratar(self, metodo: str, caminho: str, query: dict, bruto: bytes, headers) -> tuple[int, object, dict]:
        with self._lock:
            self.chamadas.append((metodo, caminho))
            if self.fora_da_janela:
                raise ErroApi(
                    503, "ServicoIndisponivel", "Serviço Indisponível",
                    "Serviço não está disponível no momento. Serviço solicitado pode estar "
                    "em manutenção ou fora da janela de funcionamento.",
                )
            if caminho.rstrip("/") == "/v1/auth":
                if metodo != "POST":
                    raise ErroApi(405, "RequisicaoInvalida", "Método não permitido.")
                status, corpo = self._autenticar(bruto, headers.get("Content-Type") or "")
                return status, corpo, {}
            if not caminho.startswith("/v2/pix/"):
                raise _nao_encontrado("Recurso inexistente.")
            self._exigir_token(headers.get("Authorization") or "")
            partes = [urllib.parse.unquote(p) for p in caminho[len("/v2/pix/"):].strip("/").split("/")]
            recurso = partes[0]
            if recurso in _ESCOPO_DO_RECURSO:
                escopo = _ESCOPO_DO_RECURSO[recurso] + (".read" if metodo == "GET" else ".write")
                if escopo not in self.escopos:
                    raise ErroApi(
                        403, "AcessoNegado", "Acesso Negado",
                        f"Requisição de participante autenticado que viola alguma regra de "
                        f"autorização (escopo {escopo} ausente).",
                    )
            corpo = self._json(bruto) if bruto else None
            rota = self._rota(metodo, partes)
            return rota(partes, query, corpo)

    def _json(self, bruto: bytes):
        try:
            return json.loads(bruto.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _invalida(f"O corpo da requisição não é JSON válido ({exc}).") from exc

    def _autenticar(self, bruto: bytes, content_type: str) -> tuple[int, dict]:
        if "application/x-www-form-urlencoded" not in content_type:
            raise _invalida("Content-Type deve ser application/x-www-form-urlencoded.")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(bruto.decode("utf-8", "replace")).items()}
        faltando = [c for c in ("client_id", "client_secret", "grant_type") if not form.get(c)]
        if faltando:
            raise _invalida("Campos obrigatórios ausentes.", [(c, "campo obrigatório") for c in faltando])
        if form["grant_type"] != "client_credentials":
            raise _invalida("grant_type não suportado; use client_credentials.")
        token = secrets.token_urlsafe(48)
        self.tokens[token] = time.monotonic() + self.ttl_token
        return 200, {
            "access_token": token,
            "expires_in": self.ttl_token,
            "token_type": "Bearer",
            "scope": " ".join(self.escopos),
        }

    def _exigir_token(self, authorization: str) -> None:
        partes = authorization.split(" ", 1)
        ok = (
            len(partes) == 2 and partes[0] == "Bearer"
            and partes[1] in self.tokens and time.monotonic() < self.tokens[partes[1]]
        )
        if not ok:
            raise ErroApi(401, "AcessoNegado", "Não autorizado",
                          "Token de acesso ausente, inválido ou expirado.")

    def invalidar_tokens(self) -> None:
        """Faz o próximo request dar 401 (simula o token vencido)."""
        with self._lock:
            self.tokens.clear()

    def _rota(self, metodo: str, partes: list[str]):
        n = len(partes)
        rotas = {
            ("POST", "cob", 1): self._post_cob,
            ("GET", "cob", 1): lambda p, q, c: self._listar(q, self.cobs.values(), "cobs",
                                                          lambda x: x["calendario"]["criacao"]),
            ("PUT", "cob", 2): lambda p, q, c: self._criar_cob(p[1], c),
            ("PATCH", "cob", 2): self._patch_cob,
            ("GET", "cob", 2): lambda p, q, c: (200, _publico(self._achar(self.cobs, p[1])), {}),
            ("GET", "cobv", 1): lambda p, q, c: self._listar(q, self.cobvs.values(), "cobs",
                                                           lambda x: x["calendario"]["criacao"]),
            ("PUT", "cobv", 2): lambda p, q, c: (201, _publico(self._criar_cobv(p[1], c)), {}),
            ("PATCH", "cobv", 2): lambda p, q, c: (200, _publico(self._revisar_cobv(p[1], c)), {}),
            ("GET", "cobv", 2): lambda p, q, c: (200, _publico(self._achar(self.cobvs, p[1])), {}),
            ("GET", "lotecobv", 1): self._listar_lotes,
            ("PUT", "lotecobv", 2): self._put_lote,
            ("PATCH", "lotecobv", 2): self._patch_lote,
            ("GET", "lotecobv", 2): self._get_lote,
            ("POST", "loc", 1): self._post_loc,
            ("GET", "loc", 1): self._listar_locs,
            ("GET", "loc", 2): lambda p, q, c: (200, _publico(self._achar_loc(p[1])), {}),
            ("DELETE", "loc", 3): self._delete_loc_txid,
            ("GET", "pix", 1): self._listar_pix,
            ("GET", "pix", 2): lambda p, q, c: (200, _publico(self._achar_pix(p[1])), {}),
            ("PUT", "pix", 4): self._put_devolucao,
            ("GET", "pix", 4): self._get_devolucao,
            ("GET", "webhook", 1): self._listar_webhooks,
            ("PUT", "webhook", 2): self._put_webhook,
            ("GET", "webhook", 2): lambda p, q, c: (200, _publico(self._achar(self.webhooks, p[1])), {}),
            ("DELETE", "webhook", 2): self._delete_webhook,
        }
        rota = rotas.get((metodo, partes[0], n))
        if rota is None or (n == 3 and partes[2] != "txid") or (n == 4 and partes[2] != "devolucao"):
            raise _nao_encontrado("Recurso inexistente.")
        return rota

    # ───────────── utilidades ───────────── #

    @staticmethod
    def _achar(tabela: dict, chave: str) -> dict:
        if chave not in tabela:
            raise _nao_encontrado()
        return tabela[chave]

    def _periodo(self, q: dict, *, obrigatorio: bool = True):
        ini, fim = (q.get("inicio") or [""])[0], (q.get("fim") or [""])[0]
        if not (ini and fim):
            if not obrigatorio:
                return None
            raise _invalida(
                "Os parâmetros inicio e fim são obrigatórios.",
                [(k, "parâmetro obrigatório (date-time RFC 3339)") for k, v in (("inicio", ini), ("fim", fim)) if not v],
            )
        try:
            di, df = _ler_data_hora(ini), _ler_data_hora(fim)
        except ValueError as exc:
            raise _invalida("inicio e fim devem ser date-time RFC 3339 com fuso.") from exc
        if di > df:
            raise _invalida("inicio deve ser menor ou igual a fim.")
        return ini, fim, di, df

    def _paginar(self, q: dict, itens: list, nome: str, ini: str, fim: str):
        try:
            pagina = int((q.get("paginacao.paginaAtual") or ["0"])[0] or 0)
            por = int((q.get("paginacao.itensPorPagina") or ["100"])[0] or 100)
        except ValueError as exc:
            raise _invalida("Parâmetros de paginação inválidos.") from exc
        por = max(1, min(por, 1000))
        fatia = itens[pagina * por:(pagina + 1) * por]
        return 200, {
            "parametros": {
                "inicio": ini,
                "fim": fim,
                "paginacao": {
                    "paginaAtual": pagina,
                    "itensPorPagina": por,
                    "quantidadeDePaginas": max(1, -(-len(itens) // por)),
                    "quantidadeTotalDeItens": len(itens),
                },
            },
            nome: [_publico(x) for x in fatia],
        }, {}

    def _listar(self, q, itens, nome, data_de):
        ini, fim, di, df = self._periodo(q)
        selecionados = [x for x in itens if di <= _ler_data_hora(data_de(x)) <= df]
        return self._paginar(q, selecionados, nome, ini, fim)

    # ───────────── locations ───────────── #

    def _nova_loc(self, tipo: str, quando: datetime | None = None) -> dict:
        self._prox_loc += 1
        sub = "cobv/" if tipo == "cobv" else ""
        loc = {
            "id": self._prox_loc,
            "location": f"{_DOMINIO_QR}/v2/{sub}{uuid.uuid4().hex}",
            "tipoCob": tipo,
            "criacao": _iso(quando or _agora()),
        }
        self.locs[loc["id"]] = loc
        return loc

    def _achar_loc(self, bruto: str) -> dict:
        if not re.fullmatch(r"\d{1,19}", bruto or ""):
            raise _nao_encontrado()
        return self._achar(self.locs, int(bruto))

    def _loc_para_vincular(self, corpo: dict, tipo: str, erro_tipo: str):
        if "loc" not in corpo:
            return None
        lid = (corpo.get("loc") or {}).get("id") if isinstance(corpo.get("loc"), dict) else None
        loc = self.locs.get(lid) if isinstance(lid, int) else None
        if loc is None:
            raise ErroApi(400, erro_tipo, "Cobrança inválida.", "A location informada não existe.",
                          [("loc.id", "location inexistente")])
        if loc["tipoCob"] != tipo:
            raise ErroApi(400, erro_tipo, "Cobrança inválida.",
                          f"A location informada é do tipo {loc['tipoCob']}, não {tipo}.")
        if loc.get("txid"):
            raise ErroApi(400, erro_tipo, "Cobrança inválida.",
                          "A location informada já está vinculada a outra cobrança.")
        return loc

    def _post_loc(self, p, q, c):
        if not isinstance(c, dict) or c.get("tipoCob") not in ("cob", "cobv"):
            raise ErroApi(400, "PayloadLocationOperacaoInvalida", "PayloadLocation inválido.",
                          "A presente requisição busca criar uma location sem respeitar o _schema_ estabelecido.",
                          [("tipoCob", "deve ser cob ou cobv")])
        loc = self._nova_loc(c["tipoCob"])
        return 201, _publico(loc), {"location": loc["location"]}

    def _listar_locs(self, p, q, c):
        ini, fim, di, df = self._periodo(q)
        presente = (q.get("txIdPresente") or [""])[0].lower()
        tipo = (q.get("tipoCob") or [""])[0]
        sel = []
        for loc in self.locs.values():
            if not di <= _ler_data_hora(loc["criacao"]) <= df:
                continue
            if presente in ("true", "false") and bool(loc.get("txid")) != (presente == "true"):
                continue
            if tipo and loc["tipoCob"] != tipo:
                continue
            sel.append(loc)
        return self._paginar(q, sel, "loc", ini, fim)

    def _delete_loc_txid(self, p, q, c):
        loc = self._achar_loc(p[1])
        txid = loc.pop("txid", None)
        if not txid:
            raise _nao_encontrado("A location não possui cobrança vinculada.")
        cob = self.cobs.get(txid) or self.cobvs.get(txid)
        if cob is not None:
            cob.pop("loc", None)
            cob.pop("location", None)
        return 200, _publico(loc), {}

    # ───────────── cobrança imediata ───────────── #

    def _validar_cob(self, c, *, parcial: bool = False) -> None:
        if not isinstance(c, dict):
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.",
                          "O corpo da requisição não é um objeto JSON.")
        v: list[tuple[str, str]] = []
        if not parcial or "calendario" in c:
            cal = c.get("calendario")
            if not isinstance(cal, dict):
                v.append(("cob.calendario", "O objeto cob.calendario não respeita o _schema_."))
            elif "expiracao" in cal and (not isinstance(cal["expiracao"], int) or cal["expiracao"] <= 0):
                v.append(("cob.calendario.expiracao", "O campo cob.calendario.expiracao deve ser inteiro positivo."))
        if not parcial or "valor" in c:
            valor = c.get("valor")
            if not (isinstance(valor, dict) and _valor_ok(valor.get("original"))):
                v.append(("cob.valor.original", "O campo cob.valor.original não respeita o _schema_."))
        if not parcial:
            chave = c.get("chave")
            if not (isinstance(chave, str) and 0 < len(chave) <= 77):
                v.append(("cob.chave", "O campo cob.chave não respeita o _schema_."))
        if "devedor" in c:
            _validar_devedor(c["devedor"], v, "cob", endereco=False)
        if "solicitacaoPagador" in c and not (isinstance(c["solicitacaoPagador"], str)
                                              and len(c["solicitacaoPagador"]) <= 140):
            v.append(("cob.solicitacaoPagador", "Até 140 caracteres."))
        if v:
            raise ErroApi(
                400, "CobOperacaoInvalida", "Cobrança inválida.",
                "A requisição que busca alterar ou criar uma cobrança para pagamento imediato "
                "não respeita o _schema_ ou está semanticamente errada.", v,
            )

    def _criar_cob(self, txid: str, c):
        if not _TXID.match(txid or ""):
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.",
                          "O txid não respeita o padrão [a-zA-Z0-9]{26,35}.", [("cob.txid", "formato inválido")])
        if txid in self.cobs or txid in self.cobvs:
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.",
                          "O txid informado já foi utilizado em outra cobrança.", [("cob.txid", "txid duplicado")])
        self._validar_cob(c)
        loc = self._loc_para_vincular(c, "cob", "CobOperacaoInvalida") or self._nova_loc("cob")
        loc["txid"] = txid
        cob: dict = {
            "calendario": {"criacao": _iso(_agora()), "expiracao": int(c["calendario"].get("expiracao", 86400))},
            "txid": txid,
            "revisao": 0,
            "loc": {"id": loc["id"], "location": loc["location"], "tipoCob": "cob"},
            "location": loc["location"],
            "status": "ATIVA",
        }
        if "devedor" in c:
            cob["devedor"] = dict(c["devedor"])
        cob["valor"] = {
            "original": c["valor"]["original"],
            "modalidadeAlteracao": int(c["valor"].get("modalidadeAlteracao", 0)),
        }
        cob["chave"] = c["chave"]
        for campo in ("solicitacaoPagador", "infoAdicionais"):
            if c.get(campo):
                cob[campo] = copy.deepcopy(c[campo])
        cob["pixCopiaECola"] = _br_code(loc["location"], cob["valor"]["original"])
        self.cobs[txid] = cob
        return 201, _publico(cob), {}

    def _post_cob(self, p, q, c):
        return self._criar_cob(uuid.uuid4().hex, c)

    def _patch_cob(self, p, q, c):
        cob = self._achar(self.cobs, p[1])
        if not isinstance(c, dict) or not c:
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.", "Corpo vazio: nada a revisar.")
        permitidos = {"calendario", "devedor", "loc", "valor", "solicitacaoPagador", "infoAdicionais", "status"}
        extras = sorted(set(c) - permitidos)
        if extras:
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.", "Campos não revisáveis.",
                          [(f"cob.{k}", "campo não pode ser revisado") for k in extras])
        if cob["status"] != "ATIVA":
            raise ErroApi(400, "CobOperacaoInvalida", "Operação inválida.",
                          "Cobrança não encontra-se mais com o status ATIVA, somente cobranças ativas podem ser revisadas.")
        self._validar_cob(c, parcial=True)
        if "status" in c and c["status"] != "REMOVIDA_PELO_USUARIO_RECEBEDOR":
            raise ErroApi(400, "CobOperacaoInvalida", "Cobrança inválida.",
                          "O único status aceito na revisão é REMOVIDA_PELO_USUARIO_RECEBEDOR.")
        if "loc" in c:
            loc = self._loc_para_vincular(c, "cob", "CobOperacaoInvalida")
            antiga = self.locs.get((cob.get("loc") or {}).get("id"))
            if antiga is not None:
                antiga.pop("txid", None)
            loc["txid"] = cob["txid"]
            cob["loc"] = {"id": loc["id"], "location": loc["location"], "tipoCob": "cob"}
            cob["location"] = loc["location"]
        mudou = False
        if "valor" in c:
            cob["valor"]["original"] = c["valor"]["original"]
            if "modalidadeAlteracao" in c["valor"]:
                cob["valor"]["modalidadeAlteracao"] = int(c["valor"]["modalidadeAlteracao"])
            mudou = True
        if "calendario" in c and "expiracao" in c["calendario"]:
            cob["calendario"]["expiracao"] = c["calendario"]["expiracao"]
            mudou = True
        for campo in ("devedor", "solicitacaoPagador", "infoAdicionais", "status"):
            if campo in c:
                cob[campo] = copy.deepcopy(c[campo])
                mudou = True
        if mudou:
            cob["revisao"] += 1  # mudança só de loc não conta revisão (spec)
        if cob.get("location"):
            cob["pixCopiaECola"] = _br_code(cob["location"], cob["valor"]["original"])
        return 200, _publico(cob), {}

    # ───────────── cobrança com vencimento ───────────── #

    def _validar_cobv(self, c, *, parcial: bool = False, prefixo: str = "cobv") -> None:
        if not isinstance(c, dict):
            raise ErroApi(400, "CobVOperacaoInvalida", "Cobrança inválida.",
                          "O corpo da requisição não é um objeto JSON.")
        v: list[tuple[str, str]] = []
        if not parcial or "calendario" in c:
            cal = c.get("calendario")
            if not (isinstance(cal, dict) and _data_ok(cal.get("dataDeVencimento"))):
                v.append((f"{prefixo}.calendario.dataDeVencimento", "Data no formato AAAA-MM-DD é obrigatória."))
            elif "validadeAposVencimento" in cal and not (
                    isinstance(cal["validadeAposVencimento"], int) and cal["validadeAposVencimento"] >= 0):
                v.append((f"{prefixo}.calendario.validadeAposVencimento", "Inteiro maior ou igual a zero."))
        if not parcial or "devedor" in c:
            _validar_devedor(c.get("devedor"), v, prefixo, endereco=not parcial)
        if not parcial or "valor" in c:
            valor = c.get("valor")
            if not (isinstance(valor, dict) and _valor_ok(valor.get("original"))):
                v.append((f"{prefixo}.valor.original", f"O campo {prefixo}.valor.original não respeita o _schema_."))
        if not parcial:
            chave = c.get("chave")
            if not (isinstance(chave, str) and 0 < len(chave) <= 77):
                v.append((f"{prefixo}.chave", f"O campo {prefixo}.chave não respeita o _schema_."))
        if v:
            raise ErroApi(
                400, "CobVOperacaoInvalida", "Cobrança inválida.",
                "A requisição que busca alterar ou criar uma cobrança com vencimento "
                "não respeita o _schema_ ou está semanticamente errada.", v,
            )

    def _criar_cobv(self, txid: str, c, *, lote: str | None = None) -> dict:
        if not _TXID.match(txid or ""):
            raise ErroApi(400, "CobVOperacaoInvalida", "Cobrança inválida.",
                          "O txid não respeita o padrão [a-zA-Z0-9]{26,35}.", [("cobv.txid", "formato inválido")])
        if txid in self.cobs or txid in self.cobvs:
            raise ErroApi(400, "CobVOperacaoInvalida", "Cobrança inválida.",
                          "O txid informado já foi utilizado em outra cobrança.", [("cobv.txid", "txid duplicado")])
        self._validar_cobv(c)
        loc = self._loc_para_vincular(c, "cobv", "CobVOperacaoInvalida") or self._nova_loc("cobv")
        loc["txid"] = txid
        cal = c["calendario"]
        cobv: dict = {
            "calendario": {
                "criacao": _iso(_agora()),
                "dataDeVencimento": cal["dataDeVencimento"],
                "validadeAposVencimento": int(cal.get("validadeAposVencimento", 30)),
            },
            "txid": txid,
            "revisao": 0,
            "loc": {"id": loc["id"], "location": loc["location"], "tipoCob": "cobv"},
            "status": "ATIVA",
            "devedor": dict(c["devedor"]),
            "recebedor": dict(RECEBEDOR_FICTICIO),
            "valor": copy.deepcopy(c["valor"]),
            "chave": c["chave"],
        }
        if c.get("solicitacaoPagador"):
            cobv["solicitacaoPagador"] = c["solicitacaoPagador"]
        cobv["pixCopiaECola"] = _br_code(loc["location"], cobv["valor"]["original"])
        if lote:
            cobv["_lote"] = lote
        self.cobvs[txid] = cobv
        return cobv

    def _revisar_cobv(self, txid: str, c) -> dict:
        cobv = self._achar(self.cobvs, txid)
        if not isinstance(c, dict) or not c:
            raise ErroApi(400, "CobVOperacaoInvalida", "Cobrança inválida.", "Corpo vazio: nada a revisar.")
        if cobv["status"] != "ATIVA":
            raise ErroApi(400, "CobVOperacaoInvalida", "Operação inválida.",
                          "Cobrança não encontra-se mais com o status ATIVA, somente cobranças ativas podem ser revisadas.")
        revisaveis = {k: v for k, v in c.items() if k != "txid"}
        self._validar_cobv(revisaveis, parcial=True)
        if "valor" in revisaveis:
            cobv["valor"].update(copy.deepcopy(revisaveis["valor"]))
        if "calendario" in revisaveis:
            cobv["calendario"].update({k: v for k, v in revisaveis["calendario"].items() if k != "criacao"})
        for campo in ("devedor", "solicitacaoPagador", "status"):
            if campo in revisaveis:
                cobv[campo] = copy.deepcopy(revisaveis[campo])
        cobv["revisao"] += 1
        cobv["pixCopiaECola"] = _br_code(cobv["loc"]["location"], cobv["valor"]["original"]) if cobv.get("loc") else ""
        return cobv

    # ───────────── lote de cobv ───────────── #

    def _erro_lote(self, detalhe: str, violacoes=None) -> ErroApi:
        return ErroApi(400, "LoteCobVOperacaoInvalida", "Lote de cobranças inválido.", detalhe, violacoes)

    def _put_lote(self, p, q, c):
        lid = p[1]
        if not _ID_LIVRE.match(lid or ""):
            raise self._erro_lote("O id do lote não respeita o padrão [a-zA-Z0-9]{1,35}.")
        if not isinstance(c, dict):
            raise self._erro_lote("O corpo da requisição não é um objeto JSON.")
        v = []
        if not (isinstance(c.get("descricao"), str) and c["descricao"].strip()):
            v.append(("loteCobV.descricao", "O campo loteCobV.descricao não respeita o _schema_."))
        itens = c.get("cobsv")
        if not (isinstance(itens, list) and itens and all(isinstance(i, dict) for i in itens)):
            v.append(("loteCobV.cobsV", "O objeto loteCobV.cobsV não respeita o _schema_."))
        if v:
            raise self._erro_lote(
                "A requisição que busca alterar ou criar um lote de cobranças com vencimento "
                "não respeita o _schema_ ou está semanticamente errada.", v)
        txids = [i.get("txid") for i in itens]
        if any(not (isinstance(t, str) and _TXID.match(t)) for t in txids) or len(set(txids)) != len(txids):
            raise self._erro_lote("Cada cobrança do lote precisa de um txid único no padrão [a-zA-Z0-9]{26,35}.")
        existente = self.lotes.get(lid)
        if existente is not None:
            if set(txids) != set(existente["itens"]):
                raise self._erro_lote("Alterações só podem manter o conjunto original de cobranças, "
                                      "sem adicionar ou remover itens.")
        else:
            for t in txids:
                if t in self.cobs or t in self.cobvs:
                    raise self._erro_lote(f"O txid {t} já pertence a uma cobrança criada fora deste lote.")
        agora = _iso(_agora())
        lote = existente or {"id": lid, "criacao": agora, "itens": {}}
        lote["descricao"] = c["descricao"]
        for item in itens:
            t = item["txid"]
            lote["itens"][t] = {"criacao": agora, "txid": t, "status": "EM_PROCESSAMENTO",
                                "_pedido": copy.deepcopy(item)}
        lote["_pronto_em"] = time.monotonic() + self.atraso
        self.lotes[lid] = lote
        return 202, None, {}

    def _patch_lote(self, p, q, c):
        lote = self._achar(self.lotes, p[1])
        itens = c.get("cobsv") if isinstance(c, dict) else None
        if not (isinstance(itens, list) and itens and all(isinstance(i, dict) for i in itens)):
            raise self._erro_lote("O objeto loteCobV.cobsV não respeita o _schema_.",
                                  [("loteCobV.cobsV", "lista de cobranças a revisar é obrigatória")])
        for item in itens:
            if item.get("txid") not in lote["itens"]:
                raise self._erro_lote("Não é possível adicionar cobranças ao lote na revisão.",
                                      [("loteCobV.cobsV.txid", f"{item.get('txid')!r} não pertence ao lote")])
        for item in itens:
            alvo = lote["itens"][item["txid"]]
            alvo["status"] = "EM_PROCESSAMENTO"
            alvo["_revisao"] = copy.deepcopy(item)
        lote["_pronto_em"] = time.monotonic() + self.atraso
        return 202, None, {}

    def _processar_lote(self, lote: dict) -> None:
        if time.monotonic() < lote.get("_pronto_em", 0):
            return
        for t, item in lote["itens"].items():
            if item["status"] != "EM_PROCESSAMENTO":
                continue
            pedido = item.pop("_pedido", None)
            revisao = item.pop("_revisao", None)
            try:
                if pedido is not None:
                    self.cobvs.pop(t, None)  # PUT de novo no mesmo lote substitui a cobrança
                    self._criar_cobv(t, {k: v for k, v in pedido.items() if k != "txid"}, lote=lote["id"])
                if revisao is not None:
                    self._revisar_cobv(t, revisao)
                item["status"] = "CRIADA"
                item.pop("problema", None)
            except ErroApi as exc:
                item["status"] = "NEGADA"
                item["problema"] = exc.corpo()

    def _lote_publico(self, lote: dict) -> dict:
        self._processar_lote(lote)
        cobsv = []
        for item in lote["itens"].values():
            pub = {"txid": item["txid"], "status": item["status"]}
            if item["status"] != "NEGADA":
                pub = {"criacao": item["criacao"], **pub}
            if item.get("problema"):
                pub["problema"] = item["problema"]
            cobsv.append(pub)
        return {"descricao": lote["descricao"], "criacao": lote["criacao"], "cobsv": cobsv}

    def _get_lote(self, p, q, c):
        return 200, self._lote_publico(self._achar(self.lotes, p[1])), {}

    def _listar_lotes(self, p, q, c):
        ini, fim, di, df = self._periodo(q)
        sel = [self._lote_publico(lt) for lt in self.lotes.values()
               if di <= _ler_data_hora(lt["criacao"]) <= df]
        return self._paginar(q, sel, "lotes", ini, fim)

    # ───────────── Pix recebidos e devolução ───────────── #

    def _achar_pix(self, e2eid: str) -> dict:
        if not _E2E.match(e2eid or ""):
            raise _nao_encontrado("e2eid fora do padrão [a-zA-Z0-9]{32}.")
        return self._achar(self.pix, e2eid)

    def _listar_pix(self, p, q, c):
        ini, fim, di, df = self._periodo(q)
        txid = (q.get("txid") or [""])[0]
        tx_presente = (q.get("txIdPresente") or [""])[0].lower()
        dev_presente = (q.get("devolucaoPresente") or [""])[0].lower()
        sel = []
        for pix in self.pix.values():
            if not di <= _ler_data_hora(pix["horario"]) <= df:
                continue
            if txid and pix.get("txid") != txid:
                continue
            if tx_presente in ("true", "false") and bool(pix.get("txid")) != (tx_presente == "true"):
                continue
            if dev_presente in ("true", "false") and bool(pix.get("devolucoes")) != (dev_presente == "true"):
                continue
            sel.append(pix)
        return self._paginar(q, sel, "pix", ini, fim)

    def _erro_dev(self, detalhe: str, violacoes=None) -> ErroApi:
        return ErroApi(400, "PixDevolucaoInvalida", "Devolução inválida.", detalhe, violacoes)

    def _put_devolucao(self, p, q, c):
        pix = self._achar_pix(p[1])
        did = p[3]
        if not _ID_LIVRE.match(did or ""):
            raise self._erro_dev("O id da devolução não respeita o padrão [a-zA-Z0-9]{1,35}.")
        if not (isinstance(c, dict) and _valor_ok(c.get("valor"))):
            raise self._erro_dev(
                "A presente requisição de devolução não respeita o _schema_ ou não faz sentido semanticamente.",
                [("valor", "obrigatório, string com 2 casas decimais")])
        natureza = c.get("natureza", "ORIGINAL")
        if natureza not in ("ORIGINAL", "RETIRADA"):
            raise self._erro_dev("natureza deve ser ORIGINAL ou RETIRADA.")
        if "descricao" in c and not (isinstance(c["descricao"], str) and len(c["descricao"]) <= 140):
            raise self._erro_dev("descricao: até 140 caracteres.")
        devolucoes = pix.setdefault("devolucoes", [])
        if any(d["id"] == did for d in devolucoes):
            raise self._erro_dev("Já existe uma devolução com este id para este Pix.")
        ja = sum((Decimal(d["valor"]) for d in devolucoes
                  if d["status"] in ("EM_PROCESSAMENTO", "DEVOLVIDO")), Decimal("0"))
        if ja + Decimal(c["valor"]) > Decimal(pix["valor"]):
            raise self._erro_dev("O valor da devolução excede o valor ainda disponível do Pix.")
        dev = {
            "id": did,
            "rtrId": _e2e("D"),
            "valor": c["valor"],
            "natureza": natureza,
            "horario": {"solicitacao": _iso(_agora())},
            "status": "EM_PROCESSAMENTO",
            "_pronto_em": time.monotonic() + self.atraso,
        }
        if c.get("descricao"):
            dev["descricao"] = c["descricao"]
        devolucoes.append(dev)
        return 201, _publico(dev), {}

    def _get_devolucao(self, p, q, c):
        pix = self._achar_pix(p[1])
        for dev in pix.get("devolucoes") or []:
            if dev["id"] == p[3]:
                if dev["status"] == "EM_PROCESSAMENTO" and time.monotonic() >= dev.get("_pronto_em", 0):
                    dev["status"] = "DEVOLVIDO"
                    dev["horario"]["liquidacao"] = _iso(_agora())
                return 200, _publico(dev), {}
        raise _nao_encontrado()

    # ───────────── webhook ───────────── #

    def _put_webhook(self, p, q, c):
        chave = p[1]
        url = c.get("webhookUrl") if isinstance(c, dict) else None
        if not (0 < len(chave) <= 77) or not (isinstance(url, str) and url.startswith("https://")):
            raise ErroApi(400, "WebhookOperacaoInvalida", "Webhook inválido.",
                          "A presente requisição busca criar um webhook sem respeitar o _schema_ ou, "
                          "ainda, com sentido semanticamente inválido.",
                          [("webhookUrl", "obrigatório, URL https")])
        self.webhooks[chave] = {"webhookUrl": url, "chave": chave, "criacao": _iso(_agora())}
        return 200, None, {}

    def _delete_webhook(self, p, q, c):
        if p[1] not in self.webhooks:
            raise _nao_encontrado()
        del self.webhooks[p[1]]
        return 204, None, {}

    def _listar_webhooks(self, p, q, c):
        periodo = self._periodo(q, obrigatorio=False)
        itens = list(self.webhooks.values())
        if periodo:
            ini, fim, di, df = periodo
            itens = [w for w in itens if di <= _ler_data_hora(w["criacao"]) <= df]
        else:
            ini = fim = ""
        return self._paginar(q, itens, "webhooks", ini, fim)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SimuladorC6/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _tratar(self, metodo: str) -> None:
        estado: EstadoC6 = self.server.estado  # type: ignore[attr-defined]
        rid = str(uuid.uuid4())
        partes = urllib.parse.urlsplit(self.path)
        tamanho = int(self.headers.get("Content-Length") or 0)
        bruto = self.rfile.read(tamanho) if tamanho > 0 else b""
        tipo = "application/json"
        try:
            status, corpo, extra = estado.tratar(
                metodo, partes.path,
                urllib.parse.parse_qs(partes.query, keep_blank_values=True),
                bruto, self.headers,
            )
        except ErroApi as exc:
            status, corpo, extra, tipo = exc.status, exc.corpo(rid), {}, "application/problem+json"
        except Exception as exc:  # noqa: BLE001 - o simulador nunca derruba a conexão
            status, extra, tipo = 500, {}, "application/problem+json"
            corpo = {"type": _ERRO + "ErroInterno", "title": "Erro interno do simulador",
                     "status": 500, "detail": f"{type(exc).__name__}: {exc}", "correlationId": rid}
        dados = b"" if corpo is None or status == 204 else json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if dados:
            self.send_header("Content-Type", f"{tipo}; charset=utf-8")
        if status != 204:
            self.send_header("Content-Length", str(len(dados)))
        self.send_header("x-request-id", rid)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if dados:
            self.wfile.write(dados)

    def do_GET(self):  # noqa: N802
        self._tratar("GET")

    def do_POST(self):  # noqa: N802
        self._tratar("POST")

    def do_PUT(self):  # noqa: N802
        self._tratar("PUT")

    def do_PATCH(self):  # noqa: N802
        self._tratar("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._tratar("DELETE")

    def log_message(self, fmt, *args):  # noqa: D401 - silencioso por padrão
        if not getattr(self.server, "silencioso", True):
            # Só método, caminho e status: o http.server não loga header, então
            # o Bearer nunca aparece aqui.
            sys.stderr.write("[simulador-c6] " + (fmt % args) + "\n")


class Simulador:
    """Servidor HTTP do simulador numa thread. Use com `with` ou `iniciar()`/`parar()`."""

    def __init__(self, porta: int = 0, *, host: str = "127.0.0.1", silencioso: bool = True, **opcoes):
        if host not in HOSTS_LOCAIS:
            raise ValueError("o simulador só escuta em loopback (127.0.0.1/localhost)")
        self.estado = EstadoC6(**opcoes)
        self.servidor = ThreadingHTTPServer((host, porta), _Handler)
        self.servidor.daemon_threads = True
        self.servidor.estado = self.estado  # type: ignore[attr-defined]
        self.servidor.silencioso = silencioso  # type: ignore[attr-defined]
        self.url = f"http://{host}:{self.servidor.server_address[1]}"
        self._thread: threading.Thread | None = None

    def iniciar(self) -> "Simulador":
        self._thread = threading.Thread(target=self.servidor.serve_forever, name="simulador-c6", daemon=True)
        self._thread.start()
        return self

    def parar(self) -> None:
        self.servidor.shutdown()
        self.servidor.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "Simulador":
        return self.iniciar()

    def __exit__(self, *exc) -> None:
        self.parar()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Simulador local da API Pix do C6 (só para ensaio).")
    ap.add_argument("--porta", type=int, default=8765, help="porta em 127.0.0.1 (0 = qualquer livre)")
    ap.add_argument("--atraso-lote", type=float, default=0.3,
                    help="segundos até o lote e a devolução saírem de EM_PROCESSAMENTO")
    ap.add_argument("--sem-pix-pago", action="store_true", help="não semeia o Pix pago (sem e2eid para o P_05)")
    ap.add_argument("--fora-da-janela", action="store_true", help="responde 503 em tudo")
    ap.add_argument("--escopos", default=" ".join(ESCOPOS_PADRAO), help="escopos devolvidos no token")
    ap.add_argument("--verboso", action="store_true", help="loga método, caminho e status de cada chamada")
    args = ap.parse_args(argv)

    sim = Simulador(
        args.porta, silencioso=not args.verboso, atraso=args.atraso_lote,
        semear_pix=not args.sem_pix_pago, fora_da_janela=args.fora_da_janela, escopos=args.escopos,
    ).iniciar()
    print(f"Simulador do C6 ouvindo em {sim.url}  (Ctrl+C para parar)")
    print("ATENÇÃO: respostas fictícias. Nada daqui vai para o roteiro.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sim.parar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
