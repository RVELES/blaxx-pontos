# C6 Developers: dados da organização (Roteiro v3.0)

Bloco "FORMULÁRIOS INICIAIS > DADOS DA ORGANIZAÇÃO" do *Roteiro de Testes - C6 Developers v3.0*.
Levantado em 22/10/2026 só com o que está documentado no projeto. Caminhos relativos a
`Dropbox/Blaxx Pontos/`; `memória:` = `~/.claude/projects/-Users-ricardoveles-Library-CloudStorage-Dropbox-Blaxx-Pontos/memory/`.

Campo vazio = sem fonte no projeto. Não preencher de cabeça: tem que bater com o cartão CNPJ
e com o novo cadastro em developers.c6bank.com.br.

| N.º | Campo do roteiro | Valor | Situação |
|---|---|---|---|
| 1 | CNPJ / IDENTIFICADOR DA ORGANIZAÇÃO | *(a preencher)* | FALTANDO |
| 2 | NOME DA EMPRESA CLIENTE / PARCEIRA | *(a preencher)* | FALTANDO |
| 3 | NOME DO SOFTWARE | BlaXx Pontos | confirmado |
| 4 | RESPONSÁVEL PELA EMPRESA | Ricardo Veles | confirmado |
| 5 | E-MAIL | rveles@blaxxpontos.com.br | confirmado |
| 6 | TELEFONE | +55 11 97648-7447 | confirmado, mas o dono precisa validar |

## Fontes e observações por campo

**1. CNPJ:** o número não aparece em nenhum lugar do projeto.
- `memória: cnpj-e-conta-abertos.md:11` diz que o CNPJ da BlaXx existe (09/08/2026), mas não traz o número.
- Os lugares onde o CNPJ deveria estar continuam com marcador: `blaxx-spa/public/termos.html:61` e `blaxx-spa/public/privacidade.html:60` (`[CNPJ]`), minutas em `docs/legal/MINUTA_*.docx` (`[PREENCHER]`).
- NÃO use o `00.000.000/0001-00` do rodapé (`blaxx/home.html:297`). É marcador.
- NÃO use o CNPJ que aparece em `_confidencial/financeiro/` (NFS-e e proposta). Ele é da Sigma Trust LTDA, outra empresa.
- Ação: copiar do cartão CNPJ da Receita.

**2. Nome da empresa:** nenhuma razão social da BlaXx está documentada no projeto.
- Candidato a confirmar: `BLAXX REWARDS LTDA`. Esse nome veio do pedido desta tarefa. A busca no projeto inteiro (texto, docx, pdf, xlsx e memória) não encontrou nenhuma ocorrência.
- O que existe é a marca "BlaXx Rewards", sem "LTDA": `_interno/design-assets/assets/exports/BlaXx_Assinatura_Email_Branco.html:37`.
- Ação: usar a razão social exatamente como está no cartão CNPJ.

**3. Nome do software:** BlaXx Pontos
- `blaxx/home-embarque.html:7` (`<title>… | BlaXx Pontos`)
- `blaxx/home-embarque.html:53` (`og:site_name` = "BlaXx Pontos")
- `memória: login-google-conta-empresa.md:12` (projeto Google Cloud "BlaXx Pontos")

**4. Responsável pela empresa:** Ricardo Veles
- `_interno/design-assets/assets/exports/BlaXx_Assinatura_Email_Branco.html:36` (nome) e `:37` (cargo "CEO · BlaXx Rewards")

**5. E-mail:** rveles@blaxxpontos.com.br
- `_interno/design-assets/assets/exports/BlaXx_Assinatura_Email_Branco.html:38`
- `blaxx_exe/backend/app/config.py:361` ("conta da empresa")
- `blaxx/assets/blaxx-config.js:41` ("conta da empresa")
- Ação: usar o mesmo e-mail do novo cadastro no C6 Developers.

**6. Telefone:** +55 11 97648-7447
- `_interno/design-assets/assets/exports/BlaXx_Assinatura_Email_Branco.html:39`. É a assinatura de e-mail oficial de 20/06/2026, a mesma dos campos 4 e 5. O arquivo `BlaXx_Assinatura_Email_Branco 2.html` é idêntico.
- O WhatsApp público do site usa OUTRO número (`blaxx/contato.html:112`). Ele não foi usado aqui porque é canal comercial/SAC, não o contato do responsável.
- Ação: o dono confirma qual número vai para o C6.
