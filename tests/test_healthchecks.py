"""Sprint 8 — testes dos healthchecks granulares (/healthz, /readyz, /metrics/health)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db


@pytest.fixture
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_healthz_returns_200_with_uptime(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert "uptime_s" in body
    assert "timestamp" in body


def test_livez_always_200(client):
    r = client.get("/livez")
    assert r.status_code == 200
    assert r.get_json() == {"alive": True}


def test_readyz_checks_db_and_routes(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ready"] is True
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["routes"] == "ok"
    assert body["checks"]["schema"] == "ok"


def test_readyz_acusa_coluna_faltando(app, client):
    """Código novo sem a migration: /readyz 503, o Render não promove."""
    from sqlalchemy import text
    from app.extensions import db
    with app.app_context():
        db.session.execute(text("ALTER TABLE pix_charges DROP COLUMN provider_ref"))
        db.session.commit()
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.get_json()["checks"]["schema"].startswith("FAIL")


def test_metrics_health_diagnostic(client):
    r = client.get("/metrics/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["service"] == "blaxx-pontos-backend"
    assert body["db"] == "ok"
    assert "providers" in body
    assert "pix" in body["providers"]


def test_metrics_endpoint_open_in_dev(client):
    """Em test/dev, /metrics é aberto (basic auth gated só em prod)."""
    r = client.get("/metrics")
    # 200 quando lib instalada, 503 se não estiver
    assert r.status_code in (200, 503)
