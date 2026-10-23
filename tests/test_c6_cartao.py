"""C6 Bank, compra de pontos com CARTÃO pelo Checkout (link de pagamento C6 Pay).

O que se prova aqui:
  · provider: POST /v1/checkouts/ com amount em reais (Decimal → número de 2
    casas), referência de 10 alfanuméricos e só cartão de crédito; GET e PUT
    /cancel no caminho certo; id fora de UUID nunca vira URL; Accept com
    problem+json em toda chamada (o sandbox recusa só application/json)
  · serviço: mesmas regras da compra por PIX (pacote do DB, teto mensal com a
    carteira travada), mínimo de R$ 5,00, erro do C6 vira PixError sem charge
  · API: cria (201, gate desligado = 503) e consulta só a própria charge; o
    polling credita quando a API do C6 diz PAID
  · webhook CHECKOUT PAID: credita uma vez, só depois da reconsulta; corpo
    dizendo PAID com a API em CREATED não credita; valor menor não credita;
    reentrega não dobra; referência desconhecida é 200 sem efeito; rede/5xx
    devolve 503 e 4xx permanente devolve 200

Os corpos seguem docs/c6-homologacao/specs/checkout.yaml e webhook-api.yaml,
e o formato da resposta do GET confirmado no sandbox em 22/10.

Roda com:
    cd backend && python -m pytest tests/test_c6_cartao.py -v
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import Config, TestConfig
from app.extensions import db
from app.models import PixCharge, PixChargeStatus, PointPackage, User, Wallet
from app.pix import c6 as c6_mod
from app.pix.c6 import C6Error, C6PixProvider, _IndeterminateError
from app.pix.provider import PixChargeResponse
from app.services import purchase as purchase_svc

VALID_CPF = "52998224725"
WEBHOOK_TOKEN = "tok-webhook-c6-" + "z" * 20
PIX_KEY = "11111111-2222-4333-8444-555555555555"
CHECKOUT_ID = "e4cf07fc-6b14-4477-a694-5053189726f7"
CHECKOUT_URL = "https://checkout.c6bank.info/01J3NCKY6Q99QC4D7T733D35QD"

AUTH_OK = (200, json.dumps({"access_token": "jwt-1", "expires_in": 300,
                            "scope": "checkout.read checkout.write checkout.cancel"}))


# ───────────────────── provider (HTTP dublado) ───────────────────── #

class _StubSSL:
    def load_cert_chain(self, certfile, keyfile):
        pass


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(c6_mod.ssl, "create_default_context", lambda: _StubSSL())
    return C6PixProvider(
        client_id="99999999-8888-4777-8666-555555555555", client_secret="segredo",
        pix_key=PIX_KEY, cert_path="/dev/null", key_path="/dev/null", sandbox=True,
    )


class _Http:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[tuple[str, str, bytes | None, dict]] = []

    def __call__(self, method, url, data, headers):
        self.calls.append((method, url, data, headers))
        if url.endswith("/v1/auth/"):
            return AUTH_OK
        return self.resp

    def api(self):
        return [c for c in self.calls if not c[1].endswith("/v1/auth/")]


class TestProvider:
    def test_create_checkout_body_and_path(self, provider, monkeypatch):
        http = _Http((201, json.dumps({"id": CHECKOUT_ID, "url": CHECKOUT_URL})))
        monkeypatch.setattr(provider, "_http", http)

        r = provider.create_checkout(18005, "AbC123xYz9", "BlaXx — pacote Start",
                                     "https://app.example/dashboard?compra_cartao=1")

        assert r == {"id": CHECKOUT_ID, "url": CHECKOUT_URL}
        method, url, data, headers = http.api()[0]
        assert method == "POST"
        assert url == "https://baas-api-sandbox.c6bank.info/v1/checkouts/"
        # Número em reais, 2 casas, sem resíduo de float no JSON.
        assert '"amount": 180.05' in data.decode()
        body = json.loads(data)
        assert body["external_reference_id"] == "AbC123xYz9"
        assert body["payment"] == {"card": {"type": "CREDIT", "installments": 1}}
        assert body["redirect_url"].startswith("https://")
        assert headers["Authorization"] == "Bearer jwt-1"
        assert headers["Accept"] == "application/json, application/problem+json"

    def test_minimum_amount_is_5_reais(self, provider, monkeypatch):
        http = _Http((201, "{}"))
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.create_checkout(499, "AbC123xYz9")
        assert http.calls == []
        # 5,00 exatos passam da validação local.
        http.resp = (201, json.dumps({"id": CHECKOUT_ID, "url": CHECKOUT_URL}))
        provider.create_checkout(500, "AbC123xYz9")
        assert json.loads(http.api()[0][2])["amount"] == 5.0

    def test_installments_require_interest_type(self, provider, monkeypatch):
        http = _Http((201, json.dumps({"id": CHECKOUT_ID, "url": CHECKOUT_URL})))
        monkeypatch.setattr(provider, "_http", http)
        provider.create_checkout(10000, "AbC123xYz9", installments=3)
        card = json.loads(http.api()[0][2])["payment"]["card"]
        assert card == {"type": "CREDIT", "installments": 3, "interest_type": "BY_ISSUER"}

    @pytest.mark.parametrize("ref", ["", "12345678901", "abc-123", "../x"])
    def test_bad_reference_never_reaches_api(self, provider, monkeypatch, ref):
        http = _Http((201, "{}"))
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.create_checkout(1000, ref)
        assert http.calls == []

    @pytest.mark.parametrize("resp", [
        {"id": "../webhooks", "url": CHECKOUT_URL},
        {"id": CHECKOUT_ID, "url": "http://inseguro"},
        {"url": CHECKOUT_URL},
        ["lista"],
    ])
    def test_response_without_uuid_or_https_url_is_an_error(self, provider, monkeypatch, resp):
        monkeypatch.setattr(provider, "_http", _Http((201, json.dumps(resp))))
        with pytest.raises(C6Error):
            provider.create_checkout(1000, "AbC123xYz9")

    def test_timeout_on_create_is_an_error(self, provider, monkeypatch):
        def http(method, url, data, headers):
            if url.endswith("/v1/auth/"):
                return AUTH_OK
            raise _IndeterminateError("timeout")
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.create_checkout(1000, "AbC123xYz9")

    def test_get_and_cancel_paths(self, provider, monkeypatch):
        http = _Http((200, json.dumps({"id": CHECKOUT_ID, "status": "CREATED", "amount": 5})))
        monkeypatch.setattr(provider, "_http", http)
        assert provider.get_checkout(CHECKOUT_ID)["status"] == "CREATED"
        http.resp = (204, "")
        assert provider.cancel_checkout(CHECKOUT_ID) is None
        urls = [f"{c[0]} {c[1]}" for c in http.api()]
        assert urls == [
            f"GET https://baas-api-sandbox.c6bank.info/v1/checkouts/{CHECKOUT_ID}",
            f"PUT https://baas-api-sandbox.c6bank.info/v1/checkouts/{CHECKOUT_ID}/cancel",
        ]
        assert all(c[3]["Accept"] == "application/json, application/problem+json"
                   for c in http.api())

    @pytest.mark.parametrize("ruim", ["", "x", "../../v2/pix/webhook/k", CHECKOUT_ID + "/cancel"])
    def test_bad_checkout_id_never_becomes_url(self, provider, monkeypatch, ruim):
        http = _Http((200, "{}"))
        monkeypatch.setattr(provider, "_http", http)
        with pytest.raises(C6Error):
            provider.get_checkout(ruim)
        with pytest.raises(C6Error):
            provider.cancel_checkout(ruim)
        assert http.calls == []

    def test_http_error_carries_status(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_http", _Http((404, json.dumps({"title": "Não encontrado"}))))
        with pytest.raises(C6Error) as exc:
            provider.get_checkout(CHECKOUT_ID)
        assert exc.value.status == 404

    def test_pix_calls_also_send_problem_json_accept(self, provider, monkeypatch):
        """O PUT /webhook/{chave} do Pix era recusado com 400 por isso."""
        http = _Http((200, "{}"))
        monkeypatch.setattr(provider, "_http", http)
        provider.register_webhook("https://exemplo.test/payments/c6/webhook/t")
        assert http.api()[0][3]["Accept"] == "application/json, application/problem+json"
        assert "/v2/pix/webhook/" in http.api()[0][1]

    def test_amount_cents_from_checkout(self):
        assert C6PixProvider.checkout_amount_cents({"amount": 5}) == 500
        assert C6PixProvider.checkout_amount_cents({"amount": 180.05}) == 18005
        assert C6PixProvider.checkout_amount_cents({"amount": None}) == 0


# ───────────────────── app: serviço, API e webhook ───────────────────── #

class FakeC6(C6PixProvider):
    """C6 sem rede: o teste programa os checkouts que a API 'devolve'."""

    def __init__(self):  # noqa: D401  (sem certificado)
        self.pix_key = PIX_KEY
        self.checkouts: dict[str, object] = {}
        self.created: list[dict] = []
        self.get_calls: list[str] = []
        self.create_error: Exception | None = None
        self._n = 0

    def create_charge(self, req):
        return PixChargeResponse(txid=req.txid, br_code="brcode", qr_code_image="")

    def get_cob(self, txid):
        raise AssertionError("charge de cartão nunca pode consultar /cob")

    def create_checkout(self, amount_cents, external_reference_id, description="",
                        redirect_url="", installments=1):
        if self.create_error:
            raise self.create_error
        self._n += 1
        cid = f"{self._n:08d}-6b14-4477-a694-5053189726f7"
        self.created.append({"amount_cents": amount_cents, "ref": external_reference_id,
                             "description": description, "redirect_url": redirect_url,
                             "id": cid})
        self.checkouts[cid] = {"id": cid, "external_reference_id": external_reference_id,
                               "amount": amount_cents / 100, "status": "CREATED"}
        return {"id": cid, "url": f"https://checkout.c6bank.info/L{self._n}"}

    def get_checkout(self, checkout_id):
        self.get_calls.append(checkout_id)
        r = self.checkouts[checkout_id]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fake():
    return FakeC6()


@pytest.fixture
def app(fake):
    app = create_app(TestConfig, pix_provider=fake)
    app.config.update(C6_WEBHOOK_TOKEN=WEBHOOK_TOKEN, C6_CHECKOUT_ENABLED=True,
                      RATELIMIT_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _user(app, email="cartao@test.com", cpf=VALID_CPF) -> str:
    with app.app_context():
        u = User(name="Cartão User", email=email, cpf=cpf, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0, pending_pts=0))
        db.session.commit()
        return u.id


def _auth(app, uid) -> dict:
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=uid)}"}


def _balance(app, uid) -> int:
    with app.app_context():
        return db.session.query(Wallet).filter_by(user_id=uid).one().balance_pts


def _charge(app, charge_id) -> PixCharge:
    with app.app_context():
        ch = db.session.get(PixCharge, charge_id)
        db.session.expunge(ch)
        return ch


def _criar(app, uid, **kw) -> PixCharge:
    with app.app_context():
        user = db.session.get(User, uid)
        ch = purchase_svc.create_card_checkout(user, **kw)
        db.session.refresh(ch)
        db.session.expunge(ch)
        return ch


def _evento(charge: PixCharge, status="PAID", **info_extra) -> dict:
    info = {"id": charge.provider_ref, "status": status, "transaction_type": "CREDIT",
            "external_reference_id": charge.txid, "amount": charge.amount_cents / 100,
            "payer": {"name": "José da Silva", "tax_id": "51448724000133"},
            "payment": {"card": {"type": "CREDIT", "installments": 1, "token": "tok-cartao"}}}
    info.update(info_extra)
    return {"external_id": charge.provider_ref, "date_time": "2026-10-22T20:12:12Z",
            "status": status, "client_id": "913dc8cd", "partner_id": "00010",
            "service": "CHECKOUT", "information": json.dumps(info)}


def _webhook(client, ev):
    return client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)


class TestServico:
    def test_package_creates_card_charge(self, app, fake):
        uid = _user(app)
        ch = _criar(app, uid, package_key="start")
        pacote = Config.POINT_PACKAGES["start"]
        assert ch.flow == "c6-cartao"
        assert ch.status == PixChargeStatus.PENDING
        assert ch.amount_cents == int(round(pacote["price_brl"] * 100))
        assert ch.points_to_credit == pacote["points"]
        # Referência: 10 alfanuméricos = txid; id do checkout = provider_ref.
        assert len(ch.txid) == 10 and ch.txid.isalnum()
        assert ch.provider_ref == fake.created[0]["id"]
        assert ch.br_code.startswith("https://checkout.c6bank.info/")
        assert fake.created[0]["ref"] == ch.txid
        assert fake.created[0]["amount_cents"] == ch.amount_cents
        assert f"compra_cartao={ch.id}" in fake.created[0]["redirect_url"]
        assert _balance(app, uid) == 0

    def test_references_are_unique(self, app, fake):
        uid = _user(app)
        refs = {_criar(app, uid, amount_brl=10).txid for _ in range(5)}
        assert len(refs) == 5

    def test_db_price_wins_like_pix(self, app, fake):
        uid = _user(app)
        with app.app_context():
            db.session.merge(PointPackage(key="start", label="Start", points=777,
                                          price_cents=4321, active=True, sort_order=0))
            db.session.commit()
        ch = _criar(app, uid, package_key="start")
        assert (ch.amount_cents, ch.points_to_credit) == (4321, 777)

    def test_below_5_reais_is_refused(self, app, fake):
        uid = _user(app)
        with app.app_context():
            db.session.merge(PointPackage(key="mini", label="Mini", points=40,
                                          price_cents=499, active=True, sort_order=9))
            db.session.commit()
        with pytest.raises(purchase_svc.PixError, match="R\\$ 5,00"):
            _criar(app, uid, package_key="mini")
        assert fake.created == []
        with app.app_context():
            assert db.session.query(PixCharge).count() == 0

    def test_same_validations_as_pix(self, app, fake):
        uid = _user(app)
        for kw in ({}, {"package_key": "start", "amount_brl": 10},
                   {"package_key": "nao-existe"}, {"amount_brl": 9.99}):
            with pytest.raises(purchase_svc.PixError):
                _criar(app, uid, **kw)
        assert fake.created == []

    def test_monthly_limit_counts_pending_card_charges(self, app, fake, monkeypatch):
        uid = _user(app)
        teto = Config.cents_to_pts(10_000) * 2  # cabe exatamente 2 compras de R$ 100
        monkeypatch.setattr(Config, "PURCHASE_MAX_POINTS_PER_MONTH", teto)
        _criar(app, uid, amount_brl=100)
        _criar(app, uid, amount_brl=100)
        with pytest.raises(purchase_svc.PixError, match="limite mensal"):
            _criar(app, uid, amount_brl=100)
        assert len(fake.created) == 2

    def test_provider_error_is_friendly_and_leaves_nothing(self, app, fake):
        uid = _user(app)
        fake.create_error = C6Error("HTTP 503: fora do horário", status=503)
        with pytest.raises(purchase_svc.PixError, match="Tente de novo"):
            _criar(app, uid, amount_brl=50)
        with app.app_context():
            assert db.session.query(PixCharge).count() == 0

    def test_requires_c6_provider(self):
        app = create_app(TestConfig)  # provider mock
        with app.app_context():
            db.create_all()
            u = User(name="X", email="x@test.com", cpf=VALID_CPF, role="user")
            u.set_password("StrongP@ss1!")
            db.session.add(u)
            db.session.flush()
            db.session.add(Wallet(user_id=u.id, balance_pts=0, pending_pts=0))
            db.session.commit()
            with pytest.raises(purchase_svc.PixError, match="indisponível"):
                purchase_svc.create_card_checkout(u, amount_brl=50)
            db.session.remove()
            db.drop_all()


class TestApi:
    def test_create_returns_201_with_url(self, app, client, fake):
        uid = _user(app)
        r = client.post("/payments/c6/card/checkout", json={"package": "start"},
                        headers=_auth(app, uid))
        assert r.status_code == 201
        body = r.get_json()
        assert body["url"].startswith("https://checkout.c6bank.info/")
        assert body["status"] == "pending" and body["flow"] == "c6-cartao"
        assert body["id"]

    def test_requires_login(self, client):
        assert client.post("/payments/c6/card/checkout", json={"package": "start"}).status_code == 401

    def test_disabled_gate_is_503(self, app, client, fake):
        uid = _user(app)
        app.config["C6_CHECKOUT_ENABLED"] = False
        r = client.post("/payments/c6/card/checkout", json={"package": "start"},
                        headers=_auth(app, uid))
        assert r.status_code == 503
        assert fake.created == []

    def test_validation_error_is_400(self, app, client, fake):
        uid = _user(app)
        r = client.post("/payments/c6/card/checkout", json={"amount_brl": 1},
                        headers=_auth(app, uid))
        assert r.status_code == 400

    def test_get_only_own_card_charge(self, app, client, fake):
        dono = _user(app)
        outro = _user(app, email="outro@test.com", cpf="11144477735")
        ch = _criar(app, dono, amount_brl=50)
        assert client.get(f"/payments/c6/card/checkout/{ch.id}",
                          headers=_auth(app, outro)).status_code == 404
        r = client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, dono))
        assert r.status_code == 200 and r.get_json()["status"] == "pending"

    def test_polling_credits_when_api_says_paid(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        for _ in range(2):
            r = client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, uid))
            assert r.get_json()["status"] == "paid"
        assert _balance(app, uid) == ch.points_to_credit
        # PAID não reconsulta mais: a segunda leitura não foi ao C6.
        assert fake.get_calls == [ch.provider_ref]

    def test_polling_failure_does_not_break_screen(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref] = _IndeterminateError("timeout")
        r = client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, uid))
        assert r.status_code == 200 and r.get_json()["status"] == "pending"

    def test_expired_at_c6_frees_the_charge(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "EXPIRED"
        r = client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, uid))
        assert r.get_json()["status"] == "expired"
        assert _balance(app, uid) == 0

    def test_pix_polling_route_does_not_call_cob_for_card(self, app, client, fake):
        """GET /pix/charge/<id> com charge de cartão consulta o checkout, não /cob."""
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        r = client.get(f"/pix/charge/{ch.id}", headers=_auth(app, uid))
        assert r.status_code == 200 and r.get_json()["status"] == "paid"
        assert _balance(app, uid) == ch.points_to_credit


class TestWebhook:
    def test_paid_credits_once_after_api_confirms(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        resultados = []
        for _ in range(2):  # reentrega
            r = _webhook(client, _evento(ch))
            assert r.status_code == 200
            resultados.append(r.get_json()["credito"])
        assert resultados == ["credited", "replay"]
        assert _balance(app, uid) == ch.points_to_credit
        assert _charge(app, ch.id).status == PixChargeStatus.PAID
        assert fake.get_calls == [ch.provider_ref, ch.provider_ref]

    def test_body_paid_but_api_created_does_not_credit(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)  # API segue CREATED
        r = _webhook(client, _evento(ch))
        assert r.status_code == 200
        assert r.get_json()["credito"] == "pending"
        assert _balance(app, uid) == 0
        assert _charge(app, ch.id).status == PixChargeStatus.PENDING

    def test_lower_amount_at_api_does_not_credit(self, app, client, fake, caplog):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref].update(status="PAID", amount=49.99)
        r = _webhook(client, _evento(ch))
        assert r.get_json()["credito"] == "valor-divergente"
        assert _balance(app, uid) == 0
        assert any(rec.levelno == logging.ERROR for rec in caplog.records)

    def test_reference_mismatch_at_api_does_not_credit(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref].update(status="PAID", external_reference_id="OUTRA00001")
        r = _webhook(client, _evento(ch))
        assert r.get_json()["credito"] == "referencia-divergente"
        assert _balance(app, uid) == 0

    def test_api_is_queried_by_stored_id_not_body_id(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        ev = _evento(ch, id="ffffffff-ffff-4fff-8fff-ffffffffffff")
        r = _webhook(client, ev)
        assert r.get_json()["credito"] == "credited"
        assert fake.get_calls == [ch.provider_ref]

    def test_unknown_reference_is_200_without_effect(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        ev = _evento(ch, external_reference_id="NAOEXISTE1")
        r = _webhook(client, ev)
        assert r.status_code == 200
        assert r.get_json()["credito"] == "desconhecido"
        assert r.get_json()["result"] == "registrado"
        assert fake.get_calls == []
        assert _balance(app, uid) == 0

    def test_pix_charge_reference_is_not_a_card_purchase(self, app, client, fake):
        """txid de Pix na referência do Checkout não acha nada (flow diferente)."""
        uid = _user(app)
        with app.app_context():
            pix = purchase_svc.create_charge(db.session.get(User, uid), amount_brl=50)
            txid = pix.txid
        ev = {"service": "CHECKOUT", "status": "PAID",
              "information": json.dumps({"id": CHECKOUT_ID, "status": "PAID",
                                         "external_reference_id": txid[:10], "amount": 50})}
        r = _webhook(client, ev)
        assert r.get_json()["credito"] == "desconhecido"
        assert _balance(app, uid) == 0

    def test_non_paid_events_do_not_query_api(self, app, client, fake):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        for st in ("CREATED", "DECLINED", "EXPIRED"):
            r = _webhook(client, _evento(ch, status=st))
            assert r.status_code == 200 and "credito" not in r.get_json()
        assert fake.get_calls == []

    @pytest.mark.parametrize("erro", [
        _IndeterminateError("timeout"),
        C6Error("HTTP 503: fora do horário", status=503),
        C6Error("HTTP 429", status=429),
    ])
    def test_network_or_5xx_is_503_so_c6_retries(self, app, client, fake, erro):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref] = erro
        r = _webhook(client, _evento(ch))
        assert r.status_code == 503
        assert r.get_json()["credito"] == "deferred"
        assert _balance(app, uid) == 0

    @pytest.mark.parametrize("st", [400, 403, 404])
    def test_permanent_4xx_is_200_with_error_log(self, app, client, fake, caplog, st):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref] = C6Error(f"HTTP {st}", status=st)
        r = _webhook(client, _evento(ch))
        assert r.status_code == 200
        assert r.get_json()["credito"] == "erro-permanente"
        assert any(rec.levelno == logging.ERROR for rec in caplog.records)
        assert _balance(app, uid) == 0

    def test_log_never_carries_payer_or_card_token(self, app, client, fake, caplog):
        caplog.set_level(logging.DEBUG)
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        fake.checkouts[ch.provider_ref]["status"] = "PAID"
        _webhook(client, _evento(ch))
        log = "\n".join(r.getMessage() for r in caplog.records)
        for proibido in ("José da Silva", "51448724000133", "tok-cartao", ch.br_code):
            assert proibido not in log, proibido

    def test_pix_body_with_card_reference_is_ignored(self, app, client, fake):
        """Corpo Pix com txid de cartão: não consulta /cob (FakeC6 falharia)."""
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json={"pix": [{"txid": ch.txid, "endToEndId": "E1"}]})
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "fluxo-cartao"
        assert _balance(app, uid) == 0


class TestConfigPublica:
    """O site decide o fluxo do botão "Pagar com cartão" por /payments/card/config."""

    def test_c6_ligado_anuncia_provider_c6_sem_chave(self, client):
        j = client.get("/payments/card/config").get_json()
        assert j["enabled"] is True and j["provider"] == "c6"
        assert j["public_key"] == "" and j["checkout_path"] == "/payments/c6/card/checkout"

    def test_c6_desligado_nao_anuncia(self, app, client):
        app.config["C6_CHECKOUT_ENABLED"] = False
        j = client.get("/payments/card/config").get_json()
        assert j["provider"] != "c6"


class TestRevisao:
    """Regressões da revisão adversarial de 23/10 (teto, ERROR, estorno)."""

    def _api(self, fake, ch, **kw):
        d = dict(fake.checkouts[ch.provider_ref])
        d.update(kw)
        fake.checkouts[ch.provider_ref] = d

    def test_charge_do_mes_anterior_ainda_pagavel_conta_no_teto(self, app, fake, monkeypatch):
        from datetime import timedelta
        from app.services.wallet import _month_start_utc
        uid = _user(app)
        monkeypatch.setattr(Config, "PURCHASE_MAX_POINTS_PER_MONTH",
                            Config.cents_to_pts(10_000) * 2)
        velha = _criar(app, uid, amount_brl=100)
        with app.app_context():
            db.session.query(PixCharge).filter_by(id=velha.id).update(
                {"created_at": _month_start_utc() - timedelta(hours=1)})
            db.session.commit()
        _criar(app, uid, amount_brl=100)
        with pytest.raises(purchase_svc.PixError, match="limite mensal"):
            _criar(app, uid, amount_brl=100)

    def test_error_terminal_libera_o_teto(self, app, client, fake, monkeypatch):
        uid = _user(app)
        monkeypatch.setattr(Config, "PURCHASE_MAX_POINTS_PER_MONTH", Config.cents_to_pts(10_000))
        ch = _criar(app, uid, amount_brl=100)
        self._api(fake, ch, status="ERROR")
        client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, uid))
        assert _charge(app, ch.id).status == PixChargeStatus.EXPIRED
        _criar(app, uid, amount_brl=100)

    def test_webhook_cancelled_de_charge_paga_flagra_estorno(self, app, client, fake, caplog):
        uid = _user(app)
        ch = _criar(app, uid, amount_brl=50)
        self._api(fake, ch, status="PAID")
        client.get(f"/payments/c6/card/checkout/{ch.id}", headers=_auth(app, uid))
        assert _balance(app, uid) == ch.points_to_credit
        self._api(fake, ch, status="CANCELLED")
        caplog.set_level(logging.ERROR)
        r = _webhook(client, _evento(ch, status="CANCELLED"))
        assert r.status_code == 200
        assert "ESTORNADO" in caplog.text
        assert _balance(app, uid) == ch.points_to_credit  # nada é debitado sozinho
