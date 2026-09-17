"""Configuracoes do app."""
from __future__ import annotations
import os
import re
import sys


def _normalize_db_url(url: str) -> str:
    """Aceita formatos do Heroku/Neon/Supabase e ajusta para psycopg v3.

    - postgres://...  → postgresql+psycopg://...
    - postgresql://...→ postgresql+psycopg://...
    - sqlite://... fica intacto
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


# Fallback padrão quando DATABASE_URL não está configurada (dev/local).
_DEFAULT_DB_URL = "sqlite:///blaxx.db"


def _clean_pasted_db_url(raw: str) -> str:
    """Remove sujeira comum de colagem no painel (Render/Neon/etc).

    Casos que vimos derrubar o boot ("Could not parse SQLAlchemy URL"):
      * espaço/quebra-de-linha nas pontas → strip;
      * aspas/crase/<> envolvendo o valor inteiro;
      * prefixo de comando colado por engano: `psql 'postgres://...'`;
      * prefixo `DATABASE_URL=` colado junto do valor;
      * QUEBRA DE LINHA OU ESPAÇO NO MEIO da string (colagem multi-linha no
        textarea do painel) — uma URL nunca contém whitespace, então qualquer
        whitespace interno é lixo e é removido.
    """
    s = raw.strip().strip("'\"`")
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    if s.lower().startswith("psql "):
        s = s[5:].strip().strip("'\"`")
    if s.lower().startswith("database_url="):
        s = s.split("=", 1)[1].strip().strip("'\"`")
    # URLs não têm espaços/\n/\t: remove qualquer whitespace interno residual.
    s = re.sub(r"\s+", "", s)
    return s


def _resolve_db_url() -> str:
    """Lê DATABASE_URL tolerando erros comuns de colagem no painel (Render/etc).

    Causa real de boot-crash em prod ("Could not parse SQLAlchemy URL"): a
    variável existe mas vem com espaço/quebra-de-linha/aspas — aí o default de
    os.environ.get() NÃO entra (a chave não está "ausente") e o make_url()
    recebe lixo. Limpamos antes de usar (ver _clean_pasted_db_url):
      * string vazia após limpeza ⇒ cai no SQLite default (boot não quebra);
      * valor não-vazio é validado com make_url() AQUI, com mensagem clara e
        acionável — melhor que o traceback opaco do SQLAlchemy lá no boot.
    """
    raw = os.environ.get("DATABASE_URL") or ""
    cleaned = _clean_pasted_db_url(raw)
    if not cleaned:
        return _DEFAULT_DB_URL
    url = _normalize_db_url(cleaned)
    try:
        from sqlalchemy.engine.url import make_url
        make_url(url)
    except Exception as exc:  # noqa: BLE001 — diagnóstico de boot
        # Diagnóstico SEM vazar o valor (pode conter senha): só metadados.
        had_ws = bool(re.search(r"\s", raw.strip()))
        scheme = url.split("://", 1)[0] if "://" in url else "(sem ://)"
        print(
            "[config] DATABASE_URL inválida após limpeza — boot vai abortar. "
            f"len_bruto={len(raw)} len_limpo={len(cleaned)} "
            f"tinha_whitespace_interno={had_ws} scheme={scheme!r}. "
            "Verifique o valor no painel: deve ser "
            "postgresql://USUARIO:SENHA@HOST/BANCO?sslmode=require "
            "(sem aspas, sem espaços, em uma única linha).",
            file=sys.stderr,
            flush=True,
        )
        raise
    return url


class Config:
    SQLALCHEMY_DATABASE_URI = _resolve_db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Confiabilidade com Neon/serverless: o provedor FECHA conexões ociosas e o
    # pooler derruba SSL, gerando "SSL connection has been closed unexpectedly"
    # (500 no próximo SELECT). pool_pre_ping testa a conexão antes de usar e
    # reconecta; pool_recycle descarta conexões velhas antes do timeout do Neon.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")

    # ---------------- JWT ----------------
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
    # Sprint 1: access token reduzido de 24h para 30min (padrao fintech).
    # Refresh token continua 30 dias. O frontend usa /auth/refresh para
    # renovar silenciosamente. Sobrescrivivel via env BLAXX_JWT_ACCESS_MIN.
    JWT_ACCESS_TOKEN_EXPIRES = int(os.environ.get("BLAXX_JWT_ACCESS_MIN", 30)) * 60
    JWT_REFRESH_TOKEN_EXPIRES = 60 * 60 * 24 * 30 # 30 dias

    # SEC-1: dual-mode auth. Aceita Authorization: Bearer (apps nativos iOS/
    # Android/desktop) E cookie httpOnly (SPA web + PWA). O web sai do
    # localStorage (vulnerável a XSS) e passa a depender do cookie httpOnly
    # com SameSite=Strict, que o JS nunca enxerga. Apps nativos continuam
    # com Bearer (não têm cookie jar de browser).
    JWT_TOKEN_LOCATION = ["cookies", "headers"]
    JWT_ACCESS_COOKIE_NAME = "blaxx_session"
    JWT_REFRESH_COOKIE_NAME = "blaxx_refresh"
    JWT_ACCESS_COOKIE_PATH = "/"
    JWT_REFRESH_COOKIE_PATH = "/auth/refresh"
    # SameSite=Strict bloqueia envio do cookie em navegacoes cross-site → mata
    # CSRF naturalmente. Em producao, Secure=True forca HTTPS.
    #
    # Sprint 1-2 (P0): CSRF protect HABILITADO (defesa em profundidade).
    # SameSite=Strict ja cobre o caso comum, mas browsers antigos / quirks de
    # PWA podem deixar passar — flask-jwt-extended emite X-CSRF-TOKEN header
    # ligado ao cookie. Cliente DEVE ler o cookie csrf_access_token (NAO
    # httpOnly) e ecoar no header X-CSRF-TOKEN em mutacoes (POST/PUT/PATCH/
    # DELETE). Como apps nativos iOS/Android/desktop usam Authorization:
    # Bearer (que NAO tem cookie), CSRF protect so afeta requests cookie-only
    # (browser SPA) — comportamento desejado. Bearer header e' preferido pela
    # flask-jwt-extended quando ambos presentes (vide _bearer_user em auth.py).
    JWT_COOKIE_SECURE = os.environ.get("BLAXX_COOKIE_SECURE", "1") == "1"
    JWT_COOKIE_SAMESITE = "Strict"
    JWT_COOKIE_CSRF_PROTECT = True
    JWT_CSRF_IN_COOKIES = True
    JWT_ACCESS_CSRF_COOKIE_NAME = "csrf_access_token"
    JWT_REFRESH_CSRF_COOKIE_NAME = "csrf_refresh_token"
    # METHODS sujeitos a CSRF check: padrao da lib (POST, PUT, PATCH, DELETE)
    JWT_CSRF_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
    # httpOnly default da Flask-JWT-Extended ja e' True; reforcamos por clareza.
    JWT_COOKIE_HTTPONLY = True

    # ---------------- SMS (Twilio) — Onda 3 ----------------
    # Em dev, deixe SMS_BACKEND=console e o código sai no log do server.
    SMS_BACKEND = os.environ.get("SMS_BACKEND", "console")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_PHONE = os.environ.get("TWILIO_FROM_PHONE", "")
    PHONE_OTP_TTL = int(os.environ.get("PHONE_OTP_TTL", "600"))         # 10 min verify
    MFA_CHALLENGE_TTL = int(os.environ.get("MFA_CHALLENGE_TTL", "300")) # 5 min login
    PHONE_OTP_COOLDOWN = int(os.environ.get("PHONE_OTP_COOLDOWN", "60"))

    # ---------------- Rate limiter ----------------
    # storage_uri: em produção idealmente Redis; em dev/sandbox usa memória
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "200 per minute")
    # Desliga rate limit em testes (TestConfig sobrescreve)
    RATELIMIT_ENABLED = True

    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
    ] or ["*"]

    # URL base do front-end (web SPA). Usada para montar links absolutos em
    # e-mails (ex.: reset de senha -> {FRONTEND_URL}/redefinir-senha?token=...).
    # Em prod, setar no Render. Default = deploy Netlify do SPA React.
    FRONTEND_URL = os.environ.get(
        "FRONTEND_URL", "https://blaxx-pontos-app.netlify.app"
    ).rstrip("/")

    # ---------------- Conversao ponto <-> R$ ----------------
    # 1 ponto = R$ 0,09 = 9 centavos. Mantemos o conversor em CENTAVOS para
    # evitar floats no ledger. Toda matematica usa CENTS_PER_POINT.
    #   pts -> cents: pts * CENTS_PER_POINT
    #   cents -> pts: cents // CENTS_PER_POINT  (floor; resto fica como house edge)
    #   pts -> BRL:   pts * CENTS_PER_POINT / 100  (display)
    CENTS_PER_POINT = int(os.environ.get("BLAXX_CENTS_PER_POINT", 9))

    # ---------------- Limites de resgate ----------------
    # Usuarios VIP nao tem teto diario (vide redeem.py).
    # Demais usuarios: teto em R$ convertido para pontos.
    REDEEM_MIN_POINTS = int(os.environ.get("BLAXX_REDEEM_MIN_POINTS", 1))
    # R$ 100.000,00 / R$ 0,09 = 1.111.111 pts. Arredondado pra cima.
    REDEEM_MAX_POINTS_PER_DAY = int(os.environ.get(
        "BLAXX_REDEEM_MAX_POINTS_PER_DAY", 1_111_111
    ))

    # ---------------- Limites de envio (transfer) ----------------
    TRANSFER_MIN_POINTS = 100
    TRANSFER_MAX_POINTS_PER_DAY = 50_000
    PIX_CHARGE_TTL_SECONDS = 30 * 60

    # ---------------- Sprint 1-2 (P0) · Limites MENSAIS por usuario --------
    # Empilham sobre os limites diarios. VIP ignora (mesma semantica do diario).
    # Defaults conservadores; ajustaveis via env var sem mudar codigo.
    TRANSFER_MAX_POINTS_PER_MONTH = int(os.environ.get(
        "BLAXX_TRANSFER_MAX_POINTS_PER_MONTH", 50_000))
    PURCHASE_MAX_POINTS_PER_MONTH = int(os.environ.get(
        "BLAXX_PURCHASE_MAX_POINTS_PER_MONTH", 100_000))
    REDEEM_MAX_POINTS_PER_MONTH = int(os.environ.get(
        "BLAXX_REDEEM_MAX_POINTS_PER_MONTH", 100_000))

    # ---------------- Step-up 2FA em operações sensíveis (B13) ----------------
    # Acima deste valor, transferência/resgate exigem o código TOTP — MAS só
    # para usuários que têm 2FA ativo (não-disruptivo p/ quem não configurou).
    SENSITIVE_OP_THRESHOLD_PTS = int(os.environ.get("BLAXX_SENSITIVE_OP_THRESHOLD_PTS", 20_000))

    # ---------------- Alertas de transações suspeitas (B14) ----------------
    ALERT_HIGH_VALUE_PTS = int(os.environ.get("BLAXX_ALERT_HIGH_VALUE_PTS", 30_000))
    ALERT_VELOCITY_COUNT = int(os.environ.get("BLAXX_ALERT_VELOCITY_COUNT", 5))
    ALERT_VELOCITY_WINDOW_MIN = int(os.environ.get("BLAXX_ALERT_VELOCITY_WINDOW_MIN", 10))
    ALERT_DISTINCT_RECIPIENTS = int(os.environ.get("BLAXX_ALERT_DISTINCT_RECIPIENTS", 4))

    @classmethod
    def brl_per_point(cls) -> float:
        """Display helper: R$ por ponto (ex: 0.09)."""
        return cls.CENTS_PER_POINT / 100.0

    @classmethod
    def pts_to_cents(cls, pts: int) -> int:
        return pts * cls.CENTS_PER_POINT

    @classmethod
    def cents_to_pts(cls, cents: int) -> int:
        """Floor: 1000 cents / 9 = 111 pts (1 cent fica como house edge)."""
        return cents // cls.CENTS_PER_POINT

    @classmethod
    def rate_label(cls) -> str:
        """String human-readable usada no /redeem/quote."""
        return f"1 pt = R$ {cls.brl_per_point():.2f}".replace(".", ",")

    # ---------------- PIX provider selection ----------------
    # "mock"  → MockPixProvider (default, demo)
    # "asaas" → AsaasPixProvider (produção)
    PIX_PROVIDER = os.environ.get("PIX_PROVIDER", "asaas").lower()
    # T18: MP_ACCESS_TOKEN / MP_WEBHOOK_SECRET / MP_NOTIFICATION_URL /
    # MP_WEBHOOK_MAX_CLOCK_SKEW removidos — MercadoPagoPixProvider não existe
    # mais desde o corte de gateway (01–03/08); nada lia essas quatro vars.

    # ---------------- Payout (PIX de saída — venda/resgate) ----------------
    # "auto":   chama provider.request_payout (mock paga na hora; MP sem
    #           payout_provider FALHA e estorna — só use auto com provider real)
    # "manual": payout fica PROCESSING, admins são notificados, executam a
    #           transferência PIX no banco e confirmam em /admin/payouts.
    #           É o modo correto de produção enquanto não há provider de
    #           payout integrado (Efí/Stark/MP Money Out).
    # B-9: `Config.PAYOUT_MODE` foi REMOVIDO. Ele default-ava "auto" e, por
    # ser atributo de Config, virava `app.config["PAYOUT_MODE"]` — que
    # discordava do modo realmente em vigor toda vez que o factory forçava
    # "manual" por falta de payout_provider. Um valor que diz "auto" num app
    # que roda manual é pior que valor nenhum: quem consulta conclui que o
    # resgate paga sozinho.
    # A verdade única é `app.config["PAYOUT_MODE_EFFECTIVE"]`, resolvido em
    # `__init__.py` a partir da env var E da capacidade do provider.

    # ---- Asaas: PIX de SAÍDA (payout do resgate) ---- #
    # Nem MercadoPago nem Stripe enviam PIX para chave de terceiro. O Asaas
    # envia (POST /v3/transfers). Com ASAAS_API_KEY setada, o provider é
    # injetado como payout_provider e PAYOUT_MODE pode virar "auto".
    # Chave de produção começa com $aact_prod_; sandbox com $aact_hmlg_.
    ASAAS_API_KEY = os.environ.get("ASAAS_API_KEY", "").strip()
    # "sandbox" (default, seguro) | "production"
    ASAAS_ENV = os.environ.get("ASAAS_ENV", "sandbox").strip().lower()
    # Token estático que o Asaas envia no header `asaas-access-token`. É a
    # ÚNICA autenticação do webhook (não há assinatura HMAC), por isso o
    # handler também reconsulta a API antes de mexer no ledger.
    ASAAS_WEBHOOK_TOKEN = os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()

    # ---- C6 Bank: PIX de ENTRADA (cobrança) ---- #
    # API Pix padrão Bacen, OAuth2 client_credentials + mTLS. Só cobrança:
    # com PIX_PROVIDER=c6 o resgate fica em PAYOUT_MODE manual.
    C6_CLIENT_ID = os.environ.get("C6_CLIENT_ID", "").strip()
    C6_CLIENT_SECRET = os.environ.get("C6_CLIENT_SECRET", "").strip()
    # Chave DICT da conta C6 que recebe as compras.
    C6_PIX_KEY = os.environ.get("C6_PIX_KEY", "").strip()
    # "sandbox" (default, seguro) | "production"
    C6_ENV = os.environ.get("C6_ENV", "sandbox").strip().lower()
    # Certificado de cliente (mTLS): caminhos de arquivo OU conteúdo PEM
    # inline (é assim que o Render guarda segredo multilinha).
    C6_CERT_PATH = os.environ.get("C6_CERT_PATH", "").strip()
    C6_KEY_PATH = os.environ.get("C6_KEY_PATH", "").strip()
    C6_CERT_PEM = os.environ.get("C6_CERT_PEM", "")
    C6_KEY_PEM = os.environ.get("C6_KEY_PEM", "")
    # Segredo no caminho da URL do webhook (/payments/c6/webhook/<token>).
    # O handler também reconsulta a API antes de creditar.
    C6_WEBHOOK_TOKEN = os.environ.get("C6_WEBHOOK_TOKEN", "").strip()

    # ---- Stripe: cartão INTERNACIONAL ---- #
    # Stripe não faz PIX no Brasil (invite-only + 60 dias de histórico) nem
    # paga PIX a terceiros — entra só para cartão internacional.
    # Chave de produção: sk_live_… | teste: sk_test_…
    STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "").strip()
    # Assinatura do webhook (whsec_…). A Stripe ASSINA o corpo — sem esse
    # segredo o webhook é rejeitado (fail-closed).
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    STRIPE_CURRENCY = os.environ.get("STRIPE_CURRENCY", "brl").strip().lower()
    # Chave PUBLICÁVEL (pk_live_… / pk_test_…). É pública por design — vai no
    # JS do browser. É ela que o Stripe Elements usa para tokenizar o cartão,
    # de modo que o PAN nunca chega ao nosso backend.
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()

    # PIX manual (QR estático) — EMV BR Code da conta PJ. O endpoint
    # /pix/custom-charge recusa (503) em produção enquanto isto for o
    # placeholder. Antes de 2026-07 esta env var nem era lida — setar no
    # Render não tinha efeito.
    BLAXX_STATIC_PIX_BRCODE = os.environ.get(
        "BLAXX_STATIC_PIX_BRCODE",
        "00020126360014BR.GOV.BCB.PIX0114blaxxpontos5204000053039865802BR"
        "5908Blaxx Pontos6009SAO PAULO63041234",  # placeholder — NÃO pagável
    )

    # ---------------- Cartão de crédito (MercadoPago Checkout API) ----------
    # CARD_ENABLED=1 liga o blueprint /card. MP_PUBLIC_KEY é a public key da
    # aplicação MP (pública por design — vai pro frontend tokenizar o cartão;
    # o PAN nunca toca o backend, só o card_token single-use).
    CARD_ENABLED = os.environ.get("CARD_ENABLED", "0").strip() == "1"
    MP_PUBLIC_KEY = os.environ.get("MP_PUBLIC_KEY", "")
    # Nome que aparece na fatura do cartão (soft descriptor). Máx 22 chars.
    # Perdeu o prefixo MP_ junto com a saída do MercadoPago — o cartão agora é
    # Stripe, e `services/card_purchase.py` lê `Config.STATEMENT_DESCRIPTOR`.
    # A env var antiga segue aceita para não exigir mexer no painel do Render.
    STATEMENT_DESCRIPTOR = (
        os.environ.get("STATEMENT_DESCRIPTOR")
        or os.environ.get("MP_STATEMENT_DESCRIPTOR")
        or "BLAXXPONTOS"
    )[:22]
    # Parcelamento máximo oferecido no checkout web (1 = à vista).
    CARD_MAX_INSTALLMENTS = int(os.environ.get("CARD_MAX_INSTALLMENTS", 1))

    # ---------------- Google OAuth ----------------
    # Client IDs criados em https://console.cloud.google.com → Credenciais.
    # WEB: usado pelo site Netlify (Google Identity Services no browser).
    # IOS: usado pelo app Mac/iOS via ASWebAuthenticationSession.
    # Backend valida o ID token contra QUALQUER um dos clients confiáveis.
    # Não precisa secret pra validar id_tokens — só pra fluxo authorization-code.
    #
    # Compat: aceita GOOGLE_CLIENT_ID (single) como fallback de
    # GOOGLE_WEB_CLIENT_ID, pra simplificar setup em Render/Heroku/etc onde
    # só queremos um nome de variável.
    #
    # Defaults públicos: estes Client IDs NÃO são segredos — já estão
    # embutidos no binário do app iOS/Mac, no Info.plist e no bundle JS do
    # site. Usá-los como fallback evita que o login Google quebre quando a
    # variável de ambiente não está setada no Render (causa do bug "funciona
    # no site, falha no app": o IOS_CLIENT_ID não estava configurado, então o
    # id_token do app — cujo aud é o client iOS — caía em audience mismatch).
    # A validação de assinatura, expiração, issuer e email_verified continua
    # intacta; o aud segue sendo conferido contra estes IDs específicos.
    # Client Web, confirmado no Google Cloud Console em 02/08/2026.
    #
    # Já apontou para dois valores errados: `1086156839608-779t8vpo…` (projeto
    # sem nenhum client do BlaXx) e `105341431878-tj5vi2is…` (aposentado). Como
    # o backend só aceita ID token cujo `aud` esteja nesta lista, cada divergência
    # devolvia 401 "Token Google inválido" para TODOS os logins web, sem erro no
    # boot e sem teste pegando — a suíte usa client IDs fictícios.
    #
    # Por isso `tests/test_google_oauth.py::test_17` compara este valor com o do
    # `blaxx/assets/blaxx-config.js`. Mudou aqui, mude lá (e vice-versa).
    #
    # Nota: o client Web vive no projeto 602998235238 e o iOS no 105341431878.
    # Projetos diferentes é incomum, mas funciona — o backend aceita os dois
    # audiences, e o app iOS já publicado depende do seu.
    GOOGLE_WEB_CLIENT_ID_DEFAULT = (
        "602998235238-ab43odgkvqjph1l0tgu8n49iafgkrcke.apps.googleusercontent.com"
    )
    GOOGLE_IOS_CLIENT_ID_DEFAULT = (
        "105341431878-3msc2p3tjk3p5ro6i34d0b0qks3nf9dj.apps.googleusercontent.com"
    )
    GOOGLE_WEB_CLIENT_ID = (
        os.environ.get("GOOGLE_WEB_CLIENT_ID")
        or os.environ.get("GOOGLE_CLIENT_ID")
        or GOOGLE_WEB_CLIENT_ID_DEFAULT
    )
    GOOGLE_IOS_CLIENT_ID = (
        os.environ.get("GOOGLE_IOS_CLIENT_ID") or GOOGLE_IOS_CLIENT_ID_DEFAULT
    )

    # B-8: os client_ids acima são identificadores OAuth públicos — não são
    # segredo. O problema é serem fallback SILENCIOSO: sem a env var, o backend
    # aceita tokens emitidos para estes projetos e nada indica que a variável
    # faltou. Se o projeto Google for trocado e a env var não for setada, o
    # /auth/google continua validando contra o projeto ANTIGO, funcionando o
    # bastante para ninguém investigar.
    # Não dá para simplesmente remover: o app iOS já publicado depende deste
    # audience, e apagá-lo derruba o login de quem já instalou. Então o default
    # fica, e o boot grita — ver `__init__.py`.
    GOOGLE_CLIENT_IDS_EM_DEFAULT = [
        nome
        for nome, veio_do_ambiente in (
            ("GOOGLE_WEB_CLIENT_ID",
             bool(os.environ.get("GOOGLE_WEB_CLIENT_ID")
                  or os.environ.get("GOOGLE_CLIENT_ID"))),
            ("GOOGLE_IOS_CLIENT_ID", bool(os.environ.get("GOOGLE_IOS_CLIENT_ID"))),
        )
        if not veio_do_ambiente
    ]

    @classmethod
    def google_allowed_audiences(cls) -> list[str]:
        """Lista de audiences (client_ids) aceitos pelo /auth/google."""
        return [a for a in (cls.GOOGLE_WEB_CLIENT_ID, cls.GOOGLE_IOS_CLIENT_ID) if a]

    # T18: removido o esquema genérico de webhook PIX (PIX_WEBHOOK_SECRET,
    # PIX_WEBHOOK_ALLOWED_IPS) — desenhado para o Mercado Pago, nunca lido fora
    # dos helpers mortos que também saíram em pix.py. Cada provedor ativo tem
    # verificação própria: Asaas reconsulta a API antes de mexer no ledger
    # (webhook sem assinatura, só token estático), Stripe valida a assinatura
    # real do SDK.
    # Sprint 4 (S4-10) · Versao atual dos documentos legais.
    # Quando atualizar termos/privacidade/LGPD, bumpe TERMS_CURRENT_VERSION.
    # No proximo login, usuarios com user.terms_accepted_version != atual
    # serao redirecionados pra re-aceitar antes de continuar.
    # Convencao: "1.0", "1.1" (patch sem mudanca material), "2.0" (mudanca
    # material que exige re-aceite explicito).
    TERMS_CURRENT_VERSION = os.environ.get("BLAXX_TERMS_VERSION", "1.0")

    # ---------------- Níveis de cliente (loyalty tiers) ----------------
    # Nível por PONTOS ACUMULADOS (lifetime) = soma de todos os créditos
    # confirmados no ledger (nunca cai por gastar/resgatar). 4 categorias
    # progressivas. Faixas em pontos (min inclusivo). Sobrescrivível por env.
    #   Bronze 0+ · Prata 5.000+ · Ouro 20.000+ · Black 50.000+
    TIER_BRONZE_MIN = int(os.environ.get("BLAXX_TIER_BRONZE_MIN", 0))
    TIER_PRATA_MIN = int(os.environ.get("BLAXX_TIER_PRATA_MIN", 5_000))
    TIER_OURO_MIN = int(os.environ.get("BLAXX_TIER_OURO_MIN", 20_000))
    TIER_BLACK_MIN = int(os.environ.get("BLAXX_TIER_BLACK_MIN", 50_000))

    @classmethod
    def tiers(cls) -> list[dict]:
        """Definição canônica dos 4 níveis (ordem crescente)."""
        return [
            {"key": "bronze", "label": "Bronze", "min_points": cls.TIER_BRONZE_MIN,
             "color": "#CD7F32", "text_color": "#FFFFFF",
             "perks": "Acesso ao programa, catálogo de benefícios e PIX."},
            {"key": "prata", "label": "Prata", "min_points": cls.TIER_PRATA_MIN,
             "color": "#9AA0A6", "text_color": "#0B0B0C",
             "perks": "Ofertas exclusivas Prata + atendimento prioritário."},
            {"key": "ouro", "label": "Ouro", "min_points": cls.TIER_OURO_MIN,
             "color": "#D4AF37", "text_color": "#0B0B0C",
             "perks": "Bônus em campanhas + benefícios premium."},
            {"key": "black", "label": "Black", "min_points": cls.TIER_BLACK_MIN,
             "color": "#0B0B0C", "text_color": "#C6FF00",
             "perks": "Tudo do Ouro + experiências Black e limites VIP."},
        ]

    # BlaXx VIP — categoria FORA da escala por pontos (não é atingida
    # acumulando pontos). É concedida apenas por convite (admin seta is_vip).
    # Benefícios: compras de pontos SEM limite (vide services/purchase.py),
    # exchange preferencial e concierge. min_points é um sentinela alto só
    # para manter o tipo Int nos clientes (SwiftUI/JSON); a UI mostra
    # "Por convite" para a chave 'vip'.
    VIP_TIER = {
        "key": "vip", "label": "BlaXx VIP", "min_points": 999_999_999,
        "color": "#0A0A0A", "text_color": "#C6FF00", "invite_only": True,
        "perks": "Compras de pontos ilimitadas, exchange preferencial e "
                 "concierge dedicado — exclusivo, apenas por convite.",
    }

    @classmethod
    def tiers_catalog(cls) -> list[dict]:
        """Catálogo COMPLETO de categorias para exibição: os 4 níveis por
        pontos + BlaXx VIP (por convite) no topo."""
        return cls.tiers() + [cls.VIP_TIER]

    @classmethod
    def tier_for_points(cls, lifetime_points: int) -> dict:
        """Retorna o nível atual para um total de pontos acumulados."""
        current = cls.tiers()[0]
        for t in cls.tiers():
            if lifetime_points >= t["min_points"]:
                current = t
        return current

    @classmethod
    def tier_progress(cls, lifetime_points: int) -> dict:
        """Nível atual + próximo + quanto falta (pontos e %)."""
        tiers = cls.tiers()
        current = cls.tier_for_points(lifetime_points)
        idx = next(i for i, t in enumerate(tiers) if t["key"] == current["key"])
        nxt = tiers[idx + 1] if idx + 1 < len(tiers) else None
        if nxt is None:
            return {
                "lifetime_points": lifetime_points,
                "current": current, "next": None,
                "points_to_next": 0, "progress_pct": 100,
            }
        span = nxt["min_points"] - current["min_points"]
        gained = lifetime_points - current["min_points"]
        pct = 100 if span <= 0 else max(0, min(100, int(gained * 100 / span)))
        return {
            "lifetime_points": lifetime_points,
            "current": current, "next": nxt,
            "points_to_next": max(0, nxt["min_points"] - lifetime_points),
            "progress_pct": pct,
        }

    # ---------------- Apple Wallet (PassKit) ----------------
    # Geração do cartão Blaxx como .pkpass para a carteira do iPhone.
    # O .pkpass precisa ser ASSINADO com um certificado Pass Type ID emitido
    # pela Apple (conta Apple Developer). Enquanto os certificados não forem
    # configurados, o backend monta o pass mas NÃO assina — o endpoint
    # /card/pass responde 503 com instrução clara (frontends mostram "em breve").
    #
    # Para ativar, configure no Render (Environment) e suba os arquivos:
    #   APPLE_PASS_TYPE_ID        = pass.com.blaxx.cartao   (Identifier do Pass Type ID)
    #   APPLE_TEAM_ID             = ABCDE12345              (Apple Developer Team ID)
    #   APPLE_PASS_CERT_PATH      = /etc/secrets/pass.p12   (cert + chave privada, formato PKCS#12)
    #   APPLE_PASS_CERT_PASSWORD  = ********                (senha do .p12)
    #   APPLE_WWDR_CERT_PATH      = /etc/secrets/wwdr.pem   (Apple WWDR intermediate G4, PEM)
    #   APPLE_PASS_ORG_NAME       = Blaxx Pontos
    APPLE_PASS_TYPE_ID = os.environ.get("APPLE_PASS_TYPE_ID", "")
    APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", "")
    APPLE_PASS_CERT_PATH = os.environ.get("APPLE_PASS_CERT_PATH", "")
    APPLE_PASS_CERT_PASSWORD = os.environ.get("APPLE_PASS_CERT_PASSWORD", "")
    APPLE_WWDR_CERT_PATH = os.environ.get("APPLE_WWDR_CERT_PATH", "")
    APPLE_PASS_ORG_NAME = os.environ.get("APPLE_PASS_ORG_NAME", "Blaxx Pontos")

    @classmethod
    def apple_pass_configured(cls) -> bool:
        """True quando todos os segredos p/ assinar o .pkpass estão presentes."""
        return all([
            cls.APPLE_PASS_TYPE_ID, cls.APPLE_TEAM_ID,
            cls.APPLE_PASS_CERT_PATH, cls.APPLE_WWDR_CERT_PATH,
        ])

    # Pacotes — pts mantidos, precos recalculados ao novo rate (R$ 0,09/pt).
    # Plus/Prime/Black mantem um pequeno desconto progressivo embutido
    # (~5%/10%/15% de bonus implicito sobre o preco face).
    POINT_PACKAGES = {
        "start": {"price_brl": 180.00, "points": 2_000,  "label": "Start"},
        "plus":  {"price_brl": 470.00, "points": 5_500,  "label": "Plus"},   # ~5% bonus
        "prime": {"price_brl": 972.00, "points": 12_000, "label": "Prime"},  # ~10% bonus
        "black": {"price_brl": 2142.00, "points": 28_000, "label": "Black"},  # ~15% bonus
    }


class TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    TESTING = True
    SECRET_KEY = "test"
    JWT_SECRET_KEY = "test-jwt"
    RATELIMIT_ENABLED = False
    # Testes usam SOMENTE Bearer header para evitar que o cookie de sessão
    # "salve" requests com token adulterado (JWT_TOKEN_LOCATION dual causaria
    # fallback para cookie válido). Produção mantém dual-mode ['cookies','headers'].
    JWT_TOKEN_LOCATION = ["headers"]
    JWT_COOKIE_CSRF_PROTECT = False
