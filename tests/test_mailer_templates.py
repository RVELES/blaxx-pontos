"""Os e-mails saem com HTML da marca + texto puro, e o nome do usuário é escapado."""

from app.services import mailer as mailer_mod


class _Captura:
    name = "captura"

    def __init__(self):
        self.msgs = []

    def send(self, msg):
        self.msgs.append(msg)
        return True


def _com_captura(monkeypatch):
    cap = _Captura()
    monkeypatch.setattr(mailer_mod, "_singleton", cap)
    return cap


def test_reset_tem_html_com_botao_e_texto(monkeypatch):
    cap = _com_captura(monkeypatch)
    url = "https://blaxxpontos.com.br/redefinir-senha?token=abc"
    mailer_mod.send_password_reset("a@b.com", "Ana Souza", url)
    msg = cap.msgs[0]
    assert url in msg.body_text
    assert msg.body_html and url in msg.body_html
    assert "#59FD27" in msg.body_html and "Ana" in msg.body_html


def test_nome_do_usuario_e_escapado_no_html(monkeypatch):
    cap = _com_captura(monkeypatch)
    mailer_mod.send_email_verification("a@b.com", "<script>x</script>", "123456")
    html = cap.msgs[0].body_html
    assert "<script>" not in html
    assert "123456" in html and "123456" in cap.msgs[0].body_text


def test_boas_vindas_nao_promete_bonus_que_nao_existe(monkeypatch):
    cap = _com_captura(monkeypatch)
    mailer_mod.send_welcome("a@b.com", "Ana", code="654321")
    msg = cap.msgs[0]
    assert "500" not in msg.body_text and "500" not in msg.body_html
    assert "654321" in msg.body_html
