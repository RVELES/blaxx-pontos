# E-mail para o C6: versão pronta com os resultados reais (22/10/2026)

Nada foi enviado. O envio é do dono, pela caixa rveles@blaxxpontos.com.br.

- **Para:** homologacaoapi@c6bank.com
- **Assunto:** Homologação de API – BlaXx Pontos – Roteiro de Testes v3.0 (Pix e Checkout)
- **Anexos:** `envio-c6/Roteiro de Testes - C6 Developers v3.0 - BlaXx.docx` e o mesmo em PDF

Os anexos já trazem CNPJ, razão social e telefone da ficha cadastral municipal (CCM 0.426.125-9).

---

Prezados,

Encaminhamos em anexo o Roteiro de Testes – C6 Developers v3.0 preenchido pela BLAXX REWARDS LTDA, CNPJ 68.260.588/0001-56, referente à integração do software BlaXx Pontos com as APIs de Autenticação, Pix e Checkout.

Nossa solicitação anterior foi cancelada em 22/10/2026. Refizemos o cadastro no portal C6 Developers e executamos os testes no ambiente sandbox em 22/10/2026, entre 20h45 e 21h05 (horário de Brasília). Os campos "Status Code – Retornado" e "Response Body – Retornado" trazem os retornos reais da API; o access token, o token do nosso endpoint de webhook e a chave Pix aparecem mascarados.

Resumo:

- Autenticação (AT_01) e Pix: 18 testes concluídos com o status esperado (P_01, P_02, P_04, P_05_02 e P_06).
- Checkout: C_01, C_02, C_03 e C_04 concluídos. No C_03 a API retornou 204, conforme a especificação (o roteiro indica 200).
- Lote de cobranças com vencimento (P_03_01 e P_03_04): o sandbox retornou HTTP 502 (Cloudflare "Bad gateway", origem baas-api-sandbox.c6bank.info) em três execuções seguidas, inclusive às 21h03, enquanto os demais endpoints respondiam normalmente. Por isso P_03_02 e P_03_03 não puderam ser executados. Podem verificar, por favor?
- P_05_01, P_05_03 e P_05_04 dependem de um Pix pago no sandbox. Não encontramos na documentação como simular o pagamento de uma cobrança; se houver um meio, executamos em seguida.
- C_0501: o webhook de Checkout foi cadastrado com sucesso (C_04); o registro do evento CREATED recebido será enviado em complemento.
- C_0502: conforme a OBS.2 do roteiro, os links de pagamento do sandbox não podem ser pagos pelo cliente (o link retorna bloqueio de acesso), então não há evento PAID para registrar.
- C_06: no roteiro v3.0 o teste aparece só no sumário, sem passos nem campo de resultado. Se ele for necessário, pedimos orientação de como registrá-lo.
- Os escopos da credencial sandbox incluem checkout.read, checkout.write e checkout.cancel, mas não checkout.capture, citado no roteiro.

Ficamos à disposição para qualquer ajuste e, após a análise, solicitamos a liberação das credenciais de produção.

Atenciosamente,

Ricardo Veles
BLAXX REWARDS LTDA · CNPJ 68.260.588/0001-56
Av. Marquês de São Vicente, 405, conj. 1804 – Várzea da Barra Funda – São Paulo/SP – CEP 01139-001
rveles@blaxxpontos.com.br | WhatsApp (11) 92470-6095
