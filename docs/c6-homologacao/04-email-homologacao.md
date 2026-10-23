# Rascunho do e-mail de homologação para o C6

Rascunho para o dono enviar da própria caixa. Nada foi enviado. Trechos entre `[colchetes]`
são para preencher ou apagar antes do envio. Os dados da organização seguem
`02-dados-organizacao.md`: CNPJ e razão social não têm fonte no projeto e vêm do cartão CNPJ.

## Cabeçalho

| | |
|---|---|
| **Para** | homologacaoapi@c6bank.com |
| **De** | rveles@blaxxpontos.com.br (o mesmo e-mail do novo cadastro no C6 Developers) |
| **Assunto** | Homologação de API: BlaXx Pontos, Roteiro de Testes v3.0 preenchido (novo cadastro) |
| **Anexo** | `Roteiro de Testes - C6 Developers v3.0 - BlaXx.docx` (o arquivo **sem** "EXEMPLO" no nome) |
| **Anexo opcional** | o mesmo roteiro em PDF, gerado com `--pdf`, para leitura rápida |

## Corpo

> Prezados,
>
> Encaminhamos em anexo o Roteiro de Testes – C6 Developers v3.0 preenchido pela
> [RAZÃO SOCIAL, como no cartão CNPJ], CNPJ [00.000.000/0000-00], referente à integração do
> software BlaXx Pontos com as APIs de Autenticação e Pix[ e Checkout].
>
> Nossa solicitação anterior foi cancelada em 22/10/2026 porque o roteiro não foi enviado dentro
> do prazo. Fizemos um novo cadastro no portal C6 Developers em [DD/MM/AAAA], com este mesmo
> e-mail, e executamos os testes no ambiente sandbox em [DD/MM/AAAA], entre [hh:mm] e [hh:mm].
> Os campos "Status Code – Retornado" e "Response Body – Retornado" trazem os retornos reais da
> API. Por segurança, o access token, o token do nosso endpoint de webhook e a chave Pix
> aparecem mascarados.
>
> [Manter só os itens que se aplicarem ao resultado real:]
> - Nos testes [P_01_02, P_01_03, P_03_01 e P_03_02], o status retornado pela API
>   ([201, 200, 202 e 202]) segue a especificação da API Pix e difere do indicado no roteiro.
> - Os testes P_05_01, P_05_03 e P_05_04 dependem de um Pix pago no sandbox. [Registramos o
>   retorno real ([404]) e pedimos orientação sobre como simular o pagamento de uma cobrança
>   no sandbox.]
>
> Ficamos à disposição para qualquer ajuste e solicitamos, após a análise, a liberação das
> credenciais de produção.
>
> Atenciosamente,
>
> Ricardo Veles
> CEO · BlaXx Rewards
> rveles@blaxxpontos.com.br | [+55 11 97648-7447]

## Antes de enviar

- [ ] O anexo é o arquivo **sem** "EXEMPLO" no nome. Os campos não podem trazer o carimbo
  "[EXEMPLO FICTÍCIO - NÃO ENVIAR]".
- [ ] O roteiro foi gerado com `--cnpj` e `--razao-social` copiados do cartão CNPJ. Não usar o
  `00.000.000/0001-00` do rodapé do site, que é marcador, nem o CNPJ da Sigma Trust que aparece
  em `_confidencial/financeiro/`.
- [ ] O telefone está confirmado. O padrão é o da assinatura oficial; o WhatsApp do site é
  outro número. Para trocar, use `--telefone`.
- [ ] O resumo do terminal não mostrou `ROTEIRO INCOMPLETO` nem `PENDENTE`. Nenhum teste deu
  5xx: um 503 quer dizer sandbox fora da janela (seg-sex, 7h-22h), e aí é preciso rodar de novo.
- [ ] As datas e horários do corpo batem com o `executado_em` do arquivo de resultados.
- [ ] A lista de divergências do corpo bate com a coluna "Situação" do resumo. Apague os itens
  que não aconteceram.
- [ ] A chave Pix vai mascarada (padrão). Se o dono decidir mostrá-la, gere de novo com
  `--mostrar-chave-pix`.
- [ ] Se a homologação incluir CHECKOUT, gere o roteiro com `--com-checkout`, inclua
  "e Checkout" no corpo e confira os 6 testes C_* no resumo.
- [ ] Abra o .docx no Word antes de anexar e confira o checkbox PIX marcado e os campos de
  organização.

## Perguntas que podem ir no mesmo e-mail (opcional)

Se ainda não tiverem resposta no cadastro, as perguntas abaixo saem de `01-mapa-roteiro.md`:

1. A credencial sandbox inclui os escopos `webhook.read` e `webhook.write`? Eles são exigidos
   em P_06 e não aparecem na lista de escopos do roteiro.
2. Como simular o pagamento de uma cobrança no sandbox, para obter um `endToEndId` para P_05?
3. O webhook de Pix chega no formato Bacen (`{"pix": [...]}`) ou no envelope da Webhook API
   (`information` como string)? Ele traz o `txid`?
4. Nos testes P_01_02, P_01_03, P_03_01 e P_03_02, vale o status da especificação ou o do roteiro?

## Como gerar o anexo

```bash
cd "blaxx_exe/backend"
python3 scripts/c6_preencher_roteiro.py \
    --resultados docs/c6-homologacao/resultados-sandbox.json \
    --cnpj "<do cartão CNPJ>" --razao-social "<do cartão CNPJ>" --pdf
```

O formato do arquivo de resultados está em `formato-resultados.md`.
