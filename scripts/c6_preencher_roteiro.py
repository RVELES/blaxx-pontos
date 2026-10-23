#!/usr/bin/env python3
"""Preenche uma CÓPIA do "Roteiro de Testes - C6 Developers v3.0" com os retornos reais.

Por que existe: o C6 cancelou a homologação em 22/10/2026 porque o roteiro preenchido
não chegou. Este script transforma o arquivo de resultados do executor do sandbox
(`scripts/c6_homologacao.py`, formato em `docs/c6-homologacao/formato-resultados.md`)
no .docx que vai anexado ao e-mail para homologacaoapi@c6bank.com.

O que ele faz:
  1. COPIA o roteiro original (nunca escreve nele) para
     docs/c6-homologacao/Roteiro de Testes - C6 Developers v3.0 - BlaXx.docx;
  2. preenche os campos de formulário legados do Word (FORMTEXT / FORMCHECKBOX,
     w:fldChar + w:ffData) trocando só o RESULTADO do campo, sem mexer na estrutura:
       - DADOS DA ORGANIZAÇÃO: só os campos que têm fonte no projeto
         (ver docs/c6-homologacao/02-dados-organizacao.md). CNPJ e razão social
         ficam vazios até o dono passar --cnpj e --razao-social;
       - marca o checkbox PIX (AUTENTICAÇÃO é obrigatório e não tem checkbox);
       - AT_01 e os 24 testes P_01..P_06: "Status Code - Retornado" e
         "Response Body - Retornado";
     os testes que não usamos (agendamento, boleto, checkout, extrato, transações,
     Pix Automático, Bolepix) ficam desmarcados e vazios, como o roteiro manda.
     Com --com-checkout, CHECKOUT também é marcado e C_01..C_04, C_0501 e C_0502
     passam a ser exigidos (o C_06 só existe no sumário do .docx, sem tabela);
  3. mascara segredos de novo, mesmo que o executor já tenha mascarado
     (access_token, client_secret, token do webhook, JWT, PEM e, por padrão,
     a chave Pix);
  4. confere o resultado: relê o .docx, compara cada campo com o que devia estar
     lá, garante que nenhum outro campo mudou e varre o texto atrás de segredo.
     Se qualquer conferência falhar, o arquivo de saída é apagado.

Nenhuma chamada de rede. Nenhum segredo é impresso: os valores das variáveis de
ambiente C6_* só servem para achar e mascarar ocorrências.

Uso:
    # rascunho agora (sem resultados): só organização + checkbox, sai como "- RASCUNHO.docx"
    python3 scripts/c6_preencher_roteiro.py --parcial

    # depois do sandbox
    python3 scripts/c6_preencher_roteiro.py \\
        --resultados docs/c6-homologacao/resultados-sandbox.json \\
        --cnpj "<do cartão CNPJ>" --razao-social "<do cartão CNPJ>" --pdf

    # validação com dados fictícios (a saída leva EXEMPLO no nome)
    python3 scripts/c6_preencher_roteiro.py \\
        --resultados "docs/c6-homologacao/resultados - EXEMPLO.json"

Requer: lxml (pip install lxml). O ElementTree da stdlib reescreve os prefixos de
namespace e quebra o mc:Ignorable do Word, por isso não serve aqui.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from http import HTTPStatus
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.exit("Falta a biblioteca lxml: pip install lxml")

BACKEND = Path(__file__).resolve().parent.parent
PASTA_SAIDA = BACKEND / "docs" / "c6-homologacao"
ROTEIRO_ORIGINAL = Path.home() / "Downloads" / "Roteiro de Testes - C6 Developers v3.0.docx"
NOME_SAIDA = "Roteiro de Testes - C6 Developers v3.0 - BlaXx.docx"
NOME_SAIDA_EXEMPLO = "Roteiro de Testes - C6 Developers v3.0 - BlaXx - EXEMPLO.docx"
NOME_SAIDA_RASCUNHO = "Roteiro de Testes - C6 Developers v3.0 - BlaXx - RASCUNHO.docx"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
PLACEHOLDER = " " * 5  # resultado padrão de um FORMTEXT vazio no Word


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


# ---------------------------------------------------------------------------
# Dados da organização (tabela 0 do documento)
# Só entra valor com fonte no projeto. Fontes: docs/c6-homologacao/02-dados-organizacao.md
# ---------------------------------------------------------------------------
ORG_LINHAS = [
    # (linha da tabela, rótulo no .docx, chave)
    (1, "CNPJ / IDENTIFICADOR DA ORGANIZAÇÃO", "cnpj"),
    (2, "NOME DA EMPRESA CLIENTE / PARCEIRA", "empresa"),
    (3, "NOME DO SOFTWARE", "software"),
    (4, "RESPONSÁVEL PELA EMPRESA", "responsavel"),
    (5, "E-MAIL", "email"),
    (6, "TELEFONE", "telefone"),
]
ORG_PADRAO = {
    # sem fonte no projeto: o dono informa com --cnpj (cartão CNPJ da Receita)
    "cnpj": "",
    # sem fonte no projeto: o dono informa com --razao-social (cartão CNPJ)
    "empresa": "",
    # blaxx/home-embarque.html:7 e :53 (og:site_name)
    "software": "BlaXx Pontos",
    # _interno/design-assets/assets/exports/BlaXx_Assinatura_Email_Branco.html:36
    "responsavel": "Ricardo Veles",
    # mesma assinatura :38; app/config.py:361 ("conta da empresa")
    "email": "rveles@blaxxpontos.com.br",
    # mesma assinatura :39 (o dono confirma antes de enviar)
    "telefone": "+55 11 97648-7447",
}

# Tabela 1: "MARQUE O CHECKBOX COM AS APIS QUE VOCÊ TESTARÁ"
CHECKBOX_LINHAS = {
    3: "AGENDAMENTO DE PAGAMENTOS",
    4: "BOLETO",
    5: "CHECKOUT",
    6: "EXTRATO",
    7: "PIX",
    8: "TRANSAÇÕES E RECEBÍVEIS",
    9: "PIX AUTOMÁTICO",
    10: "BOLEPIX",
}

# ---------------------------------------------------------------------------
# Testes no escopo: (id, índice da tabela de resultado, rótulo impresso no .docx)
# Índice = posição 0-based em body.iter(w:tbl). O .docx tem rótulos errados
# (P_03_03 -> "P_02_03", P_04_04 -> "P_02_04"), por isso a busca é por posição
# e o rótulo só serve de conferência. Ver 01-mapa-roteiro.md.
# ---------------------------------------------------------------------------
TESTES = [
    ("AT_01", 4, "AT_01"),
    ("P_01_01", 53, "P_01_01"),
    ("P_01_02", 55, "P_01_02"),
    ("P_01_03", 57, "P_01_03"),
    ("P_01_04", 59, "P_01_04"),
    ("P_01_05", 61, "P_01_05"),
    ("P_02_01", 63, "P_02_01"),
    ("P_02_02", 65, "P_02_02"),
    ("P_02_03", 67, "P_02_03"),
    ("P_02_04", 69, "P_02_04"),
    ("P_03_01", 71, "P_03_01"),
    ("P_03_02", 73, "P_03_02"),
    ("P_03_03", 75, "P_02_03"),  # rótulo errado no .docx
    ("P_03_04", 77, "P_03_04"),
    ("P_04_01", 79, "P_04_01"),
    ("P_04_02", 81, "P_04_02"),
    ("P_04_03", 83, "P_04_03"),
    ("P_04_04", 85, "P_02_04"),  # rótulo errado no .docx
    ("P_05_01", 87, "P_05_01"),
    ("P_05_02", 89, "P_05_02"),
    ("P_05_03", 91, "P_05_03"),
    ("P_05_04", 93, "P_05_04"),
    ("P_06_01", 95, "P_06_01"),
    ("P_06_02", 97, "P_06_02"),
    ("P_06_03", 99, "P_06_03"),
]
# Só com --com-checkout. C_0501/C_0502 são eventos RECEBIDOS no nosso webhook: o
# "status" é o que o nosso endpoint respondeu e o "corpo" é o evento recebido.
TESTES_CHECKOUT = [
    ("C_01", 37, "C_01"),
    ("C_02", 39, "C_02"),
    ("C_03", 41, "C_03"),
    ("C_04", 43, "C_04"),
    ("C_0501", 45, "C_0501"),
    ("C_0502", 47, "C_0502"),
]
IDS_CHECKOUT = [t[0] for t in TESTES_CHECKOUT]

# (status pedido no roteiro, status da spec). Só para o resumo: no .docx vai SEMPRE
# o status que a API devolveu. Specs: pix-api.yaml v3.0.1 e checkout.yaml v1.2.0
# (None = a spec não cobre, ex.: evento recebido no nosso webhook).
ESPERADO = {
    "C_01": (201, 201), "C_02": (200, 200), "C_03": (200, 204), "C_04": (204, 200),
    "C_0501": (200, None), "C_0502": (200, None),
    "AT_01": (200, 200),
    "P_01_01": (201, 201), "P_01_02": (200, 201), "P_01_03": (201, 200),
    "P_01_04": (200, 200), "P_01_05": (200, 200),
    "P_02_01": (201, 201), "P_02_02": (200, 200), "P_02_03": (200, 200), "P_02_04": (200, 200),
    "P_03_01": (201, 202), "P_03_02": (200, 202), "P_03_03": (200, 200), "P_03_04": (200, 200),
    "P_04_01": (201, 201), "P_04_02": (200, 200), "P_04_03": (200, 200), "P_04_04": (200, 200),
    "P_05_01": (200, 200), "P_05_02": (200, 200), "P_05_03": (201, 201), "P_05_04": (200, 200),
    "P_06_01": (200, 200), "P_06_02": (200, 200), "P_06_03": (204, 204),
}

ROTULO_RESULTADO = ("Status Code - Retornado", "Response Body - Retornado")


class ErroRoteiro(Exception):
    """Falha que impede gerar um roteiro confiável."""


# ---------------------------------------------------------------------------
# Máscara
# ---------------------------------------------------------------------------
CHAVES_SECRETAS = {
    "access_token", "refresh_token", "id_token", "client_secret", "clientsecret",
    "password", "senha", "secret", "private_key", "privatekey", "authorization",
    "cvv", "cvc", "security_code", "codigo_seguranca",
}
RE_PAN = re.compile(r"^\d{13,19}$")  # número de cartão (só em chave com card/cartao/pan)
RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
RE_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)
RE_BEARER = re.compile(r"(Bearer\s+)(?!<)[A-Za-z0-9._~+/=-]{8,}", re.I)
RE_WEBHOOK_TOKEN = re.compile(r"(/webhook/)([^/?#\s\"<]+)")
ENV_SEGREDOS = ("C6_CLIENT_SECRET", "C6_CLIENT_ID", "C6_WEBHOOK_TOKEN")


def _ja_mascarado(s: str) -> bool:
    return s.startswith("<mascarado") or s.startswith("<TOKEN:") or s.startswith("<CHAVE_PIX:")


class Mascara:
    def __init__(self, mascarar_chave_pix: bool) -> None:
        self.mascarar_chave_pix = mascarar_chave_pix
        # valor -> marcador; preenchido com env vars e com as chaves Pix vistas nos corpos
        self.literais: dict[str, str] = {}
        for nome in ENV_SEGREDOS:
            v = os.environ.get(nome, "")
            if len(v) >= 6:
                marca = f"<TOKEN:{len(v)}ch>" if nome == "C6_WEBHOOK_TOKEN" else f"<mascarado, {len(v)} caracteres>"
                self.literais[v] = marca
        chave_env = os.environ.get("C6_PIX_KEY", "")
        if mascarar_chave_pix and len(chave_env) >= 6:
            self.literais[chave_env] = f"<CHAVE_PIX:{len(chave_env)}ch>"

    def aprender(self, obj) -> None:
        """Primeira passada: coleta as chaves Pix que aparecem em campos "chave"."""
        if not self.mascarar_chave_pix:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "chave" and isinstance(v, str) and len(v) >= 6 and not _ja_mascarado(v):
                    self.literais.setdefault(v, f"<CHAVE_PIX:{len(v)}ch>")
                else:
                    self.aprender(v)
        elif isinstance(obj, list):
            for v in obj:
                self.aprender(v)

    def texto(self, s: str) -> str:
        if _ja_mascarado(s):
            return s
        # literais mais longos primeiro, para um não cortar o outro
        for valor in sorted(self.literais, key=len, reverse=True):
            if valor in s:
                s = s.replace(valor, self.literais[valor])
        s = RE_PEM.sub("<PEM mascarado>", s)
        s = RE_JWT.sub(lambda m: f"<mascarado, {len(m.group(0))} caracteres>", s)
        s = RE_BEARER.sub(lambda m: m.group(1) + f"<mascarado, {len(m.group(0)) - len(m.group(1))} caracteres>", s)
        return s

    def obj(self, obj, chave_pai: str = ""):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in CHAVES_SECRETAS and isinstance(v, str) and v and not _ja_mascarado(v):
                    out[k] = f"<mascarado, {len(v)} caracteres>"
                elif kl == "webhookurl" and isinstance(v, str):
                    url = self.texto(v)
                    out[k] = RE_WEBHOOK_TOKEN.sub(
                        lambda m: m.group(1) + (m.group(2) if _ja_mascarado(m.group(2)) else f"<TOKEN:{len(m.group(2))}ch>"),
                        url,
                    )
                elif k == "chave" and isinstance(v, str) and self.mascarar_chave_pix and not _ja_mascarado(v):
                    out[k] = f"<CHAVE_PIX:{len(v)}ch>"
                elif (any(p in kl for p in ("card", "cartao", "pan")) and isinstance(v, (str, int))
                      and RE_PAN.match(str(v))):
                    out[k] = f"<cartão final {str(v)[-4:]}>"
                else:
                    out[k] = self.obj(v, kl)
            return out
        if isinstance(obj, list):
            return [self.obj(v, chave_pai) for v in obj]
        if isinstance(obj, str):
            return self.texto(obj)
        return obj


def encurtar(obj, max_itens: int, max_chars: int):
    """Corta listas e strings enormes (ex.: imagem base64) com aviso explícito no corpo."""
    if isinstance(obj, dict):
        return {k: encurtar(v, max_itens, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        limite = max_itens if max_itens > 0 else len(obj)
        itens = [encurtar(v, max_itens, max_chars) for v in obj[:limite]]
        if len(obj) > limite:
            itens.append(f"<+{len(obj) - limite} itens omitidos; total {len(obj)}>")
        return itens
    if isinstance(obj, str) and max_chars and len(obj) > max_chars:
        return obj[:120] + f"… <{len(obj)} caracteres; truncado>"
    return obj


# ---------------------------------------------------------------------------
# Resultados do executor
# ---------------------------------------------------------------------------
def carregar_resultados(caminho: Path, ids_validos: list[str]) -> tuple[dict[str, dict], bool, dict]:
    """Lê o JSON do executor. Aceita a lista pura ou {"resultados": [...], ...}.

    Devolve (por_id, eh_exemplo, metadados).
    """
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ErroRoteiro(f"Não consegui ler {caminho.name}: {exc}") from exc

    meta: dict = {}
    if isinstance(dados, dict):
        meta = {k: v for k, v in dados.items() if k != "resultados"}
        lista = dados.get("resultados")
    else:
        lista = dados
    if not isinstance(lista, list):
        raise ErroRoteiro('Formato inválido: esperado uma lista de {id, status, corpo} ou {"resultados": [...]}.')

    # Resultado de simulador local (scripts/c6_simulador.py) ou fictício nunca vira roteiro "real".
    host = str(meta.get("host") or meta.get("base_url") or "").lower()
    ambiente = str(meta.get("ambiente") or "").lower()
    simulado = (bool(meta.get("simulado")) or ambiente in ("simulador", "exemplo", "local", "teste")
                or any(h in host for h in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")))
    eh_exemplo = "EXEMPLO" in caminho.name.upper() or bool(meta.get("exemplo")) or simulado
    por_id: dict[str, dict] = {}
    erros = []
    for i, item in enumerate(lista):
        if not isinstance(item, dict):
            erros.append(f"item {i}: não é objeto")
            continue
        tid = str(item.get("id", "")).strip().upper()
        if tid in IDS_CHECKOUT and tid not in ids_validos:
            erros.append(f"{tid}: teste de CHECKOUT no arquivo; rode com --com-checkout para incluí-lo")
            continue
        if tid not in ids_validos:
            faixa = "AT_01, P_01_01..P_06_03" + (", C_01..C_04, C_0501, C_0502" if IDS_CHECKOUT[0] in ids_validos else "")
            erros.append(f"item {i}: id desconhecido {item.get('id')!r} (válidos: {faixa})")
            continue
        if tid in por_id:
            erros.append(f"{tid}: aparece mais de uma vez")
            continue
        status = item.get("status")
        if status is not None:
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                erros.append(f"{tid}: status inválido {status!r} (inteiro 100-599 ou null)")
                continue
        if "corpo" not in item:
            erros.append(f"{tid}: falta o campo corpo (use null quando a resposta não tiver corpo)")
            continue
        por_id[tid] = item
    if erros:
        raise ErroRoteiro("Arquivo de resultados com erro:\n  - " + "\n  - ".join(erros))
    return por_id, eh_exemplo, meta


def formatar_status(status: int) -> str:
    try:
        return f"{status} {HTTPStatus(status).phrase}"
    except ValueError:
        return str(status)


def formatar_corpo(corpo, mascara: Mascara, max_itens: int, max_chars: int) -> str:
    if corpo is None or corpo == "" or corpo == {}:
        return "(resposta sem corpo)"
    if isinstance(corpo, str):
        try:
            corpo = json.loads(corpo)
        except json.JSONDecodeError:
            return mascara.texto(corpo.strip()) or "(resposta sem corpo)"
    corpo = encurtar(mascara.obj(corpo), max_itens, max_chars)
    return json.dumps(corpo, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Campos de formulário legados
# ---------------------------------------------------------------------------
def texto_de(el) -> str:
    return "".join(t.text or "" for t in el.iter(q("t")))


def campos_do(el) -> list:
    """Runs com fldChar begin + ffData dentro de el, na ordem do documento."""
    return [ff.getparent().getparent() for ff in el.iter(q("ffData"))]


def _resultado_formtext(run_begin):
    """Devolve (runs do resultado, run separate, run end). Exige tudo no mesmo parágrafo."""
    p = run_begin.getparent()
    irmaos = list(p)
    i = irmaos.index(run_begin)
    sep = end = None
    instr = ""
    for j in range(i + 1, len(irmaos)):
        r = irmaos[j]
        if r.tag != q("r"):
            continue
        it = r.find(q("instrText"))
        if it is not None and sep is None:
            instr += it.text or ""
        fc = r.find(q("fldChar"))
        if fc is not None:
            tipo = fc.get(q("fldCharType"))
            if tipo == "begin":
                raise ErroRoteiro("Campo aninhado inesperado dentro de FORMTEXT.")
            if tipo == "separate" and sep is None:
                sep = j
            elif tipo == "end":
                end = j
                break
    if "FORMTEXT" not in instr or sep is None or end is None:
        raise ErroRoteiro("Estrutura de FORMTEXT inesperada (begin/separate/end fora do mesmo parágrafo).")
    resultado = [r for r in irmaos[sep + 1:end] if r.tag == q("r")]
    return resultado, irmaos[sep], irmaos[end]


def ler_formtext(run_begin) -> str:
    resultado, _, _ = _resultado_formtext(run_begin)
    partes = []
    for r in resultado:
        for c in r:
            if c.tag == q("t"):
                partes.append(c.text or "")
            elif c.tag in (q("br"), q("cr")):
                partes.append("\n")
    return "".join(partes)


ORDEM_RPR = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
    "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden",
    "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
    "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
    "specVanish", "oMath",
]


def _por_no_rpr(rpr, el) -> None:
    """Insere el no rPr respeitando a ordem do schema (CT_RPr)."""
    nome = etree.QName(el).localname
    existente = rpr.find(q(nome))
    if existente is not None:
        rpr.replace(existente, el)
        return
    pos = ORDEM_RPR.index(nome)
    for i, filho in enumerate(rpr):
        ln = etree.QName(filho).localname
        if ln in ORDEM_RPR and ORDEM_RPR.index(ln) > pos:
            rpr.insert(i, el)
            return
    rpr.append(el)


def escrever_formtext(run_begin, texto: str, mono_pt: float | None = None) -> None:
    """Troca SÓ o resultado do campo (entre separate e end), mantendo begin/instr/sep/end."""
    resultado, run_sep, _ = _resultado_formtext(run_begin)
    modelo_rpr = None
    for r in resultado:
        if r.find(q("rPr")) is not None:
            modelo_rpr = r.find(q("rPr"))
            break
    p = run_begin.getparent()
    for r in resultado:
        p.remove(r)

    novo = etree.Element(q("r"))
    rpr = copy.deepcopy(modelo_rpr) if modelo_rpr is not None else etree.Element(q("rPr"))
    if mono_pt:
        fonts = etree.Element(q("rFonts"))
        for att in ("ascii", "hAnsi", "cs"):
            fonts.set(q(att), "Courier New")
        _por_no_rpr(rpr, fonts)
        meio = str(int(round(mono_pt * 2)))
        for nome in ("sz", "szCs"):
            el = etree.Element(q(nome))
            el.set(q("val"), meio)
            _por_no_rpr(rpr, el)
    if len(rpr):
        novo.append(rpr)
    linhas = texto.split("\n") if texto else [PLACEHOLDER]
    for k, linha in enumerate(linhas):
        if k:
            etree.SubElement(novo, q("br"))
        t = etree.SubElement(novo, q("t"))
        t.text = linha
        t.set(f"{{{XML_NS}}}space", "preserve")
    run_sep.addnext(novo)


def marcar_checkbox(run_begin, marcado: bool) -> None:
    cb = run_begin.find(q("fldChar")).find(q("ffData")).find(q("checkBox"))
    if cb is None:
        raise ErroRoteiro("Campo esperado como FORMCHECKBOX não é checkbox.")
    checked = cb.find(q("checked"))
    if checked is None:
        checked = etree.SubElement(cb, q("checked"))  # vem depois de size/sizeAuto e default
    checked.set(q("val"), "1" if marcado else "0")


def checkbox_marcado(run_begin) -> bool:
    cb = run_begin.find(q("fldChar")).find(q("ffData")).find(q("checkBox"))
    el = cb.find(q("checked"))
    if el is None:
        el = cb.find(q("default"))
    if el is None:
        return False
    return el.get(q("val"), "1") in ("1", "true", "on")


# ---------------------------------------------------------------------------
# CNPJ (numérico ou alfanumérico, IN RFB 2.229/2024)
# ---------------------------------------------------------------------------
def normalizar_cnpj(valor: str) -> str:
    bruto = re.sub(r"[.\-/\s]", "", valor.strip().upper())
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", bruto):
        raise ErroRoteiro("CNPJ com formato inválido (esperado 14 posições, as 2 últimas numéricas).")

    def dv(base: str) -> int:
        pesos = list(range(len(base) - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum((ord(c) - 48) * p for c, p in zip(base, pesos))
        r = soma % 11
        return 0 if r < 2 else 11 - r

    d1 = dv(bruto[:12])
    d2 = dv(bruto[:12] + str(d1))
    if bruto[12:] != f"{d1}{d2}":
        raise ErroRoteiro("CNPJ com dígito verificador inválido: confira no cartão CNPJ.")
    return f"{bruto[:2]}.{bruto[2:5]}.{bruto[5:8]}/{bruto[8:12]}-{bruto[12:]}"


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------
def _linha(tbl, idx):
    linhas = tbl.findall(q("tr"))
    if idx >= len(linhas):
        raise ErroRoteiro("Tabela com menos linhas que o esperado: o roteiro mudou de versão?")
    return linhas[idx]


def localizar(body, testes: list) -> dict:
    """Acha cada campo pela posição e confere o rótulo. Aborta se o documento não bater."""
    tbls = list(body.iter(q("tbl")))
    if len(tbls) < 100:
        raise ErroRoteiro(f"O documento tem {len(tbls)} tabelas; esperado >= 100. É o Roteiro v3.0?")
    achado = {"org": {}, "check": {}, "testes": {}}

    t0 = tbls[0]
    if texto_de(_linha(t0, 0)).strip() != "DADOS DA ORGANIZAÇÃO":
        raise ErroRoteiro("Tabela 0 não é DADOS DA ORGANIZAÇÃO.")
    for idx, rotulo, chave in ORG_LINHAS:
        tr = _linha(t0, idx)
        if rotulo not in texto_de(tr):
            raise ErroRoteiro(f"Linha {idx} da tabela 0 não é {rotulo!r}.")
        campos = campos_do(tr)
        if len(campos) != 1:
            raise ErroRoteiro(f"Linha {rotulo!r}: esperado 1 campo, achei {len(campos)}.")
        achado["org"][chave] = campos[0]

    t1 = tbls[1]
    if "MARQUE O CHECKBOX" not in texto_de(_linha(t1, 0)):
        raise ErroRoteiro("Tabela 1 não é a dos checkboxes de API.")
    # Roteiro de 22/10 renomeou EXTRATO para "SALDO & EXTRATO": acha a linha pelo
    # rótulo, não pela posição, e exige só os que vamos marcar ou conferir.
    for tr in t1.findall(q("tr")):
        celulas = tr.findall(q("tc"))
        if len(celulas) < 3:
            continue
        rotulo = texto_de(celulas[1]).strip()
        campos = campos_do(celulas[2])
        if (rotulo and len(campos) == 1
                and campos[0].find(q("fldChar")).find(q("ffData")).find(q("checkBox")) is not None):
            achado["check"][rotulo] = campos[0]
    for rotulo in ("PIX", "CHECKOUT"):
        if rotulo not in achado["check"]:
            raise ErroRoteiro(f"Checkbox de {rotulo!r} não encontrado na tabela 1.")

    # Tabelas de resultado por rótulo. O roteiro de 22/10 corrigiu os rótulos
    # trocados (P_03_03, P_04_04) e deslocou as posições (E_02 novo); a
    # posição antiga só desempata quando o rótulo aparece repetido (v3.0 antigo).
    def _cab(t):
        return texto_de(_linha(t, 0)).strip()

    def _eh_resultado(t):
        linhas = t.findall(q("tr"))
        return len(linhas) >= 3 and tuple(
            texto_de(c).strip() for c in linhas[1].findall(q("tc"))) == ROTULO_RESULTADO

    resultado_tbls = [(i, t) for i, t in enumerate(tbls) if _eh_resultado(t)]
    for tid, ti, rotulo in testes:
        por_id = [i for i, t in resultado_tbls if _cab(t) == f"TESTE: {tid}"]
        if len(por_id) == 1:
            ti = por_id[0]
        elif not (ti < len(tbls) and _cab(tbls[ti]) == f"TESTE: {rotulo}"):
            raise ErroRoteiro(f"{tid}: tabela de resultado 'TESTE: {tid}' não encontrada.")
        tbl = tbls[ti]
        rotulo = _cab(tbl).removeprefix("TESTE: ")
        cab = _cab(tbl)
        if cab != f"TESTE: {rotulo}":
            raise ErroRoteiro(f"{tid}: a tabela {ti} diz {cab!r}, esperado 'TESTE: {rotulo}'.")
        h = [texto_de(c).strip() for c in _linha(tbl, 1).findall(q("tc"))]
        if tuple(h) != ROTULO_RESULTADO:
            raise ErroRoteiro(f"{tid}: a tabela {ti} não é de resultado ({h}).")
        celulas = _linha(tbl, 2).findall(q("tc"))
        cs, cb = campos_do(celulas[0]), campos_do(celulas[1])
        if len(cs) != 1 or len(cb) != 1:
            raise ErroRoteiro(f"{tid}: esperado 1 campo em cada célula do resultado.")
        achado["testes"][tid] = (cs[0], cb[0])
    return achado


def montar(args) -> int:
    origem = Path(args.roteiro).expanduser().resolve()
    if not origem.is_file():
        raise ErroRoteiro(f"Roteiro original não encontrado: {origem}")

    testes = TESTES + (TESTES_CHECKOUT if args.com_checkout else [])
    ids = [t[0] for t in testes]
    marcar = {"PIX", "CHECKOUT"} if args.com_checkout else {"PIX"}

    por_id: dict[str, dict] = {}
    eh_exemplo = False
    meta: dict = {}
    if args.resultados:
        por_id, eh_exemplo, meta = carregar_resultados(Path(args.resultados).expanduser(), ids)
    elif not args.parcial:
        raise ErroRoteiro("Sem --resultados: use --parcial para gerar só organização + checkbox.")

    faltando = [t for t in ids if t not in por_id or por_id[t].get("status") is None]
    if args.saida:
        saida = Path(args.saida).expanduser()
    else:  # rascunho incompleto nunca sai com o nome do arquivo que vai anexado
        saida = PASTA_SAIDA / (NOME_SAIDA_EXEMPLO if eh_exemplo else NOME_SAIDA_RASCUNHO if faltando else NOME_SAIDA)
    saida = saida.resolve()
    if saida == origem:
        raise ErroRoteiro("A saída não pode ser o roteiro original.")
    if eh_exemplo and "EXEMPLO" not in saida.name.upper():
        raise ErroRoteiro("Resultados de EXEMPLO ou do simulador local só podem gerar arquivo com EXEMPLO no nome.")
    if not eh_exemplo and "EXEMPLO" in saida.name.upper():
        raise ErroRoteiro("Resultados reais não podem ir para um arquivo chamado EXEMPLO.")

    if faltando and not args.parcial:
        raise ErroRoteiro(
            f"{len(faltando)} teste(s) sem status: {', '.join(faltando)}. "
            "O roteiro só vai para o C6 completo; use --parcial para um rascunho."
        )

    org = dict(ORG_PADRAO)
    for chave, valor in (("cnpj", args.cnpj), ("empresa", args.razao_social), ("software", args.software),
                         ("responsavel", args.responsavel), ("email", args.email), ("telefone", args.telefone)):
        if valor is not None:
            org[chave] = valor.strip()
    if org["cnpj"]:
        org["cnpj"] = normalizar_cnpj(org["cnpj"])

    mascara = Mascara(mascarar_chave_pix=not args.mostrar_chave_pix)
    for item in por_id.values():
        mascara.aprender(item.get("corpo"))

    with zipfile.ZipFile(origem) as zin:
        xml_original = zin.read("word/document.xml")
        parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
        raiz = etree.fromstring(xml_original, parser)
        body = raiz.find(q("body"))
        campos = localizar(body, testes)

        # Estado de partida: tudo vazio e desmarcado (senão é outro arquivo).
        todos_texto = [r for r in campos_do(body) if r.find(q("fldChar")).find(q("ffData")).find(q("textInput")) is not None]
        todos_check = [r for r in campos_do(body) if r.find(q("fldChar")).find(q("ffData")).find(q("checkBox")) is not None]
        ja_preenchidos = [i for i, r in enumerate(todos_texto) if ler_formtext(r) != PLACEHOLDER]
        if ja_preenchidos or any(checkbox_marcado(r) for r in todos_check):
            raise ErroRoteiro("O roteiro de origem já tem campos preenchidos: use o original em branco do C6.")

        esperado_por_campo: dict[int, str] = {}  # id(run) -> texto esperado

        for chave, run in campos["org"].items():
            if org[chave]:
                escrever_formtext(run, org[chave])
                esperado_por_campo[id(run)] = org[chave]

        for rotulo, run in campos["check"].items():
            marcar_checkbox(run, rotulo in marcar)

        prefixo = "[EXEMPLO FICTÍCIO - NÃO ENVIAR]\n" if eh_exemplo else ""
        resumo = []
        for tid in ids:
            run_status, run_corpo = campos["testes"][tid]
            item = por_id.get(tid)
            if not item or item.get("status") is None:
                motivo = (item or {}).get("erro") or "sem resultado"
                resumo.append((tid, None, mascara.texto(str(motivo))))
                continue
            st = formatar_status(item["status"])
            corpo = formatar_corpo(item.get("corpo"), mascara, args.max_itens, args.max_chars)
            nota = item.get("nota")
            if nota:
                corpo += "\n\nObs.: " + mascara.texto(str(nota).strip())
            st_txt = ("EXEMPLO " + st) if eh_exemplo else st
            corpo_txt = prefixo + corpo
            escrever_formtext(run_status, st_txt)
            escrever_formtext(run_corpo, corpo_txt, mono_pt=args.fonte_pt)
            esperado_por_campo[id(run_status)] = st_txt
            esperado_por_campo[id(run_corpo)] = corpo_txt
            resumo.append((tid, item["status"], ""))

        # Conferência 1: cada campo tem exatamente o que devia; os outros seguem vazios.
        n_preenchidos = 0
        for r in todos_texto:
            atual = ler_formtext(r)
            if id(r) in esperado_por_campo:
                if atual != esperado_por_campo[id(r)]:
                    raise ErroRoteiro("Conferência falhou: um campo não ficou com o texto esperado.")
                n_preenchidos += 1
            elif atual != PLACEHOLDER:
                raise ErroRoteiro("Conferência falhou: um campo fora do escopo foi alterado.")
        for rotulo, run in campos["check"].items():
            if checkbox_marcado(run) != (rotulo in marcar):
                raise ErroRoteiro(f"Conferência falhou: checkbox {rotulo!r} no estado errado.")
        outros_check = [r for r in todos_check if r not in campos["check"].values()]
        if any(checkbox_marcado(r) for r in outros_check):
            raise ErroRoteiro("Conferência falhou: checkbox fora da tabela de APIs marcado.")

        # Conferência 2: nenhum segredo no texto do documento.
        texto_doc = texto_de(body)
        vazamentos = []
        for nome in ENV_SEGREDOS + (() if args.mostrar_chave_pix else ("C6_PIX_KEY",)):
            v = os.environ.get(nome, "")
            if len(v) >= 6 and v in texto_doc:
                vazamentos.append(nome)
        if RE_JWT.search(texto_doc):
            vazamentos.append("JWT")
        if RE_PEM.search(texto_doc) or "PRIVATE KEY" in texto_doc:
            vazamentos.append("PEM/chave privada")
        if RE_BEARER.search(texto_doc):
            vazamentos.append("Bearer token")
        if re.search(r'"access_token":\s*"(?!<mascarado)', texto_doc):
            vazamentos.append("access_token")
        if vazamentos:
            raise ErroRoteiro("Segredo detectado no documento final: " + ", ".join(vazamentos) + ". Nada foi salvo.")

        # Mantém a declaração XML do original byte a byte (aspas duplas + CRLF do Word).
        m = re.match(rb"<\?xml[^>]*\?>\s*", xml_original)
        declaracao = m.group(0) if m else b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        novo_xml = declaracao + etree.tostring(raiz, encoding="UTF-8")

        saida.parent.mkdir(parents=True, exist_ok=True)
        tmp = saida.with_name(saida.name + ".tmp")
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                for info in zin.infolist():  # mesma ordem, mesmos metadados
                    dados = novo_xml if info.filename == "word/document.xml" else zin.read(info.filename)
                    zout.writestr(info, dados, compress_type=info.compress_type)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # Conferência 3: o .docx gravado abre e o XML relido bate com o gerado.
    try:
        with zipfile.ZipFile(tmp) as zchk:
            if zchk.testzip() is not None:
                raise ErroRoteiro("ZIP corrompido.")
            relido = etree.fromstring(zchk.read("word/document.xml"), etree.XMLParser(huge_tree=True))
            if texto_de(relido.find(q("body"))) != texto_doc:
                raise ErroRoteiro("Texto relido difere do gerado.")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, saida)

    # Resumo (sem segredos)
    sha = hashlib.sha256(origem.read_bytes()).hexdigest()[:16]
    print(f"Roteiro original : {origem.name} (sha256 {sha}…, não alterado)")
    if meta:
        info = {k: meta[k] for k in ("executado_em", "ambiente", "host", "executor") if k in meta}
        if info:
            print(f"Resultados       : {Path(args.resultados).name} {json.dumps(info, ensure_ascii=False)}")
    print(f"Saída            : {saida}")
    if eh_exemplo:
        print("MODO EXEMPLO     : dados fictícios ou de simulador. NÃO enviar este arquivo ao C6.")
    print("Organização      : " + ", ".join(f"{k}={'ok' if org[k] else 'A PREENCHER'}" for _, _, k in ORG_LINHAS))
    print(f"Checkbox         : {' e '.join(sorted(marcar))} marcado(s); demais desmarcados")
    print(f"Chave Pix        : {'visível' if args.mostrar_chave_pix else 'mascarada'}")
    print(f"Campos de texto  : {n_preenchidos} preenchidos de {len(todos_texto)}")
    print()
    print(f"{'Teste':<9} {'Roteiro':>7} {'Spec':>5} {'Retornado':>9}  Situação")
    divergentes = 0
    for tid, st, motivo in resumo:
        r_rot, r_spec = ESPERADO[tid]
        if st is None:
            sit = f"PENDENTE ({motivo})"
        elif st == r_rot:
            sit = "ok"
        elif st == r_spec:
            sit = "igual à spec, diverge do roteiro"
        else:
            sit = "DIVERGE (conferir antes de enviar)"
            divergentes += 1
        if st is not None and st >= 500:
            sit += " | 5xx: sandbox fora da janela/instável? rodar de novo"
        print(f"{tid:<9} {r_rot:>7} {r_spec if r_spec is not None else '-':>5} {st if st is not None else '-':>9}  {sit}")
    pend_org = [rot for _, rot, k in ORG_LINHAS if not org[k]]
    if pend_org:
        print("\nA preencher pelo dono: " + "; ".join(pend_org))
    if faltando:
        print(f"ROTEIRO INCOMPLETO: {len(faltando)} teste(s) sem resultado. Não enviar ao C6 assim.")
    if divergentes:
        print(f"{divergentes} teste(s) com status diferente do roteiro e da spec.")

    if args.pdf:
        exportar_pdf(saida)
    return 0


def exportar_pdf(docx: Path) -> None:
    soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not Path(soffice).exists():
        print("PDF: LibreOffice (soffice) não encontrado; pulei.")
        return
    pdf = docx.with_suffix(".pdf")
    pdf.unlink(missing_ok=True)  # não confundir um PDF antigo com sucesso
    with tempfile.TemporaryDirectory() as perfil:
        cmd = [soffice, f"-env:UserInstallation=file://{perfil}", "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", str(docx.parent), str(docx)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0 or not pdf.exists():
        print("PDF: falha na conversão (o .docx continua válido).")
    else:
        print(f"PDF              : {pdf}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Preenche uma cópia do Roteiro de Testes C6 v3.0.")
    ap.add_argument("--resultados", help="JSON do executor: lista de {id, status, corpo}")
    ap.add_argument("--roteiro", default=str(ROTEIRO_ORIGINAL), help="roteiro original em branco (só leitura)")
    ap.add_argument("--saida", help=f"padrão: docs/c6-homologacao/{NOME_SAIDA}")
    ap.add_argument("--parcial", action="store_true", help="aceita testes sem resultado (rascunho; não enviar)")
    ap.add_argument("--com-checkout", action="store_true",
                    help="marca também CHECKOUT e exige C_01..C_04, C_0501, C_0502 (padrão: só PIX)")
    ap.add_argument("--cnpj", help="CNPJ do cartão CNPJ (o dígito verificador é conferido)")
    ap.add_argument("--razao-social", help="razão social exatamente como no cartão CNPJ")
    ap.add_argument("--software", help=f"padrão: {ORG_PADRAO['software']}")
    ap.add_argument("--responsavel", help=f"padrão: {ORG_PADRAO['responsavel']}")
    ap.add_argument("--email", help=f"padrão: {ORG_PADRAO['email']}")
    ap.add_argument("--telefone", help=f"padrão: {ORG_PADRAO['telefone']} (\"\" deixa vazio)")
    ap.add_argument("--mostrar-chave-pix", action="store_true",
                    help="não mascara a chave Pix nos corpos (decisão do dono; padrão: mascarar)")
    ap.add_argument("--max-itens", type=int, default=3, help="itens por lista no corpo (0 = sem corte)")
    ap.add_argument("--max-chars", type=int, default=600, help="tamanho máximo de uma string no corpo (0 = sem corte)")
    ap.add_argument("--fonte-pt", type=float, default=8.0, help="corpo em Courier New neste tamanho (0 = fonte do roteiro)")
    ap.add_argument("--pdf", action="store_true", help="exporta também um PDF ao lado (LibreOffice)")
    args = ap.parse_args(argv)
    if args.fonte_pt is not None and args.fonte_pt <= 0:
        args.fonte_pt = None
    try:
        return montar(args)
    except ErroRoteiro as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
