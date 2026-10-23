# C6 Developers: mapa do roteiro (AT_01 + P_01..P_06) contra o provider

Levantado em 22/10/2026, sem nenhuma chamada à API do C6 e sem credencial.

Fontes:

- **Roteiro**: `~/Downloads/Roteiro de Testes - C6 Developers v3.0.docx`. O original não foi
  alterado; o texto saiu de uma cópia descompactada (`word/document.xml`).
- **Specs oficiais** (baixadas em 22/10 de `developers.c6bank.com.br/yamls/`, desta vez sem
  bloqueio do Cloudflare): `authentication-api.yaml`, `pix-api.yaml` v3.0.1 e
  `webhook-api.yaml` v1.0.10.
- **Código**: `app/pix/c6.py` (C6PixProvider), `app/api/c6_webhook.py`,
  `tests/test_c6_cobranca.py` (47 testes, todos passando em 22/10, offline) e
  `~/Downloads/registrar_webhook_c6.py`.

Escopo: a seção AUTENTICAÇÃO (AT_01) e a seção PIX (P_01 a P_06), **24 testes Pix + 1 de
autenticação = 25**. Ficam de fora AP_* (agendamento), B_*/BP_* (boleto/bolepix), CK_*, E_*, TR_*
e PA_* (Pix Automático). Os dados da organização (formulários iniciais) estão em
`02-dados-organizacao.md`.

## Resumo

| | |
|---|---|
| Testes no escopo | 25 (AT_01 + 24 de Pix) |
| Chamadas que o provider **já faz** | 6: AT_01, P_01_01, P_01_04, P_06_01, P_06_02, P_06_03 |
| Chamadas que **faltam** | 19: POST/PATCH/lista de `cob`, todo `cobv`, todo `lotecobv`, todo `loc`, Pix recebidos e devolução |
| Divergências entre o status pedido no roteiro e o da spec | 4 (P_01_02, P_01_03, P_03_01, P_03_02) |
| Bloqueador externo | P_05_01, P_05_03 e P_05_04 precisam de um Pix **pago** no sandbox (um e2eid) |

Mesmo nas 6 chamadas que já existem, o provider não serve para preencher o roteiro do jeito
que está: ele devolve só o dict do sucesso, levanta exceção sem o status em qualquer não-2xx,
nunca expõe o corpo do AT_01 e não mascara nada. O roteiro pede, em todo teste, o
**Status Code retornado** e o **Response Body retornado**. Ver a lacuna L1.

## Convenção de caminho

O roteiro escreve os endpoints sem o prefixo de versão. A URL real é esta:

| Roteiro | URL real (spec) |
|---|---|
| `/auth` | `{host}/v1/auth/` |
| `/pix/<recurso>` | `{host}/v2/pix/<recurso>` (o `/pix` do roteiro é o prefixo da API) |
| `/pix/pix/{e2eid}` | `{host}/v2/pix/pix/{e2eid}` (o recurso também se chama `pix`) |
| `/pix/webhook/{chave}` | `{host}/v2/pix/webhook/{chave}`. **Não** é a Webhook API `/v1/webhooks/`: o próprio `webhook-api.yaml` diz que webhook de Pix se configura pela API Pix |

`{host}`: sandbox `https://baas-api-sandbox.c6bank.info` (seg-sex, 7h-22h; fora disso 503),
produção `https://baas-api.c6bank.info`. O provider já usa `AUTH_PATH="/v1/auth/"` e
`PIX_PATH="/v2/pix"`, o que bate com a spec.

## Pré-requisitos que o roteiro pede

1. Certificado (.crt + .key) importado na ferramenta (mTLS). No provider: `load_cert_chain`
   com `C6_CERT_PATH`/`C6_KEY_PATH` ou `C6_CERT_PEM`/`C6_KEY_PEM`.
2. Token de acesso no header de toda requisição. No provider: `Authorization: Bearer`, com cache
   e renovação 30 s antes do `expires_in`.
3. Escopos listados no roteiro para Pix: `pix.read pix.write payloadlocation.read
   payloadlocation.write lotecobv.read lotecobv.write cob.read cob.write cobv.read cobv.write`.
   **Faltam `webhook.read` e `webhook.write` nessa lista**, e a spec exige os dois para P_06.
   Ao receber a nova credencial, confirmar que o `scope` do AT_01 traz os **12** escopos.

## Tabela A: o que o roteiro pede (literal)

Header comum a todos os P_*: `Authorization: Bearer {token}`. "Campos" é o que o roteiro manda
enviar, com as palavras dele.

| ID | Título | Método | Endpoint (roteiro) | Campos (roteiro) | Status esperado | Resultado esperado (roteiro) |
|---|---|---|---|---|---|---|
| AT_01 | Geração do token de sessão | POST | `/auth` | Header `Content-Type: application/x-www-form-urlencoded`; body `client_id`, `client_secret` (os recebidos com o .crt/.key) | 200 | Body `{access_token, expires_in: 300, token_type: "Bearer", scope}`. "Autenticação realizada com sucesso e token de acesso retornado no response body." |
| P_01_01 | Criar cobrança imediata com txid | PUT | `/pix/cob/{txid}` | body `calendario`, `devedor`, `valor`, `chave` | 201 | Cobrança Pix criada com sucesso. |
| P_01_02 | Criar cobrança imediata sem txid | POST | `/pix/cob` | body `calendario`, `devedor`, `valor`, `chave` | 200 | Cobrança Pix criada com sucesso. |
| P_01_03 | Revisar cobrança imediata | PATCH | `/pix/cob/{txid}` | campos alteráveis (ex.: valor, devedor, solicitação) | 201 | Cobrança atualizada com sucesso. |
| P_01_04 | Consultar cobrança imediata | GET | `/pix/cob/{txid}` | só o header | 200 | Detalhes da cobrança retornados. |
| P_01_05 | Consultar lista de cobranças imediatas | GET | `/pix/cob?inicio={data}&fim={data}` | query `inicio`, `fim` | 200 | Lista de cobranças retornada com sucesso. |
| P_02_01 | Criar cobrança com vencimento com txid | PUT | `/pix/cobv/{txid}` | body `calendario`, `devedor`, `valor`, `chave` | 201 | Cobrança com vencimento criada com sucesso. |
| P_02_02 | Revisar cobrança com vencimento | PATCH | `/pix/cobv/{txid}` | campos alteráveis (ex.: valor, devedor, solicitação) | 200 | Cobrança atualizada com sucesso. |
| P_02_03 | Consultar cobrança com vencimento | GET | `/pix/cobv/{txid}` | só o header | 200 | Detalhes da cobrança retornados. |
| P_02_04 | Consultar lista de cobranças com vencimento | GET | `/pix/cobv?inicio={data}&fim={data}` | query `inicio`, `fim` | 200 | Lista de cobranças retornada com sucesso. |
| P_03_01 | Criar lote de cobranças com vencimento | PUT | `/pix/lotecobv/{id}` | body `cobsv`, `descricao` | 201 | Lote criado ou atualizado com sucesso. |
| P_03_02 | Revisar cobranças dentro do lote | PATCH | `/pix/lotecobv/{id}` | campos alteráveis (ex.: valor, devedor, solicitação) | 200 | Cobrança atualizada com sucesso. |
| P_03_03 | Consultar lote específico | GET | `/pix/lotecobv/{id}` | só o header | 200 | Detalhes da cobrança retornados. |
| P_03_04 | Consultar lista de lotes | GET | `/pix/lotecobv?inicio={data}&fim={data}` | query `inicio`, `fim` | 200 | Lista de cobranças retornada com sucesso. |
| P_04_01 | Criar location do payload | POST | `/pix/loc` | body `tipoCob` | 201 | Location criada com sucesso. |
| P_04_02 | Consultar locations cadastradas | GET | `/pix/loc` | só o header | 200 | Dados do location retornado com sucesso. |
| P_04_03 | Recuperar location específica | GET | `/pix/loc/{id}` | só o header | 200 | Dados do location retornado com sucesso. |
| P_04_04 * | Desvincular cobrança de uma location | DELETE | `/pix/loc/{id}/txid` | só o header | 200 | Desvinculação realizada com sucesso. |
| P_05_01 | Consultar Pix recebido específico | GET | `/pix/pix/{e2eid}` | só o header | 200 | Dados do PIX retornado com sucesso. |
| P_05_02 | Consultar lista de Pix recebidos | GET | `/pix/pix?inicio={data}&fim={data}` | query `inicio`, `fim` | 200 | Dados do PIX retornado com sucesso. |
| P_05_03 | Solicitar devolução | PUT | `/pix/pix/{e2eid}/devolucao/{id}` | só o header (o roteiro não lista body) | 201 | Devolução solicitada com sucesso. |
| P_05_04 | Consultar devolução | GET | `/pix/pix/{e2eid}/devolucao/{id}` | só o header | 200 | Dados da devolução de PIX retornado com sucesso. |
| P_06_01 | Cadastrar webhook Pix | PUT | `/pix/webhook/{chave}` | só o header (o roteiro não lista body) | 200 | Webhook cadastrado com sucesso. |
| P_06_02 | Consultar webhook | GET | `/pix/webhook/{chave}` | só o header | 200 | Dados do Webhook cadastrado retornado com sucesso. |
| P_06_03 | Excluir webhook | DELETE | `/pix/webhook/{chave}` | só o header | 204 | Webhook removido com sucesso. |

\* No .docx, o quarto teste de P_04 está rotulado **"P_02_04"** nas duas tabelas dele (erro de
cópia do C6). Neste mapa ele é chamado de P_04_04.

## Tabela B: roteiro contra spec contra provider

"Obrigatório (spec)" lista o que a spec exige **além** do header `Authorization`. Um
asterisco (\*) em "Status spec" marca divergência com o roteiro.

| ID | URL real | Obrigatório (spec) | Escopo | Status spec | Provider hoje | O que falta para a homologação |
|---|---|---|---|---|---|---|
| AT_01 | `POST /v1/auth/` | form `client_id`, `client_secret`, **`grant_type=client_credentials`** (o roteiro omite; a spec exige) | n/a | 200 | **Sim**: `_authenticate()` manda os 3 campos e lê `access_token`, `expires_in`, `scope` | Expor status + corpo cru, com o `access_token` mascarado; checar os 12 escopos (hoje só avisa sem `cob.write`) |
| P_01_01 | `PUT /v2/pix/cob/{txid}` | `calendario` (expiracao), `valor.original` (string `\d{1,10}\.\d{2}`), `chave` (até 77). `devedor` é opcional na spec: `{cpf(11)\|cnpj(14), nome}` | cob.write | 201 | **Sim**: `create_charge()` manda `calendario.expiracao`, `valor{original, modalidadeAlteracao:0}`, `chave`, `solicitacaoPagador` e `devedor` **só se** vier nome + CPF/CNPJ válido | Passar devedor sempre (o roteiro pede); capturar status/corpo cru; usar txid novo a cada rodada (o fallback de duplicata mascara o 201) |
| P_01_02 | `POST /v2/pix/cob` | igual a P_01_01 | cob.write | **201**\* | **Não** | Método novo. O txid vem na resposta (gerado pelo PSP) |
| P_01_03 | `PATCH /v2/pix/cob/{txid}` | body `CobRevisada`, qualquer subconjunto: `valor`, `devedor`, `solicitacaoPagador`, `calendario.expiracao`, `loc`, `status=REMOVIDA_PELO_USUARIO_RECEBEDOR` | cob.write | **200**\* | **Não** | Método novo. Rodar numa cob ATIVA (a do P_01_01) |
| P_01_04 | `GET /v2/pix/cob/{txid}` | nenhum | cob.read | 200 | **Sim**: `get_cob()` (valida txid `[a-zA-Z0-9]{26,35}`) | Só capturar status/corpo cru |
| P_01_05 | `GET /v2/pix/cob?inicio=&fim=` | `inicio`, `fim` (date-time RFC 3339). Opcionais: cpf, cnpj, locationPresente, status, paginacao.* | cob.read | 200 | **Não** | Método novo (também útil para conciliação) |
| P_02_01 | `PUT /v2/pix/cobv/{txid}` | `calendario.dataDeVencimento` (YYYY-MM-DD), `devedor{nome, cpf, logradouro, cidade, uf, cep}` (todos obrigatórios na spec), `valor.original`, `chave` | cobv.write | 201 | **Não** | Método novo + **dados de devedor com endereço** (ver "Dados a preencher") |
| P_02_02 | `PATCH /v2/pix/cobv/{txid}` | body `CobVRevisada` (subconjunto) | cobv.write | 200 | **Não** | Método novo |
| P_02_03 | `GET /v2/pix/cobv/{txid}` | nenhum (opcional `revisao`) | cobv.read | 200 | **Não** | Método novo |
| P_02_04 | `GET /v2/pix/cobv?inicio=&fim=` | `inicio`, `fim` | cobv.read | 200 | **Não** | Método novo |
| P_03_01 | `PUT /v2/pix/lotecobv/{id}` | `descricao`, `cobsv[]` com `{txid, calendario.dataDeVencimento, devedor, valor.original, chave}` em cada item | lotecobv.write | **202**\* (assíncrono) | **Não** | Método novo; consultar depois porque a resposta é 202 |
| P_03_02 | `PATCH /v2/pix/lotecobv/{id}` | `cobsv[]{txid, campos revisados}`; não pode incluir nem remover cobranças do lote original | lotecobv.write | **202**\* | **Não** | Método novo |
| P_03_03 | `GET /v2/pix/lotecobv/{id}` | nenhum | lotecobv.read | 200 | **Não** | Método novo |
| P_03_04 | `GET /v2/pix/lotecobv?inicio=&fim=` | `inicio`, `fim` | lotecobv.read | 200 | **Não** | Método novo |
| P_04_01 | `POST /v2/pix/loc` | `tipoCob` ∈ {`cob`, `cobv`} | payloadlocation.write | 201 (+ header `location`) | **Não** | Método novo |
| P_04_02 | `GET /v2/pix/loc?inicio=&fim=` | **`inicio`, `fim` obrigatórios** (o roteiro não cita query) | payloadlocation.read | 200 | **Não** | Método novo; mandar inicio/fim mesmo sem o roteiro pedir |
| P_04_03 | `GET /v2/pix/loc/{id}` | nenhum (id int64) | payloadlocation.read | 200 | **Não** | Método novo |
| P_04_04 | `DELETE /v2/pix/loc/{id}/txid` | nenhum; a loc precisa estar **vinculada** a uma cob | payloadlocation.write | 200 | **Não** | Método novo + criar uma cob com `loc.id` antes (o `create_charge` não aceita `loc`) |
| P_05_01 | `GET /v2/pix/pix/{e2eid}` | nenhum (e2eid `[a-zA-Z0-9]{32}`) | pix.read | 200 | **Não** | Método novo + **um Pix pago no sandbox** |
| P_05_02 | `GET /v2/pix/pix?inicio=&fim=` | `inicio`, `fim`. Opcionais: txid, txIdPresente, devolucaoPresente, cpf, cnpj, paginacao.* | pix.read | 200 | **Não** | Método novo (dá 200 mesmo com a lista vazia) |
| P_05_03 | `PUT /v2/pix/pix/{e2eid}/devolucao/{id}` | **body `valor`** (o roteiro não cita); opcionais `natureza` (ORIGINAL\|RETIRADA), `descricao` (até 140). `id` é nosso; o `$ref DevolucaoId` está quebrado na spec, e no padrão Bacen ele é `[a-zA-Z0-9]{1,35}` | pix.write | 201 | **Não** | Método novo + Pix pago |
| P_05_04 | `GET /v2/pix/pix/{e2eid}/devolucao/{id}` | nenhum | pix.read | 200 | **Não** | Método novo |
| P_06_01 | `PUT /v2/pix/webhook/{chave}` | **body `webhookUrl`** (uri; o roteiro não cita) | webhook.write | 200 (sem corpo) | **Sim**: `register_webhook(url)` manda `{"webhookUrl": url}` | Capturar status cru; mascarar o token da URL |
| P_06_02 | `GET /v2/pix/webhook/{chave}` | nenhum | webhook.read | 200 | **Sim**: `get_webhook()` | O corpo traz `webhookUrl` **com o `C6_WEBHOOK_TOKEN` no caminho**: mascarar antes de registrar ou colar |
| P_06_03 | `DELETE /v2/pix/webhook/{chave}` | nenhum | webhook.write | 204 | **Sim**: `delete_webhook()` (corpo vazio vira `{}`) | Capturar status cru; **registrar de novo depois** (P_06_01) se o webhook precisar continuar ativo |

## Divergências e erros do próprio roteiro

Na hora de preencher, vale o **status que a API devolver de verdade**. Não ajustar o número
para bater com o roteiro.

1. **Status diferentes entre roteiro e spec**:
   - P_01_02 (`POST /cob`): o roteiro diz 200, a spec diz 201.
   - P_01_03 (`PATCH /cob`): o roteiro diz 201, a spec diz 200.
   - P_03_01 (`PUT /lotecobv`): o roteiro diz 201, a spec diz 202.
   - P_03_02 (`PATCH /lotecobv`): o roteiro diz 200, a spec diz 202.
2. **Campos que a spec exige e o roteiro não cita**:
   - `grant_type` em AT_01.
   - `webhookUrl` em P_06_01.
   - `valor` em P_05_03.
   - `inicio`/`fim` em P_04_02.
   - Endereço completo do devedor em P_02_01 e em cada item do lote P_03_01.
3. **Escopos**: a lista da seção PIX não traz `webhook.read`/`webhook.write`.
4. **Rótulos trocados no .docx**. Por causa deles, quem for preencher precisa localizar as
   tabelas **pela posição**, nunca pelo rótulo:
   - A tabela de resultado de P_03_03 está rotulada "TESTE: P_02_03".
   - As duas tabelas do 4º teste de P_04 estão rotuladas "TESTE: P_02_04", o que dá **duas
     tabelas "P_02_04"** no documento.
   - Os títulos de P_06_02 e P_06_03 dizem "P_06 – PIX RECEBIDOS" (deveria ser "WEBHOOKS PIX").
   - A tabela-sumário da seção PIX tem o cabeçalho "TESTES DA API DE EXTRATO".
5. **Instrução do roteiro**: "informe no campo 'descrição' o response body / retorno da API
   com as mensagens retornadas pelo processo", e "Caso algum item deste roteiro não seja
   utilizado em sua aplicação, não preencha o checkbox". O checkbox é **por API**: ao marcar
   PIX, espera-se P_01 a P_06 completos.

### Onde fica cada campo de resultado no .docx

Cada teste tem uma tabela "Status Code - Retornado | Response Body - Retornado". A linha R2,
com duas células vazias, é a que se preenche. O índice abaixo é 0-based, na ordem de
`w:tbl` em `word/document.xml` (`body.iter(w:tbl)`).

| Teste | Tabela | Teste | Tabela | Teste | Tabela |
|---|---|---|---|---|---|
| AT_01 | 4 | P_02_04 | 69 | P_04_04 (rótulo P_02_04) | 85 |
| P_01_01 | 53 | P_03_01 | 71 | P_05_01 | 87 |
| P_01_02 | 55 | P_03_02 | 73 | P_05_02 | 89 |
| P_01_03 | 57 | P_03_03 (rótulo P_02_03) | 75 | P_05_03 | 91 |
| P_01_04 | 59 | P_03_04 | 77 | P_05_04 | 93 |
| P_01_05 | 61 | P_04_01 | 79 | P_06_01 | 95 |
| P_02_01 | 63 | P_04_02 | 81 | P_06_02 | 97 |
| P_02_02 | 65 | P_04_03 | 83 | P_06_03 | 99 |
| P_02_03 | 67 | | | | |

Checkbox "APIs que serão testadas": são 8 campos legados `w:checkBox`, na ordem AGENDAMENTO(0),
BOLETO(1), CHECKOUT(2), EXTRATO(3), **PIX(4)**, TRANSAÇÕES(5), PIX AUTOMÁTICO(6), BOLEPIX(7).
Hoje todos estão desmarcados. AUTENTICAÇÃO não tem checkbox (o teste é obrigatório). Na
cópia, marcar só o índice 4.

## Ordem de execução e dependências

Tudo deve rodar dentro de **uma** janela do sandbox (seg-sex, 7h-22h). O token dura 300 s e o
provider renova sozinho.

1. **AT_01**: confere os 12 escopos no `scope` e aborta se faltar algum.
2. **P_06_01 → P_06_02**: registra o webhook antes de qualquer pagamento, para a notificação
   chegar.
3. **P_01_01** (txid novo) → **P_01_02** (o txid vem do C6) → **P_01_03** (PATCH na cob do
   P_01_01, por exemplo `solicitacaoPagador`) → **P_01_04** → **P_01_05** (inicio/fim cobrindo
   a hora do teste).
4. **P_02_01 → P_02_02 → P_02_03 → P_02_04** (vencimento futuro).
5. **P_03_01** (202, assíncrono) → esperar o processamento → **P_03_02** (só txids do lote
   original) → **P_03_03 → P_03_04**.
6. **P_04_01** (`tipoCob=cob`, guarda o `id`) → criar uma cob com `loc: {id}` → **P_04_02**
   (com inicio/fim) → **P_04_03** (mostra o txid vinculado) → **P_04_04**.
7. **Pagar uma cobrança no sandbox** para obter um `endToEndId`. Este é o **bloqueador**: nem a
   spec nem o projeto documentam como simular pagamento no sandbox. Depois disso:
   **P_05_01 → P_05_02 → P_05_03** (devolução parcial, por exemplo `valor` menor que o pago,
   com `id` nosso) **→ P_05_04**.
8. **P_06_03** por último. Se o webhook tiver que continuar ativo, rodar P_06_01 de novo (fora
   do roteiro).

## Lacunas para fechar antes de as credenciais chegarem

**L1. Executor de homologação (não existe).** O roteiro pede status e corpo crus, e o
`_request` não dá isso:

- engole o status em caso de sucesso;
- em erro levanta `C6Error("HTTP n: ...")` com o corpo resumido em 300 caracteres;
- descarta headers (o `location` do P_04_01).

Recomendação: um script à parte que reutiliza o `C6PixProvider` só para mTLS e token
(`_bearer()` + `_http()`) e grava, por teste: método, URL, corpo enviado, status, corpo
recebido e horário, tudo já mascarado. Ele também deve:

- checar a janela do sandbox (reaproveitar `dentro_da_janela` do `registrar_webhook_c6.py`);
- ler credenciais só de variável de ambiente ou de caminho informado na hora.

As chamadas que o produto não usa (cobv, lotecobv, loc, POST/PATCH cob) ficam no executor,
**não** no provider de produção.

**L2. Métodos ausentes no provider: 19 chamadas.**

- `POST /cob`, `PATCH /cob/{txid}`, `GET /cob?inicio&fim`
- `PUT|PATCH|GET /cobv/{txid}`, `GET /cobv`
- `PUT|PATCH|GET /lotecobv/{id}`, `GET /lotecobv`
- `POST /loc`, `GET /loc`, `GET /loc/{id}`, `DELETE /loc/{id}/txid`
- `GET /pix/{e2eid}`, `GET /pix`, `PUT|GET /pix/{e2eid}/devolucao/{id}`

Destas, valem a pena **no produto** (não só na homologação):

- `GET /pix/{e2eid}`: conciliar Pix sem txid e o webhook no formato envelope (L5);
- `GET /pix` e `GET /cob` em lista: conciliação diária;
- devolução: estorno operacional, que hoje é manual.

**L3. `create_charge` não cobre P_01_01 nem P_04_04 como o roteiro pede.**

- O `devedor` só sai com nome + CPF/CNPJ válidos, e o roteiro exige o campo.
- Não aceita `loc`, que o P_04_04 precisa.
- Em qualquer `C6Error` ele faz GET e "reaproveita" a cob: num txid repetido o roteiro
  receberia o 200 do GET no lugar do 201 do PUT. O executor tem que gerar txid novo e chamar
  o PUT direto.

**L4. Validação de identificador antes de virar path.** É a mesma proteção que o
`_validate_txid` já dá (um id externo não pode virar `../outro-endpoint` assinado com o nosso
certificado), estendida aos ids novos:

| Identificador | Regra |
|---|---|
| e2eid | `[a-zA-Z0-9]{32}` |
| id de devolução | `[a-zA-Z0-9]{1,35}` |
| id de loc | inteiro |
| id de lote | string alfanumérica (a definir) |

**L5. Formato do corpo do webhook (risco real, fora do roteiro).** As duas specs documentam
corpos diferentes:

- `pix-api.yaml` (padrão Bacen): `{"pix": [{endToEndId, txid, valor, horario, ...}]}`. É o
  único formato que o `c6_webhook.py` entende.
- `webhook-api.yaml` (`WebhookNotificationPix`): um envelope
  `{date_time, status: "PAID", service: "PIX", key, tax_id, information}`, em que
  **`information` é uma STRING com JSON** `{"pix":[...]}`. No exemplo, os itens nem trazem
  `txid`.

Se o C6 mandar o envelope, o handler (linhas 62-65) não acha `pix` nem `txid`, devolve
**200 `{"ok": true, "results": []}` sem log nenhum** e a compra não é creditada pela
notificação, só pelo polling. Correção sugerida:

- decodificar `information` quando vier string;
- logar WARNING para todo corpo não reconhecido;
- quando faltar o txid, buscar com `GET /pix/{e2eid}`;
- cobrir os dois formatos em teste.

O formato real só se confirma no sandbox, olhando o corpo recebido depois do pagamento do
passo 7.

**L6. Máscara nos registros e no roteiro.**

- O corpo do AT_01 traz o `access_token`. Colar como `"<mascarado, N caracteres>"`.
- Os corpos de P_06_01 e P_06_02 trazem `webhookUrl` com o `C6_WEBHOOK_TOKEN`. Trocar por
  `<TOKEN:Nch>`, como o `registrar_webhook_c6.py` já faz.
- A chave Pix aparece em quase todo corpo (`chave`, `pixCopiaECola`). Mascarar nos logs locais
  sempre. **Decisão do dono:** mostrar ou não a chave no roteiro enviado ao C6 (ela é do
  próprio C6 e vai em todo QR, mas a regra do projeto manda mascarar).
- `client_secret`, cert e key nunca aparecem em resposta, e o executor nunca pode imprimi-los.

**L7. Pix pago no sandbox (bloqueador externo).** P_05_01, P_05_03 e P_05_04 exigem um
`e2eid` real. P_05_02 dá 200 até com a lista vazia. Perguntar ao C6, junto com o novo
cadastro, como simular o pagamento de uma cob no sandbox. Sem isso, esses três testes voltam
404 e o roteiro vai com esse retorno real, explicado.

**L8. Checagem de escopo.** O `_authenticate` só avisa quando falta `cob.write`. Para a
homologação, comparar o `scope` com os 12 escopos (os 10 do roteiro mais `webhook.read` e
`webhook.write`) e parar antes de gastar a janela.

## Dados a preencher (não inventar)

| Uso | Campo | Situação |
|---|---|---|
| P_01_01, P_01_02 | devedor `nome` + `cpf` (ou `cnpj`) | a preencher pelo dono (dado de teste aceito pelo sandbox) |
| P_02_01, P_03_01 | devedor `nome`, `cpf`, `logradouro`, `cidade`, `uf`, `cep` | a preencher |
| todos os P_* | `chave` = chave Pix da **nova** credencial sandbox | chega com as credenciais (`C6_PIX_KEY`) |
| P_01_*, P_02_*, P_03_* | valores (ex.: `"1.00"`) e vencimento futuro | definir na execução |
| P_06_01 | `webhookUrl` = `https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/<C6_WEBHOOK_TOKEN>` | o token vem da env var, nunca escrito em arquivo |
| Formulários iniciais | CNPJ, razão social etc. | ver `02-dados-organizacao.md` |

## Perguntas para o C6 junto com o novo cadastro

1. A credencial sandbox vai com os 12 escopos, incluindo `webhook.read` e `webhook.write`?
2. Como se simula o pagamento de uma cobrança no sandbox (para obter um e2eid para P_05)?
3. O webhook de Pix chega no formato Bacen (`{"pix":[...]}`) ou no envelope da Webhook API
   (`information` como string)? E ele traz `txid`?
4. Os status corretos são os da spec (201/200/202/202) ou os do roteiro (200/201/201/200) nos
   testes P_01_02, P_01_03, P_03_01 e P_03_02?
