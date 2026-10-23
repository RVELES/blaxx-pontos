"""Executor da homologação C6 (scripts/c6_homologacao.py) e simulador local.

O que se prova aqui:
  · mascaramento: access_token, client_secret, token do webhook, chave Pix
    (também URL-encoded), CPF e PEM nunca saem crus, nem na tela nem no arquivo
  · ordem: o plano é o do roteiro (AT_01, P_01_01..P_06_03) e a execução
    contra o simulador chama a API nessa ordem, encadeando txid/ids
  · recusa de produção: sem C6_ENV=producao E --producao juntos, não roda,
    e não abre conexão nenhuma
  · horário: seg-sex 7h-22h em Brasília (com margem antes das 22h); fora
    disso sai com código 2 sem tocar a rede
  · ponta a ponta no simulador: 25/25, JSON e resumo gravados, código 0;
    --estrito, sem Pix pago, 503 e escopo faltando dão código != 0

Nenhum teste fala com o C6: um fixture recusa qualquer conexão que não seja
127.0.0.1, e o simulador só escuta lá.

Roda com:
    cd backend && python -m pytest tests/test_c6_homologacao.py -v
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SP = ZoneInfo("America/Sao_Paulo")


def _carregar(nome: str):
    if nome in sys.modules:
        return sys.modules[nome]
    spec = importlib.util.spec_from_file_location(nome, SCRIPTS / f"{nome}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


sim = _carregar("c6_simulador")
hml = _carregar("c6_homologacao")

ROTEIRO = [
    "AT_01",
    "P_01_01", "P_01_02", "P_01_03", "P_01_04", "P_01_05",
    "P_02_01", "P_02_02", "P_02_03", "P_02_04",
    "P_03_01", "P_03_02", "P_03_03", "P_03_04",
    "P_04_01", "P_04_02", "P_04_03", "P_04_04",
    "P_05_01", "P_05_02", "P_05_03", "P_05_04",
    "P_06_01", "P_06_02", "P_06_03",
]

# Status que a SPEC define (o simulador segue a spec): é o que o C6 deve devolver.
STATUS_SPEC = {
    "AT_01": 200, "P_01_01": 201, "P_01_02": 201, "P_01_03": 200, "P_01_04": 200, "P_01_05": 200,
    "P_02_01": 201, "P_02_02": 200, "P_02_03": 200, "P_02_04": 200,
    "P_03_01": 202, "P_03_02": 202, "P_03_03": 200, "P_03_04": 200,
    "P_04_01": 201, "P_04_02": 200, "P_04_03": 200, "P_04_04": 200,
    "P_05_01": 200, "P_05_02": 200, "P_05_03": 201, "P_05_04": 200,
    "P_06_01": 200, "P_06_02": 200, "P_06_03": 204,
}

# Valores FICTÍCIOS para o modo real (nenhum é credencial de verdade).
SEGREDO_FALSO = "segredo-falso-de-teste-" + "z" * 20
CLIENT_ID_FALSO = "99999999-8888-4777-8666-555555555555"
CHAVE_FALSA = "chave.ficticia+teste@exemplo.invalid"
TOKEN_WEBHOOK_FALSO = "tok-webhook-falso-" + "y" * 24


@pytest.fixture(autouse=True)
def sem_rede_externa(monkeypatch):
    """Qualquer conexão fora de 127.0.0.1/localhost derruba o teste."""
    original = socket.create_connection
    tentativas: list = []

    def guarda(endereco, *a, **k):
        host = endereco[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            tentativas.append(host)
            raise AssertionError(f"teste tentou abrir conexão externa com {host}")
        return original(endereco, *a, **k)

    monkeypatch.setattr(socket, "create_connection", guarda)
    return tentativas


def _quinta(hora: int, minuto: int = 0) -> datetime:
    """22/10/2026 é quinta-feira. Devolve o instante em UTC (Brasília = UTC-3)."""
    return datetime(2026, 10, 22, hora, minuto, tzinfo=SP).astimezone(timezone.utc)


def _env_real(tmp_path: Path, **extra) -> dict:
    """Ambiente completo do modo sandbox, com valores fictícios."""
    crt, key = tmp_path / "c.crt", tmp_path / "c.key"
    crt.write_text("x" * 200)
    key.write_text("x" * 200)
    env = {
        "C6_CLIENT_ID": CLIENT_ID_FALSO,
        "C6_CLIENT_SECRET": SEGREDO_FALSO,
        "C6_PIX_KEY": CHAVE_FALSA,
        "C6_WEBHOOK_URL": f"https://exemplo.invalid/payments/c6/webhook/{TOKEN_WEBHOOK_FALSO}",
        "C6_CERT_PATH": str(crt),
        "C6_KEY_PATH": str(key),
        "C6_DEVEDOR_NOME": "Pessoa Ficticia",
        "C6_DEVEDOR_CPF": "529.982.247-25",
        "C6_DEVEDOR_LOGRADOURO": "Rua Ficticia, 1",
        "C6_DEVEDOR_CIDADE": "Sao Paulo",
        "C6_DEVEDOR_UF": "SP",
        "C6_DEVEDOR_CEP": "01001-000",
    }
    env.update(extra)
    return env


def _par_autoassinado(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teste-ficticio")])
    agora = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(nome).issuer_name(nome)
        .public_key(chave.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(days=1)).not_valid_after(agora + timedelta(days=30))
        .sign(chave, hashes.SHA256())
    )
    crt, key = tmp_path / "par.crt", tmp_path / "par.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(chave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return crt, key


def _rodar_simulador(tmp_path: Path, *extra_args, **opcoes_sim):
    """Sobe um simulador próprio (para inspecionar o estado) e roda o executor contra ele."""
    with sim.Simulador(0, atraso=0.05, **opcoes_sim) as s:
        codigo = hml.main(
            ["--simulador-url", s.url, "--saida", str(tmp_path), "--intervalo-lote", "0.06", *extra_args],
            env={}, dormir=time.sleep,
        )
        tokens = list(s.estado.tokens)
        chamadas = list(s.estado.chamadas)
    arquivos = sorted(tmp_path.glob("*.json"))
    doc = json.loads(arquivos[-1].read_text(encoding="utf-8")) if arquivos else None
    return codigo, doc, tokens, chamadas


# ───────────────────── horário ───────────────────── #

class TestJanela:
    @pytest.mark.parametrize("quando, esperado", [
        (datetime(2026, 10, 19, 6, 59), False),   # segunda, antes das 7h
        (datetime(2026, 10, 19, 7, 0), True),     # segunda, abriu
        (datetime(2026, 10, 22, 13, 30), True),   # quinta, meio do dia
        (datetime(2026, 10, 23, 21, 54), True),   # sexta, 6 min antes de fechar
        (datetime(2026, 10, 23, 21, 56), False),  # sexta, 4 min: não dá tempo
        (datetime(2026, 10, 23, 22, 0), False),   # sexta, fechou
        (datetime(2026, 10, 24, 10, 0), False),   # sábado
        (datetime(2026, 10, 25, 10, 0), False),   # domingo
    ])
    def test_janela_em_horario_de_brasilia(self, quando, esperado):
        ok, motivo = hml.dentro_da_janela(quando)  # sem fuso = Brasília
        assert ok is esperado, motivo

    def test_converte_utc_para_brasilia(self):
        # 10:30 UTC = 07:30 em Brasília (dentro); 01:30 UTC de sexta = 22:30 de quinta (fora).
        assert hml.dentro_da_janela(datetime(2026, 10, 22, 10, 30, tzinfo=timezone.utc))[0] is True
        assert hml.dentro_da_janela(datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc))[0] is False
        # 09:30 UTC = 06:30 em Brasília: o relógio da máquina em UTC não engana a checagem.
        assert hml.dentro_da_janela(datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc))[0] is False

    def test_fora_da_janela_sai_com_2_sem_tocar_a_rede(self, tmp_path, sem_rede_externa, capsys):
        sabado = datetime(2026, 10, 24, 10, 0, tzinfo=SP)
        codigo = hml.main(["--saida", str(tmp_path)], env=_env_real(tmp_path), relogio=lambda: sabado)
        assert codigo == 2
        assert sem_rede_externa == []
        assert list(tmp_path.glob("*.json")) == []
        assert "fora da janela" in capsys.readouterr().out

    def test_quinta_23h_tambem_sai(self, tmp_path):
        codigo = hml.main(["--saida", str(tmp_path)], env=_env_real(tmp_path), relogio=lambda: _quinta(23))
        assert codigo == 2

    def test_ignorar_janela_segue_para_a_checagem(self, tmp_path, capsys):
        sabado = datetime(2026, 10, 24, 10, 0, tzinfo=SP)
        env = _env_real(tmp_path, C6_CLIENT_SECRET="")
        codigo = hml.main(["--ignorar-janela", "--saida", str(tmp_path)], env=env, relogio=lambda: sabado)
        assert codigo == 3  # passou da janela e parou na configuração
        assert "C6_CLIENT_SECRET=FALTA" in capsys.readouterr().out


# ───────────────────── recusa de produção ───────────────────── #

class TestRecusaProducao:
    def test_c6_env_producao_sem_flag_recusa_sem_rede(self, tmp_path, sem_rede_externa, capsys):
        env = _env_real(tmp_path, C6_ENV="producao")
        codigo = hml.main(["--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10))
        assert codigo == 3
        assert sem_rede_externa == []
        assert "RECUSADO" in capsys.readouterr().out

    def test_flag_producao_sem_c6_env_recusa(self, tmp_path, capsys):
        codigo = hml.main(["--producao", "--saida", str(tmp_path)], env=_env_real(tmp_path),
                          relogio=lambda: _quinta(10))
        assert codigo == 3
        assert "C6_ENV=producao" in capsys.readouterr().out

    @pytest.mark.parametrize("valor", ["prod", "production", "produção", "PRODUCAO"])
    def test_apelidos_de_producao_tambem_exigem_flag(self, tmp_path, valor):
        env = _env_real(tmp_path, C6_ENV=valor)
        assert hml.main(["--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10)) == 3

    def test_c6_env_desconhecido_recusa(self, tmp_path):
        env = _env_real(tmp_path, C6_ENV="staging")
        assert hml.main(["--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10)) == 3

    def test_simulador_com_producao_recusa(self, tmp_path):
        assert hml.main(["--simulador", "--producao", "--saida", str(tmp_path)], env={}) == 3

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1:8765", "http://baas-api-sandbox.c6bank.info", "http://10.0.0.5:8765",
    ])
    def test_simulador_url_so_aceita_loopback_http(self, tmp_path, url):
        assert hml.main(["--simulador-url", url, "--saida", str(tmp_path)], env={}) == 3

    def test_host_por_modo(self):
        assert hml.config_de_env({}, "sandbox").host == hml.API_HOST_SANDBOX
        assert hml.config_de_env({}, "producao").host == hml.API_HOST_PROD


# ───────────────────── configuração ───────────────────── #

class TestConfiguracao:
    def test_falta_variavel_sai_com_3_e_nunca_imprime_valor(self, tmp_path, capsys, sem_rede_externa):
        env = _env_real(tmp_path, C6_PIX_KEY="", C6_DEVEDOR_CEP="123")
        codigo = hml.main(["--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10))
        saida = capsys.readouterr().out
        assert codigo == 3
        assert "C6_PIX_KEY=FALTA" in saida and "C6_DEVEDOR_CEP=FALTA" in saida
        assert SEGREDO_FALSO not in saida and CLIENT_ID_FALSO not in saida
        assert TOKEN_WEBHOOK_FALSO not in saida
        assert sem_rede_externa == []

    def test_cpf_com_digito_errado_e_recusado(self, tmp_path):
        cfg = hml.config_de_env(_env_real(tmp_path, C6_DEVEDOR_CPF="529.982.247-26"), "sandbox")
        itens = {n: ok for n, ok, _ in hml.conferir_config(cfg)}
        assert itens["C6_DEVEDOR_CPF"] is False

    def test_validadores_de_documento(self):
        assert hml.cpf_valido("52998224725") and not hml.cpf_valido("11111111111")
        assert hml.cnpj_valido("11.222.333/0001-81") and not hml.cnpj_valido("11222333000180")

    def test_checar_com_par_valido_nao_chama_o_c6(self, tmp_path, capsys, sem_rede_externa):
        crt, key = _par_autoassinado(tmp_path)
        env = _env_real(tmp_path, C6_CERT_PATH=str(crt), C6_KEY_PATH=str(key))
        codigo = hml.main(["--checar", "--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10))
        saida = capsys.readouterr().out
        assert codigo == 0, saida
        assert "par certificado/chave confere" in saida
        assert "Nenhuma chamada ao C6" in saida
        assert "PRIVATE KEY" not in saida
        assert sem_rede_externa == []
        assert list(tmp_path.glob("*.json")) == []

    def test_checar_com_par_trocado_diz_a_causa(self, tmp_path, capsys):
        crt, key = _par_autoassinado(tmp_path)
        env = _env_real(tmp_path, C6_CERT_PATH=str(key), C6_KEY_PATH=str(crt))  # invertidos
        codigo = hml.main(["--checar", "--saida", str(tmp_path)], env=env, relogio=lambda: _quinta(10))
        saida = capsys.readouterr().out
        assert codigo == 3
        assert "trocadas" in saida
        assert "BEGIN" not in saida


# ───────────────────── mascaramento ───────────────────── #

class TestMascaramento:
    def _masc(self, **kw):
        m = hml.Mascarador(**kw)
        m.segredo(SEGREDO_FALSO)
        m.parcial(CLIENT_ID_FALSO, "client_id")
        m.chave_pix(CHAVE_FALSA)
        m.segredo(TOKEN_WEBHOOK_FALSO, f"<TOKEN:{len(TOKEN_WEBHOOK_FALSO)}ch>")
        return m

    def test_access_token_por_nome_de_campo(self):
        m = self._masc()
        corpo = {"access_token": "eyJ.abc.def", "expires_in": 300, "token_type": "Bearer", "scope": "cob.read"}
        saida = m.objeto(corpo)
        assert saida["access_token"] == "<mascarado, 11 caracteres>"
        assert saida["scope"] == "cob.read" and saida["expires_in"] == 300

    def test_token_emitido_some_de_qualquer_texto(self):
        m = self._masc()
        token = "tokEmitidoPeloC6_" + "q" * 30
        m.segredo(token)
        texto = m.texto(f"Authorization: Bearer {token} e de novo {token}")
        assert token not in texto
        assert "<mascarado," in texto

    def test_bearer_desconhecido_tambem_e_mascarado(self):
        assert "abcdefghijklmnop" not in hml.Mascarador().texto("Bearer abcdefghijklmnop")

    def test_client_secret_nunca_aparece(self):
        m = self._masc()
        form = {"client_id": CLIENT_ID_FALSO, "client_secret": SEGREDO_FALSO, "grant_type": "client_credentials"}
        texto = json.dumps(m.objeto(form), ensure_ascii=False)
        assert SEGREDO_FALSO not in texto
        assert CLIENT_ID_FALSO not in texto and "…5555" in texto  # client_id só com o final
        assert m.texto(f"erro com {SEGREDO_FALSO}") == f"erro com <mascarado, {len(SEGREDO_FALSO)} caracteres>"

    def test_webhook_url_com_token_conhecido_e_desconhecido(self):
        m = self._masc()
        nosso = m.objeto({"webhookUrl": f"https://x.invalid/payments/c6/webhook/{TOKEN_WEBHOOK_FALSO}"})
        assert TOKEN_WEBHOOK_FALSO not in json.dumps(nosso)
        assert nosso["webhookUrl"].endswith(f"/webhook/<TOKEN:{len(TOKEN_WEBHOOK_FALSO)}ch>")
        # Uma URL antiga, com outro token, também sai mascarada (pela regra do campo).
        outro = m.objeto({"webhookUrl": "https://x.invalid/payments/c6/webhook/tokenAntigoQualquer"})
        assert "tokenAntigoQualquer" not in outro["webhookUrl"]

    def test_chave_pix_em_qualquer_forma(self):
        m = self._masc()
        from urllib.parse import quote

        texto = m.texto(f"/v2/pix/webhook/{quote(CHAVE_FALSA, safe='')} e chave={CHAVE_FALSA}")
        assert CHAVE_FALSA not in texto and quote(CHAVE_FALSA, safe="") not in texto
        assert texto.count("<chave-pix:…alid>") == 2

    def test_expor_chave_pix_e_decisao_explicita(self):
        m = self._masc(expor_chave_pix=True)
        assert CHAVE_FALSA in m.texto(f"chave={CHAVE_FALSA}")

    def test_documento_so_com_dois_ultimos_digitos(self):
        saida = hml.Mascarador().objeto({"devedor": {"cpf": "52998224725", "cnpj": "11.222.333/0001-81"}})
        assert saida["devedor"]["cpf"] == "*********25"
        assert saida["devedor"]["cnpj"] == "**.***.***/****-81"

    def test_pem_e_removido(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"
        texto = hml.Mascarador().texto(f"antes {pem} depois")
        assert "PRIVATE KEY" not in texto and "<PEM removido>" in texto

    def test_vazamentos_detecta_o_que_sobrou(self):
        m = self._masc()
        assert m.vazamentos("tudo limpo <mascarado, 3 caracteres>") == []
        assert m.vazamentos(f"sujo {SEGREDO_FALSO}") != []


# ───────────────────── ordem do roteiro ───────────────────── #

class TestOrdem:
    def test_plano_e_o_roteiro_na_ordem(self):
        assert [t.id for t in hml.PLANO] == ROTEIRO

    def test_tabelas_do_docx_batem_com_o_mapa(self):
        esperado = {
            "AT_01": 4, "P_01_01": 53, "P_01_02": 55, "P_01_03": 57, "P_01_04": 59, "P_01_05": 61,
            "P_02_01": 63, "P_02_02": 65, "P_02_03": 67, "P_02_04": 69, "P_03_01": 71, "P_03_02": 73,
            "P_03_03": 75, "P_03_04": 77, "P_04_01": 79, "P_04_02": 81, "P_04_03": 83, "P_04_04": 85,
            "P_05_01": 87, "P_05_02": 89, "P_05_03": 91, "P_05_04": 93, "P_06_01": 95, "P_06_02": 97,
            "P_06_03": 99,
        }
        assert {t.id: t.tabela_docx for t in hml.PLANO} == esperado
        rotulos = {t.id: t.rotulo_docx for t in hml.PLANO if t.rotulo_docx}
        assert rotulos == {"P_03_03": "P_02_03", "P_04_04": "P_02_04"}

    def test_status_do_roteiro_e_da_spec(self):
        divergentes = {t.id: (t.status_roteiro, t.status_spec) for t in hml.PLANO if t.status_roteiro != t.status_spec}
        assert divergentes == {"P_01_02": (200, 201), "P_01_03": (201, 200),
                               "P_03_01": (201, 202), "P_03_02": (200, 202)}
        assert {t.id: t.status_spec for t in hml.PLANO} == STATUS_SPEC

    def test_execucao_chama_a_api_na_ordem_e_encadeia_ids(self, tmp_path):
        codigo, doc, _tokens, chamadas = _rodar_simulador(tmp_path)
        assert codigo == 0
        testes = doc["testes"]
        assert [t["id"] for t in testes] == ROTEIRO
        horarios = [t["horario"] for t in testes]
        assert horarios == sorted(horarios)

        # As chamadas principais aparecem no simulador nesta ordem (as auxiliares ficam no meio).
        principais = [(t["metodo"], t["endpoint"].split("?")[0]) for t in testes]
        iterador = iter(chamadas)
        for metodo, caminho in principais:
            if "<chave-pix" in caminho:
                caminho = caminho.split("<chave-pix")[0]
            assert any(m == metodo and c.startswith(caminho) for m, c in iterador), (metodo, caminho)

        por_id = {t["id"]: t for t in testes}
        txid = por_id["P_01_01"]["corpo_retornado"]["txid"]
        assert por_id["P_01_03"]["endpoint"].endswith(txid) and por_id["P_01_04"]["endpoint"].endswith(txid)
        assert por_id["P_01_03"]["corpo_retornado"]["revisao"] == 1
        assert por_id["P_01_02"]["corpo_retornado"]["txid"] != txid
        txid_v = por_id["P_02_01"]["corpo_retornado"]["txid"]
        assert por_id["P_02_02"]["endpoint"].endswith(txid_v) and por_id["P_02_03"]["endpoint"].endswith(txid_v)
        lote = por_id["P_03_01"]["endpoint"].rsplit("/", 1)[1]
        assert por_id["P_03_02"]["endpoint"].endswith(lote) and por_id["P_03_03"]["endpoint"].endswith(lote)
        assert {c["status"] for c in por_id["P_03_03"]["corpo_retornado"]["cobsv"]} == {"CRIADA"}
        loc = str(por_id["P_04_01"]["corpo_retornado"]["id"])
        assert por_id["P_04_03"]["endpoint"].endswith(f"/loc/{loc}")
        assert por_id["P_04_03"]["corpo_retornado"].get("txid")  # vinculada pela auxiliar
        assert por_id["P_04_04"]["endpoint"].endswith(f"/loc/{loc}/txid")
        assert "txid" not in por_id["P_04_04"]["corpo_retornado"]
        e2e = por_id["P_05_01"]["corpo_retornado"]["endToEndId"]
        assert e2e in por_id["P_05_03"]["endpoint"] and e2e in por_id["P_05_04"]["endpoint"]
        dev = por_id["P_05_03"]["corpo_retornado"]["id"]
        assert por_id["P_05_04"]["endpoint"].endswith(f"/devolucao/{dev}")
        # Listas sempre com inicio e fim (P_04_02 inclusive, que o roteiro não cita).
        for tid in ("P_01_05", "P_02_04", "P_03_04", "P_04_02", "P_05_02"):
            assert "inicio=" in por_id[tid]["endpoint"] and "fim=" in por_id[tid]["endpoint"]


# ───────────────────── ponta a ponta no simulador ───────────────────── #

class TestPontaAPonta:
    def test_simulador_25_de_25(self, tmp_path):
        codigo, doc, tokens, _ = _rodar_simulador(tmp_path)
        assert codigo == 0
        assert doc["modo"] == "simulador" and "NÃO" in doc["aviso"]
        assert doc["resumo"] == {"total": 25, "ok": 25, "divergente": 0, "pulado": 0,
                                 "erro_de_rede": 0, "codigo_de_saida": 0}
        assert {t["id"]: t["status_retornado"] for t in doc["testes"]} == STATUS_SPEC
        assert doc["escopos_faltando"] == []
        for t in doc["testes"]:
            assert t["x_request_id"], t["id"]
            assert t["horario"].endswith("-03:00")
            if t["corpo_retornado"] is not None:
                assert t["corpo_retornado_indentado"] == json.dumps(t["corpo_retornado"], ensure_ascii=False, indent=2)
        # P_03_01 e P_03_02 aguardaram o lote assíncrono com consultas auxiliares.
        assert any("aguardar o processamento do lote" in a["titulo"] for a in doc["auxiliares"])
        assert len(list(tmp_path.glob("*-simulador.md"))) == 1

    def test_arquivos_sem_nenhum_segredo(self, tmp_path):
        codigo, doc, tokens, _ = _rodar_simulador(tmp_path)
        assert codigo == 0 and tokens
        textos = [p.read_text(encoding="utf-8") for p in tmp_path.iterdir()]
        assert len(textos) == 2
        for texto in textos:
            for proibido in (*tokens, hml.SIM_CLIENT_SECRET, hml.SIM_WEBHOOK_TOKEN, hml.SIM_PIX_KEY,
                             hml.SIM_CLIENT_ID, "52998224725"):
                assert proibido not in texto
        at = doc["testes"][0]
        assert at["corpo_retornado"]["access_token"].startswith("<mascarado, ")
        assert at["corpo_enviado"]["client_secret"].startswith("<mascarado, ")
        assert "<TOKEN:" in json.dumps(doc["testes"][23]["corpo_retornado"])  # P_06_02 devolve a URL

    def test_resumo_md_tem_o_bloco_de_cada_tabela(self, tmp_path):
        _rodar_simulador(tmp_path)
        md = sorted(tmp_path.glob("*.md"))[-1].read_text(encoding="utf-8")
        assert "SIMULADOR LOCAL" in md
        for t in hml.PLANO:
            assert f"### {t.id} · " in md and f"tabela nº {t.tabela_docx}" in md
        assert 'rotulado "TESTE: P_02_03"' in md and 'rotulado "TESTE: P_02_04"' in md

    def test_estrito_reprova_os_4_divergentes(self, tmp_path):
        codigo, doc, _, _ = _rodar_simulador(tmp_path, "--estrito")
        assert codigo == 1
        divergentes = sorted(t["id"] for t in doc["testes"] if t["situacao"] == "divergente")
        assert divergentes == ["P_01_02", "P_01_03", "P_03_01", "P_03_02"]

    def test_sem_pix_pago_pula_p05_e_sai_com_1(self, tmp_path):
        codigo, doc, _, _ = _rodar_simulador(tmp_path, semear_pix=False)
        assert codigo == 1
        situacao = {t["id"]: t["situacao"] for t in doc["testes"]}
        assert situacao["P_05_01"] == situacao["P_05_03"] == situacao["P_05_04"] == "pulado"
        assert situacao["P_05_02"] == "ok"  # lista vazia ainda é 200
        assert "C6_E2EID" in next(t for t in doc["testes"] if t["id"] == "P_05_01")["motivo"]
        assert situacao["P_06_03"] == "ok"  # o resto do roteiro roda

    def test_e2eid_informado_vence_a_busca(self, tmp_path):
        with sim.Simulador(0, atraso=0.05) as s:
            e2e = next(iter(s.estado.pix))
            codigo = hml.main(["--simulador-url", s.url, "--saida", str(tmp_path), "--intervalo-lote", "0.06",
                               "--e2eid", e2e], env={})
        doc = json.loads(sorted(tmp_path.glob("*.json"))[-1].read_text(encoding="utf-8"))
        assert codigo == 0
        assert not any("procurar um Pix" in a["titulo"] for a in doc["auxiliares"])

    def test_sandbox_fora_do_ar_aborta_no_at01_com_4(self, tmp_path):
        codigo, doc, _, chamadas = _rodar_simulador(tmp_path, fora_da_janela=True)
        assert codigo == 4
        assert doc["testes"][0]["status_retornado"] == 503
        assert doc["testes"][0]["corpo_retornado"]["title"] == "Serviço Indisponível"
        assert all(t["situacao"] == "pulado" for t in doc["testes"][1:])
        assert len(chamadas) == 1  # só o AT_01: não gasta a janela

    def test_escopo_faltando_aborta_antes_dos_testes_pix(self, tmp_path):
        poucos = " ".join(e for e in sim.ESCOPOS_PADRAO if not e.startswith("webhook"))
        codigo, doc, _, chamadas = _rodar_simulador(tmp_path, escopos=poucos)
        assert codigo == 4
        assert doc["escopos_faltando"] == ["webhook.read", "webhook.write"]
        assert len(chamadas) == 1

    def test_ignorar_escopos_segue_e_registra_o_403(self, tmp_path):
        poucos = " ".join(e for e in sim.ESCOPOS_PADRAO if not e.startswith("webhook"))
        codigo, doc, _, _ = _rodar_simulador(tmp_path, "--ignorar-escopos", escopos=poucos)
        assert codigo == 1
        assert {t["id"]: t["status_retornado"] for t in doc["testes"]}["P_06_01"] == 403

    def test_401_renova_o_token_e_repete_uma_vez(self, tmp_path):
        with sim.Simulador(0, atraso=0.05) as s:
            masc = hml.Mascarador()
            cfg = hml.config_simulador(s.url)
            args = hml._parser().parse_args(["--simulador-url", s.url])
            args.intervalo_lote = 0.01
            ex = hml.Execucao(cfg, hml.ClienteHttp(s.url, None, sem_proxy=True), masc, args,
                              lambda: datetime.now(timezone.utc), lambda _s: None)
            assert ex.executar_teste(hml.PLANO[0])["situacao"] == "ok"
            s.estado.invalidar_tokens()
            reg = ex.executar_teste(hml.PLANO[1])  # P_01_01
        assert reg["status_retornado"] == 201
        assert any("401" in a["titulo"] for a in ex.auxiliares)
        assert any("renovar o token" in a["titulo"] for a in ex.auxiliares)
        assert any("401" in o for o in reg["observacoes"])

    def test_json_segue_o_contrato_do_preenchedor(self, tmp_path):
        """docs/c6-homologacao/formato-resultados.md: {executado_em, ambiente, host, executor, resultados}."""
        _, doc, _, _ = _rodar_simulador(tmp_path)
        assert doc["ambiente"] == "simulador" and doc["simulado"] is True  # o preenchedor trata como EXEMPLO
        assert doc["executor"].startswith("scripts/c6_homologacao.py") and doc["executado_em"]
        itens = doc["resultados"]
        assert [i["id"] for i in itens] == ROTEIRO
        for i in itens:
            assert "corpo" in i
            assert isinstance(i["status"], int) and 100 <= i["status"] <= 599
            assert not i["url"].startswith("http")  # sem host
        por = {i["id"]: i for i in itens}
        assert por["P_06_03"]["corpo"] is None and por["P_03_01"]["corpo"] is None
        assert por["AT_01"]["corpo"]["access_token"].startswith("<mascarado, ")
        assert "especificação" in por["P_01_02"]["nota"]
        # A nota vai para o .docx enviado ao C6: nada de arquivo interno nem de jargão nosso.
        notas = " ".join(i.get("nota", "") for i in itens)
        assert ".md" not in notas and "auxiliar" not in notas

    def test_pulado_vira_status_null_com_erro(self, tmp_path):
        _, doc, _, _ = _rodar_simulador(tmp_path, semear_pix=False)
        por = {i["id"]: i for i in doc["resultados"]}
        assert por["P_05_01"]["status"] is None and "e2eid" in por["P_05_01"]["erro"]
        assert por["P_05_02"]["status"] == 200

    def test_simulador_padrao_em_processo(self, tmp_path):
        """`--simulador` sobe o próprio simulador e derruba no fim."""
        codigo = hml.main(["--simulador", "--saida", str(tmp_path), "--intervalo-lote", "0.3"], env={})
        assert codigo == 0
        assert len(list(tmp_path.glob("*-simulador.json"))) == 1


# ───────────────────── simulador (contrato mínimo) ───────────────────── #

class TestSimulador:
    def test_so_escuta_em_loopback(self):
        with pytest.raises(ValueError):
            sim.Simulador(0, host="0.0.0.0")

    def test_sem_token_401_e_lista_sem_periodo_400(self):
        abrir = urllib.request.build_opener(urllib.request.ProxyHandler({})).open  # sem proxy do ambiente

        with sim.Simulador(0) as s:
            with pytest.raises(urllib.error.HTTPError) as exc:
                abrir(s.url + "/v2/pix/cob?inicio=x", timeout=5)
            assert exc.value.code == 401
            assert exc.value.headers.get("x-request-id")
            st, corpo = s.estado._autenticar(b"client_id=a&client_secret=b&grant_type=client_credentials",
                                             "application/x-www-form-urlencoded")
            req = urllib.request.Request(s.url + "/v2/pix/loc",
                                         headers={"Authorization": "Bearer " + corpo["access_token"]})
            with pytest.raises(urllib.error.HTTPError) as exc:
                abrir(req, timeout=5)
            assert exc.value.code == 400
            problema = json.loads(exc.value.read())
            assert problema["type"].startswith("https://pix.bcb.gov.br/api/v2/error/")
            assert {v["propriedade"] for v in problema["violacoes"]} == {"inicio", "fim"}
