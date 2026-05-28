"""Diagnostico: compara model SQLAlchemy com o schema real do DB.

Uso:
    $env:DATABASE_URL = "postgresql://..."
    python scripts/check_schema.py

Detecta:
  - Colunas faltantes (model tem, DB nao tem) -> causa 500 ao INSERT
  - Colunas extras (DB tem, model nao mais)   -> warning, nao critico
  - Enum types faltando valores                -> causa 500 ao INSERT

Util pra diagnosticar 500 em prod apos refactor.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("ERRO: DATABASE_URL nao setada")
        return 1

    # Normaliza prefix
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    is_sqlite = url.startswith("sqlite")

    # Importa os models pra extrair schema esperado
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.extensions import db
    from app import models  # noqa: F401 — registra tudo

    expected_tables = db.metadata.tables  # nome -> Table object

    from sqlalchemy import create_engine, inspect, text
    engine = create_engine(url)
    insp = inspect(engine)

    print(f"Tipo de DB: {'SQLite' if is_sqlite else 'Postgres'}")
    print(f"Tabelas esperadas (model): {len(expected_tables)}")
    print(f"Tabelas reais (DB): {len(insp.get_table_names())}")
    print()

    errors = 0
    warnings = 0

    # === Compara cada tabela ===
    for table_name, table in sorted(expected_tables.items()):
        if not insp.has_table(table_name):
            print(f"  TABELA FALTA: {table_name}")
            errors += 1
            continue

        expected_cols = {c.name for c in table.columns}
        real_cols = {c["name"] for c in insp.get_columns(table_name)}

        missing = expected_cols - real_cols
        extra = real_cols - expected_cols

        if missing:
            print(f"  [ERRO] {table_name}: colunas faltando no DB")
            for c in sorted(missing):
                col = table.columns[c]
                print(f"      - {c} ({col.type})")
            errors += 1
        if extra:
            warnings += 1
            print(f"  [warn] {table_name}: colunas extras no DB (sem uso): {sorted(extra)}")

    # === Enums Postgres ===
    if not is_sqlite:
        print()
        print("=== Enums Postgres ===")
        # TxType esperado
        expected_txtype = {"purchase", "transfer_out", "transfer_in",
                           "redeem", "refund", "bonus", "expire"}
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT unnest(enum_range(NULL::txtype))::text"
                )).fetchall()
            actual_txtype = {r[0] for r in rows}
            missing_vals = expected_txtype - actual_txtype
            print(f"  txtype atual no DB: {sorted(actual_txtype)}")
            if missing_vals:
                print(f"  [ERRO] txtype falta valores: {sorted(missing_vals)}")
                print(f"  Aplique manualmente:")
                for v in sorted(missing_vals):
                    print(f"    ALTER TYPE txtype ADD VALUE IF NOT EXISTS '{v}';")
                errors += 1
            else:
                print(f"  [OK] txtype completo")
        except Exception as e:
            print(f"  [warn] nao foi possivel ler txtype: {e}")
            warnings += 1

    print()
    print(f"=== Resumo: {errors} erros, {warnings} warnings ===")
    if errors == 0:
        print("Schema OK. Se ainda da 500, problema esta em outro lugar (Sentry).")
    else:
        print("Schema desatualizado. Aplique os ALTERs/migrations sugeridos acima.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
