# C6 Developers: executor da homologação (passo a passo do dia)

Escrito em 22/10/2026, antes de existir credencial nova. Nenhuma chamada ao C6 foi feita para
escrever isto: tudo foi ensaiado contra o simulador local.

O executor (`scripts/c6_homologacao.py`) roda o **AT_01 e os 24 testes Pix (P_01_01 a P_06_03)
na ordem do roteiro**, encadeia txid, id do lote, id da location, e2eid e id da devolução, e
grava os retornos reais, já mascarados, em `docs/c6-homologacao/resultados/`. É a lacuna L1 de
`01-mapa-roteiro.md`. O provider de produção (`app/pix/c6.py`) não foi alterado: o executor só
reaproveita dele o host, os caminhos e o tratamento de PEM/mTLS.

| Arquivo | Para quê |
|---|---|
| `scripts/c6_homologacao.py` | o executor |
| `scripts/c6_simulador.py` | C6 de mentira em 127.0.0.1, no formato da spec, para ensaiar sem credencial |
| `tests/test_c6_homologacao.py` | máscara, ordem, recusa de produção, horário e ponta a ponta no simulador |
| `docs/c6-homologacao/resultados/` | uma dupla `.json` + `.md` por execução (pasta ignorada pelo git) |

## 1. Antes do dia (sem credencial)

Na pasta do backend:

```zsh
cd "/Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx Pontos/blaxx_exe/backend"
python3 -m pytest tests/test_c6_homologacao.py -q     # 58 testes, sem rede
python3 scripts/c6_homologacao.py --simulador          # ensaio completo, 25/25, código 0
```

O ensaio grava `resultados/<data-hora>-simulador.json` e `.md`, com o aviso "SIMULADOR LOCAL" em
cima. **Esses arquivos nunca vão para o roteiro.** O preenchedor também os reconhece (host
127.0.0.1, `"ambiente": "simulador"`) e os trata como EXEMPLO.

## 2. O que precisa estar em mãos

| O quê | De onde vem |
|---|---|
| `client_id` e `client_secret` novos | e-mail do C6 após o novo cadastro em developers.c6bank.com.br |
| certificado `.crt` e chave `.key` | mesmo e-mail. Guarde **fora do Dropbox e de qualquer repositório**, com `chmod 600` |
| chave Pix da conta sandbox | mesmo e-mail / portal |
| token do webhook (`C6_WEBHOOK_TOKEN`) | painel do Render, serviço `blaxx-pontos-exe`. É o que monta a URL do P_06_01 |
| devedor de teste: nome, CPF (ou CNPJ), logradouro, cidade, UF, CEP | **a preencher pelo dono**, com dado de teste que o sandbox aceite. O projeto não tem esse dado e o executor não inventa: sem ele, recusa rodar |
| (opcional) e2eid de um Pix pago no sandbox | só se o C6 explicar como pagar uma cobrança no sandbox (bloqueador L7) |

Janela do sandbox: **segunda a sexta, 7h às 22h (Brasília)**. Fora dela o C6 responde 503 e o
executor nem tenta: sai com código 2. Ele também recusa começar a menos de 5 minutos das 22h.

## 3. Exportar as variáveis

Só no terminal, na hora. Nada disto vai para arquivo, chat ou commit. Os segredos entram com
`read -s`, que não ecoa nem grava no histórico.

```zsh
cd "/Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx Pontos/blaxx_exe/backend"

export C6_ENV=sandbox                                   # é o padrão; produção exige outra coisa (seção 7)
read -rs "C6_CLIENT_ID?client_id: "         && export C6_CLIENT_ID;         echo
read -rs "C6_CLIENT_SECRET?client_secret: " && export C6_CLIENT_SECRET;     echo
export C6_CERT_PATH="$HOME/.c6-sandbox/sandbox.crt"     # ajuste para onde salvou
export C6_KEY_PATH="$HOME/.c6-sandbox/sandbox.key"
read -rs "C6_PIX_KEY?chave Pix do sandbox: " && export C6_PIX_KEY;          echo
read -rs "C6_WEBHOOK_TOKEN?token do webhook (Render): " && export C6_WEBHOOK_TOKEN; echo
export C6_WEBHOOK_URL="https://blaxx-pontos-exe.onrender.com/payments/c6/webhook/$C6_WEBHOOK_TOKEN"

# devedor de teste (preencha você; não há valor no projeto)
export C6_DEVEDOR_NOME="..."
export C6_DEVEDOR_CPF="..."           # ou C6_DEVEDOR_CNPJ; pontuação é aceita
export C6_DEVEDOR_LOGRADOURO="..."
export C6_DEVEDOR_CIDADE="..."
export C6_DEVEDOR_UF="SP"
export C6_DEVEDOR_CEP="..."           # 8 dígitos; hífen é aceito

# opcional: e2eid de um Pix pago no sandbox (P_05_01, P_05_03, P_05_04)
# export C6_E2EID="E..."
```

No bash, troque `read -rs "VAR?prompt: "` por `read -rsp "prompt: " VAR`.

Alternativa ao par de arquivos: `C6_CERT_PEM` e `C6_KEY_PEM` com o conteúdo PEM (é o formato do
painel do Render). Se as duas formas estiverem definidas, os arquivos (`*_PATH`) vencem.

| Variável | Obrigatória | Observação |
|---|---|---|
| `C6_CLIENT_ID` | sim | sai nos registros só com os 4 últimos caracteres |
| `C6_CLIENT_SECRET` | sim | nunca sai; no AT_01 aparece como `<mascarado, N caracteres>` |
| `C6_CERT_PATH`, `C6_KEY_PATH` | sim | o executor confere que a chave é do certificado e a validade antes de qualquer chamada |
| `C6_PIX_KEY` | sim | sai como `<chave-pix:…XXXX>` (ver seção 6 sobre mostrar ou não) |
| `C6_WEBHOOK_URL` | sim | o P_06_01 cadastra esta URL; o token do caminho sai como `<TOKEN:Nch>` |
| `C6_DEVEDOR_*` | sim | CPF/CNPJ com dígito verificador conferido; nos registros, só os 2 últimos dígitos |
| `C6_ENV` | não | `sandbox` (padrão) ou `producao` |
| `C6_E2EID` | não | equivale a `--e2eid` |
| `C6_WEBHOOK_TOKEN` | não | usado só para mascarar |

## 4. Conferir sem chamar o C6

```zsh
python3 scripts/c6_homologacao.py --checar
```

Mostra a janela, a lista `VARIAVEL=ok|FALTA` (nunca o valor) e o resultado do par certificado e
chave, por exemplo `titular=CN=... · válido até 31/12/2027`. Se o PEM estiver achatado, com aspas
ou com `\n` literal, ele conserta sozinho. Se estiver trocado, cortado ou com senha, diz qual é o
problema. Termina com "Nenhuma chamada ao C6 foi feita". Código 0 = pode rodar.

## 5. Rodar

```zsh
python3 scripts/c6_homologacao.py
```

Leva menos de 2 minutos. O que mais demora é o lote, que espera até 10 consultas de 3 s. Cada
linha da tela é um teste:

```
[P_01_01] PUT    /v2/pix/cob/blxhml...  -> 201 (esperado 201)  ok  x-request-id=...
[P_01_02] POST   /v2/pix/cob  -> 201 (esperado 200/201)  ok  x-request-id=...
```

O que a execução cria **no sandbox**:

- cobranças imediatas de R$ 0,10 (revisada para R$ 0,11) e R$ 0,12;
- uma cobrança com vencimento em +7 dias, de R$ 0,20 (revisada para R$ 0,21);
- um lote com 2 cobranças, de R$ 0,30 e R$ 0,31 (a 1ª revisada para R$ 0,32);
- uma location e uma cobrança de R$ 0,40 vinculada a ela, que o P_04_04 desvincula;
- se houver Pix pago, uma devolução parcial de R$ 0,01;
- o webhook: cadastrado no P_06_01 e **excluído no P_06_03**, como o roteiro manda. Para deixá-lo
  cadastrado no fim, rode com `--manter-webhook`.

Todo txid é novo a cada execução, então rodar de novo é seguro: nada é reaproveitado, e o 201 do
P_01_01 nunca vira o 200 de uma consulta (lacuna L3).

Chamadas que o roteiro não lista mas que os testes exigem vão como **auxiliares**, registradas à
parte e fora do roteiro: consulta do lote enquanto ele processa, criação da cobrança vinculada à
location, busca do e2eid na lista de Pix e renovação do token.

## 6. Conferir o resultado

A tela termina com o resumo e os dois caminhos:

```
Resultado: 25 ok, 0 divergentes, 0 pulados, 0 erros de rede, de 25.
JSON:   .../docs/c6-homologacao/resultados/2026-10-23_101502.json
Resumo: .../docs/c6-homologacao/resultados/2026-10-23_101502.md
Código de saída: 0
```

| Código | Significa | O que fazer |
|---|---|---|
| 0 | os 25 status conferiram | seguir para o preenchimento (seção 8) |
| 1 | algum teste divergente, pulado ou sem resposta | ver "Pendências" no `.md` |
| 2 | fora da janela do sandbox | rodar de seg a sex, entre 7h e 22h |
| 3 | configuração incompleta ou execução recusada | a tela diz o que falta |
| 4 | AT_01 falhou, ou o token veio sem algum dos 12 escopos | nenhum teste Pix rodou; ver o corpo do AT_01 |

**No `.md`**, cada teste tem um bloco "Status Code - Retornado" + "Response Body - Retornado"
(JSON indentado), com horário, x-request-id e o nº da tabela no .docx. Use o nº, não o rótulo: o
.docx do C6 chama a tabela do P_03_03 de "P_02_03" e a do P_04_04 de "P_02_04".

**No `.json`**, a parte de cima segue o contrato de `formato-resultados.md`
(`executado_em`, `ambiente`, `host`, `executor`, `resultados: [{id, status, corpo, nota, erro}]`),
que é o que o preenchedor lê. Embaixo, em `testes` e `auxiliares`, fica o detalhe de cada chamada:
URL, headers enviados (com o Bearer mascarado), corpo enviado, duração e observações internas.

Antes de gravar, o executor procura no texto final qualquer segredo conhecido, Bearer ou bloco PEM.
Se achar algum, **recusa gravar**.

### Casos esperados

- **Status da spec × do roteiro.** Em P_01_02 (spec 201, roteiro 200), P_01_03 (200 × 201),
  P_03_01 (202 × 201) e P_03_02 (202 × 200), o executor aceita os dois e registra na `nota` do
  teste que vale a especificação. Com `--estrito`, só o número do roteiro conta. O valor gravado é
  sempre o que a API devolveu: nunca ajuste o número.
- **P_05_01, P_05_03 e P_05_04 pulados** ("nenhum Pix pago no período"): é o bloqueador L7. Sem um
  Pix pago no sandbox não há e2eid. Se o C6 explicar como pagar, pague uma cobrança e rode de novo
  com `C6_E2EID=...`. O P_05_02 dá 200 mesmo com a lista vazia.
- **Código 4 por escopo**: o token veio sem `webhook.read`/`webhook.write` (ou outro dos 12). Peça
  ao C6 antes de gastar a janela. `--ignorar-escopos` roda assim mesmo e grava os 403.
- **503 no meio**: o sandbox saiu do ar ou a janela fechou. Rode tudo de novo.
- **Lote "ainda em processamento"**: o P_03_02 e o P_03_03 rodam assim mesmo, e a observação
  registra o fato. Para esperar mais, use `--tentativas-lote 20` ou `--intervalo-lote 5`.
- **Erro de TLS ou 401 no AT_01**: certificado de outra credencial, ou client_id/secret trocados.
  Rode `--checar` de novo.

### Chave Pix: mostrar ou não (decisão do dono, L6)

Por padrão a chave sai mascarada em tudo. Para ela aparecer no roteiro enviado ao C6, são
necessárias **as duas coisas**: rodar o executor com `--expor-chave-pix` e o preenchedor com
`--mostrar-chave-pix`. Com só uma delas, a chave continua mascarada.

## 7. Produção

A homologação é no sandbox. O executor só aponta para `https://baas-api.c6bank.info` com
`C6_ENV=producao` **e** `--producao` juntos. Com só um dos dois, recusa (código 3). Em produção as
cobranças e a devolução são de verdade.

## 8. Depois: roteiro e e-mail

1. **Roteiro**: `scripts/c6_preencher_roteiro.py`, feito em paralelo, lê o `.json` deste executor
   direto. O carregador dele foi testado com um resultado do simulador: aceitou os 25 ids e marcou
   o arquivo como EXEMPLO. Uso, conforme o cabeçalho do script:
   ```zsh
   python3 scripts/c6_preencher_roteiro.py \
       --resultados "docs/c6-homologacao/resultados/<data-hora>.json" \
       --cnpj "<do cartão CNPJ>" --razao-social "<do cartão CNPJ>" --pdf
   ```
   O original em `~/Downloads` nunca é alterado: o preenchedor trabalha numa cópia.
2. **E-mail** para homologacaoapi@c6bank.com: o executor não envia nada. O rascunho está em
   `04-email-homologacao.md`.
3. **Limpar o terminal**:
   ```zsh
   unset C6_CLIENT_ID C6_CLIENT_SECRET C6_PIX_KEY C6_WEBHOOK_TOKEN C6_WEBHOOK_URL \
         C6_CERT_PATH C6_KEY_PATH C6_E2EID C6_DEVEDOR_NOME C6_DEVEDOR_CPF C6_DEVEDOR_CNPJ \
         C6_DEVEDOR_LOGRADOURO C6_DEVEDOR_CIDADE C6_DEVEDOR_UF C6_DEVEDOR_CEP
   ```

## Referência rápida

```
python3 scripts/c6_homologacao.py [opções]

  --simulador            sobe o simulador local e roda contra ele (ignora as C6_* do ambiente)
  --simulador-url URL    usa um simulador já rodando (só http://127.0.0.1 ou localhost)
  --checar               só confere janela, variáveis e certificado
  --producao             permite produção (exige também C6_ENV=producao)
  --estrito              só o status do roteiro conta
  --e2eid E2EID          Pix pago para o P_05 (ou C6_E2EID)
  --manter-webhook       recadastra o webhook depois do P_06_03
  --expor-chave-pix      não mascara a chave Pix
  --ignorar-escopos      segue mesmo sem os 12 escopos
  --ignorar-janela       não confere o horário (só se o C6 informar outro)
  --horas-lista N        período das consultas de lista (padrão 24 h)
  --tentativas-lote N    consultas do lote antes de seguir (padrão 10)
  --intervalo-lote S     segundos entre elas (padrão 3)
  --timeout S            por chamada (padrão 30)
  --saida DIR            pasta dos resultados

python3 scripts/c6_simulador.py [--porta 8765] [--sem-pix-pago] [--fora-da-janela] [--verboso]
```

O simulador segue os status e campos da spec, não do roteiro, e tem um Pix pago semeado. O
formato real do C6 só se confirma no sandbox. Pontos a observar na primeira rodada real: se o id
do lote é aceito como número, se o header `x-request-id` existe (sem ele, o executor usa o
`correlationId` do corpo de erro) e se a lista de Pix traz o Pix pago.
