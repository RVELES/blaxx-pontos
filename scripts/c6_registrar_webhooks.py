"""Cadastra no C6 os dois webhooks que a produção precisa (Pix e Checkout).

Sem eles o C6 não avisa o pagamento e a compra só é creditada pelo polling
da tela do cliente (que não roda se ele fechar o app).

  Pix       PUT  /v2/pix/webhook/{chave}  {"webhookUrl": <base>/payments/c6/webhook/<token>}
            (o C6 chama <url>/pix, padrão Bacen; a rota aceita as duas)
  Checkout  POST /v1/webhooks/            {"url": <base>/payments/c6/webhook/<token>/checkout,
                                           "service": "CHECKOUT"}

Lê tudo de variáveis de ambiente (as mesmas do Render): C6_CLIENT_ID,
C6_CLIENT_SECRET, C6_PIX_KEY, C6_CERT_PATH/C6_KEY_PATH (ou C6_CERT_PEM/
C6_KEY_PEM), C6_ENV (sandbox|production) e C6_WEBHOOK_TOKEN. Antes de
cadastrar, prova que a produção ACEITA o token (POST vazio deve dar 200, não
401): cadastrar uma URL que a própria produção recusa seria pior que nada.
Nada secreto é impresso: token e client_id saem mascarados.

Uso (o token entra pelo terminal, nunca por arquivo ou chat):
    source ~/.config/blaxx/c6/sandbox.env      # ou o de produção
    read -rs "C6_WEBHOOK_TOKEN?token do webhook (Render): " && export C6_WEBHOOK_TOKEN; echo
    python3 scripts/c6_registrar_webhooks.py            # só confere
    python3 scripts/c6_registrar_webhooks.py --gravar   # cadastra e reconfere
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pix.c6 import C6Error, C6PixProvider, resolve_cert_paths  # noqa: E402

BASE_PADRAO = "https://blaxx-pontos-exe.onrender.com"


def _mascara(texto: str, *segredos: str) -> str:
    for s in segredos:
        if s:
            texto = texto.replace(s, s[:4] + "…")
    return texto


def _producao_aceita(url: str) -> int:
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gravar", action="store_true", help="cadastra (sem isto, só consulta)")
    ap.add_argument("--base", default=BASE_PADRAO, help="URL pública do backend")
    args = ap.parse_args()

    token = os.environ.get("C6_WEBHOOK_TOKEN", "").strip()
    cid = os.environ.get("C6_CLIENT_ID", "").strip()
    if not token:
        print("Falta C6_WEBHOOK_TOKEN (copie do Render, serviço blaxx-pontos-exe).")
        return 2
    sandbox = os.environ.get("C6_ENV", "sandbox").strip().lower() != "production"
    cert, key = resolve_cert_paths(
        os.environ.get("C6_CERT_PATH", ""), os.environ.get("C6_KEY_PATH", ""),
        os.environ.get("C6_CERT_PEM", ""), os.environ.get("C6_KEY_PEM", ""),
    )
    p = C6PixProvider(cid, os.environ["C6_CLIENT_SECRET"], os.environ["C6_PIX_KEY"],
                      cert_path=cert, key_path=key, sandbox=sandbox)
    m = lambda s: _mascara(s, token, cid, os.environ.get("C6_PIX_KEY", "").strip())  # noqa: E731

    url_pix = f"{args.base}/payments/c6/webhook/{token}"
    url_checkout = f"{url_pix}/checkout"
    print(f"Ambiente C6: {'SANDBOX' if sandbox else 'PRODUÇÃO'}  ({p.host})")

    st = _producao_aceita(url_checkout)
    print(f"Backend aceita o token? POST {m(url_checkout)} -> {st}")
    if st != 200:
        print("O backend recusou o token (esperado 200). Confira o C6_WEBHOOK_TOKEN no Render. Nada cadastrado.")
        return 1

    def chamar(metodo, base, caminho, corpo=None):
        try:
            return 200, p._request(metodo, caminho, corpo, base=base)
        except C6Error as e:
            return getattr(e, "status", None) or 0, str(e)

    chave = urllib.parse.quote(p.pix_key)
    if args.gravar:
        st, r = chamar("PUT", "/v2/pix", f"/webhook/{chave}", {"webhookUrl": url_pix})
        print(f"Pix      PUT  webhook -> {st}  {m(json.dumps(r, ensure_ascii=False))[:300]}")
        st, r = chamar("POST", "/v1/webhooks", "/", {"url": url_checkout, "service": "CHECKOUT"})
        print(f"Checkout POST webhook -> {st}  {m(json.dumps(r, ensure_ascii=False))[:300]}")

    st, r = chamar("GET", "/v2/pix", f"/webhook/{chave}")
    print(f"Pix      GET  webhook -> {st}  {m(json.dumps(r, ensure_ascii=False))[:300]}")
    st2, r2 = chamar("GET", "/v1/webhooks", "/?service=CHECKOUT")
    print(f"Checkout GET  webhook -> {st2}  {m(json.dumps(r2, ensure_ascii=False))[:300]}")
    ok = (st == 200 and isinstance(r, dict) and r.get("webhookUrl") == url_pix
          and st2 == 200 and isinstance(r2, dict) and r2.get("url") == url_checkout)
    print("RESULTADO:", "os dois webhooks apontam para a produção" if ok else "ainda falta cadastrar (rode com --gravar)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
