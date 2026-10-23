# C6 Developers: CHECKOUT (C_01..C_06) no sandbox e no nosso webhook

Levantado em 22/10/2026. Nenhuma API do C6 foi chamada e nenhuma credencial foi lida.

Fontes:

- **Roteiro**: `~/Downloads/Roteiro de Testes - C6 Developers v3.0.docx`, lido de uma cópia de
  `word/document.xml`. O original não foi alterado. Os índices de tabela seguem a convenção do
  `01-mapa-roteiro.md`: 0-based, `body.iter(w:tbl)`.
- **Specs**: `specs/checkout.yaml` (Checkout C6 Bank v1.2.0) e `specs/webhook-api.yaml`
  (Notificações v1.0.10). Nas citações abaixo, `checkout.yaml:N` e `webhook-api.yaml:N` são
  números de linha desses arquivos.
- **Documentação pública** (WebFetch em 22/10):
  - `https://developers.c6bank.com.br/sitemap.xml` lista 17 páginas.
  - As páginas HTML `/apis/checkout`, `/apis/errors`, `/faq`, `/get-started` e
    `/apis/release-notes` devolveram **404** ao WebFetch, porque o portal é SPA. Nada foi
    aproveitado delas.
  - `https://developers.c6bank.com.br/yamls/authentication-api.yaml` respondeu. É a fonte
    sobre escopos.
- **Código**: `app/api/c6_webhook.py`, em duas versões:
  - **HEAD** (`deccb91`, igual a `fork/main`; o handler é do commit `20d0234`, que está no fork,
    portanto em produção). Tem 170 linhas.
  - **Cópia de trabalho NÃO commitada**, alterada por outro fluxo (última gravação 22/10
    18:51). Tem **321 linhas** (a primeira versão deste documento leu uma de 273; os números
    de linha da seção 4.3 foram refeitos).

  Os dois casos estão descritos na seção 4.

Revisão cética (22/10): índices do .docx recontados com script próprio, linhas das specs
conferidas, o receptor testado com o cliente de teste do Flask (arquivo fora do repo) e os
22 testes de `tests/test_c6_webhook_notificacoes.py` rodados (todos passam). As correções já
estão no texto.

## Resumo

| Pergunta | Resposta curta |
|---|---|
| Como o checkout chega a PAID no sandbox? | **Alguém paga o link no navegador.** A spec diz que pagamento "não é registrado manualmente": não existe endpoint de simulação. **Nenhuma fonte** traz cartão de teste, valor mágico ou aprovação automática: **a confirmar com o C6**. |
| C_06 (`POST /authorize`) | O roteiro chama o teste de "AUTORIZAR CHECKOUT COM **TOKEN**" e **não tem tabela** para ele. O caminho com fonte usa o **token** de um cartão salvo (`save_card=true`) no pagamento do C_0502, sem SDK. O caminho com **card hash** está bloqueado: a spec diz que a instalação do SDK está "indisponível no momento" e não informa a URL do script. |
| Escopos | O roteiro pede `checkout.write/read/capture/cancel`. Nenhuma spec liga escopo a operação. O AT_01 passa a conferir mais 4 escopos, e `checkout.capture` pode faltar porque a captura está "EM BREVE". |
| Webhook hoje (HEAD/produção) | Um evento `service=CHECKOUT` recebe **200 `{"ok": true, "results": []}`** sem log, sem crédito e sem erro. O C_0501/C_0502 "passa" no status, mas **não deixa evidência**. |
| Mudança mínima | Cabe na rota existente. A cópia de trabalho já faz o que está proposto aqui, com testes; ver 4.3. URL recomendada para o C_04 (decisão do dono, igual ao `05-mapa-checkout.md`): `https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/<C6_WEBHOOK_TOKEN>/checkout`. |

## Tabela A: o que o roteiro pede (literal) contra a spec

Tabelas do .docx: sumário em 35; "Sequência/Resultado esperado" e "Retornado" de C_01 a
C_0502 em 36 a 47. O checkbox CHECKOUT é o `w:checkBox` de índice **2**.

| ID | Roteiro (tabela) | Método e endpoint (roteiro) | Status roteiro | Spec | Divergência |
|---|---|---|---|---|---|
| C_01 | 36/37 | POST `/checkouts`, campos obrigatórios `amount`, `payment` | 201 | `POST /v1/checkouts/` → 201 `{id, url}` (`checkout.yaml:107-136`); obrigatórios `amount`, `payment` (`:1101-1103`) | nenhuma |
| C_02 | 38/39 | GET `/checkouts/{id}/` | 200 | 200 (`checkout.yaml:175-183`) | status igual; o roteiro tem barra final, o path da spec (`/{id}`, `:169`) não |
| C_03 | 40/41 | PUT `/checkouts/{id}/cancel` | **200** | **204** sem corpo (`checkout.yaml:257-271`) | status |
| C_04 | 42/43 | **PUT** `/webhooks`, body `url`, `service: "CHECKOUT"` | **204** | **POST** `/v1/webhooks/` → **200** com `WebhookResponse` (`webhook-api.yaml:92-108`, `:769-789`). O próprio roteiro, no B_07 (boleto), usa POST/200 | método e status |
| C_0501 | 44/45 | gerar um link depois do C_04 | 200 (o **nosso** endpoint), evento `CREATED` | evento CHECKOUT/CREATED (`webhook-api.yaml:34`, `:435-458`) | nenhuma |
| C_0502 | 46/47 | gerar um link e **"Acesse o link e realize o pagamento"** | 200, evento `PAID` | evento CHECKOUT/PAID (`webhook-api.yaml:40`, `:545-568`) | nenhuma |
| C_06 | só no sumário (35, R7): "AUTORIZAR CHECKOUT COM TOKEN" | **não tem tabela** de sequência nem de resultado | — | `POST /v1/checkouts/authorize` → 200, e o resultado vem no corpo (`checkout.yaml:141-164`) | o roteiro não diz o que fazer nem onde colar o resultado |

Na hora de preencher, vale o status que a API devolver de verdade, como no `01-mapa-roteiro.md`.

## 1. Como um checkout chega a PAID no sandbox

### O que as fontes dizem

- **O roteiro manda um humano pagar.** No C_0502 (tabela 46): "Acesse o link e realize o
  pagamento."
- **Não existe gatilho por API.** No evento PAID de Checkout, a spec diz que pagamentos "não são
  registrados manualmente (não são notificados após o uso de um endpoint)" e são "processados
  automaticamente pelo fluxo" (`webhook-api.yaml:430-432`). O mesmo texto aparece para Boleto
  (`:186`) e Pix (`:600`).
- **A URL do link** é para onde "o cliente pagador deve ser direcionado"
  (`checkout.yaml:22`, `:1306-1310`). Os exemplos divergem no host: `checkout.c6bank.info` em
  `checkout.yaml:1310` e `payment.c6pay.com.br` no evento CREATED (`webhook-api.yaml:458`).
- **Formas de pagamento do link**: carteiras digitais, cartão de crédito/débito e Pix
  (`checkout.yaml:58-64`). Com `payment.card.save_card=true`, só cartão de crédito aparece
  (`:44`, `:1277`).
- **Cada checkout aceita até 3 tentativas de pagamento**, e cada tentativa gera notificação
  (`checkout.yaml:112`). Recusa vira o evento `DECLINED` (`webhook-api.yaml:37`, `:497-520`).
- **Valor mínimo de R$ 5,00** (`checkout.yaml:839-844`). Os valores de R$ 1,00 usados no Pix
  **não servem** aqui.
- **Expiração padrão de 7 dias** (`checkout.yaml:1139-1144`). Isso dá folga para pagar o link
  no mesmo dia.

### O que NÃO tem fonte (a confirmar com o C6)

- **Cartão de teste do sandbox**: nenhuma spec traz um.
  - O `4111111111111111` / `JOAO DA SILVA` / `12/2025` / `123` do `/sdk-doc`
    (`checkout.yaml:441-448`, `:528-535`) é exemplo de código do SDK. A spec não diz que é
    cartão de teste do sandbox.
  - A validade 12/2025 já venceu.
  - Usar esse cartão como se fosse oficial é **suposição**.
- **Valor mágico ou nome do titular que force aprovação ou recusa**: sem fonte.
- **Simulação de pagamento** (endpoint ou botão no sandbox): sem fonte. O texto citado acima
  indica que não existe por API.
- **Aprovação automática do sandbox**: sem fonte. O evento `DECLINED` com `return_code: "78"`,
  `message: "Blocked Card"` no exemplo (`webhook-api.yaml:520`) mostra que o fluxo chama
  bandeira/emissor, ou um simulador deles.
- **Pagar o link com Pix no sandbox**: tem o mesmo bloqueador do L7 do `01-mapa-roteiro.md`
  (como pagar um Pix no sandbox).
- **Cartão real no sandbox**: não usar sem o C6 confirmar que não haverá cobrança.

### Pergunta exata para o e-mail ao C6 (homologacaoapi@c6bank.com)

> No sandbox da API de Checkout (`https://baas-api-sandbox.c6bank.info/v1/checkouts/`), como
> levamos um checkout ao status PAID para o teste C_0502?
> (a) Existem cartões de teste (número, validade, CVV, nome) para pagar a URL do link, e algum
> deles força aprovação ou recusa (DECLINED)?
> (b) Existe valor mágico, endpoint ou botão que simule o pagamento?
> (c) O pagamento do link via Pix funciona no sandbox, e como se paga esse Pix?
> (d) A URL devolvida no sandbox é `checkout.c6bank.info` ou `payment.c6pay.com.br`?
> (e) O C_06 ("Autorizar checkout com token") não tem tabela no roteiro v3.0. Onde colamos o
> resultado, e o token pode ser o `card_info.token` obtido no pagamento do C_0502 com
> `save_card=true`?

## 2. C_06: `POST /authorize`

### Campos obrigatórios (`authorizeRequest`, `checkout.yaml:858-957`)

| Campo | Regra (spec) | Linha |
|---|---|---|
| `amount` | number, R$ 5,00 a R$ 500.000,00, ponto decimal | `:839-844` |
| `external_reference_id` | `^[a-zA-Z0-9]{1,10}$`, **não pode repetir** para o mesmo cliente | `:1145-1151` |
| `payer.name` | até 100, não vazio | `:886-891` |
| `payer.tax_id` | 11 a 14 dígitos, sem máscara | `:892-897` |
| `payer.email` | regex de e-mail, até 150 | `:898-904` |
| `payer.phone_number` | `^[0-9]{1,11}$` | `:905-910` |
| `address` | `street` (até 200), `number` (number; street + number ≤ 40 posições), `city` (até 60), `state` (`[A-Z]{2}`), `zip_code` (`\d{8}`); `complement` opcional | `:797-838` |
| `payment.card.installments` | 1 a 18; acima de 1 exige `interest_type` | `:953-955`, `:1185-1189` |
| `payment.card.card_info` | **card_hash OU token**, só um dos dois | `:1051-1056` |

Recomendado: `payment.card.type` = `CREDIT` (`:1297-1302`). `payment` é obrigatório na lista
de `required` (`:862`), e `card` é obrigatório dentro de `payment` para o fluxo com cartão.

Ambiguidades da spec, todas **a confirmar**:

1. **`address` na raiz.** O `required` lista `address` na raiz (`:865`), mas `properties` não
   tem `address` na raiz. Só existe `payer.address` (`:911-912`). Sugestão: mandar o mesmo
   endereço em `payer.address`. Se a API reclamar, repetir na raiz.
2. **Formato de `card_info`.** O schema é `oneOf` de duas **strings**, com exemplo string
   (`:1051-1056`). Já o `/sdk-doc` manda pôr o hash em `payment -> card -> card_info -> card_hash`
   (`:599`), o que sugere objeto `{"card_hash": "..."}`. Por analogia, o token seria
   `{"token": "..."}`.
3. **`session_key`.** A spec diz que ele "deve ser enviado junto com o card hash no momento da
   autorização" (`:1248-1252`), mas `authorizeRequest` não tem campo para ele.
4. **`transactionId` do `init`.** É "id relacionado com a transação que será enviado
   posteriormente no payload do pagamento" (`:555`), mas a spec não diz em qual campo.
   Além disso, os pré-requisitos dizem que a chave pública e a de sessão "devem ser
   informadas no método `init`" (`:325`), enquanto a assinatura documentada é
   `init({ env, transactionId })` e as chaves vão no `createClient` (`:343`, `:353`).
5. **Status de recusa.** O 200 do `/authorize` não quer dizer aprovado: "Você deve avaliar o
   conteúdo do objeto para verificar o status da autorização" (`:159-160`).

### Caminho A, que o roteiro pede: token de cartão salvo (sem SDK)

O título do C_06 é "AUTORIZAR CHECKOUT COM TOKEN", e o `/authorize` é descrito como
autorização "utilizando um token de cartão obtido em uma autorização anterior"
(`checkout.yaml:146`; ver também `:45-46`, `:119`, `:1303-1305`).

O token sai de um pagamento aprovado com `payment.card.save_card=true` (`:41-44`, `:1273-1277`):

- **na resposta do `/authorize`**, em `card_info.token` (`card_info_response`, `:1057-1076`).
  Esse schema só é usado no `authorizeResponse` (`:1011`); o GET de consulta usa
  `card_info` (`:217-218`), então que a **consulta** devolva o token é **a confirmar**;
- **no evento PAID**, em `payment.card.token` dentro de `information` (exemplo em
  `webhook-api.yaml:568`). O exemplo DECLINED (`:520`) também traz um `token`.

Por isso, o checkout **novo** do C_0502 (não o do C_01, que é cancelado no C_03, como no
`05-mapa-checkout.md`) deve ser criado com:

```json
{"amount": 5.00, "external_reference_id": "<novo, ≤10 alfanum>",
 "description": "Homologacao BlaXx C_0502",
 "payment": {"card": {"type": "CREDIT", "installments": 1, "save_card": true}}}
```

`amount` 5.00 é o mínimo da spec, não um valor validado no sandbox.

Depois que o dono pagar o link, o executor:

1. faz `GET /{id}`;
2. lê o token **só em memória**;
3. chama `POST /authorize` com um `external_reference_id` novo, `payer`, `address`, `amount`
   ≥ 5 e `payment.card {type, installments: 1, card_info: <token>}`.

Pendência: o schema da consulta usa `card_info` (o `oneOf`), e não `card_info_response`
(`:217-218`). Que o `GET` devolve o token é **a confirmar** no sandbox. Se não devolver, a única
outra fonte do token é o corpo do evento PAID, e esse corpo o nosso handler **não** guarda nem
loga, de propósito (ver 4.3). Nesse caso, perguntar ao C6.

### Caminho B: card hash (checkout transparente), hoje bloqueado

Como funciona pela spec:

1. **`GET /v1/checkouts/generate/public-key?ttl=1..10`** (`checkout.yaml:724-757`) devolve
   `{public_key (PEM), session_key, expires_in}` (`:1239-1257`). A chave vale no máximo 10
   minutos (`:52-54`, `:736`). A spec diz "Necessário permissão para uso da requisição" sem
   nomear o escopo (`:734`).
2. **SDK no navegador**, objeto global `window.C6PaySDK` (`:390`):
   - `init({ env: 'homolog' | 'prod', transactionId })` é obrigatório e vem antes de tudo,
     porque "inicializa o SDK + antifraude" (`:317`, `:343-348`, `:564`);
   - `createClient({ publicKey, sessionKey })` (`:353`, `:460`);
   - `client.generateCardHash(card)` → Promise da string do hash (`:361`, `:462-468`). O
     `card` leva `{number, holderName, expirationMonth (MM), expirationYear (AAAA), cvv, brand}`,
     todos obrigatórios (`:614-623`).
   - O hash vai em `payment.card.card_info` do `/authorize` (`:599`, `:1097-1099`).
3. **De onde vem o SDK: sem fonte.**
   - "A instalação do SDK está indisponível no momento. Esta documentação serve apenas como
     referência" (`checkout.yaml:332-334`).
   - Os exemplos usam `script.src = 'C6PaySDK.js'`, um caminho relativo (`:398`, `:521`), e
     `<script src="" async>`, com o src vazio (`:406`).
   - Há também um erro de digitação no exemplo: `generateCardHashcard` (`:588`).
   - **A confirmar com o C6**: a URL do bundle para `env: 'homolog'`.
4. **Hash fora do navegador (Node/Python): não.**
   - A spec diz só que o SDK usa a Web Crypto API (`:292`, `:298`, `:316`). Não descreve
     algoritmo, padding nem formato do hash.
   - O `init` liga um antifraude descrito como obrigatório (`:564`).
   - Replicar a criptografia seria adivinhar e provavelmente quebraria o antifraude.
   - Pior: um script nosso receberia o número do cartão, o que este documento proíbe (abaixo).
5. **Se o C6 liberar o SDK**:
   - fazer uma **página HTML local** fora do backend, servida em `localhost` por um servidor
     estático. O backend não serve: o `after_request` de `app/__init__.py:354-375` manda
     `Content-Security-Policy: default-src 'none'`, e o SDK não carregaria;
   - a página carrega o SDK pela URL oficial e chama `init` → `createClient` →
     `generateCardHash`;
   - o dono digita o cartão de teste na própria página;
   - a página mostra só o hash, uma vez. O executor pede o hash por entrada sem eco
     (`getpass`), dentro da validade da `public_key`, e o usa só em memória.

### Regra: dado de cartão

- **Número, CVV, validade e nome do cartão NUNCA passam pelo nosso servidor nem pelo
  executor**, e nunca vão para arquivo, log, terminal, `resultados-sandbox.json`, `.docx`,
  print, histórico do shell ou commit. É o que o SDK manda: "não envie number, cvv ou demais
  dados do cartão ao backend, apenas o card hash" (`checkout.yaml:494`, `:509`).
- **No caminho A**, o cartão é digitado **só na página do C6** (a URL do link), nunca em página
  nossa.
- **No caminho B**, o cartão é digitado **só na página local com o SDK**:
  - sem `setDebugMode(true)`;
  - sem `setLogger` que imprima `context`;
  - sem o `console.log('Card Hash:', ...)` do exemplo (`:464`, `:575`).
- **O card hash e o token de cartão também são segredos**, porque autorizam cobrança:
  - ficam só na memória do executor;
  - no registro e no roteiro, vão como `<mascarado, N caracteres>`.
- **O token aparece em `information.payment.card.token`** dos eventos DECLINED e PAID
  (`webhook-api.yaml:520`, `:568`) e, possivelmente, no corpo do C_02 depois do pagamento
  (a confirmar, ver acima). O executor mascara antes de gravar, e o webhook não loga (4.3).
  **Atenção**: a máscara do `scripts/c6_preencher_roteiro.py` hoje **não** pega a chave
  `token`, nem `payer`, nem o conteúdo de `information` em string (ver A7 do
  `05-mapa-checkout.md`); o executor tem que entregar esses corpos já mascarados.
- **`payer` (nome, CPF/CNPJ, e-mail, telefone, endereço)** volta em quase todo corpo de checkout
  e dos eventos:
  - usar dado de teste;
  - mascarar `tax_id`, `email` e `phone_number` no roteiro, como a regra do projeto manda para
    dados pessoais.
- **`card_info_response.number`** volta mascarado pelo C6 (`483430******6234`,
  `checkout.yaml:1061-1064`). Pode ir para o roteiro como veio.

## 3. Escopos e o AT_01

- **O que o roteiro pede.** Na seção CHECKOUT: `checkout.write`, `checkout.read`,
  `checkout.capture`, `checkout.cancel`.
- **O que as specs dizem.**
  - O `checkout.yaml` só declara `bearerAuth` (`:1311-1315`). Nenhuma operação tem `security`
    com escopo.
  - O exemplo de `scope` do `authentication-api.yaml` é
    `"bankslip.write webhook.read pix.read webhook.write pix.write bankslip.read"`, sem nada de
    checkout.
  - O campo da resposta é **`scope`** (singular). O roteiro escreve "scopes".
- **Implicações para o AT_01.**
  1. **Conferir mais escopos.** Com PIX e CHECKOUT marcados, o AT_01 confere os **16**
     escopos: os 12 do `01-mapa-roteiro.md` (10 do Pix, mais `webhook.read` e `webhook.write`)
     e os 4 de checkout.
     - O C_04 usa a Webhook API (`/v1/webhooks/`). Que ela usa `webhook.read/write` é
       **inferência**: os nomes estão no exemplo do auth, mas nenhuma spec liga escopo a esse
       endpoint.
  2. **Qual escopo cada chamada usa é suposição**, porque não há mapeamento na spec. A
     distribuição provável é esta:
     - `checkout.write` para `POST /`, `/authorize` e `generate/public-key`;
     - `checkout.read` para `GET /{id}`;
     - `checkout.cancel` para `PUT /{id}/cancel`;
     - `checkout.capture` para `PUT /{id}/capture`, que nenhum C_* usa.
  3. **Parar cedo.** Faltando `checkout.write`, `checkout.read` ou `checkout.cancel`, o
     executor deve **parar a seção CHECKOUT antes de gastar a janela**. Pela introdução do
     roteiro, o sintoma é `403 Forbidden`.
  4. **`checkout.capture` ausente só gera aviso.** A captura está "DISPONÍVEL EM BREVE"
     (`checkout.yaml:29-30`, `:234`), e o C6 pode não conceder esse escopo.
  5. **Perguntar no e-mail** se a nova credencial sandbox virá com os 4 escopos de checkout e
     qual escopo o `generate/public-key` exige.

## 4. Webhook no nosso backend

### 4.1 O que acontece HOJE (HEAD = `fork/main` = produção, `git show HEAD:app/api/c6_webhook.py`)

Evento de entrada, pelo formato da spec (`webhook-api.yaml:441-458`, `:790-808`):

```
POST /payments/c6/webhook/<C6_WEBHOOK_TOKEN>
{"external_id": "...", "date_time": "...", "client_id": "...", "partner_id": "...",
 "service": "CHECKOUT", "status": "CREATED"|"PAID"|..., "information": "<JSON em string>"}
```

Passo a passo, nas linhas do HEAD:

1. **Rota.** `@bp.post("/webhook/<token>")` e `.../pix` (linhas 51-52), registradas sob
   `/payments/c6` (`app/__init__.py:569`). A URL sem sufixo casa. **Com sufixo `/checkout`, dá
   404** no HEAD.
2. **Token.** `_verify_token` (35-48) compara em tempo constante. Token errado devolve 401
   (55-57).
3. **Corpo.** `request.get_json(silent=True)` (59), **sem `force`**:
   - se o C6 não mandar `Content-Type: application/json`, o resultado é `None`, vira `{}` e
     segue com lista vazia;
   - se mandar, `payload` é o dict do envelope.
4. **Itens.** `payload.get("pix")` é `None`, então não é lista (62-63). O fallback
   `[payload] if payload.get("txid") else []` (65) dá **`items = []`**, porque o envelope não
   tem `txid`.
5. **Laço.** O laço (69-94) não roda. `deferred` continua `False`.
6. **Resposta.** **`200 {"ok": true, "results": []}`** (102).

Resultado:

- **Responde 200**, e o C_0501/C_0502 "passa" no status.
- **Não loga nada**: nenhuma linha do caminho vazio chama o logger.
- **Não quebra**: nada lê `information`.
- **Não credita**: `_process` (105-165) nunca é chamado. Mesmo que fosse, exigiria `txid` de
  uma `PixCharge`, e checkout não tem.

O problema é que **não fica evidência**. O roteiro pede o "Response Body - Retornado" de C_0501
e C_0502, e não teríamos o evento recebido nem prova de que chegou.

Fora do handler, nada atrapalha:

- o `after_request` (`app/__init__.py:354-375`) só põe headers;
- o limiter não tem limite global (`app/extensions.py:69`); a rota tem 120/min (HEAD linha 53).

### 4.2 Mudança mínima proposta (não implementada por este fluxo)

**Cabe na rota existente. Não precisa de rota nova.**

1. **Ler o corpo com `force=True`.** Trocar a linha 59 por
   `request.get_json(silent=True, force=True)`, porque nenhuma spec garante o `Content-Type`
   das notificações.
2. **Desviar para um handler próprio.** Logo depois do `isinstance(payload, dict)` (60-61), se
   não houver chave `pix` e houver `service`, chamar uma função própria. Essa função:
   - normaliza `service` e `status` (maiúsculas, saneados para log);
   - **se `service == "CHECKOUT"`**:
     - decodifica `information` com `json.loads` quando vier string. Os exemplos às vezes
       trazem o **envelope inteiro** serializado ali dentro (`webhook-api.yaml:458`, `:270`),
       então é preciso desembrulhar mais de um nível;
     - loga **uma linha INFO** só com `external_id`, `status`, `date_time`,
       `information.id`, `external_reference_id`, `amount`, `transaction_type`/`card.type`,
       `installments`, `return_code`/`message` (quando houver recusa) e
       `fraud_analysis.recommendation`;
     - **nunca** loga `payer` (nome, `tax_id`, e-mail, telefone, endereço),
       `payment.card.token`, `url`, `description`, `client_id`, o envelope cru ou o
       `information` cru (exemplos de conteúdo sensível em `webhook-api.yaml:520`, `:568`);
     - responde **200** `{"ok": true, "service": "CHECKOUT", "status": ..., "external_id": ...,
       "result": "registrado"}`;
     - **não toca no banco nem no ledger**: não importa `purchase`, não chama
       `confirm_payment`.
   - **outros `service`** (`BANK_SLIP`, `BANK_SLIP_PIX`, `PIX`): log e 200. Para `PIX`, o log
     deve ser **WARNING**, porque é o envelope do L5 do `01-mapa-roteiro.md`: um Pix pago chegou
     num formato que o crédito não entende.
3. **Idempotência.** Como nada é persistido nem creditado, reentrega é inofensiva por
   construção. Não é preciso tabela nova. Quando existir compra por cartão, vale a regra do Pix:
   - o corpo é gatilho;
   - reconsultar `GET /v1/checkouts/{id}` antes de creditar;
   - usar a chave de idempotência por checkout no ledger.
4. **Testes** em arquivo **novo** (por exemplo `tests/test_c6_webhook_checkout.py`, porque o
   `tests/test_c6_homologacao.py` é de outro fluxo):
   - envelope CREATED/PAID/DECLINED dá 200 `registrado`;
   - `information` com envelope aninhado é decodificado;
   - `information` ilegível dá 200 com WARNING;
   - o log **não** contém `tax_id`, e-mail nem token (usar `caplog`);
   - **nenhum** `LedgerEntry`/`PixCharge` muda;
   - sem `Content-Type`, ainda dá 200 `registrado`;
   - token errado dá 401;
   - o corpo `{"pix": [...]}` continua indo para `_process` (regressão do Pix).

**URL que o C_04 deve cadastrar** (`POST /v1/webhooks/`, body
`{"url": ..., "service": "CHECKOUT"}`):

```
https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/<C6_WEBHOOK_TOKEN>/checkout
```

Recomendação revisada (a primeira versão recomendava a URL **sem** sufixo; decisão final do
dono, e o `05-mapa-checkout.md` diz o mesmo):

- na cópia de trabalho, base e `/checkout` tratam o checkout igual: o desvio é pelo corpo
  (`service`), não pelo caminho (testado: 200 `registrado` nas duas);
- "funcionar no HEAD" sem sufixo é enganoso: o HEAD responde 200 `{"ok": true, "results": []}`
  **sem log** (4.1), então o C_0501 passaria sem evidência. Com o sufixo, o HEAD dá **404** e a
  falta do deploy aparece na hora;
- é https, como a spec exige (`webhook-api.yaml:767`);
- o token vem da env var na hora e nunca é escrito em arquivo.

O webhook de checkout é por `client_id` (`webhook-api.yaml:753-759`), separado do webhook Pix
do P_06_01, que é cadastrado pela API Pix e recebe o sufixo `/pix`.

**A mudança precisa estar em produção antes do C_04.** Sem ela, o C_0501/C_0502 não deixa
evidência. Deploy é decisão do dono (skill `blaxx-deploy`, push no fork `RVELES/blaxx-pontos`).
Este fluxo não faz deploy.

### 4.3 A cópia de trabalho (não commitada) já faz isso

Outro fluxo alterou `app/api/c6_webhook.py` (última gravação 18:51 de 22/10; `M` no
`git status`). Nas linhas da cópia de trabalho (321 linhas):

- **Docstring** (23-32) descreve o formato de checkout e a regra de não creditar.
- **Rota nova** `@bp.post("/webhook/<token>/checkout")` (66), além das duas antigas (64-65).
- **`get_json(silent=True, force=True)`** (74).
- **Desvio** (77-90): sem chave `pix` e com `service`:
  - `service != "PIX"` vai para `_notificacao_c6` (78-79);
  - `service == "PIX"` desembrulha `information` e, se achar a lista `pix`, segue para o
    crédito normal com reconsulta na API (83-90); se não achar, **WARNING** e 200
    `ignored: information` (84-89).
- **`_limpo`** (243-247) remove tudo fora de `[A-Za-z0-9_.:+\- ]` e corta o tamanho, contra
  log injection.
- **`_decodifica_information`** (250-266) faz até 3 passos de string/envelope; basta para o
  exemplo CREATED da spec (string → envelope → string → `{id, url}`).
- **`_resumo_checkout`** (269-290) deixa de fora `payer`, `card.token`, `url`, `description`
  e `client_id`.
- **`_notificacao_c6`** (293-321):
  - `service != CHECKOUT` (BANK_SLIP etc.) gera log INFO e 200 `ignorado` (298-303);
  - CHECKOUT gera uma linha INFO `c6 checkout <STATUS>: external_id=... date_time=... id=...
    status=... referencia=... valor=... tipo=...` e 200 `registrado` (305-321);
  - `information` ilegível gera WARNING e ainda 200 (306-310);
  - não toca no banco.

Conferido nesta revisão (cliente de teste do Flask, arquivo fora do repo, exemplos
CREATED/PAID/DECLINED da spec): 200 `registrado` na URL base e na `/checkout`; 200 sem
`Content-Type`; 401 com token errado; **405 para PUT e GET** (se o C6 notificar com outro
método que não POST, a notificação se perde: a confirmar); 404 em sufixo desconhecido; o log
não contém nome, CPF/CNPJ, e-mail, token do cartão, URL do link nem `client_id`.

Situação em relação à proposta 4.2:

1. **`service=PIX` no envelope**: tratado (desembrulha e credita com reconsulta; WARNING quando
   não dá). A primeira versão deste documento dizia que era "INFO e ignorado"; isso mudou na
   gravação das 18:51.
2. **Testes**: existem em `tests/test_c6_webhook_notificacoes.py` (não commitado, outro
   fluxo): 22 testes, todos passando em 22/10, cobrindo rota `/checkout` e base, log sem dado
   do pagador/cartão, DECLINED, `information` objeto/ilegível, token errado, sem
   `Content-Type`, log injection e o Pix no envelope.
3. **O que falta**: guardar o corpo recebido (mascarado) para o "Response Body - Retornado"
   do C_0501/C_0502. Hoje só existe a linha de resumo no log. Decisão do dono, porque mexe
   em `app/`.
4. **Commit e deploy**: ainda não aconteceram. O que está em produção é o HEAD (4.1).

## 5. Ordem de execução e passos manuais do dono

Dentro de uma janela do sandbox (seg-sex, 7h-22h, segundo o `01-mapa-roteiro.md`):

1. **Antes do dia**:
   - **[dono]** decidir e fazer o deploy da mudança 4.2/4.3;
   - **[dono]** mandar o e-mail com as perguntas das seções 1 e 3;
   - **[dono]** escolher os dados de teste do `payer` e do `address`. Não inventar: usar o que o
     C6 aceitar, de preferência os dados da própria empresa (`02-dados-organizacao.md`).
2. **AT_01**: conferir os 16 escopos (seção 3).
3. **[dono]** acordar o serviço no Render (um GET em qualquer rota pública) e abrir os logs do
   serviço `blaxx-pontos-exe`. Ficam abertos o dia todo: é dali que sai a evidência de C_0501 e
   C_0502.
4. **C_04**: `POST /v1/webhooks/` com a URL de 4.2 e `service: "CHECKOUT"`.
   - registrar o status real (spec: 200; roteiro: 204);
   - mascarar o token na `url` do corpo (`<TOKEN:Nch>`) e o `client_id`.
Mesma ordem do `05-mapa-checkout.md` (unificada nesta revisão; a primeira versão deste
documento pagava o checkout do C_01 e cancelava um segundo, o 05 fazia o contrário). Fica a
leitura literal do roteiro: o C_03 cancela o checkout do C_01, e o C_0502 "gera um link de
pagamento" novo.

5. **C_01**: checkout **A**, valor ≥ R$ 5,00, sem `save_card` (a forma de pagamento é escolha
   do dono; o roteiro só pede `amount` e `payment`).
   - Guardar `id` e `url`. A `url` não é segredo, mas não precisa ir para o log.
   - **C_0501**: confirmar no log do Render a linha `c6 checkout CREATED: external_id=<id de A>`.
6. **C_02**: `GET /{id de A}`, status `CREATED`.
7. **C_0502**: checkout **B**, com `save_card: true` (corpo da seção 2).
   - **[dono]** abrir a `url` do checkout B no navegador e pagar com o **cartão de teste que o
     C6 informar**. São no máximo 3 tentativas, e uma recusa gera `DECLINED`.
   - O cartão é digitado **só** na página do C6. Nada de print com os dados do cartão, nada de
     copiar e colar em chat, arquivo ou terminal.
   - Confirmar no log a linha `c6 checkout PAID: ...`.
   - Opcional (fora da tabela): `GET /{id de B}` para mostrar `status: PAID`.
8. **C_03**: `PUT /{id de A}/cancel` (A ainda não pago: é só "abortado").
   - Registrar o status real (spec: 204; roteiro: 200).
   - Não cancelar o B: cancelar um checkout pago gera estorno (`checkout.yaml:38`, `:262`) e
     pode invalidar o token do C_06 (a confirmar).
9. **C_06** (caminho A): o executor lê o token do B (`GET /{id de B}`, se devolver; ver
   seção 2), só em memória, e chama `POST /authorize`.
   - Registrar o corpo com o token e os dados do `payer` mascarados.
   - Se o C6 exigir card hash (caminho B): **[dono]** digita o cartão de teste na página local
     com o SDK e cola o hash no executor por entrada sem eco, dentro de 10 minutos da
     `public_key`.
10. **Roteiro**:
    - **[dono]** decidir o que vai no "Response Body - Retornado" de C_0501/C_0502. A sugestão é
      o envelope recebido, com `information` resumido e `client_id` mascarado, mais o nosso 200.
      O roteiro não especifica.
    - **[dono]** decidir onde colar o C_06, que não tem tabela.
    - **[dono]** marcar o checkbox CHECKOUT (índice 2).
    - O `formato-resultados.md` hoje exclui os `C_*` e precisa ser estendido pelo fluxo dono
      desse arquivo.

## Perguntas para o C6 (somar às do `01-mapa-roteiro.md`)

1. Cartões de teste e forma de levar um checkout a PAID no sandbox (texto completo na seção 1).
2. A credencial sandbox virá com `checkout.write`, `checkout.read`, `checkout.capture` e
   `checkout.cancel`? Qual escopo o `GET /generate/public-key` exige? A Webhook API
   (`/v1/webhooks/`) usa `webhook.read`/`webhook.write`?
3. C_06: não há tabela no roteiro v3.0. O teste é "com token" (`save_card`) ou com card hash?
   Onde colamos o resultado?
4. `/authorize`:
   - `address` vai na raiz ou em `payer.address`?
   - `card_info` é string ou objeto `{card_hash}`/`{token}`?
   - Onde vão o `session_key` e o `transactionId` do `init`?
5. Qual é a URL oficial do `C6PaySDK.js` para `env: 'homolog'`? A spec diz que a instalação
   está "indisponível no momento".
6. C_04: PUT/204 (roteiro) ou POST/200 (spec)? C_03: 200 (roteiro) ou 204 (spec)?
7. As notificações de checkout chegam com `Content-Type: application/json`? Há reenvio
   em caso de não-2xx, e com que política?
