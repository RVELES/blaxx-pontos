# Formato do arquivo de resultados (executor → roteiro)

Contrato entre o executor do sandbox (`scripts/c6_homologacao.py`) e o preenchedor do roteiro
(`scripts/c6_preencher_roteiro.py`). Definido em 22/10/2026. Quando escrevi este contrato, o executor
ainda não existia. Se ele gravar outro formato, quem se adapta é o preenchedor, e este arquivo
é atualizado junto.

## Arquivo

- JSON em UTF-8. Nome sugerido: `docs/c6-homologacao/resultados-sandbox.json`.
- O arquivo é versionável só porque o executor já grava tudo mascarado. Mesmo assim, ele traz
  txids, a chave Pix do sandbox e o devedor de teste. Antes de qualquer commit, decidir se vai
  para o `.gitignore`.
- Formas aceitas:
  - **lista pura** `[ {id, status, corpo}, ... ]`;
  - **objeto com metadados** `{"executado_em": ..., "ambiente": "sandbox", "host": ...,
    "executor": ..., "resultados": [ ... ]}`. O preenchedor imprime `executado_em`, `ambiente`,
    `host` e `executor` no resumo e ignora as outras chaves.
- Se o executor rodou contra o simulador local (`scripts/c6_simulador.py`), o preenchedor trata
  o resultado como **EXEMPLO**. Ele reconhece esse caso por `"ambiente": "simulador"` (ou
  `exemplo`, `local`, `teste`), por `"simulado": true` ou por um `host`/`base_url` em
  127.0.0.1/localhost. Assim, um ensaio no simulador nunca vira o roteiro que vai para o C6.
  Rodando contra o C6, o executor grava `"ambiente": "sandbox"` e o host
  `https://baas-api-sandbox.c6bank.info`.

## Cada item

| Campo | Obrigatório | Tipo | Regra |
|---|---|---|---|
| `id` | sim | string | Um destes 25: `AT_01`, `P_01_01`..`P_01_05`, `P_02_01`..`P_02_04`, `P_03_01`..`P_03_04`, `P_04_01`..`P_04_04`, `P_05_01`..`P_05_04`, `P_06_01`..`P_06_03`. Use **`P_03_03`** e **`P_04_04`**, mesmo o .docx imprimindo "P_02_03" e "P_02_04" nessas tabelas: o preenchedor acha a tabela pela posição. Repetir um id é erro. |
| `status` | sim | inteiro ou `null` | O status HTTP **que a API devolveu de verdade**. Nunca ajustar o número para bater com o roteiro. `null` quer dizer que não houve resposta HTTP (timeout, erro de TLS); nesse caso, preencha `erro`. |
| `corpo` | sim | objeto, lista, string ou `null` | O corpo da resposta. Se for JSON, mande já decodificado (objeto ou lista); se não for, mande o texto cru. `null` quando a resposta vem sem corpo (204, ou o 200 vazio do P_06_01, por exemplo). |
| `nota` | não | string | Observação curta. Vai para o .docx depois do corpo, como "Obs.: ...". Serve, por exemplo, para explicar um 404 do P_05 sem Pix pago no sandbox. |
| `erro` | não | string | Motivo quando `status` é `null`. Aparece só no resumo do terminal, nunca no .docx. |
| `metodo`, `url`, `executado_em`, `duracao_ms` | não | qualquer | O preenchedor ignora. A `url` vai **sem host e sem query com segredo**. |

Os testes `AP_*`, `B_*`, `C_*`, `E_*`, `TR_*`, `PA_*` e `BP_*` **não** entram no arquivo: no
roteiro, eles ficam desmarcados e vazios.

## Máscara (responsabilidade do executor, conferida de novo no preenchedor)

O executor grava já mascarado. O preenchedor mascara outra vez e, se ainda sobrar qualquer
segredo no texto final, aborta sem salvar.

| O quê | Onde aparece | Como gravar |
|---|---|---|
| `access_token` | corpo do AT_01 | `"<mascarado, N caracteres>"` |
| token do webhook (`C6_WEBHOOK_TOKEN`) | `webhookUrl` do P_06_01/P_06_02 | `.../webhook/<TOKEN:Nch>` |
| `client_id`, `client_secret`, cert, key, header `Authorization` | nunca deveriam aparecer | não gravar a requisição, só a resposta |
| chave Pix (`C6_PIX_KEY`) | `chave` em quase todo corpo | pode ir crua no arquivo. No .docx, o preenchedor troca por `<CHAVE_PIX:Nch>`, a menos que se rode com `--mostrar-chave-pix` (essa decisão é do dono) |

Além disso, o preenchedor reconhece e mascara JWT, blocos PEM, `Bearer <token>` e os valores
das variáveis `C6_CLIENT_ID`, `C6_CLIENT_SECRET`, `C6_WEBHOOK_TOKEN` e `C6_PIX_KEY` que estiverem
no ambiente, em qualquer campo.

## O que o preenchedor faz com o corpo

- Formata como JSON indentado em Courier New 8 pt (`--fonte-pt` muda o tamanho).
- Corta listas longas em 3 itens e anota `"<+K itens omitidos; total N>"` (`--max-itens`;
  `0` não corta).
- Corta strings com mais de 600 caracteres, como uma imagem base64, e anota
  `"… <N caracteres; truncado>"` (`--max-chars`; `0` não corta).
- `null`, `""` ou `{}` viram "(resposta sem corpo)".
- O status sai como `201 Created`, `204 No Content` etc.

## Exemplo mínimo

```json
{
  "executado_em": "2026-10-23T10:30:00-03:00",
  "ambiente": "sandbox",
  "host": "https://baas-api-sandbox.c6bank.info",
  "executor": "scripts/c6_homologacao.py",
  "resultados": [
    {"id": "AT_01", "status": 200, "corpo": {"access_token": "<mascarado, 1234 caracteres>", "expires_in": 300, "token_type": "Bearer", "scope": "..."}},
    {"id": "P_03_01", "status": 202, "corpo": null},
    {"id": "P_05_01", "status": 404, "corpo": {"title": "Não Encontrado", "status": 404}, "nota": "Sem Pix pago no sandbox não há e2eid real."},
    {"id": "P_06_03", "status": 204, "corpo": null}
  ]
}
```

O exemplo completo e fictício, com os 25 testes, está em `resultados - EXEMPLO.json`. O arquivo
leva **EXEMPLO** no nome e `"exemplo": true` no conteúdo. Com ele, o preenchedor só aceita gerar
uma saída que também tenha EXEMPLO no nome, e cada campo preenchido recebe o carimbo
"[EXEMPLO FICTÍCIO - NÃO ENVIAR]". No sentido contrário, um resultado real nunca é gravado em
arquivo chamado EXEMPLO.

## Regras de bloqueio do preenchedor

- Faltou algum dos 25 ids, ou algum veio com `status: null`: o preenchedor recusa, a menos que
  se rode com `--parcial`, que gera rascunho e não deve ser enviado.
- `status` fora de 100-599 ou que não seja inteiro, `id` desconhecido, `corpo` ausente: o
  preenchedor recusa e lista os erros.
- Status 5xx: gera o arquivo, mas avisa que o sandbox pode estar fora da janela (seg-sex,
  7h-22h). Nesse caso, rodar de novo.
