"""C6 Bank, notificações no formato do C6 (`service` + `information`).

O que se prova aqui:
  · Checkout (cartão / link de pagamento, homologação C_0501/C_0502): o
    evento é reconhecido, responde 200, NÃO credita ponto nenhum e o log
    não leva dado do pagador nem token do cartão
  · Pix no envelope do C6 (`service: "PIX"`, lista `pix` dentro de
    `information` em string, sem txid): o txid vem de GET /pix/{e2eid}, o
    crédito continua só depois da reconsulta CONCLUIDA, sem dobrar
  · o formato Bacen de sempre (`{"pix": [...]}`) segue intocado
    (coberto em tests/test_c6_cobranca.py)

Os corpos abaixo seguem os exemplos de docs/c6-homologacao/specs/webhook-api.yaml.

Roda com:
    cd backend && python -m pytest tests/test_c6_webhook_notificacoes.py -v
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
from app.config import TestConfig
from app.extensions import db
from app.models import PixCharge, PixChargeStatus, User, Wallet
from app.pix.c6 import C6Error, C6PixProvider, _IndeterminateError
from app.pix.provider import PixChargeResponse

VALID_CPF = "52998224725"
WEBHOOK_TOKEN = "tok-webhook-c6-" + "y" * 20
PIX_KEY = "11111111-2222-4333-8444-555555555555"
E2E = "E31872495202512011643tabwNhezy94"

# Dados do pagador e do cartão nos exemplos da spec: NUNCA podem ir para log.
PAGADOR_NOME = "José da Silva"
PAGADOR_DOC = "51448724000133"
PAGADOR_EMAIL = "pagador@email.com.br"
CARTAO_TOKEN = "da01d6da-fb5f-4a9a-85a0-a6629f5de603125562"


class FakeC6Provider(C6PixProvider):
    """C6 sem rede: `get_cob`/`get_pix` devolvem o que o teste programou."""

    def __init__(self):  # noqa: D401  (não chama super: sem certificado)
        self.pix_key = PIX_KEY
        self.cobs: dict[str, dict] = {}
        self.pixes: dict[str, object] = {}
        self.get_cob_calls: list[str] = []
        self.get_pix_calls: list[str] = []

    def create_charge(self, req):
        return PixChargeResponse(txid=req.txid, br_code="brcode", qr_code_image="")

    def get_cob(self, txid):
        self.get_cob_calls.append(txid)
        return self.cobs[txid]

    def get_pix(self, e2eid):
        self.get_pix_calls.append(e2eid)
        r = self.pixes[e2eid]
        if isinstance(r, Exception):
            raise r
        return r


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


def _mk_user_and_charge(app, points=2000, cents=18000) -> tuple[str, str]:
    with app.app_context():
        u = User(name="Webhook User", email="c6n@test.com", cpf=VALID_CPF, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
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
            "pix": [{"endToEndId": E2E, "valor": v}]}


def _balance(app, user_id) -> int:
    with app.app_context():
        return db.session.query(Wallet).filter_by(user_id=user_id).one().balance_pts


def _status(app, txid) -> str:
    with app.app_context():
        return db.session.query(PixCharge).filter_by(txid=txid).one().status.value


def _checkout_evento(status="PAID", **extra_info) -> dict:
    """Envelope CHECKOUT como no exemplo de pagamento da spec."""
    info = {
        "id": "cce1123a-a1ea-48f5-b95f-9821b95d36dd", "status": status,
        "transaction_type": "CREDIT", "external_reference_id": "A123456789",
        "amount": 98.45, "description": "Cobrança do produto A",
        "payer": {"name": PAGADOR_NOME, "tax_id": PAGADOR_DOC, "email": PAGADOR_EMAIL,
                  "address": {"street": "Av. Nove de Julho", "number": "123",
                              "city": "Rio de Janeiro", "state": "RJ",
                              "zip_code": "05093000"}},
        "payment": {"card": {"type": "CREDIT", "installments": 1,
                             "fixed_installments": True,
                             "authenticate": "NOT_REQUIRED", "token": CARTAO_TOKEN}},
        "fraud_analysis": {"analyzed": True, "recommendation": "APPROVE"},
    }
    info.update(extra_info)
    return {
        "external_id": "cce1123a-a1ea-48f5-b95f-9821b95d36dd",
        "date_time": "2025-11-27T20:12:12.089629237Z", "status": status,
        "client_id": "913dc8cd-2d75-46ea-abed-51f0dad85c6d", "partner_id": "00010",
        "service": "CHECKOUT", "information": json.dumps(info),
        "expire_at": "1795810126",
    }


def _pix_evento(pix_items: list[dict]) -> dict:
    """Envelope PIX como no WebhookNotificationPix da spec."""
    return {
        "date_time": "2025-12-01T16:43:54.192482677Z", "status": "PAID",
        "service": "PIX", "key": PIX_KEY, "tax_id": "99367503000167",
        "information": json.dumps({"pix": pix_items}),
    }


def _textos_de_log(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ─────────────────────────── Checkout ─────────────────────────── #

class TestCheckout:
    def test_paid_on_checkout_route_is_recorded_not_credited(self, app, client, c6_provider, caplog):
        uid, txid = _mk_user_and_charge(app)
        caplog.set_level(logging.INFO)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout",
                        json=_checkout_evento("PAID"))
        assert r.status_code == 200
        body = r.get_json()
        assert body["result"] == "registrado"
        assert body["service"] == "CHECKOUT" and body["status"] == "PAID"
        # Nenhuma consulta, nenhum crédito, a cobrança Pix não mexe.
        assert c6_provider.get_cob_calls == [] and c6_provider.get_pix_calls == []
        assert _balance(app, uid) == 0
        assert _status(app, txid) == PixChargeStatus.PENDING.value
        log = _textos_de_log(caplog)
        assert "c6 checkout PAID" in log
        assert "cce1123a-a1ea-48f5-b95f-9821b95d36dd" in log
        assert "referencia=A123456789" in log and "valor=98.45" in log
        assert "antifraude=APPROVE" in log

    def test_log_never_carries_payer_or_card_data(self, app, client, caplog):
        caplog.set_level(logging.DEBUG)
        for status in ("CREATED", "PAID", "DECLINED", "EXPIRED"):
            r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout",
                            json=_checkout_evento(status))
            assert r.status_code == 200
        log = _textos_de_log(caplog)
        for proibido in (PAGADOR_NOME, "Silva", PAGADOR_DOC, PAGADOR_EMAIL,
                         CARTAO_TOKEN, "Nove de Julho", "05093000",
                         "913dc8cd-2d75-46ea-abed-51f0dad85c6d"):
            assert proibido not in log, proibido

    def test_created_example_from_spec_on_base_route(self, client, caplog):
        """Exemplo literal de criação da spec: o ENVELOPE inteiro vem
        serializado dentro de `information`, e a URL é a base, sem /checkout."""
        caplog.set_level(logging.INFO)
        interno = {"id": "c07bc13a-1b5a-4882-abe8-bc07efa5f167",
                   "url": "https://payment.c6pay.com.br/payment/c07bc13a"}
        envelope = {"external_id": "c07bc13a-1b5a-4882-abe8-bc07efa5f167",
                    "date_time": "2025-12-18T14:58:51.716648139Z", "status": "CREATED",
                    "client_id": "913dc8cd", "partner_id": "00010",
                    "service": "CHECKOUT", "information": json.dumps(interno)}
        evento = dict(envelope, information=json.dumps(envelope))
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}", json=evento)
        assert r.status_code == 200
        assert r.get_json()["result"] == "registrado"
        log = _textos_de_log(caplog)
        assert "c6 checkout CREATED" in log
        assert "id=c07bc13a-1b5a-4882-abe8-bc07efa5f167" in log
        assert "payment.c6pay" not in log

    def test_declined_logs_the_reason(self, client, caplog):
        caplog.set_level(logging.INFO)
        ev = _checkout_evento("DECLINED")
        info = json.loads(ev["information"])
        info["payment"]["card"].update(return_code="78", message="Blocked Card")
        ev["information"] = json.dumps(info)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.status_code == 200
        assert "recusa=78 Blocked Card" in _textos_de_log(caplog)

    def test_information_as_object_also_works(self, client, caplog):
        caplog.set_level(logging.INFO)
        ev = _checkout_evento("PAID")
        ev["information"] = json.loads(ev["information"])
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.status_code == 200
        assert "referencia=A123456789" in _textos_de_log(caplog)

    def test_unreadable_information_still_200_with_warning(self, client, caplog):
        caplog.set_level(logging.INFO)
        ev = _checkout_evento("PAID")
        ev["information"] = "{não é json"
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.status_code == 200
        assert r.get_json()["result"] == "registrado"
        assert any(rec.levelno == logging.WARNING and "ilegível" in rec.getMessage()
                   for rec in caplog.records)

    def test_wrong_token_is_401(self, client):
        r = client.post(f"/payments/c6/webhook/{'errado' * 6}/checkout",
                        json=_checkout_evento("PAID"))
        assert r.status_code == 401

    def test_body_without_json_content_type_is_parsed(self, client):
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout",
                        data=json.dumps(_checkout_evento("CREATED")),
                        content_type="text/plain")
        assert r.status_code == 200
        assert r.get_json()["result"] == "registrado"

    def test_log_injection_is_neutralized(self, client, caplog):
        caplog.set_level(logging.INFO)
        ev = _checkout_evento("PAID")
        ev["external_id"] = "abc\n2026-01-01 INFO compra creditada"
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.status_code == 200
        msgs = [rec.getMessage() for rec in caplog.records if "c6 checkout" in rec.getMessage()]
        assert msgs and all("\n" not in m for m in msgs)

    def test_other_services_are_acknowledged_and_ignored(self, app, client, c6_provider):
        uid, _ = _mk_user_and_charge(app)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}",
                        json={"service": "BANK_SLIP", "status": "PAID",
                              "external_id": "01KY5B3KG4D6DJ2WD7DDVN6CW0",
                              "information": "{}"})
        assert r.status_code == 200
        assert r.get_json()["result"] == "ignorado"
        assert c6_provider.get_cob_calls == []
        assert _balance(app, uid) == 0


# ───────────────────── Pix no envelope do C6 ───────────────────── #

class TestPixEnvelope:
    def test_txid_comes_from_api_and_credits_once(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = {"endToEndId": E2E, "txid": txid, "valor": "180.00"}
        c6_provider.cobs[txid] = _cob_paga()
        ev = _pix_evento([{"endToEndId": E2E, "valor": "180.00", "chave": PIX_KEY,
                           "horario": "2025-12-01T19:43:48.944Z"}])
        results = []
        for path in (f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                     f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix"):
            r = client.post(path, json=ev)
            assert r.status_code == 200
            results.append(r.get_json()["results"][0]["result"])
        assert results == ["credited", "replay"]
        assert c6_provider.get_pix_calls == [E2E, E2E]
        assert c6_provider.get_cob_calls == [txid, txid]
        assert _balance(app, uid) == 2000
        assert _status(app, txid) == PixChargeStatus.PAID.value

    def test_body_saying_paid_is_not_enough(self, app, client, c6_provider):
        """A notificação diz PAID, mas a cobrança na API ainda está ATIVA."""
        uid, txid = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = {"endToEndId": E2E, "txid": txid}
        c6_provider.cobs[txid] = {"status": "ATIVA", "valor": {"original": "180.00"}}
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E, "valor": "180.00"}]))
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "pending"
        assert _balance(app, uid) == 0

    def test_txid_inside_information_skips_e2e_lookup(self, app, client, c6_provider):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.cobs[txid] = _cob_paga()
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E, "txid": txid}]))
        assert r.get_json()["results"][0]["result"] == "credited"
        assert c6_provider.get_pix_calls == []
        assert _balance(app, uid) == 2000

    def test_pix_unknown_at_api_is_not_an_error(self, app, client, c6_provider):
        uid, _ = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = C6Error("HTTP 404: não encontrado", status=404)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E, "valor": "10.00"}]))
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "sem-txid"
        assert c6_provider.get_cob_calls == []
        assert _balance(app, uid) == 0

    def test_pix_without_txid_at_api_is_logged_not_credited(self, app, client, c6_provider):
        """Pix avulso (sem cobrança) ou txid que não é de cobrança nossa."""
        uid, _ = _mk_user_and_charge(app)
        for txid_api in (None, "***", "curto"):
            c6_provider.pixes[E2E] = {"endToEndId": E2E, "txid": txid_api}
            r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                            json=_pix_evento([{"endToEndId": E2E}]))
            assert r.status_code == 200
            assert r.get_json()["results"][0]["result"] == "sem-txid"
        assert c6_provider.get_cob_calls == []
        assert _balance(app, uid) == 0

    @pytest.mark.parametrize("erro", [
        _IndeterminateError("timeout"),
        C6Error("HTTP 503: fora do horário", status=503),
    ])
    def test_lookup_failure_returns_503_so_c6_retries(self, app, client, c6_provider, erro):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = erro
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E}]))
        assert r.status_code == 503
        assert r.get_json()["results"][0]["result"] == "deferred"
        assert _balance(app, uid) == 0
        assert _status(app, txid) == PixChargeStatus.PENDING.value

    def test_malformed_e2e_never_reaches_the_api(self, app, client, c6_provider):
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": "../../cob/x"}]))
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "sem-txid"
        assert c6_provider.get_pix_calls == []

    def test_unreadable_information_is_200_and_ignored(self, client, c6_provider):
        ev = _pix_evento([])
        ev["information"] = "lixo"
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix", json=ev)
        assert r.status_code == 200
        assert r.get_json()["ignored"] == "information"
        assert c6_provider.get_pix_calls == []


# ───────────────────── provider: GET /pix/{e2eid} ───────────────────── #

class _ProviderSemRede(C6PixProvider):
    def __init__(self, resposta):  # noqa: D401  (sem certificado)
        self.host = "https://c6.invalid"
        self.user_agent = "teste"
        self.resposta = resposta
        self.urls: list[str] = []

    def _bearer(self, *, force: bool = False) -> str:
        return "jwt"

    def _http(self, method, url, data, headers):
        self.urls.append(f"{method} {url}")
        return self.resposta


class TestProviderGetPix:
    def test_calls_bacen_path(self):
        p = _ProviderSemRede((200, json.dumps({"endToEndId": E2E, "txid": "T" * 30})))
        assert p.get_pix(E2E)["txid"] == "T" * 30
        assert p.urls == [f"GET https://c6.invalid/v2/pix/pix/{E2E}"]

    def test_rejects_bad_e2e_before_network(self):
        p = _ProviderSemRede((200, "{}"))
        for ruim in ("", "E1", "../cob/" + "a" * 25, E2E + "x"):
            with pytest.raises(C6Error):
                p.get_pix(ruim)
        assert p.urls == []

    def test_http_error_carries_status(self):
        p = _ProviderSemRede((404, json.dumps({"title": "Não encontrado"})))
        with pytest.raises(C6Error) as exc:
            p.get_pix(E2E)
        assert exc.value.status == 404


# ───────────── achados da revisão adversarial (22/10) ───────────── #

class TestRobustez:
    @pytest.mark.parametrize("st", [400, 403, 422])
    def test_permanent_4xx_on_lookup_is_200_not_eternal_503(self, app, client, c6_provider, caplog, st):
        """403 (credencial sem pix.read) não se resolve com reenvio: 503 faria o
        C6 reenviar para sempre e desligar o webhook."""
        uid, _ = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = C6Error(f"HTTP {st}", status=st)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E}]))
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "sem-txid"
        assert any(rec.levelno == logging.ERROR for rec in caplog.records)
        assert _balance(app, uid) == 0

    @pytest.mark.parametrize("st", [408, 429])
    def test_transient_4xx_still_retries(self, app, client, c6_provider, st):
        c6_provider.pixes[E2E] = C6Error(f"HTTP {st}", status=st)
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E}]))
        assert r.status_code == 503

    def test_non_string_txid_from_api_is_not_a_crash(self, client, c6_provider):
        c6_provider.pixes[E2E] = {"endToEndId": E2E, "txid": 123}
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": E2E}]))
        assert r.status_code == 200
        assert r.get_json()["results"][0]["result"] == "sem-txid"

    @pytest.mark.parametrize("service", [" PIX", "PIX ", "pix"])
    def test_service_with_spaces_or_lowercase_is_still_pix(self, app, client, c6_provider, service):
        uid, txid = _mk_user_and_charge(app)
        c6_provider.pixes[E2E] = {"endToEndId": E2E, "txid": txid}
        c6_provider.cobs[txid] = _cob_paga()
        ev = _pix_evento([{"endToEndId": E2E}])
        ev["service"] = service
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix", json=ev)
        assert r.get_json()["results"][0]["result"] == "credited"
        assert _balance(app, uid) == 2000

    def test_checkout_with_spaces_is_recognized(self, client):
        ev = _checkout_evento("PAID")
        ev["service"] = " checkout "
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.get_json()["result"] == "registrado"

    def test_newlines_in_pix_fields_do_not_forge_log_lines(self, app, client, c6_provider, caplog):
        caplog.set_level(logging.INFO)
        _mk_user_and_charge(app)
        ruim = "x\n2026-01-01 INFO compra creditada \x1b[31m"
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/pix",
                        json=_pix_evento([{"endToEndId": ruim, "valor": ruim},
                                          {"txid": "Z" * 28 + ruim, "endToEndId": ruim}]))
        assert r.status_code == 200
        for rec in caplog.records:
            m = rec.getMessage()
            assert "\n" not in m and " " not in m and "\x1b" not in m, m

    def test_deeply_nested_body_is_not_a_500(self, client):
        fundo = "[" * 100_000 + "]" * 100_000
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout",
                        data=fundo, content_type="application/json")
        assert r.status_code == 200
        ev = _checkout_evento("PAID")
        ev["information"] = fundo
        r = client.post(f"/payments/c6/webhook/{WEBHOOK_TOKEN}/checkout", json=ev)
        assert r.status_code == 200

    def test_non_ascii_token_is_401_not_500(self, client):
        r = client.post("/payments/c6/webhook/tokén-com-acento/checkout",
                        json=_checkout_evento("PAID"))
        assert r.status_code == 401
