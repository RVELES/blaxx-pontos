# PIX e cartão do C6 em produção: o que falta e em que ordem (23/10/2026)

## Onde estamos

| Peça | Estado |
|---|---|
| Código do PIX (cobrança, webhook, polling) | em produção desde 17/09; webhook novo (Checkout + PIX no envelope do C6) em produção desde 22/10 (`b4a6fa9`) |
| Código do cartão (link de pagamento C6 Pay) | em produção desde 23/10 (`2d22a77`), desligado (`C6_CHECKOUT_ENABLED` ausente) |
| Cartão | **Decisão de 24/10: PIX e cartão só pelo C6; o Stripe sai** (`CARD_ENABLED=0`, sem `STRIPE_*`) |
| Botão "Pagar com cartão" no site | pronto; só aparece quando o backend anuncia o C6 em `/payments/card/config` |
| Credenciais que o Render usa hoje | **as do sandbox ANTIGO, cancelado pelo C6 em 22/10** → a compra por PIX está quebrada |
| Credenciais novas do sandbox | em `~/.config/blaxx/c6/` (sandbox.env, sandbox.crt, sandbox.key) |
| Credenciais de PRODUÇÃO | **não existem ainda**: o C6 só libera depois de aprovar a homologação |

**Bloqueador que só o C6 resolve:** cobrar dinheiro de verdade exige a credencial de produção.
O caminho é enviar o roteiro (`envio-c6/`) para homologacaoapi@c6bank.com, a partir de
rveles@blaxxpontos.com.br, e aguardar a resposta. Depois disso, a credencial de produção é
gerada no web banking PJ do C6, no mesmo CNPJ (68.260.588/0001-56).

## Etapa A: agora, com o sandbox novo (o PIX volta a gerar cobranças)

Tudo no Render → serviço `blaxx-pontos-exe` → Environment. Os valores estão em
`~/.config/blaxx/c6/`. Copie de lá direto para o painel, sem passar por chat ou e-mail.

| Variável | Valor |
|---|---|
| `C6_CLIENT_ID` | de `sandbox.env` |
| `C6_CLIENT_SECRET` | de `sandbox.env` |
| `C6_PIX_KEY` | de `sandbox.env` |
| `C6_CERT_PEM` | conteúdo inteiro de `sandbox.crt` (o provider aceita qualquer formato de colagem) |
| `C6_KEY_PEM` | conteúdo inteiro de `sandbox.key` |
| `C6_ENV` | `sandbox` (já está) |
| `PIX_PROVIDER` | `c6` (já está) |
| `C6_WEBHOOK_TOKEN` | de `~/.config/blaxx/c6/webhook_token` (o backend recusa subir sem ele) |
| `C6_CHECKOUT_ENABLED` | `1` |
| `C6_CHECKOUT_REDIRECT_URL` | `https://blaxxpontos.com.br/comprar-livre.html` |
| `CARD_ENABLED` | `0` (ou apagar). Com `1`, o backend exige as chaves do Stripe e recusa subir |

Enquanto o C6 não liberar a produção, o QR e o link de cartão são do sandbox: o cliente
vê, mas não consegue pagar.

Depois: **Manual Deploy → Deploy latest commit** (o auto-deploy do Render não dispara).

Cadastro dos webhooks (no terminal do Mac, na pasta do backend):

```bash
source ~/.config/blaxx/c6/sandbox.env
read -rs "C6_WEBHOOK_TOKEN?token do webhook (Render): " && export C6_WEBHOOK_TOKEN; echo
python3 scripts/c6_registrar_webhooks.py --gravar
```

O script só cadastra se a produção aceitar o token, e confere os dois cadastros no final.

**O que a etapa A resolve e o que não resolve:** a geração do QR volta a funcionar, mas o
QR do sandbox não recebe dinheiro de verdade. PIX real só na etapa B.

**Atenção ao risco já conhecido:** com `C6_ENV=sandbox`, um pagamento SIMULADO pelo C6
creditaria pontos reais. No sandbox novo o C6 não permite pagar cobranças nem links,
então na prática nada é creditado. Os clientes conseguem ver o QR e o link, mas o dinheiro
não entra até a etapa B.

## Etapa B: quando o C6 aprovar (dinheiro de verdade)

1. Gerar a credencial de produção no web banking PJ do C6 (client_id, client_secret e o par .crt/.key de produção) e cadastrar a chave Pix da conta PJ.
2. Guardar uma cópia em `~/.config/blaxx/c6/producao.env|.crt|.key` (chmod 600, fora do Dropbox).
3. No Render: trocar `C6_CLIENT_ID`, `C6_CLIENT_SECRET`, `C6_PIX_KEY`, `C6_CERT_PEM`, `C6_KEY_PEM` pelos de produção e `C6_ENV=production`. Manual Deploy.
4. Rodar de novo `scripts/c6_registrar_webhooks.py --gravar` com as credenciais de produção. Os webhooks são por ambiente: os do sandbox não valem em produção.
5. Prova de vida: uma compra real de pacote pequeno por PIX e uma por cartão, conferindo o crédito na carteira e o extrato da conta PJ.

## O que já está provado

- 22/10 20h45-21h05, sandbox real: autenticação, 18 testes PIX e C_01..C_04 do Checkout
  (`resultados/`).
- O C6 recusa algumas rotas sem `Accept: application/problem+json`. Corrigido no provider de
  produção (o cadastro do webhook PIX falharia em produção sem isso).
