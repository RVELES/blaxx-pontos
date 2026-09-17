"""C6 Bank, cobrança PIX (entrada de dinheiro) e webhook.

O que se prova aqui:
  · autenticação OAuth2 client_credentials com cache e renovação em 401
  · PUT /cob/{txid} com o corpo do padrão Bacen (valor string 2 casas,
    chave, expiração, devedor só com CPF/CNPJ válido)
  · idempotência: PUT duplicado ou timeout recupera a cobrança por GET
  · sem pixCopiaECola a criação FALHA claro (nunca charge sem copia-e-cola)
  · webhook: token no caminho (401 sem ele), crédito só após reconsulta
    CONCLUIDA, entrega duplicada não dobra pontos, rota `/pix` do Bacen

Roda com:
    cd backend && python -m pytest tests/test_c6_cobranca.py -v
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import PixCharge, PixChargeStatus, User, Wallet
from app.pix import c6 as c6_mod
from app.pix.c6 import C6Error, C6PixProvider, resolve_cert_paths
from app.pix.provider import PixChargeRequest, PixChargeResponse

VALID_CPF = "52998224725"
WEBHOOK_TOKEN = "tok-webhook-c6-" + "x" * 20
# Valores FICTÍCIOS de propósito: a chave e o client_id reais do sandbox
# ficam só nas env vars, nunca no repositório.
PIX_KEY = "11111111-2222-4333-8444-555555555555"

AUTH_OK = (200, json.dumps({
    "access_token": "jwt-1", "expires_in": 300, "token_type": "Bearer",
    "scope": "cob.read cob.write pix.read pix.write webhook.read webhook.write",
}))


class _StubSSL:
    def load_cert_chain(self, certfile, keyfile):
        self.certfile, self.keyfile = certfile, keyfile


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(c6_mod.ssl, "create_default_context", lambda: _StubSSL())
    return C6PixProvider(
        client_id="99999999-8888-4777-8666-555555555555",
        client_secret="segredo",
        pix_key=PIX_KEY,
        cert_path="/dev/null",
        key_path="/dev/null",
        sandbox=True,
    )


class _Http:
    """Roteia `_http` por (método, trecho da URL). Guarda as chamadas."""

    def __init__(self, **routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def __call__(self, method, url, data, headers):
        body = json.loads(data.decode()) if data and headers.get("Content-Type", "").startswith("application/json") else None
        self.calls.append((method, url, body, headers))
        if url.endswith("/v1/auth/"):
            return AUTH_OK
        for key, resp in self.routes.items():
            if key in f"{method} {url}":
                return resp(self.calls) if callable(resp) else resp
        return 404, json.dumps({"title": "Não encontrado"})


def _req(txid="A" * 30, cents=18000):
    return PixChargeRequest(
        txid=txid, amount_cents=cents, description="BlaXx, pacote Start",
        payer_name="João Silva", payer_cpf="529.982.247-25",
        expires_in_seconds=3600, payer_email="joao@test.com",
    )


COB_OK = {"txid": "A" * 30, "status": "ATIVA", "pixCopiaECola": "00020126580014BR.GOV.BCB.PIX..."}


# ───────────────────── autenticação ───────────────────── #

class TestAuth:
    def test_token_is_cached_between_requests(self, provider, monkeypatch):
        http = _Http(**{"GET ": (200, json.dumps(COB_OK))})
        monkeypatch.setattr(provider, "_http", http)

        provider.get_cob("A" * 30)
        provider.get_cob("A" * 30)

        auths = [c for c in http.calls if c[1].endswith("/v1/auth/")]
        assert len(auths) == 1, "segundo GET tem que reaproveitar o token"
        form = http.calls[0]
        assert form[3]["Content-Type"] == "application/x-www-form-urlencoded"
        gets = [c for c in http.calls if c[0] == "GET"]
        assert gets[0][3]["Authorization"] == "Bearer jwt-1"

    def test_401_renews_token_once_and_retries(self, provider, monkeypatch):
        state = {"n": 0}

        def cob(calls):
            state["n"] += 1
            if state["n"] == 1:
                return 401, json.dumps({"title": "Unauthorized"})
            return 200, json.dumps(COB_OK)

        http = _Http(**{"GET ": cob})
        monkeypatch.setattr(provider, "_http", http)

        assert provider.get_cob("A" * 30)["status"] == "ATIVA"
        auths = [c for c in http.calls if c[1].endswith("/v1/auth/")]
        assert len(auths) == 2

    def test_auth_failure_is_explicit(self, provider, monkeypatch):
        def http(method, url, data, headers):
            return 503, json.dumps({"title": "Serviço indisponível", "correlation_id": "abc"})
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error) as exc:
            provider.get_cob("A" * 30)
        assert "503" in str(exc.value) and "correlation_id=abc" in str(exc.value)


# ───────────────────── criação da cobrança ───────────────────── #

class TestCreateCharge:
    def test_put_cob_with_bacen_body(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (201, json.dumps(COB_OK))})
        monkeypatch.setattr(provider, "_http", http)

        resp = provider.create_charge(_req())

        assert resp.br_code.startswith("00020126")
        assert resp.qr_code_image.startswith("data:image/png;base64,")
        put = [c for c in http.calls if c[0] == "PUT"][0]
        assert put[1].endswith("/v2/pix/cob/" + "A" * 30)
        body = put[2]
        # modalidadeAlteracao 0: o pagador não pode alterar o valor.
        assert body["valor"] == {"original": "180.00", "modalidadeAlteracao": 0}
        assert body["chave"] == PIX_KEY
        assert body["calendario"] == {"expiracao": 3600}
        assert body["devedor"] == {"cpf": VALID_CPF, "nome": "João Silva"}
        assert len(body["solicitacaoPagador"]) <= 140

    def test_cnpj_goes_in_devedor_and_bad_doc_is_omitted(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (201, json.dumps(COB_OK))})
        monkeypatch.setattr(provider, "_http", http)

        provider.create_charge(PixChargeRequest(
            txid="B" * 30, amount_cents=1000, description="x", payer_name="Empresa SA",
            payer_cpf="12.345.678/0001-95", expires_in_seconds=600))
        provider.create_charge(PixChargeRequest(
            txid="C" * 30, amount_cents=1000, description="x", payer_name="Sem Doc",
            payer_cpf="123", expires_in_seconds=600))

        puts = [c for c in http.calls if c[0] == "PUT"]
        assert puts[0][2]["devedor"] == {"cnpj": "12345678000195", "nome": "Empresa SA"}
        assert "devedor" not in puts[1][2]

    def test_amount_never_uses_float_rounding(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (201, json.dumps(COB_OK))})
        monkeypatch.setattr(provider, "_http", http)
        provider.create_charge(_req(cents=1005))
        assert [c for c in http.calls if c[0] == "PUT"][0][2]["valor"]["original"] == "10.05"

    def test_txid_invalido_nao_cria_cobranca(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (201, json.dumps(COB_OK))})
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.create_charge(_req(txid="curto"))
        assert http.calls == []

    def test_duplicate_put_recovers_existing_cob(self, provider, monkeypatch):
        http = _Http(**{
            "PUT ": (400, json.dumps({"title": "Cobrança já existe", "detail": "txid duplicado"})),
            "GET ": (200, json.dumps({**COB_OK, "pixCopiaECola": "brcode-existente"})),
        })
        monkeypatch.setattr(provider, "_http", http)

        resp = provider.create_charge(_req())

        assert resp.br_code == "brcode-existente"
        assert len([c for c in http.calls if c[0] == "PUT"]) == 1

    def test_timeout_on_put_consults_before_failing(self, provider, monkeypatch):
        def http(method, url, data, headers):
            if url.endswith("/v1/auth/"):
                return AUTH_OK
            if method == "PUT":
                raise c6_mod._IndeterminateError("timed out")
            return 200, json.dumps(COB_OK)
        monkeypatch.setattr(provider, "_http", http)

        resp = provider.create_charge(_req())
        assert resp.br_code == COB_OK["pixCopiaECola"]

    def test_timeout_and_no_cob_is_an_error(self, provider, monkeypatch):
        def http(method, url, data, headers):
            if url.endswith("/v1/auth/"):
                return AUTH_OK
            if method == "PUT":
                raise c6_mod._IndeterminateError("timed out")
            return 404, json.dumps({"title": "não encontrada"})
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.create_charge(_req())

    def test_raises_when_no_brcode(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (201, json.dumps({"txid": "A" * 30, "status": "ATIVA"})),
                        "GET ": (404, "{}")})
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error) as exc:
            provider.create_charge(_req())
        assert "pixCopiaECola" in str(exc.value)


# ───────────────────── consultas e webhook (registro) ───────────────────── #

class TestQueries:
    @pytest.mark.parametrize("status,expected", [
        ("ATIVA", "pending"), ("CONCLUIDA", "paid"),
        ("REMOVIDA_PELO_USUARIO_RECEBEDOR", "cancelled"), ("QUALQUER", "unknown"),
    ])
    def test_status_map(self, provider, monkeypatch, status, expected):
        http = _Http(**{"GET ": (200, json.dumps({"status": status}))})
        monkeypatch.setattr(provider, "_http", http)
        assert provider.get_charge_status("A" * 30) == expected

    def test_status_unknown_when_api_fails(self, provider, monkeypatch):
        http = _Http(**{"GET ": (500, "{}")})
        monkeypatch.setattr(provider, "_http", http)
        assert provider.get_charge_status("A" * 30) == "unknown"

    def test_cob_paid_cents_soma_pix(self):
        cob = {"status": "CONCLUIDA", "valor": {"original": "180.00"},
               "pix": [{"endToEndId": "E1", "valor": "180.00"}]}
        assert C6PixProvider.cob_paid_cents(cob) == 18000
        assert not C6PixProvider.cob_has_refund(cob)

    def test_cob_paid_cents_desconta_devolucao(self):
        """Devolvido não é pago: creditar aqui daria pontos por dinheiro que saiu."""
        cob = {"status": "CONCLUIDA", "valor": {"original": "180.00"}, "pix": [{
            "endToEndId": "E1", "valor": "180.00",
            "devolucoes": [{"valor": "180.00", "status": "DEVOLVIDO"}]}]}
        assert C6PixProvider.cob_paid_cents(cob) == 0
        assert C6PixProvider.cob_has_refund(cob)

    def test_devolucao_em_processamento_ainda_nao_liquidou(self):
        cob = {"status": "CONCLUIDA", "pix": [{
            "valor": "180.00",
            "devolucoes": [{"valor": "180.00", "status": "EM_PROCESSAMENTO"}]}]}
        assert C6PixProvider.cob_paid_cents(cob) == 0

    def test_concluida_sem_lista_de_pix_cai_no_valor_original(self):
        assert C6PixProvider.cob_paid_cents(
            {"status": "CONCLUIDA", "valor": {"original": "37.00"}}) == 3700
        assert C6PixProvider.cob_paid_cents({"status": "ATIVA", "valor": {"original": "37.00"}}) == 0

    def test_txid_fora_do_padrao_nunca_vira_url(self, provider, monkeypatch):
        """txid do webhook é entrada externa: sem validação viraria path na
        API do C6, com o nosso certificado mTLS."""
        http = _Http()
        monkeypatch.setattr(provider, "_http", http)
        for ruim in ("../webhook/chave", "curto", "A" * 40, "com espaço" + "A" * 20):
            with pytest.raises(C6Error) as exc:
                provider.get_cob(ruim)
            assert "txid" in str(exc.value)
        assert http.calls == [], "nenhuma chamada pode ter saído"

    def test_register_webhook_uses_pix_key_path(self, provider, monkeypatch):
        http = _Http(**{"PUT ": (200, "{}")})
        monkeypatch.setattr(provider, "_http", http)
        provider.register_webhook("https://api.exemplo/payments/c6/webhook/tok")
        put = [c for c in http.calls if c[0] == "PUT"][0]
        assert put[1].endswith("/v2/pix/webhook/" + PIX_KEY)
        assert put[2] == {"webhookUrl": "https://api.exemplo/payments/c6/webhook/tok"}

    def test_payout_is_not_supported(self, provider):
        from app.pix.provider import PixPayoutRequest
        with pytest.raises(NotImplementedError):
            provider.request_payout(PixPayoutRequest(txid="x", amount_cents=1, pix_key="k", description=""))


# ───────────────────── certificado ───────────────────── #

def _gerar_par() -> tuple[str, str]:
    """Par autoassinado descartável: o teste exercita o caminho real do SSL."""
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "blaxx-teste")])
    agora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(nome).issuer_name(nome)
            .public_key(chave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - timedelta(days=1))
            .not_valid_after(agora + timedelta(days=365))
            .sign(chave, hashes.SHA256()))
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        chave.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode(),
    )


@pytest.fixture(scope="module")
def par_pem():
    return _gerar_par()


@pytest.fixture(scope="module")
def outro_par_pem():
    return _gerar_par()


class TestCertMaterial:
    def test_pem_inline_becomes_private_tempfiles(self, par_pem):
        cert_pem, key_pem = par_pem
        cert, key = resolve_cert_paths(cert_pem=cert_pem, key_pem=key_pem)
        try:
            assert "BEGIN CERTIFICATE" in open(cert).read()
            # A chave nunca pode ficar legível para outros processos do host.
            assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
            # E o par tem que carregar de verdade no contexto TLS.
            import ssl as _ssl
            _ssl.create_default_context().load_cert_chain(certfile=cert, keyfile=key)
        finally:
            os.unlink(cert)
            os.unlink(key)

    def test_pem_achatado_ainda_carrega_no_ssl(self, par_pem):
        """O caso que derrubou o deploy: colado em campo de linha única."""
        cert_pem, key_pem = par_pem
        cert, key = resolve_cert_paths(
            cert_pem=" ".join(cert_pem.split()), key_pem=" ".join(key_pem.split()))
        try:
            import ssl as _ssl
            _ssl.create_default_context().load_cert_chain(certfile=cert, keyfile=key)
        finally:
            os.unlink(cert)
            os.unlink(key)

    def test_variaveis_trocadas_dizem_que_estao_trocadas(self, par_pem):
        cert_pem, key_pem = par_pem
        with pytest.raises(C6Error) as exc:
            resolve_cert_paths(cert_pem=key_pem, key_pem=cert_pem)
        assert "trocadas" in str(exc.value)

    def test_par_que_nao_casa_e_recusado(self, par_pem, outro_par_pem):
        cert_pem, _ = par_pem
        _, key_de_outro = outro_par_pem
        with pytest.raises(C6Error) as exc:
            resolve_cert_paths(cert_pem=cert_pem, key_pem=key_de_outro)
        assert "não são o mesmo par" in str(exc.value)

    def test_material_cortado_ao_colar_e_recusado(self, par_pem):
        cert_pem, key_pem = par_pem
        with pytest.raises(C6Error) as exc:
            resolve_cert_paths(cert_pem=cert_pem[:len(cert_pem) // 2], key_pem=key_pem)
        assert "bloco PEM completo" in str(exc.value)

    def test_missing_material_is_explicit(self):
        with pytest.raises(C6Error):
            resolve_cert_paths()
        with pytest.raises(C6Error):
            resolve_cert_paths(cert_path="/nao/existe.crt", key_path="/nao/existe.key")

    @pytest.mark.parametrize("mangle,desc", [
        (lambda p: p, "original"),
        (lambda p: p.replace("\n", "\\n"), "\\n literal"),
        (lambda p: p.replace("\n", "\r\n"), "CRLF do Windows"),
        (lambda p: " ".join(p.split()), "achatado numa linha só"),
        (lambda p: '"' + p + '"', "com aspas em volta"),
        (lambda p: "\n".join("  " + l for l in p.splitlines()), "indentado"),
        (lambda p: '"' + " ".join(p.split()) + '"', "achatado com aspas"),
    ])
    def test_pem_sobrevive_a_qualquer_colagem(self, mangle, desc):
        """Regressão do deploy de 17/09: o PEM colado num campo de linha única
        chegou com espaço no lugar das quebras e o boot morreu em
        `[SSL] PEM lib`, erro que não diz a causa. O normalizador reconstrói o
        bloco em vez de confiar no formato."""
        from app.pix.c6 import _normalize_pem

        pem = ("-----BEGIN CERTIFICATE-----\n"
               + "\n".join(["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2" for _ in range(4)])
               + "\n-----END CERTIFICATE-----\n")
        saida = _normalize_pem(mangle(pem))
        assert saida.startswith("-----BEGIN CERTIFICATE-----\n"), desc
        assert saida.endswith("-----END CERTIFICATE-----\n"), desc
        corpo = saida.split("-----")[2].strip().splitlines()
        assert all(len(l) <= 64 for l in corpo), desc
        assert "".join(corpo) == "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2" * 4, desc

    def test_pem_preserva_cadeia_com_varios_blocos(self):
        from app.pix.c6 import _normalize_pem

        dois = ("-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"
                "-----BEGIN CERTIFICATE-----\nREVG\n-----END CERTIFICATE-----\n")
        assert _normalize_pem(" ".join(dois.split())).count("BEGIN CERTIFICATE") == 2


# ───────────────────── webhook HTTP → ledger ───────────────────── #

class FakeC6Provider(C6PixProvider):
    """C6 sem rede: `get_cob` devolve o que o teste programou em `cobs`."""

    def __init__(self):  # noqa: D401  (não chama super: sem certificado)
        self.pix_key = PIX_KEY
        self.cobs: dict[str, dict] = {}
        self.get_cob_calls: list[str] = []

    def create_charge(self, req):
        return PixChargeResponse(txid=req.txid, br_code="brcode", qr_code_image="")

    def get_cob(self, txid):
        self.get_cob_calls.append(txid)
        return self.cobs[txid]


@pytest.fixture
def c6_provider():
    return FakeC6Provider()


@pytest.fixture
def app(c6_provider):
    app = create_app(TestConfig, pix_provider=c6_provider)
    app.config["C6_WEBHOOK_TOKEN"] = WEBHOOK_TOKEN
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk_user_and_charge(app, points=2000, cents=18000, email="c6@test.com",
                        cpf=VALID_CPF, with_wallet=True) -> tuple[str, str]:
    with app.app_context():
        u = User(name="Webhook User", email=email, cpf=cpf, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        if with_wallet:
            db.session.add(Wallet(user_id=u.id, balance_pts=0, pending_pts=0))
        ch = PixCharge(user_id=u.id, package_key="start", amount_cents=cents,
                       points_to_credit=points, br_code="brcode",
                       expires_at=PixCharge.make_expiry(3600))
        db.session.add(ch)
        db.session.commit()
        return u.id, ch.txid


def _cob_paga(cents=18000) -> dict:
    v = f"{cents / 100:.2f}"
    return {"status": "CONCLUIDA", "valor": {"original": v},
            "pix": [{"endToEndId": "E1", "valor": v}]}


def _balance(app, user_id) -> int:
    with app.app_context():
        return db.session.query(Wallet).filter_by(user_id=user_id).one().balance_pts


def _status(app, txid) -> str:
    with app.app_context():
        return db.session.query(PixCharge).filter_by(txid=txid).one().status.value


class TestWebhook:
    def test_wrong_token_is_401_and_touches_nothing(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.cobs[txid] = _cob_paga()
        r = client.post(f"/payments/c6/webhook/{'errado' * 6}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1"}]})
        assert r.status_code == 401
        assert c6_provider.get_cob_calls == []
        assert _balance(app, uid) == 0

    def test_credits_only_after_api_confirms(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        # O corpo diz "pago", mas a API diz ATIVA: não credita.
        c6_provider.cobs[txid] = {"status": "ATIVA", "valor": {"original": "180.00"}}
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1", "valor": "180.00"}]})
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "pending"
        assert _balance(app, uid) == 0
        assert _status(app, txid) == PixChargeStatus.PENDING.value

        # Agora a API confirma: credita uma vez.
        c6_provider.cobs[txid] = _cob_paga()
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1", "valor": "180.00"}]})
        assert r.get_json()["results"][0]["result"] == "credited"
        assert _balance(app, uid) == 2000
        assert _status(app, txid) == PixChargeStatus.PAID.value

    def test_duplicate_delivery_does_not_double_credit(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.cobs[txid] = _cob_paga()
        body = {"pix": [{"txid": txid, "endToEndId": "E1"}]}
        results = []
        for path in (f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                     f"/payments/c6/webhook/{WEBHOOK_TOKEN}"):
            r = client.post(path, json=body)
            assert r.status_code == 200
            results.append(r.get_json()["results"][0]["result"])
        assert results == ["credited", "replay"]
        assert _balance(app, uid) == 2000

    def test_pix_without_txid_is_logged_not_credited(self, app, client, c6_provider):
        uid, _ = _mk_user_and_charge(app)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"endToEndId": "E9", "valor": "10.00"}]})
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "sem-txid"
        assert _balance(app, uid) == 0

    def test_api_failure_returns_5xx_so_psp_retries(self, app, client, c6_provider):
        """200 diria ao C6 que entregou e o gatilho se perderia: dinheiro na
        conta, charge expirando sozinha e nada reconsultando."""
        uid, txid = _mk_user_and_charge(app)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid}]})
        assert r.status_code == 503
        assert r.get_json()["results"][0]["result"] == "deferred"
        assert _balance(app, uid) == 0
        assert _status(app, txid) == PixChargeStatus.PENDING.value

    def test_batch_partial_failure_does_not_mark_charge_paid(self, app, client, c6_provider):
        """Regressão din-02: sem rollback, o commit do item 2 persistia o item
        1 como PAID sem crédito, e o retry morria no early-return."""
        u1, tx1 = _mk_user_and_charge(app, email="a@t.com", with_wallet=False)
        u2, tx2 = _mk_user_and_charge(app, email="b@t.com", cpf="11144477735")
        c6_provider.cobs[tx1] = _cob_paga()
        c6_provider.cobs[tx2] = _cob_paga()

        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": tx1, "endToEndId": "E1"},
                                      {"txid": tx2, "endToEndId": "E2"}]})
        results = {x["txid"]: x["result"] for x in r.get_json()["results"]}
        assert results[tx1] == "deferred"
        assert results[tx2] == "credited"
        # O item que falhou continua PENDING: a reentrega do C6 conserta.
        assert _status(app, tx1) == PixChargeStatus.PENDING.value
        assert _balance(app, u2) == 2000

    def test_refund_never_credits(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        cob = _cob_paga()
        cob["pix"][0]["devolucoes"] = [{"valor": "180.00", "status": "DEVOLVIDO"}]
        c6_provider.cobs[txid] = cob
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1"}]})
        assert r.get_json()["results"][0]["result"] == "devolvido"
        assert _balance(app, uid) == 0
        assert _status(app, txid) == PixChargeStatus.PENDING.value

    def test_partial_payment_never_credits_full_points(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app, cents=18000)
        c6_provider.cobs[txid] = _cob_paga(cents=9000)   # pagou metade
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1"}]})
        assert r.get_json()["results"][0]["result"] == "valor-divergente"
        assert _balance(app, uid) == 0

    def test_unknown_txid_is_not_an_error(self, app, client, c6_provider):
        _mk_user_and_charge(app)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": "Z" * 30, "endToEndId": "E1"}]})
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "desconhecido"
        assert c6_provider.get_cob_calls == []

    def test_expired_locally_but_paid_at_c6_still_credits(self, app, client, c6_provider):
        """Regressão tst-01: dinheiro entrou na borda do TTL. Se isto regredir,
        o cliente fica com o PIX pago e sem pontos, para sempre."""
        uid, txid = _mk_user_and_charge(app)
        with app.app_context():
            ch = db.session.query(PixCharge).filter_by(txid=txid).one()
            ch.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            ch.status = PixChargeStatus.EXPIRED
            db.session.commit()
        c6_provider.cobs[txid] = _cob_paga()
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": txid, "endToEndId": "E1"}]})
        assert r.get_json()["results"][0]["result"] == "credited"
        assert _balance(app, uid) == 2000
