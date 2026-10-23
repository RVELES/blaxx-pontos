# C6 Developers: mapa do roteiro CHECKOUT (C_01..C_06) contra a spec

Levantado em 22/10/2026 sem nenhuma chamada à API do C6 e sem credencial. Segue o mesmo
formato do `01-mapa-roteiro.md` (AT_01 + Pix), que continua valendo para a autenticação.

Fontes:

- **Roteiro**: `~/Downloads/Roteiro de Testes - C6 Developers v3.0.docx` (sha256
  `66201bb5…d9f3b8`). O original não foi alterado. O texto saiu de uma cópia descompactada
  (`word/document.xml`), lida com `xml.etree` em ordem de documento.
- **Specs oficiais** (cópias em `docs/c6-homologacao/specs/`):
  - `checkout.yaml` "Checkout C6 Bank" v1.2.0, servidor `/v1/checkouts/`;
  - `webhook-api.yaml` "Notificações" v1.0.10, servidor `/v1/webhooks`;
  - para escopos, o `authentication-api.yaml` v1.1.2 baixado na mesma data.
- **Código** (somente leitura):
  - `app/pix/c6.py`;
  - `app/api/c6_webhook.py`, que tem alteração **não commitada** feita por outro fluxo
    (última gravação 22/10 18:51, 321 linhas; o HEAD `deccb91` tem 170) e que acrescenta a
    rota `/checkout`, o desvio do envelope do C6 e o tratamento de `service: "PIX"`;
  - `tests/test_c6_webhook_notificacoes.py` (também não commitado; 22 testes, todos passando
    em 22/10, rodados localmente sem rede);
  - `scripts/c6_preencher_roteiro.py` e `scripts/c6_homologacao.py`.

Revisão cética (22/10, depois da primeira versão): índices do .docx recontados com script
próprio (`zipfile` + `xml.etree`), linhas das specs conferidas uma a uma e o receptor testado
com o cliente de teste do Flask num arquivo fora do repo. As correções estão incorporadas
abaixo; o que continua sem fonte está marcado "a confirmar".

Escopo: a seção CHECKOUT do roteiro. São 7 linhas no sumário (C_01, C_02, C_03, C_04,
C_0501, C_0502 e C_06), mas só **6 têm caso de teste** no documento. Ver o achado A1.

## Resumo

| | |
|---|---|
| Testes no sumário da seção | 7 (C_01, C_02, C_03, C_04, C_0501, C_0502, C_06) |
| Testes com tabela de passos + tabela de resultado | **6**. O **C_06 só aparece no sumário**: não tem passos, resultado esperado nem campos para preencher |
| Chamadas de API que o executor faz | 5: C_01, C_02, C_03, C_04 e C_06 (este último sem roteiro) |
| Testes que são **recebimento** de webhook (nós somos o servidor) | 2: C_0501 (`CREATED`) e C_0502 (`PAID`) |
| Divergências roteiro x spec confirmadas | C_03 (200 x **204**), C_04 método (PUT x **POST**), C_04 status (204 x **200**), mais as de campos/headers da Tabela B |
| Código que já existe para Checkout | Nenhuma chamada de saída (nem no provider, nem no executor `scripts/c6_homologacao.py`). Só o **recebimento** do webhook (rotas `/payments/c6/webhook/<token>` e `.../checkout`, que apenas logam um resumo), ainda não commitado nem publicado |
| Bloqueadores externos | C_0502 exige **pagar o link no sandbox** (meio de teste não documentado). C_06 exige um **token de cartão** vindo de um pagamento aprovado com `save_card=true` |

## Convenção de caminho

O roteiro escreve os endpoints sem o prefixo de versão. A URL real fica assim:

| Roteiro | URL real (spec) | Observação |
|---|---|---|
| `/checkouts` (C_01) | `POST {host}/v1/checkouts/` | Servidor `.../v1/checkouts/` + path `/`. A barra final faz parte do path documentado ("Path /v1/checkouts/") |
| `/checkouts/{id}/` (C_02) | `GET {host}/v1/checkouts/{id}` | A spec não tem barra final, o roteiro tem. Usar a forma da spec; se der 404, registrar e perguntar ao C6 (não "tentar até passar") |
| `/checkouts/{id}/cancel` (C_03) | `PUT {host}/v1/checkouts/{id}/cancel` | |
| (sem teste) captura | `PUT {host}/v1/checkouts/{id}/capture` | "DISPONÍVEL EM BREVE" na spec. O roteiro não testa, mas exige o escopo `checkout.capture` |
| C_06 (sem endpoint no roteiro) | `POST {host}/v1/checkouts/authorize` | É o `operationId: authorize`, que a spec descreve como "autorização ... utilizando um token de cartão" |
| (sem teste) checkout transparente | `GET {host}/v1/checkouts/generate/public-key?ttl=1..10` | Fora do roteiro |
| `/webhooks` (C_04) | `POST {host}/v1/webhooks/` | Webhook API genérica, **não** a `/v2/pix/webhook/{chave}` do P_06. O próprio `webhook-api.yaml` só manda o Pix para a API Pix |

`{host}`: sandbox `https://baas-api-sandbox.c6bank.info` (seg-sex, 7h-22h) e produção
`https://baas-api.c6bank.info`. Os dois hosts são iguais aos do Pix e da autenticação.

## Pré-requisitos que o roteiro pede

Texto da seção, em resumo: certificado importado (mTLS), token de acesso no header de toda
requisição e "todos os escopos necessários". Para o Checkout ele lista **4 escopos**:

```
checkout.write
checkout.read
checkout.capture
checkout.cancel
```

O roteiro ainda manda conferir os escopos "através dos campos access_token e scopes". O campo
real se chama **`scope`** (singular): uma string com os escopos separados por espaço
(`authentication-api.yaml`, `AuthenticationResponse.scope`).

### Como o AT_01 deve conferir os escopos do Checkout

1. Ler `scope` do corpo do `POST /v1/auth/` e fazer `split()` por espaço.
2. **Obrigatórios para seguir** (abortar antes de gastar a janela se faltar algum):
   `checkout.write`, `checkout.read`, `checkout.cancel`. `checkout.capture` também é
   exigido pelo roteiro e deve estar lá, mesmo sem teste de captura.
3. **A confirmar**: `webhook.write` e `webhook.read`. A seção CHECKOUT não os lista, e o
   `webhook-api.yaml` não declara escopo em nenhuma operação. Só que o exemplo de `scope` do
   `authentication-api.yaml` traz `webhook.read`/`webhook.write`, e o P_06 do Pix exige os
   dois. Se o `scope` vier sem eles, avisar e rodar o C_04 mesmo assim: um 403 nele é o
   retorno real e a prova de que faltava escopo.
4. Nenhuma das duas specs (`checkout.yaml`, `webhook-api.yaml`) declara escopo por operação.
   Os dois `securitySchemes` são só `bearerAuth` (http bearer, JWT), sem OAuth scopes. Por
   isso o mapeamento abaixo é **inferido pelo nome**, sem fonte na spec:

| Operação | Escopo provável (inferido) |
|---|---|
| `createCheckout` (C_01) | `checkout.write` |
| `getCheckout` (C_02) | `checkout.read` |
| `cancelCheckout` (C_03) | `checkout.cancel` |
| `captureCheckout` (sem teste) | `checkout.capture` |
| `authorize` (C_06) | a confirmar (`checkout.write`?) |
| `generatePublicKey` (fora) | a confirmar. A spec só diz "Necessário permissão para uso da requisição" |
| `POST /v1/webhooks/` (C_04) | a confirmar (`webhook.write`?) |

Junto com os 12 escopos do Pix (`01-mapa-roteiro.md`, L8, que **já incluem** `webhook.read`
e `webhook.write`), a credencial nova precisa trazer **16 escopos** (12 + 4 `checkout.*`).
Hoje `_authenticate()` (`app/pix/c6.py:360-362`) só avisa quando falta `cob.write`.

## Tabela A: o que o roteiro pede (literal)

Header comum a todos os C_* com chamada: `Authorization: Bearer {token}`. O roteiro não
cita `Content-Type`. As palavras entre aspas são do roteiro.

| ID | Título (sumário) | Método | Endpoint (roteiro) | Campos (roteiro) | Status esperado | Resultado / evento esperado (roteiro) |
|---|---|---|---|---|---|---|
| C_01 | CRIAÇÃO DE CHECKOUT (LINK DE PAGAMENTO) | POST | `/checkouts` | "Enviar requisição de criação do checkout informando os campos obrigatórios: amount, payment" | 201 | "Checkout criado com sucesso e link de pagamento retornado." |
| C_02 | CONSULTA DE CHECKOUT | GET | `/checkouts/{id}/` | "Informe o identificador do checkout no path parameter" | 200 | "Dados do checkout retornados com sucesso, incluindo o status do pagamento." |
| C_03 | CANCELAMENTO DE CHECKOUT | PUT | `/checkouts/{id}/cancel` | "Informe o identificador do checkout no path parameter" | 200 | "Checkout cancelado com sucesso." |
| C_04 | CADASTRO DE WEBHOOK (EVENTOS DE CHECKOUT) | PUT | `/webhooks` | "No body, adicione os campos: url: {sua_url_de_webhook_teste}, service: \"CHECKOUT\"" | 204 | "Webhook de checkout cadastrado com sucesso." |
| C_0501 | RECEBIMENTO DE EVENTO DE CRIAÇÃO DE LINK VIA WEBHOOK | (nenhum: o C6 chama a nossa URL) | (a URL do C_04) | "é necessário possuir um webhook previamente cadastrado, como realizado no teste C_04" e "Após o cadastro de um webhook, gere um link de pagamento." | 200 | "Evento de checkout criado recebido com sucesso no webhook." Evento: ('CREATED') |
| C_0502 | RECEBIMENTO DE EVENTO DE PAGAMENTO DE LINK VIA WEBHOOK | (nenhum: o C6 chama a nossa URL) | (a URL do C_04) | igual ao C_0501, mais "Acesse o link e realize o pagamento." | 200 | "Evento de checkout pago recebido com sucesso no webhook." Evento: ('PAID') |
| C_06 | AUTORIZAR CHECKOUT COM TOKEN | **não informado** | **não informado** | **não informado** | **não informado** | **não informado**. Só existe a linha do sumário (tabela 35, R7) |

### C_06 lido inteiro

O texto `C_06` aparece **uma única vez** em `word/document.xml`, na linha R7 da tabela-sumário
35: `C_06 | AUTORIZAR CHECKOUT COM TOKEN`. A busca foi feita em todas as partes do pacote
(`document.xml`, cabeçalhos, rodapés, notas) por `C_06` e por `autoriz`, sem distinguir
maiúsculas. Não existe:

- título "C_06 – ..." (os outros testes têm um parágrafo de título antes das tabelas);
- tabela "TESTE: C_06" de "Sequência De Testes / Resultado Esperado";
- tabela "TESTE: C_06" de "Status Code - Retornado / Response Body - Retornado";
- campo FORMTEXT para o resultado.

Logo depois da tabela 47 (resultado do C_0502), o documento já passa para "EXTRATO".

O que a spec diz sobre a operação que o título sugere (`POST /authorize`):

- "Permite a autorização automática e síncrona de um checkout, utilizando um token de cartão
  obtido em uma autorização anterior."
- Corpo obrigatório (`authorizeRequest.required`): `payer`, `payment`,
  `external_reference_id`, `amount`, `address`.
  - `payer` exige `name`, `tax_id`, `email` e `phone_number`.
  - `payment.card` exige `installments` e `card_info`.
  - `card_info` é o token salvo **ou** um `card_hash` do SDK, nunca os dois.
- Resposta 200 em qualquer resultado processado: "Você deve avaliar o conteúdo do objeto para
  verificar o status da autorização". Ou seja, uma recusa também volta 200; o resultado está
  em `status` e em `payment.card.return_code`/`message`.
- O token sai de um pagamento **aprovado** de crédito com `payment.card.save_card = true`.
  Com `save_card = true`, o link só oferece cartão de crédito (Pix, débito e carteiras somem).
  No exemplo de notificação PAID do `webhook-api.yaml`, o token vem em
  `information.payment.card.token`.

Conclusão: o C_06 não tem critério de aceite no roteiro v3.0. **A confirmar com o C6**:
(a) se o C_06 vale para esta homologação; (b) qual status e qual resultado ele espera; (c) onde
colar o resultado, já que o .docx não tem campo para isso. Até lá, o executor pode rodar o
`POST /authorize` e guardar o resultado no JSON de resultados. No .docx não há onde escrever.

## Tabela B: roteiro contra spec

"Obrigatório (spec)" lista o que a spec exige **além** do header `Authorization`. Um
asterisco (\*) em "Status spec" marca divergência com o roteiro.

| ID | URL real | Obrigatório (spec) | Status spec | Resposta (spec) | Divergência com o roteiro |
|---|---|---|---|---|---|
| C_01 | `POST /v1/checkouts/` | body `amount` + `payment` (schema `checkout`, `checkout.yaml:1100-1103`). `amount` é **number em reais**, mínimo **5**, máximo **500000**, ponto decimal (ex.: `123.45`; `:839-844`). Dentro de `payment` (`:1226-1232`) o schema **não** marca nenhum meio como obrigatório (o `anyOf card/pix` só existe no `authorizeResponse`, `:1021-1023`); mandar pelo menos um é inferência. `card` exige `type` (`DEBIT`\|`CREDIT`) e `installments` (1..18; com mais de 1, `interest_type` é obrigatório; `:1024-1029`, `:1185-1189`); `pix` exige `key` = `"AUTO"` (único valor; `:1077-1087`). Header `Content-Type: application/json` (`:121`) | 201 | `{id, url}` | Status igual. O roteiro diz só "amount, payment" e não cita valor mínimo, unidade, estrutura de `payment` nem `Content-Type`. Opcionais úteis: `external_reference_id` (`^[a-zA-Z0-9]{1,10}$`, **não pode repetir** por cliente), `description` (até 300), `redirect_url` (até 300), `payer` |
| C_02 | `GET /v1/checkouts/{id}` | `id` no path (UUID gerado pelo C6) | 200 | schema `checkout` + `payment.card` com `authorization_code`, `return_code`, `message`, `card_info` etc. | Status igual. O roteiro escreve `/checkouts/{id}/` com barra final; a spec não tem |
| C_03 | `PUT /v1/checkouts/{id}/cancel` | `id` no path; sem body | **204**\* | sem corpo | **Confirmada**: roteiro 200, spec 204. Com 204, o "Response Body - Retornado" vai vazio (ver "Preenchimento") |
| C_04 | `POST /v1/webhooks/` | body `url` (só **https**) + `service` ∈ {`BANK_SLIP`, `BANK_SLIP_PIX`, `CHECKOUT`}; `Content-Type: application/json` | **200**\* | `WebhookResponse {service, client_id, url, created_at}` | **Duas, confirmadas**: (1) método PUT no roteiro, **POST** na spec; (2) status 204 no roteiro, **200** na spec. O próprio roteiro se contradiz: o B_07 (webhook de boleto, mesma API) diz "POST ... (Endpoint /webhooks/)" e status 200, igual à spec |
| C_0501 | (entrada) o C6 chama a `url` do C_04 | nossa resposta 2xx | (não documentado) | (nós respondemos) | A spec mostra o corpo da notificação, mas **não** diz o método HTTP da chamada, o status que o C6 espera, retentativas nem assinatura ("enviadas via chamadas HTTP", `webhook-api.yaml:7`). Os pseudo-caminhos da spec também não ajudam: as notificações de boleto estão sob `post:` (`:147-148`) e as de checkout sob `get:` (`:380-381`). Os 200 do roteiro são o status que **o nosso** endpoint devolve |
| C_0502 | idem | idem | idem | idem | Idem. O evento PAID "não é registrado manualmente ... processado automaticamente pelo fluxo", então só sai com pagamento real no link |
| C_06 | `POST /v1/checkouts/authorize` (inferido do título) | `payer{name,tax_id,email,phone_number}`, `payment.card{installments, card_info}`, `external_reference_id`, `amount`, `address` | 200 (inclusive em recusa) | `authorizeResponse` | Sem nada no roteiro para comparar (ver "C_06 lido inteiro") |

### Corpo da notificação de Checkout (spec, `webhook-api.yaml` > `checkout`)

O envelope é `WebhookNotification` com mais 3 campos:

```
{external_id, date_time, client_id, partner_id, service: "CHECKOUT", status, information, expire_at?}
```

`information` é **STRING com JSON**. O conteúdo muda conforme o evento:

- **CREATED**: o exemplo traz, dentro de `information`, o **envelope inteiro serializado de
  novo**, e o `information` interno tem `{id, url}`. Ou é um erro do exemplo ou o formato tem
  dupla serialização. A confirmar no sandbox.
- **EXPIRED / DECLINED / PAID**: `information` traz o checkout, mas o conteúdo dos exemplos
  muda por evento (decodificado de `webhook-api.yaml:482`, `:520`, `:568`):
  - os três: `id`, `status`, `external_reference_id`, `amount` (number), `description`,
    `payment.card{type, installments, fixed_installments, authenticate}`;
  - **EXPIRED** (`:482`): `payer` **vazio** (`{}`), sem `transaction_type`, sem token, mais
    `redirect_url`, `intention_date_time` e `expiration_date_time`;
  - **DECLINED** (`:520`): `transaction_type`, `payer` (nome, CPF/CNPJ, e-mail, endereço),
    `payment.card.return_code` = `"78"`, `message` = `"Blocked Card"` e **também um
    `token`**; sem `fraud_analysis`;
  - **PAID** (`:568`): `transaction_type`, `payer` (nome, CPF/CNPJ, e-mail, endereço),
    `payment.card.token` e `fraud_analysis{analyzed, recommendation}`.
  - Nenhum exemplo traz `payer.phone_number`.
- Eventos AUTHORIZED, ERROR e CANCELLED: "(Em breve)". Por isso **o C_03 não deve gerar
  notificação**, e não há teste do roteiro que espere uma.
- "As notificações são enviadas somente para a mesma chave de integração que gera o objeto":
  o checkout do C_0501/C_0502 tem que ser criado com o **mesmo `client_id`** que registrou o
  webhook no C_04.

### Outras divergências e inconsistências achadas na spec

1. **Unidade de valor**:
   - `amount` é number em **reais** (`123.45`);
   - `captured_amount` é integer em **centavos** (exemplo `"EM BREVE"`);
   - na notificação de boleto, `amount` vem como string (`"7.00"`);
   - na de checkout, como number (`98.45`).

   O executor deve mandar `amount` como number com 2 casas, **≥ 5.00**. Um teste de R$ 1,00,
   como os do Pix, falha na validação.
2. **Enum de `status`** do checkout:
   - `AUTHORIZED, CONFIRMATION PENDING` é **um único valor** no YAML (vírgula dentro do item),
     embora pareça dois;
   - `IN PROGRESS`, `CONFIRMATION REQUESTED` e `CANCELLATION REQUESTED` usam espaço, não `_`;
   - os dois últimos não aparecem na descrição.

   A tabela de eventos do webhook usa outro conjunto: `CREATED`, `EXPIRED`, `AUTHORIZED`,
   `DECLINED`, `ERROR`, `CANCELLED`, `PAID`. Comparar status como string exata e logar
   qualquer valor desconhecido, sem quebrar.
3. **`card_info`**:
   - o schema diz `oneOf: [card_hash, token]`, e os **dois são `type: string`** (qualquer
     string casa com os dois, então o `oneOf` nunca valida);
   - os textos apontam para objeto: "`payment.card.card_info.token`" (createCheckout) e
     "`payment -> card -> card_info -> card_hash`" (SDK);
   - o formato real do C_06 (string pura ou `{"token": "..."}`) fica **a confirmar**;
   - o schema `card` usado na **criação** (`checkout.yaml:1024-1050`) nem tem `card_info`,
     embora o texto da criação fale em `payment.card.card_info.token` (`:119`).
4. **`authorizeRequest.required` cita `address` na raiz**, mas a propriedade `address` só
   existe dentro de `payer`. A confirmar: mandar `payer.address` e, se o C6 recusar, registrar
   o erro real.
5. **`expiration_date_time` é `readOnly`**, mas a descrição diz "Padrão de 7 dias ... caso não
   seja informado". Não dá para saber se aceita o campo na criação. O executor não deve
   mandar.
6. **`capture` responde 204 com corpo** (`content: application/json`), o que contradiz o
   próprio 204. A operação está "EM BREVE" e fora do roteiro.
7. **GET com nome diferente**: o schema da resposta do `getCheckout` usa `capturedAmount`
   (camelCase); o resto da spec usa `captured_amount`.
8. **Headers opcionais** `partner-software-name` e `partner-software-version` existem nas duas
   APIs. O roteiro não pede; vale mandar "BlaXx Pontos" e a versão, para o C6 identificar o
   parceiro (sugestão, não exigência).
9. **`GET`/`DELETE /v1/webhooks/`** têm query `service` com enum só `BANK_SLIP`/`CHECKOUT`
   (sem `BANK_SLIP_PIX`) e marcam `Content-Type` como obrigatório num GET. Fora do roteiro;
   útil para conferir o C_04 (`GET ...?service=CHECKOUT`).
10. **Servidor do webhook** sem barra final (`.../v1/webhooks`) e tabela do portal com barra
    (`/v1/webhooks/`). Usar `/v1/webhooks/`, que é a forma que o B_07 do roteiro também cita.

## Ordem de execução e dependências

Tudo numa janela do sandbox (seg-sex, 7h-22h), **com a mesma credencial** (`client_id`) do
começo ao fim, porque a notificação só vai para o webhook do mesmo `client_id`.

1. **AT_01**: conferir os escopos (seção acima). Parar se faltar `checkout.write`,
   `checkout.read` ou `checkout.cancel`.
2. **Garantir o receptor no ar antes do C_04**: a URL tem que ser **https pública** e
   responder 200. Hoje a rota `/payments/c6/webhook/<token>/checkout` só existe na cópia local
   (não commitada) e **não está em produção**. Ver o achado A3.
3. **C_04**: `POST /v1/webhooks/` com
   `{"url": "https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/<C6_WEBHOOK_TOKEN>/checkout", "service": "CHECKOUT"}`.
   Conferir com `GET /v1/webhooks/?service=CHECKOUT`, fora do roteiro.
   - **Por que com o sufixo `/checkout`** (decisão do dono; o 06 segue a mesma recomendação):
     na cópia de trabalho, a URL base e a `/checkout` tratam o checkout igual (testado com o
     cliente do Flask: 200 `registrado` nas duas). A diferença está no que acontece se o deploy
     **não** tiver ido: com sufixo, o HEAD devolve **404** e a falha aparece; sem sufixo, o
     HEAD devolve **200 `{"ok": true, "results": []}` sem log nenhum**, e o C_0501 "passaria"
     sem evidência. O host `blaxx-pontos-exe.onrender.com` é o do serviço em produção
     (memória do projeto); se o C6 recusar domínio `onrender.com`, a confirmar.
4. **C_01**: criar o checkout. O `id` da resposta alimenta o C_02, o C_03 e a conferência do
   C_0501. Isso gera o evento **CREATED** do **C_0501**: anotar o horário e o `external_id`
   esperado (= `id`; no exemplo da spec, `external_id` e `information.id` são iguais,
   `webhook-api.yaml:458`).
5. **C_02**: `GET` com o `id` do C_01 (status esperado no corpo: `CREATED`).
6. **C_0501**: confirmar que a notificação CREATED chegou ao receptor (status que o nosso
   endpoint devolveu + corpo recebido, mascarado).
7. **C_0502**: criar **um segundo** checkout, porque o do C_01 vai ser cancelado no C_03.
   Recomendado: `payment.card{type: CREDIT, installments: 1, save_card: true}` para já gerar
   o token do C_06. Abrir a `url` e **pagar no sandbox** (bloqueador, ver A4). Esperar a
   notificação **PAID** e confirmar com `GET /v1/checkouts/{id}` (status `PAID`).
8. **C_03**: `PUT /v1/checkouts/{id}/cancel` no checkout do **C_01**, ainda não pago (só
   "aborta", sem estorno). Esperado real: 204. Conferir depois com um GET (`CANCELLED`). Não
   cancelar o checkout pago do passo 7, porque isso dispararia um **estorno**.
9. **C_06** (se o C6 confirmar que vale): `POST /v1/checkouts/authorize` com o token do passo
   7 e um `external_reference_id` **novo** (não pode repetir).

Dependências em resumo:

- `id` do C_01 → C_02, C_03;
- C_04 → C_0501, C_0502;
- C_04 antes do C_01, ou o CREATED não chega;
- pagamento real → C_0502 → token → C_06.

## Localização no .docx

Método igual ao do `01-mapa-roteiro.md` e ao do `scripts/c6_preencher_roteiro.py`:

- **índice da tabela de resultado**: posição 0-based em `body.iter(w:tbl)`;
- **linha R2** da tabela, com duas células: a 1ª é "Status Code - Retornado", a 2ª é
  "Response Body - Retornado";
- **conferência**: R0 = "TESTE: C_xx" e R1 = os dois rótulos.

Também conto a **posição global** de cada campo FORMTEXT, que é a ordem dos `w:ffData` com
`w:textInput` em `body.iter()` (= ordem dos `w:instrText` " FORMTEXT "). O documento tem
**142 FORMTEXT** e **8 FORMCHECKBOX** no total. Os FORMTEXT têm `w:name` **vazio**, então
não dá para achá-los por nome de campo, só pela posição.

| Teste | Tabela de passos | Tabela de resultado | FORMTEXT "Status Code - Retornado" | FORMTEXT "Response Body - Retornado" | Rótulo no .docx |
|---|---|---|---|---|---|
| C_01 | 36 | **37** | **36** | **37** | TESTE: C_01 (correto) |
| C_02 | 38 | **39** | **38** | **39** | TESTE: C_02 |
| C_03 | 40 | **41** | **40** | **41** | TESTE: C_03 |
| C_04 | 42 | **43** | **42** | **43** | TESTE: C_04 |
| C_0501 | 44 | **45** | **44** | **45** | TESTE: C_0501 |
| C_0502 | 46 | **47** | **46** | **47** | TESTE: C_0502 |
| C_06 | (não existe) | (não existe) | (não existe) | (não existe) | só na tabela-sumário 35, R7 |

Nesta seção, o índice global do FORMTEXT coincide com o índice da tabela de resultado. É
coincidência: no Pix não coincide.

Para conferir o método, apliquei o mesmo contador ao que o 01 já tinha mapeado. Bate:

- AT_01: tabela 4, FORMTEXT 6/7;
- P_01_01: tabela 53, FORMTEXT 50/51;
- dali em diante, +2 por teste até o P_06_03: tabela 99, FORMTEXT 96/97.

Os rótulos da seção CHECKOUT estão todos certos. Não há troca como a do P_03_03 e do P_04_04.

Os testes antes do Checkout ocupam os FORMTEXT 0-5 (organização), 6/7 (AT_01), 8-19 (AP_*)
e 20-35 (B_*). Depois vêm E_01 (48/49) e os P_* (50-97).

**Checkbox "APIs que serão testadas"** (tabela 1):

- **CHECKOUT = índice 2** entre os 8 campos `w:checkBox` em ordem de documento. **Confirmado**,
  igual ao que o 01 diz.
- Fica na linha R5 da tabela 1 (`4 | CHECKOUT | [checkbox]`). É a mesma linha que o
  `c6_preencher_roteiro.py` mapeia em `CHECKBOX_LINHAS[5]`.
- XML do campo: `w:name="Check3"`, `w:default w:val="0"`, `w:checked w:val="0"`.

Os três primeiros checkboxes (AGENDAMENTO, BOLETO e CHECKOUT) já trazem `w:checked
w:val="0"` explícito; os outros cinco não têm `w:checked`. Para marcar, trocar o valor para
"1" (ou remover o `w:val`). Não basta acrescentar um `w:checked` novo, porque o elemento já
existe.

Para incluir CHECKOUT, a cópia preenchida deve marcar os índices **2 e 4** (CHECKOUT e PIX).
O roteiro manda "Caso algum item deste roteiro não seja utilizado em sua aplicação, não
preencha o checkbox". Por isso, ao marcar CHECKOUT, espera-se a seção inteira preenchida.

### Preenchimento (convenções)

- Vale **o status que a API devolveu de verdade**, inclusive 204 no C_03 e 200 no C_04, mesmo
  que divirjam do roteiro. Explicar a diferença no corpo, por exemplo
  "204 sem corpo (spec cancelCheckout = 204)".
- C_03 com 204: o "Response Body - Retornado" não tem corpo. Sugestão: `(sem corpo: HTTP 204
  No Content)`.
- C_0501/C_0502: Status = o que **o nosso** endpoint respondeu ao C6 (200). Body = a
  notificação recebida, com `information` decodificado e mascarado. **A confirmar** com o C6
  se é isso que ele quer ver nos campos.
- Mascarar sempre:
  - a URL do webhook (`/webhook/<C6_WEBHOOK_TOKEN>/checkout`), no C_04 e na resposta do GET
    de conferência;
  - `payment.card.token` (C_0502, C_06);
  - os dados do `payer` (nome, CPF/CNPJ, e-mail, endereço, telefone);
  - `client_id`, que vem no `WebhookResponse` do C_04 e no envelope das notificações. O
    filtro do `c6_preencher_roteiro.py` mascara só pelo valor de ambiente `C6_CLIENT_ID`
    (troca de literal), não pela chave `client_id`. Ver A7 para os outros buracos (token do
    cartão, `payer`, `information` em string).

## Achados: o que o executor precisa e ainda não existe

**A1. C_06 sem caso de teste no roteiro v3.0.** Só existe a linha do sumário: sem endpoint,
sem status esperado e sem campos FORMTEXT. É preciso perguntar ao C6 se o C_06 vale, qual o
critério e onde registrar o resultado. Não inventar um campo no .docx.

**A2. Nenhuma chamada de Checkout no código.**

- `C6PixProvider._request` está preso a `PIX_PATH = "/v2/pix"`.
- Não existe método para `/v1/checkouts/` (create/get/cancel/authorize) nem para
  `/v1/webhooks/` (POST/GET/DELETE).
- O executor pode reaproveitar `_bearer()` (token e renovação) e `_http()` (mTLS), que já
  devolve `(status, corpo)` cru. Assim ele captura o 204 sem corpo do C_03 e o 200 do C_04,
  que o `_request` engoliria.
- As chamadas ficam no executor de homologação, **não** no provider de produção, enquanto a
  compra por cartão não existir no produto.

**A3. Receptor do webhook de Checkout não publicado.**

- `app/api/c6_webhook.py` ganhou a rota `/payments/c6/webhook/<token>/checkout` e o
  tratamento do envelope (`_notificacao_c6`, `_decodifica_information`, que desembrulha a
  dupla serialização do exemplo CREATED).
- Isso está **não commitado** (alteração de outro fluxo, última gravação 22/10 18:51) e,
  portanto, fora de produção. O Render deploya do fork `RVELES/blaxx-pontos`.
- Sem o deploy (HEAD `deccb91`): a URL com `/checkout` dá **404**; a URL base dá **200
  `{"ok": true, "results": []}`** e não loga nada (o envelope não tem `pix` nem `txid`).
  Nenhum dos dois deixa evidência.
- Com a cópia de trabalho, ele **só loga um resumo** (`_resumo_checkout`, linhas 269-290:
  sem payer, token, url, description e client_id) e devolve
  `{"ok": true, "service": "CHECKOUT", "status": ..., "external_id": ..., "result": "registrado"}`
  (linhas 318-321). Conferido com o cliente de teste do Flask usando os exemplos CREATED,
  PAID e DECLINED da spec: 200 nas duas URLs, 200 também sem `Content-Type`, 401 com token
  errado, **405 para PUT e GET**, e o log não continha nome, CPF/CNPJ, e-mail, token do
  cartão, URL do link nem `client_id`.
- Já existem testes: `tests/test_c6_webhook_notificacoes.py` (22 passando, não commitado).
- Para colar o "Response Body - Retornado" do C_0501/C_0502, o executor precisa do **corpo
  recebido**. Hoje ele não é guardado em lugar nenhum consultável; o log do Render tem só o
  resumo.
- É preciso decidir onde capturar o corpo, já mascarado: tabela, arquivo efêmero ou log em
  nível DEBUG só durante a janela. Decisão do dono, porque mexe em `app/`.
- **Método HTTP** da notificação (a spec não diz; a rota só aceita POST) e a política de
  retentativa: a confirmar no sandbox.

**A4. Pagamento do link no sandbox (bloqueador externo do C_0502 e do C_06).**

- Nem a spec nem o projeto documentam cartão de teste ou como pagar um link no sandbox.
- O `4111111111111111` que aparece na spec é só exemplo do SDK de checkout transparente,
  **não** um cartão de teste declarado. Não usar como se fosse.
- Perguntar ao C6, junto com o recadastro.

**A5. Token de cartão para o C_06.**

- Ele só existe depois de um pagamento de **crédito aprovado** com `save_card=true`.
- No exemplo PAID do webhook, ele vem em `information.payment.card.token`.
- A spec não deixa claro se o `GET /v1/checkouts/{id}` também devolve o token
  (`card_info` do GET aponta para o schema-string, não para `card_info_response.token`).
- O formato de envio no `/authorize` (string ou `{token}`) é ambíguo (inconsistência 3).

**A6. Dados de teste a preencher (não inventar).**

| Uso | Campo | Situação |
|---|---|---|
| C_01, C_0502 | `amount` ≥ **5.00** (number, reais) | definir na execução (ex.: `5.00`) |
| C_01, C_0502 | `payment`: `card{type, installments}` e/ou `pix{key:"AUTO"}` | definir. Para o C_06: `card{type:"CREDIT", installments:1, save_card:true}` |
| C_01, C_0502, C_06 | `external_reference_id` (1-10 alfanum., **único**) | gerar novo a cada rodada (não reutilizar o de uma rodada anterior) |
| C_06 | `payer{name, tax_id, email, phone_number, address{street, number, city, state, zip_code}}` | a preencher pelo dono (dado de teste aceito pelo sandbox) |
| C_04 | `url` = `https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/<C6_WEBHOOK_TOKEN>/checkout` | token só da env var, nunca escrito em arquivo |

**A7. Script de preenchimento: já conhece o Checkout, mas a máscara não cobre os dados
dele.** (Corrigido na revisão: a primeira versão dizia que o script só conhecia Pix; o outro
fluxo já o alterou.) Hoje o `scripts/c6_preencher_roteiro.py`:

- tem `TESTES_CHECKOUT` com `("C_01", 37, "C_01")` … `("C_0502", 47, "C_0502")`, usado só
  com `--com-checkout` (linhas 161-171, 595);
- marca `{"PIX", "CHECKOUT"}` com `--com-checkout` (linha 597);
- tem `ESPERADO` com `C_03 (200, 204)` e `C_04 (204, 200)` (linhas 176-178);
- não tem linha C_06, o que está certo (não há tabela para ela).

Buracos da classe `Mascara` (linhas 215-279) para os corpos de Checkout, lidos no código:

- a chave **`token`** (token do cartão em `payment.card.token`/`card_info.token`) **não** está
  em `CHAVES_SECRETAS` e não casa com JWT/Bearer/PEM: iria em claro para o .docx;
- **`payer`** (nome, `tax_id`, e-mail, telefone, endereço) não é mascarado;
- o `information` das notificações (C_0501/C_0502) é uma **string JSON**: `Mascara.texto` só
  troca literais e padrões, então o que estiver dentro dela passa sem máscara por chave;
- a `url` do `WebhookResponse` do C_04 só é mascarada se `C6_WEBHOOK_TOKEN` estiver no
  ambiente na hora de preencher (troca por literal); a máscara por chave só olha
  `webhookUrl` (Pix);
- `client_id` idem: só por literal de `C6_CLIENT_ID`. Que o `client_id` do corpo é o mesmo
  valor da credencial é **a confirmar**.

O arquivo é de outro fluxo e este documento não o altera. Até ele ser corrigido, o executor
tem que entregar os corpos de C_02 (pós-pagamento), C_0501, C_0502 e C_06 **já mascarados**
no JSON de resultados. O executor (`scripts/c6_homologacao.py`) também não tem nenhuma
chamada de Checkout ou de `/v1/webhooks/` (`grep` sem resultado).

**A8. Checagem de escopo no AT_01.** Ampliar a lista (L8 do 01) com os 4 `checkout.*`,
tratando `webhook.*` como "a confirmar".

## Perguntas para o C6 junto com o novo cadastro

1. O C_06 ("AUTORIZAR CHECKOUT COM TOKEN") vale para a homologação? O v3.0 não tem passos,
   status esperado nem campo de resultado para ele.
2. No C_04, o correto é `POST /v1/webhooks/` com 200, como na spec e no B_07, ou `PUT` com
   204, como diz o C_04? E no C_03, o cancelamento devolve 204 (spec) ou 200 (roteiro)?
3. O `POST /v1/webhooks/` exige `webhook.write`? A credencial sandbox vem com `checkout.write`,
   `checkout.read`, `checkout.capture`, `checkout.cancel` (e `webhook.*`)?
4. Como pagar um link de checkout no sandbox (cartão de teste ou simulador) para gerar o PAID
   do C_0502 e o token do C_06?
5. A notificação de Checkout chega por POST? O CREATED vem mesmo com o envelope serializado
   dentro de `information` (dupla serialização do exemplo)? Há retentativa ou assinatura?
6. No C_0501/C_0502, o que deve ir em "Status Code/Response Body - Retornado": o status que o
   nosso endpoint devolveu e o corpo recebido?
7. No `/authorize`, o `card_info` é string ou objeto `{"token": ...}`? O `address` vai na raiz
   ou em `payer.address`?

## Decisões que são do dono (não do executor)

1. Aceitar e registrar as divergências roteiro x spec (C_03 200 x 204; C_04 PUT/204 x
   POST/200) com o status real no .docx, ou esperar resposta do C6 antes de rodar.
2. Incluir ou não CHECKOUT nesta homologação (marcar o checkbox índice 2 obriga a seção
   inteira, inclusive o C_0502, que depende de pagamento manual).
3. Pagar o link do C_0502 manualmente no sandbox, e com qual cartão (só com cartão de teste
   informado pelo C6; nenhuma fonte traz um).
4. Commit e deploy da cópia de trabalho de `app/api/c6_webhook.py` antes do C_04, e qual URL
   cadastrar (recomendação: com sufixo `/checkout`, ver "Ordem de execução").
5. O que colar em "Response Body - Retornado" do C_0501/C_0502 (envelope recebido, mascarado?)
   e onde capturar esse corpo, já que o receptor só loga um resumo.
6. Quais dados do `payer`/`address` usar no C_06 e quanto deles mostrar no roteiro.
7. Rodar ou não o C_06, que não tem tabela no roteiro v3.0.
