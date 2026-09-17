import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_migration_preserves_existing_access_and_data():
    path = Path(__file__).parents[1] / "migrations/versions/011_business_modules.py"
    spec = importlib.util.spec_from_file_location("module_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("CREATE TABLE businesses (id CHAR(32) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE super_admins (id CHAR(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO businesses VALUES ('existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                row = connection.execute(
                    text(
                        "SELECT product_orders,service_appointments,customers,leads,support_tickets,revision FROM business_modules"
                    )
                ).one()
                assert row == (1, 1, 1, 0, 1, 1)
                with pytest.raises(IntegrityError):
                    connection.execute(text("UPDATE business_modules SET customers=false"))
                migration.downgrade()
                assert (
                    connection.execute(text("SELECT id FROM businesses")).scalar_one() == "existing"
                )
    finally:
        engine.dispose()
