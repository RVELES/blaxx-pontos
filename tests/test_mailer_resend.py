"""ResendMailer: o Cloudflare do Resend recusa o User-Agent padrão do urllib
(403 "error code: 1010"), então toda chamada precisa levar um User-Agent próprio."""

import io
import json

from app.services import mailer as mailer_mod
from app.services.mailer import EmailMessage, ResendMailer


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resend_envia_user_agent_proprio(monkeypatch):
    capturado = {}

    def fake_urlopen(req, timeout=None):
        capturado["ua"] = req.get_header("User-agent")
        capturado["body"] = json.loads(req.data)
        return _Resp(b'{"id": "abc"}')

    monkeypatch.setattr(mailer_mod.urllib.request, "urlopen", fake_urlopen)
    m = ResendMailer(api_key="re_teste_123456789", from_addr="BlaXx <no-reply@blaxxpontos.com.br>")
    assert m.send(EmailMessage(to="a@b.com", subject="s", body_text="t")) is True
    assert capturado["ua"] and not capturado["ua"].startswith("Python-urllib")
    assert capturado["body"]["from"] == "BlaXx <no-reply@blaxxpontos.com.br>"
