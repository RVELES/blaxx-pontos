"""Blaxx Pontos — backend Flask.

Módulo que entrega 3 funcionalidades centrais:
  1. Compra de pontos via PIX (cobrança PIX → webhook → crédito)
  2. Envio de pontos entre clientes inscritos (P2P interno)
  3. Resgate de pontos via PIX (payout PIX → débito)

A integração PIX é feita via interface abstrata (`app.pix.provider.PixProvider`)
com implementação mock (`app.pix.mock.MockPixProvider`) — pronta para ser
substituída por um provedor real (Mercado Pago, Asaas, Efí, Stark Bank, etc.)
sem mudar nenhuma regra de negócio.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

from .config import Config
from .extensions import db, jwt, limiter
from .pix.mock import MockPixProvider
from .pix.mercadopago import MercadoPagoPixProvider

# Pasta renderer/ do app Electron (relativo a app/__init__.py)
SITE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "renderer"))


def _is_production(app: "Flask | None" = None) -> bool:
    """True quando o app esta rodando em ambiente de producao.

    Producao = FLASK_ENV nao e' "development"/"test" E nao estamos em testes
    (TESTING/PYTEST_CURRENT_TEST/app.config['TESTING']/app.debug). Default
    conservador: se a variavel nao estiver setada E o app nao for explicito,
    assumimos producao (fail-safe pra rotas dev/debug).
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if app is not None:
        try:
            if app.config.get("TESTING") or app.debug:
                return False
        except Exception:
            pass
    env = (os.environ.get("FLASK_ENV") or "production").lower().strip()
    return env not in ("development", "test", "testing")


def _dev_endpoints_enabled() -> bool:
    """Gate explicito pra rotas /dev/* em prod. Default OFF.

    Em PROD as rotas /dev/* nem existem (retornam 404) a menos que
    ENABLE_DEV_ENDPOINTS=1 seja setada explicitamente.
    """
    return os.environ.get("ENABLE_DEV_ENDPOINTS", "0").strip() == "1"


def _init_sentry() -> None:
    """Sprint 3 (S3-8) · Sentry com PII scrubbing.

    Ativa apenas se SENTRY_DSN estiver setado. `before_send` remove dados
    sensiveis (email, CPF, telefone, password, token, JWT) antes de subir
    o evento. Pra rotacionar DSN, basta trocar a env var.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    # Guard de DSN: aceita só URLs válidas (https://key@host/project). Rejeita
    # placeholder como "https://", "<your-dsn-here>", "0" etc. SEM derrubar o
    # boot — em produção o app PRECISA subir mesmo com DSN mal configurado.
    if not dsn or not (dsn.startswith("https://") or dsn.startswith("http://")):
        return
    # Sanity adicional: tem que ter "@" (separador key/host)
    if "@" not in dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        # Sem sentry-sdk instalado — silencioso. Logging avisa no startup.
        return

    SENSITIVE = ("password", "token", "secret", "authorization", "jwt",
                 "cpf", "email", "phone", "pix_key", "mfa_code", "code",
                 "challenge", "id_token")

    def _scrub(d):
        if isinstance(d, dict):
            for k in list(d.keys()):
                lk = str(k).lower()
                if any(s in lk for s in SENSITIVE):
                    d[k] = "[scrubbed]"
                else:
                    d[k] = _scrub(d[k])
        elif isinstance(d, list):
            return [_scrub(x) for x in d]
        return d

    def before_send(event, hint):
        try:
            req = event.get("request") or {}
            for fld in ("data", "headers", "cookies", "query_string"):
                if fld in req:
                    req[fld] = _scrub(req[fld])
            user = event.get("user") or {}
            if user:
                # Mantem so o id (anonimo) — apaga email/username/ip
                event["user"] = {"id": user.get("id")} if user.get("id") else None
            extras = event.get("extra") or {}
            event["extra"] = _scrub(extras)
        except Exception:
            pass
        return event

    # Defesa em profundidade: qualquer erro de init (DSN malformado, transport
    # offline no boot, version mismatch da lib) NÃO pode derrubar o app. Sentry
    # é observability — falhar o boot porque o observer quebrou é o pior caso.
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("FLASK_ENV", "production"),
            release=os.environ.get("RELEASE_VERSION", "0.1.0"),
            integrations=[FlaskIntegration(), SqlalchemyIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.05")),
            send_default_pii=False,  # nunca mandar PII direto
            before_send=before_send,
        )
    except Exception as exc:
        # Log direto pro stderr — logger ainda não está configurado neste ponto.
        import sys
        print(f"[sentry] init falhou ({type(exc).__name__}): {exc} — seguindo sem Sentry",
              file=sys.stderr)


def create_app(config: type[Config] | None = None, pix_provider=None) -> Flask:
    # CI-3: valida envs ANTES de qualquer init pesado — falha cedo com mensagem
    # clara em vez de quebrar lá embaixo num import/init obscuro. Em dev/test
    # só warn no logger; em prod, EnvError derruba o boot (e Render Events
    # mostra a mensagem inteira no stderr).
    from .env_schema import validate_env
    issues = validate_env(strict=False)
    if issues:
        for i in issues:
            print(f"[env_schema] {i}", file=__import__("sys").stderr)
        if os.environ.get("FLASK_ENV", "production").lower() not in ("development", "test"):
            raise RuntimeError("Env validation failed (veja stderr) — recusando subir em prod.")

    # Sentry deve ser inicializado ANTES do Flask app pra capturar erros de boot
    _init_sentry()

    # Sprint 1-2 (P0): logging estruturado (JSON em prod, texto em dev).
    # Idempotente — chamadas repetidas em re-create_app sao no-op.
    try:
        from .logging_setup import init_logging, init_datadog_apm
        init_logging()
        init_datadog_apm()
    except Exception as exc:
        import sys
        print(f"[logging] init falhou ({type(exc).__name__}): {exc}",
              file=sys.stderr)

    # static_folder serve o QR PIX (e qualquer outro asset) em /static/*
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.from_object(config or Config)

    # Sprint 5 (S5-4) · Prometheus metrics middleware.
    # Idempotente: usa registry default; opt-in só se a lib estiver instalada.
    # `/metrics` exposto manualmente abaixo, protegido por basic auth via env vars
    # METRICS_USER / METRICS_PASS (sem env vars, 401 em prod; aberto em dev/test).
    # Em testes, criar app multiplas vezes pode tentar registrar o mesmo Gauge
    # ("Duplicated timeseries") — tratamos como warning, mantém o app sobindo.
    try:
        from prometheus_flask_exporter import PrometheusMetrics
        metrics = PrometheusMetrics(app, group_by="endpoint", path=None)
        try:
            metrics.info("blaxx_app_info", "BlaXx Pontos",
                         version=os.environ.get("RELEASE_VERSION", "dev"))
        except ValueError:
            # Re-create_app no mesmo processo (tests, hot reload) — Gauge ja
            # registrado no default registry. Não é problema.
            pass
        app.extensions["_prom_metrics"] = metrics
    except ImportError:
        app.logger.info("prometheus-flask-exporter não instalado — /metrics desligado")
        app.extensions["_prom_metrics"] = None

    # ─── Sprint 1 hardening: fail-fast em prod se secret ainda default ──
    # Se SECRET_KEY ou JWT_SECRET_KEY estao com o valor placeholder
    # "dev-only-change-me" e nao estamos em debug/test, recusamos subir
    # o app — melhor crashar no boot do que rodar em prod com chaves
    # publicas no codigo.
    _is_dev = bool(app.debug) or app.config.get("TESTING") \
              or os.environ.get("FLASK_ENV") == "development"
    if not _is_dev:
        _bad_secrets = []
        for key in ("SECRET_KEY", "JWT_SECRET_KEY"):
            val = app.config.get(key, "") or ""
            if not val or val.startswith("dev-only") or val in {"test", "test-jwt"}:
                _bad_secrets.append(key)
        if _bad_secrets:
            raise RuntimeError(
                "Recusando subir em producao: as variaveis "
                + ", ".join(_bad_secrets)
                + " ainda estao com valor default ('dev-only-change-me' ou vazio). "
                "Defina-as no ambiente antes de deployar."
            )
        # MAILER nao pode ser console em prod — loga codigo de verificacao
        # e token de reset no stdout (vai pro Render Dashboard). Forcar
        # provider real (resend/sendgrid/etc.) ou noop explicitamente.
        _mailer = (os.environ.get("MAILER") or "console").lower().strip()
        if _mailer == "console":
            raise RuntimeError(
                "Recusando subir em producao com MAILER=console (default). "
                "ConsoleMailer loga codigos de verificacao e tokens de reset "
                "no stdout. Defina MAILER=resend (+ RESEND_API_KEY) ou "
                "MAILER=noop pra ignorar emails."
            )

    db.init_app(app)
    jwt.init_app(app)

    # Onda 1 P0: callback que checa se JWT foi revogado (logout)
    # Sprint 1-2 (P0): + family-kill por user.password_changed_at;
    # + reuse-detection: se um refresh JTI ja revogado e' apresentado em
    #   /auth/refresh, bumpa password_changed_at do user (family kill).
    @jwt.token_in_blocklist_loader
    def _check_revoked(jwt_header, jwt_payload):
        from datetime import datetime as _dt, timezone as _tz
        from flask import request as _req
        from .models import RevokedToken, User

        jti = jwt_payload.get("jti")
        sub = jwt_payload.get("sub")
        iat = jwt_payload.get("iat")
        token_type = jwt_payload.get("type")

        # 1) JTI explicitamente revogado
        if jti and db.session.get(RevokedToken, jti) is not None:
            # Reuse-detection: refresh JTI ja revogado apresentado em /auth/refresh
            # → cliente esta tentando reutilizar refresh antigo (sinal de roubo).
            # Family-kill: bumpa password_changed_at pra invalidar TAMBEM o
            # refresh novo que foi emitido na rotacao anterior.
            try:
                is_refresh_call = token_type == "refresh" and \
                    _req is not None and _req.path == "/auth/refresh"
                if is_refresh_call and sub:
                    user = db.session.get(User, sub)
                    if user is not None:
                        user.password_changed_at = _dt.now(_tz.utc)
                        db.session.commit()
                        app.logger.warning(
                            "refresh token reuse detected — family killed "
                            "(user=%s jti=%s)", sub, (jti or "")[:16])
            except Exception:
                db.session.rollback()
                app.logger.exception("reuse-detection family-kill falhou")
            return True

        # 2) Family-kill por password_changed_at (logout global / reuse)
        try:
            if sub and iat is not None:
                user = db.session.get(User, sub)
                if user is not None and user.password_changed_at:
                    pwd_ts = user.password_changed_at
                    if pwd_ts.tzinfo is None:
                        pwd_ts = pwd_ts.replace(tzinfo=_tz.utc)
                    if int(pwd_ts.timestamp()) > int(iat):
                        return True
        except Exception:
            app.logger.exception("blocklist password_changed_at check falhou")
        return False

    # Rate limiter — apenas se habilitado (Config.RATELIMIT_ENABLED)
    if app.config.get("RATELIMIT_ENABLED", True):
        storage_uri = app.config["RATELIMIT_STORAGE_URI"]
        # Sprint 3 (S3-1): warning loud em prod com memory:// — sem Redis
        # cada worker tem seu proprio contador, atacante ganha cap x N.
        if not _is_dev and storage_uri.startswith("memory://"):
            app.logger.warning(
                "RATE LIMIT INSEGURO: RATELIMIT_STORAGE_URI=memory:// em "
                "producao. Com mais de 1 worker (gunicorn -w 2+), cada "
                "worker tem dict separado e o limite efetivo dobra. "
                "Configure RATELIMIT_STORAGE_URI=redis://... para garantir "
                "rate limit consistente."
            )
        limiter.storage_uri = storage_uri
        limiter.init_app(app)

    # CORS - libera o front (Netlify) chamar a API (Fly.io).
    # Garantia: sempre inclui o domínio Netlify mesmo se CORS_ORIGINS env var
    # estiver definido com lista parcial. Resolve "Failed to fetch" no Google
    # Login que vinha do preflight OPTIONS sendo bloqueado.
    # Lista canônica de origens que SEMPRE devem ser permitidas (independente
    # de CORS_ORIGINS env). Definida fora do if pra ficar acessível no fallback
    # quando supports_credentials=True não tolera "*".
    required_origins = {
        "https://blaxx-pontos-app.netlify.app",   # SPA web em produção (atual)
        "https://blaxxpontos.netlify.app",         # domínio Netlify legado
        "https://blaxxpontos.com.br",      # domínio próprio (apex) — produção
        "https://www.blaxxpontos.com.br",  # domínio próprio (www) — produção
        "https://blaxxpontos.com",       # variação .com (caso usada)
        "https://www.blaxxpontos.com",
        "http://localhost:5173",          # Vite dev server (blaxx-spa)
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    }
    configured_origins = app.config.get("CORS_ORIGINS") or []
    if configured_origins == ["*"]:
        # SEC-1: CORS proíbe "*" quando supports_credentials=True (browser
        # rejeita o preflight). Caímos pra lista explícita.
        origins_setting = sorted(required_origins)
    else:
        merged = sorted(set(configured_origins) | required_origins)
        origins_setting = merged
    # SEC-1: supports_credentials=True permite que o navegador envie/receba
    # o cookie httpOnly `blaxx_session`. Origens TÊM que ser explícitas.
    # Idempotency-Key adicionado a allow_headers pra clientes enviarem o
    # header em /redeem e /transfer (vide api/redeem.py e api/transfer.py).
    CORS(
        app,
        resources={r"/*": {"origins": origins_setting}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With",
                       "X-Request-ID", "Idempotency-Key",
                       # Sprint 1-2 (P0): SPA web ecoa csrf_access_token cookie aqui
                       "X-CSRF-TOKEN", "X-CSRF-Token"],
        expose_headers=["X-Request-ID"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        max_age=600,
    )

    # ─── Headers de segurança (Onda 2 — Spec do user, seção 6) ──────────
    # Usa middleware manual (after_request). É mais portátil e nunca quebra
    # o startup — Flask-Talisman tem assinatura de kwargs que muda entre
    # versões (TypeError em x_frame_options/x_content_type_options se versão
    # antiga). O fallback abaixo cobre os essenciais sem dependência externa.
    @app.after_request
    def _security_headers(resp):
        from flask import request as _req
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=(), payment=()")
        # CSP: estrito por default, com excecao pro /docs/ que precisa
        # carregar Swagger UI do CDN jsdelivr.
        if _req.path.startswith("/docs"):
            csp = ("default-src 'self' https://cdn.jsdelivr.net; "
                   "img-src 'self' data:; "
                   "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                   "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                   "frame-ancestors 'none'")
        else:
            csp = "default-src 'none'; frame-ancestors 'none'"
        resp.headers.setdefault("Content-Security-Policy", csp)
        return resp

    # PIX provider — Sprint 3: seleção via env var PIX_PROVIDER
    if pix_provider is None:
        provider_name = app.config.get("PIX_PROVIDER", "mock").lower()
        if provider_name == "mercadopago":
            mp_token = app.config.get("MP_ACCESS_TOKEN", "")
            if not mp_token:
                app.logger.error(
                    "PIX_PROVIDER=mercadopago mas MP_ACCESS_TOKEN está vazio. "
                    "Caindo no MockPixProvider."
                )
                pix_provider = MockPixProvider()
            else:
                pix_provider = MercadoPagoPixProvider(
                    access_token=mp_token,
                    notification_url=app.config.get("MP_NOTIFICATION_URL") or None,
                )
                app.logger.info("PIX provider: MercadoPago")
        else:
            pix_provider = MockPixProvider()
            app.logger.info("PIX provider: Mock (demo)")
    app.extensions["pix_provider"] = pix_provider

    # Blueprints
    from .api.auth import bp as auth_bp
    from .api.wallet import bp as wallet_bp
    from .api.card import bp as card_bp
    from .api.pix import bp as pix_bp
    from .api.transfer import bp as transfer_bp
    from .api.redeem import bp as redeem_bp
    from .api.partners import bp as partners_bp
    from .api.benefits import bp_benefits, bp_vouchers
    from .api.campaigns import bp as campaigns_bp
    from .api.notifications import bp as notifications_bp
    from .api.docs import bp as docs_bp
    from .api.admin import bp as admin_bp
    from .api.security import bp as security_bp, register_login_2fa_route
    from .api.push import bp as push_bp
    from .api.privacy import bp as privacy_bp

    # Onda 3 — registra /auth/login/2fa NO blueprint auth_bp existente
    # (mantém prefix /auth/* coerente, sem precisar criar segundo blueprint).
    register_login_2fa_route(auth_bp)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(wallet_bp, url_prefix="/wallet")
    app.register_blueprint(card_bp, url_prefix="/card")
    app.register_blueprint(pix_bp, url_prefix="/pix")
    app.register_blueprint(transfer_bp, url_prefix="/transfer")
    app.register_blueprint(redeem_bp, url_prefix="/redeem")
    app.register_blueprint(partners_bp, url_prefix="/partners")
    app.register_blueprint(bp_benefits, url_prefix="/benefits")
    app.register_blueprint(bp_vouchers, url_prefix="/vouchers")
    app.register_blueprint(campaigns_bp, url_prefix="/campaigns")
    app.register_blueprint(notifications_bp, url_prefix="/notifications")
    app.register_blueprint(push_bp, url_prefix="/push")
    app.register_blueprint(privacy_bp, url_prefix="/privacy")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    # Onda 3 — telefone + 2FA SMS + sessões + access-log
    app.register_blueprint(security_bp, url_prefix="/user")
    # Sprint 5 (S5-5) — Swagger UI servindo openapi.yaml
    app.register_blueprint(docs_bp, url_prefix="/docs")

    with app.app_context():
        # Sprint 1-2 (P0): em PROD, NAO rodar create_all() nem o auto-ALTER
        # do _apply_lightweight_migrations. Schema em prod e' gerenciado
        # exclusivamente por `alembic upgrade head` no preDeployCommand do
        # Render. Em dev/test mantemos pra DX rapida (sem precisar rodar
        # alembic em todo `flask run`).
        if _is_production(app):
            app.logger.info(
                "Production: skipping db.create_all() and auto-ALTER — "
                "run `alembic upgrade head` separately (preDeployCommand)."
            )
        else:
            db.create_all()
            _apply_lightweight_migrations(app)
        _autoseed_partners_if_empty(app)

    # ----- Healthchecks (Wave 6 — robustez operacional) ---------------------
    # /health    → resposta rica: uptime, versao, timestamp. Usado pelo Render
    #              health check, monitores externos, banner de offline do frontend.
    # /healthz   → alias minimalista (convencao Kubernetes/cloud). Mesmo handler.
    # /livez     → liveness probe — o processo esta vivo? (sempre 200 se servir)
    # /readyz    → readiness probe — pronto pra receber trafego? checa DB rapido.
    #
    # Todos sao publicos (sem auth), sem rate limit, e nao tocam recursos caros.
    # Custo: <2ms cada. Safe pra monitor externo pingar a cada 1min.
    _process_start_ts = datetime.now(timezone.utc)

    @app.get("/health")
    @app.get("/healthz")
    def health():
        uptime_s = int((datetime.now(timezone.utc) - _process_start_ts).total_seconds())
        return {
            "status": "ok",
            "service": "blaxx-pontos-backend",
            "uptime_s": uptime_s,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/livez")
    def livez():
        # Liveness — so confirma que o processo Python esta respondendo.
        # NUNCA falha — se o handler executou, o processo esta vivo.
        return {"alive": True}

    @app.get("/readyz")
    def readyz():
        # Readiness "esperta" — Render Pro usa esse endpoint pra decidir se
        # promove o novo processo. Devolve 503 se qualquer dependência crítica
        # estiver fora; assim deploy quebrado fica em loop sem trocar tráfego.
        #
        # Checks (rápidos, idempotentes):
        #   1) DB acessível (SELECT 1) — sem DB não vale nada
        #   2) Rotas críticas registradas — pega regressão silenciosa (caso
        #      de blueprint que não foi importado por algum motivo)
        #   3) JWT manager configurado — sem isso, login quebra silenciosamente
        checks: dict[str, str] = {}
        ok = True

        # 1) DB
        try:
            from sqlalchemy import text
            db.session.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:
            ok = False
            checks["db"] = f"FAIL: {type(e).__name__}"

        # 2) Rotas críticas (verifica blueprints carregados)
        rules = {str(r) for r in app.url_map.iter_rules()}
        REQUIRED = {"/health", "/auth/login", "/auth/me", "/auth/logout",
                    "/auth/refresh", "/push/subscribe", "/redeem/", "/transfer/"}
        missing = REQUIRED - rules
        if missing:
            ok = False
            checks["routes"] = f"FAIL: missing {sorted(missing)}"
        else:
            checks["routes"] = "ok"

        # 3) JWT manager
        try:
            assert jwt is not None and hasattr(jwt, "_decode_jwt_from_config")
            checks["jwt"] = "ok"
        except Exception as e:
            ok = False
            checks["jwt"] = f"FAIL: {type(e).__name__}"

        body = {"ready": ok, "checks": checks}
        return (body, 200) if ok else (body, 503)

    # ----- /metrics protegido (Sprint 5) -----
    @app.get("/metrics")
    def prometheus_metrics():
        """Exposição Prometheus protegida por basic auth.

        Sem METRICS_USER/METRICS_PASS configurado → 401 em prod; aberto em
        dev/test. Em prod, configure essas envs e adicione no Prometheus
        scrape_configs: basic_auth: { username, password }.
        """
        from flask import Response, request as _req
        prom = app.extensions.get("_prom_metrics")
        if prom is None:
            return jsonify({"error": "metrics_disabled"}), 503

        # Auth (skip em dev/test pra DX)
        _is_dev_local = (bool(app.debug) or app.config.get("TESTING")
                         or os.environ.get("FLASK_ENV") == "development"
                         or os.environ.get("PYTEST_CURRENT_TEST"))
        if not _is_dev_local:
            expected_user = os.environ.get("METRICS_USER", "").strip()
            expected_pass = os.environ.get("METRICS_PASS", "").strip()
            if not expected_user or not expected_pass:
                return jsonify({"error": "metrics_auth_not_configured"}), 401
            auth = _req.authorization
            if (not auth or auth.username != expected_user
                    or auth.password != expected_pass):
                return Response(
                    "Unauthorized", 401,
                    {"WWW-Authenticate": 'Basic realm="metrics"'},
                )

        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
        except Exception as exc:
            app.logger.exception("falha ao gerar metrics: %s", exc)
            return jsonify({"error": "metrics_generation_failed"}), 500

    # ----- /metrics/health (Sprint 8) — diagnóstico JSON detalhado -----
    @app.get("/metrics/health")
    def health_metrics_detail():
        uptime_s = int((datetime.now(timezone.utc) - _process_start_ts).total_seconds())
        diag: dict = {
            "service": "blaxx-pontos-backend",
            "version": os.environ.get("RELEASE_VERSION", "0.1.0"),
            "uptime_s": uptime_s,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "providers": {},
        }
        try:
            from sqlalchemy import text
            db.session.execute(text("SELECT 1"))
            diag["db"] = "ok"
        except Exception as e:
            diag["db"] = f"FAIL: {type(e).__name__}"
        try:
            from .services import push as push_svc
            diag["providers"]["push"] = push_svc.provider_status()
        except Exception:
            diag["providers"]["push"] = "error"
        diag["providers"]["pix"] = app.extensions["pix_provider"].name
        diag["providers"]["mailer"] = (os.environ.get("MAILER") or "console").lower()
        diag["providers"]["sentry"] = bool(os.environ.get("SENTRY_DSN", "").startswith("http"))
        return jsonify(diag)

    # ----- Servir o frontend (renderer/) na mesma origem (modo web, sem Electron) -----
    @app.get("/app/")
    def app_root():
        return send_from_directory(SITE_DIR, "index.html")

    @app.get("/app/<path:filename>")
    def app_file(filename: str):
        return send_from_directory(SITE_DIR, filename)

    # Aliases curtos para compatibilidade
    @app.get("/site/")
    def site_root():
        return send_from_directory(SITE_DIR, "index.html")

    @app.get("/site/<path:filename>")
    def site_file(filename: str):
        return send_from_directory(SITE_DIR, filename)

    # ----- Servir o prototipo /blaxx/ (Netlify) no mesmo backend -----
    BLAXX_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "blaxx"))

    @app.get("/blaxx/")
    def blaxx_root():
        return send_from_directory(BLAXX_DIR, "index.html")

    @app.get("/blaxx/<path:filename>")
    def blaxx_file(filename: str):
        return send_from_directory(BLAXX_DIR, filename)

    @app.get("/")
    def index():
        """Endpoint raiz.

        Em PROD retorna so {service, version, status} — NUNCA vaza rotas
        nem credenciais demo (Sprint 1 hardening). Em DEV/TEST mantem
        listagem completa pra ajudar debug local.
        """
        from flask import jsonify
        is_dev = bool(app.debug) or app.config.get("TESTING") \
                 or os.environ.get("FLASK_ENV") == "development"

        if not is_dev:
            return jsonify({
                "service": "blaxx-pontos-backend",
                "version": "0.1.0",
                "status": "ok",
            })

        return jsonify({
            "service": "blaxx-pontos-backend",
            "version": "0.1.0",
            "mode": "development",
            "pix_provider": app.extensions["pix_provider"].name,
            "endpoints": {
                "GET  /health":                  "healthcheck",
                "POST /auth/register":           "criar conta (name, email, cpf, password)",
                "POST /auth/login":              "login (email|cpf + password) -> Bearer token",
                "GET  /auth/me":                 "perfil do usuario logado",
                "GET  /wallet/":                 "saldo da carteira",
                "GET  /wallet/transactions":     "extrato (parametros: ?limit=N)",
                "GET  /pix/packages":            "pacotes de pontos disponiveis",
                "POST /pix/charge":              "criar cobranca PIX para comprar pontos",
                "POST /pix/simulate-payment":    "[mock] forcar pagamento de uma charge",
                "POST /transfer/":               "enviar pontos a outro cliente (P2P)",
                "GET  /redeem/quote":            "cotar resgate (parametro: ?points=N)",
                "POST /redeem/":                 "solicitar resgate via PIX",
                "GET  /partners/":               "lista de parceiros (?category, ?q)",
                "GET  /partners/categories":     "categorias disponiveis",
                "GET  /partners/<id>":           "detalhe do parceiro + beneficios",
                "GET  /benefits/":               "catalogo de beneficios",
                "GET  /benefits/<id>":           "detalhe do beneficio",
                "POST /benefits/<id>/redeem":    "resgatar beneficio (gera voucher)",
                "GET  /vouchers/":               "vouchers do usuario logado",
                "GET  /vouchers/<id>":           "detalhe do voucher",
                "GET  /campaigns/":              "campanhas ativas",
                "POST /campaigns/<id>/join":     "aderir a uma campanha",
                "POST /campaigns/<id>/progress": "[demo] avancar progresso (amount_brl)",
                "GET  /campaigns/mine":          "campanhas que aderi",
                "GET  /notifications/":          "minhas notificacoes (in-app)",
                "GET  /notifications/unread-count": "contador de nao lidas",
                "PATCH /notifications/<id>/read":  "marcar como lida",
                "POST /notifications/read-all":  "marcar todas como lidas",
            },
            "demo_users": [
                {"email": "mariana@blaxx.com", "password": "123456", "balance_pts": 84750},
                {"email": "lucas@blaxx.com",   "password": "123456", "balance_pts":  5000},
            ],
            "tip": "Para testar todos os fluxos de uma vez, rode 'testar-fluxos.bat'",
        })

    return app
def _apply_lightweight_migrations(app):
    """Stub: ALTER TABLE no startup. Substituir por Alembic.

    `db.create_all()` só cria TABELAS que faltam — ele NUNCA faz ALTER pra
    adicionar COLUNAS novas a uma tabela já existente. Quando o model ganha
    colunas (ex.: google_sub, phone, mfa_*, status...) e o Postgres de prod
    foi criado antes delas, todo `SELECT users.<coluna>` quebra com
    "column does not exist" → 500 em login/forgot-password/etc.

    `_sync_model_columns` resolve isso de forma genérica e idempotente:
    compara cada tabela do metadata com as colunas reais e adiciona o que
    faltar (com DEFAULT+NOT NULL correto quando a coluna é obrigatória).
    """
    with app.app_context():
        try:
            db.create_all()
            _sync_model_columns(app)
        except Exception:
            app.logger.exception("_apply_lightweight_migrations falhou")


def _sync_model_columns(app):
    """Adiciona colunas faltantes (model x tabela real) via ALTER TABLE.

    Idempotente: só adiciona o que não existe. Seguro em tabela populada:
    colunas NOT NULL recebem um DEFAULT de servidor pra backfill das linhas
    antigas; quando não dá pra inferir um default seguro, a coluna entra
    como NULLABLE (preferimos um login funcionando a um schema 100% fiel).
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.types import Boolean, Integer, DateTime, Date

    insp = inspect(db.engine)
    dialect = db.engine.dialect

    def _server_default_sql(col):
        """SQL literal pro DEFAULT de uma coluna NOT NULL, ou None."""
        d = col.default
        # Default escalar (não-callable): usa o valor literal.
        if d is not None and getattr(d, "is_scalar", False):
            val = d.arg
            if isinstance(val, bool):
                return "TRUE" if val else "FALSE"
            if isinstance(val, (int, float)):
                return str(val)
            if isinstance(val, str):
                return "'" + val.replace("'", "''") + "'"
        # Default callable (ex.: _utcnow/_new_uuid) ou ausente: inferimos
        # por tipo pra conseguir backfillar linhas existentes.
        t = col.type
        if isinstance(t, Boolean):
            return "FALSE"
        if isinstance(t, Integer):
            return "0"
        if isinstance(t, (DateTime, Date)):
            return "CURRENT_TIMESTAMP"
        return None

    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            try:
                col_type = col.type.compile(dialect=dialect)
            except Exception:
                # Tipo não compilável neste dialeto — pula com aviso.
                app.logger.warning(
                    "migration skip (tipo) %s.%s", table.name, col.name
                )
                continue
            base_ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}'
            ddl = base_ddl
            if not col.nullable:
                default_sql = _server_default_sql(col)
                if default_sql is not None:
                    ddl += f" DEFAULT {default_sql} NOT NULL"
                # Sem default seguro → deixa NULLABLE (não acrescenta NOT NULL)
                # pra não falhar o ALTER numa tabela com linhas antigas.
            try:
                db.session.execute(text(ddl))
                db.session.commit()
                app.logger.info("migration add column: %s", ddl)
            except Exception as e:
                db.session.rollback()
                # Fallback: se o ALTER com NOT NULL/DEFAULT falhar (quirk de
                # dialeto, ex.: SQLite não aceita DEFAULT não-constante),
                # tenta a versão NULLABLE simples — o importante é a coluna
                # PASSAR a existir, pra o SELECT parar de dar 500.
                if ddl != base_ddl:
                    try:
                        db.session.execute(text(base_ddl))
                        db.session.commit()
                        app.logger.info(
                            "migration add column (nullable fallback): %s",
                            base_ddl,
                        )
                        continue
                    except Exception as e2:
                        db.session.rollback()
                        e = e2
                app.logger.warning("migration skip (%s): %s", e, ddl)


def _autoseed_partners_if_empty(app):
    """Auto-seed dos 258 parceiros Livelo se a tabela Partner estiver vazia.

    Motivacao: o Render free tier pode wipear SQLite em restarts (disco
    efemero) e o DATABASE_URL pode apontar pra um Postgres recem-criado
    sem dados. Em ambos os casos, o site sobe SEM parceiros — UI fica
    vazia, parece bug. Auto-seed resolve isso de forma idempotente.

    Idempotencia: so roda se Partner.count() == 0. Em DB ja populado
    (Neon prod com dados reais), nao faz nada — sem custo.

    O arquivo data/livelo_partners.json esta versionado no repo, entao
    chega no Render junto com o codigo.
    """
    import json
    import os as _os
    from .models import Partner

    try:
        existing_count = db.session.query(Partner).count()
        if existing_count > 0:
            return  # ja tem parceiros, nao re-seedar

        json_path = _os.path.join(
            _os.path.dirname(__file__), "..", "data", "livelo_partners.json"
        )
        if not _os.path.exists(json_path):
            app.logger.warning("auto-seed: livelo_partners.json nao encontrado em %s", json_path)
            return

        # Mapa emoji por categoria (mesmo do seed_livelo.py)
        emoji_by_cat = {
            "Moda":"👗","Viagens":"✈️","Beleza":"💄","Seguros":"🛡️",
            "Esportes":"⚽","Eletrônicos":"📱","Consórcio":"🏦",
            "Supermercado":"🛒","Casa & Decoração":"🏠","Alimentação":"🍴",
            "Saúde":"⚕️","Educação":"🎓","Pet Shop":"🐾","Combustível":"⛽",
            "Farmácias":"💊","Streaming":"📺","E-commerce":"📦",
            "Restaurantes":"🍽️","Cartões":"💳","Bancos":"🏦",
            "Telecom":"📞","Outros":"🎯",
        }

        with open(json_path, "r", encoding="utf-8") as f:
            partners_data = json.load(f)

        created = 0
        for p in partners_data:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            category = (p.get("category") or "Outros").strip()
            partner = Partner(
                name=name,
                category=category,
                logo_emoji=emoji_by_cat.get(category, "🎯"),
                accrual_rule=(p.get("accrual_rule") or "Pontos por R$").strip(),
                description=(p.get("description") or "")[:300],
                is_active=True,
            )
            db.session.add(partner)
            created += 1
        db.session.commit()
        app.logger.info("[AUTO-SEED] %d parceiros Livelo importados", created)
    except Exception:
        db.session.rollback()
        app.logger.exception("auto-seed de parceiros falhou — site sobe sem dados")
