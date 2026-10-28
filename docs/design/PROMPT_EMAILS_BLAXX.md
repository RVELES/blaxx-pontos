# Prompt: e-mails transacionais da BlaXx

Este é o prompt usado para gerar o layout dos e-mails da BlaXx (outubro de 2026). Use-o como está para criar um e-mail novo no mesmo padrão, numa IA ou num briefing para um designer. O código vive em `app/services/mailer.py`. A função `_layout_html` monta o esqueleto, e cada `send_*` só preenche o conteúdo.

---

## O prompt

> Crie um e-mail transacional em HTML para a **BlaXx Rewards**, um programa de pontos brasileiro. O e-mail deve seguir a identidade do site (tema "Embarque", neo-brutalista: papel, cartão claro, preto e um único verde neon).
>
> **Restrições técnicas (e-mail, não página web):**
> - Layout só com `<table role="presentation">` e **estilo inline**. O Gmail e o Outlook ignoram `<style>`, flexbox, grid e variáveis CSS.
> - Largura máxima de 560px, centralizado, com 12px de respiro lateral no celular.
> - Fontes do sistema: `'Helvetica Neue', Helvetica, Arial, sans-serif`. Para códigos, use `'Space Mono', Menlo, 'Courier New', monospace`.
> - `<meta name="color-scheme" content="light only">`, para o modo escuro do cliente não inverter as cores da marca.
> - Um *preheader* invisível logo no início do `<body>`: a frase que aparece ao lado do assunto na caixa de entrada.
> - Gere sempre uma versão em **texto puro** com o mesmo conteúdo.
> - Todo dado do usuário (nome, mensagem, nome do produto) passa por escape de HTML.
>
> **Paleta (tokens do site, `blaxx/assets/blaxx-embarque.css`):**
> | Uso | Cor |
> |---|---|
> | Fundo da página (chão) | `#E4E1D9` |
> | Cartão | `#FBFAF7` |
> | Tinta, bordas, faixa do topo | `#0A0B0E` |
> | Neon: botão, filete, código | `#59FD27` |
> | Texto secundário | `#555860` |
> | Link sobre fundo claro (verde legível) | `#1F6B0B` |
> | Pontilhado do recibo | `#B9B6AE` |
>
> **Estrutura, de cima para baixo:**
> 1. **Faixa preta** com o logo oficial (`https://blaxxpontos.com.br/assets/blaxx-logo.png`, 112×112, `alt="BlaXx Rewards"`).
> 2. **Filete neon** de 6px separando a faixa do cartão.
> 3. **Cartão** com borda preta de 2px e cantos de 18px, contendo:
>    - um **título** curto e animado, em negrito, 24px, terminando em "!" ou "?" quando couber;
>    - uma **saudação** pelo primeiro nome: "Oi, Ana!";
>    - **1 a 2 parágrafos** curtos dizendo o que aconteceu e o que muda para a pessoa;
>    - quando houver movimento de pontos ou dinheiro, um **recibo**: tabela de rótulo à esquerda (cinza) e valor à direita (negrito), com um filete preto de 2px no topo e linhas pontilhadas entre os itens. Use sinal nos pontos (`+5.500 pts` / `-1.200 pts`), real com vírgula (`R$ 18,50`) e "Saldo atual" no fim;
>    - quando houver código (verificação, voucher), um **bloco preto** com o código em neon, monoespaçado, 30px, com espaçamento largo entre letras;
>    - um **botão** neon com texto preto, borda preta de 2px, cantos de 14px e seta "→". Logo abaixo, o link escrito por extenso para quando o botão não abrir;
>    - a **assinatura** "Um abraço, **Equipe BlaXx**";
>    - uma **nota** em letra pequena, separada por pontilhado, com prazos e o "não foi você?".
> 4. **Rodapé** fora do cartão: "BlaXx Rewards · blaxxpontos.com.br" e o WhatsApp de ajuda (11) 92470-6095.
>
> **Tom de voz:** português do Brasil, próximo e leve, como uma pessoa da equipe falando. Frases curtas. Comemore as conquistas ("Pontos na carteira!", "Troca feita!") e tranquilize nos problemas ("Mas fica tranquilo: os pontos já voltaram para a sua carteira."). Nunca prometa o que o sistema não faz: nada de bônus, prazo ou benefício que não exista no backend. Não use emoji no assunto.
>
> **Assunto:** sempre começa com "BlaXx · " e diz o fato principal com o número: "BlaXx · +5.500 pts na sua carteira".
>
> **Privacidade:** chave Pix mascarada (só os 4 últimos caracteres). Nunca mostre senha, token completo, CPF ou dado bancário. Link de redefinição de senha só no botão e no link por extenso, com a validade dita na nota.

---

## E-mails que existem hoje

| Evento | Função | Assunto |
|---|---|---|
| Esqueci a senha | `send_password_reset` | BlaXx · Redefina sua senha |
| Primeira senha (conta Google) | `send_password_reset(is_first_password=True)` | BlaXx · Crie sua senha |
| Confirmação de e-mail | `send_email_verification` | BlaXx · Seu código: 123456 |
| Boas-vindas | `send_welcome` | BlaXx · Boas-vindas! Sua conta está pronta |
| Compra de pontos (Pix ou cartão) | `send_compra_confirmada` | BlaXx · +5.500 pts na sua carteira |
| Envio de pontos (quem mandou) | `send_envio_concluido` | BlaXx · Você enviou 1.200 pts para Ana |
| Envio de pontos (quem recebeu) | `send_pontos_recebidos` | BlaXx · Ricardo te enviou 1.200 pts |
| Venda de pontos: pedido | `send_venda_solicitada` | BlaXx · Recebemos sua venda de 2.000 pts |
| Venda de pontos: Pix pago | `send_venda_paga` | BlaXx · Pix de R$ 18,50 enviado |
| Venda de pontos: Pix falhou | `send_venda_falhou` | BlaXx · Seus pontos voltaram para a carteira |
| Troca por produto | `send_troca_produto` | BlaXx · Seu voucher: Cinema 2D |

## Como os envios são disparados

`app/services/avisos_email.py` liga cada operação ao seu e-mail **depois do commit**. O envio roda em segundo plano (`mailer.disparar`), para o webhook de pagamento não esperar o Resend. Uma falha no e-mail vira log e **nunca desfaz** a movimentação. O envio de pontos só manda e-mail quando a janela de cancelamento fecha e a transferência é promovida, e uma vez só.

## Para criar um e-mail novo

1. Escreva uma função `send_<evento>` em `mailer.py` que chame `_enviar_evento(...)` com título, parágrafos, recibo, botão e nota.
2. Ligue o disparo em `avisos_email.py` e chame-o logo depois do `db.session.commit()` da operação.
3. Adicione um teste em `tests/test_emails_movimentacao.py`, ou em `test_mailer_templates.py` se for só o template.
4. Gere a prévia renderizando o `body_html` e conferindo no navegador antes de publicar.
