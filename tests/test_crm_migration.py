import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_crm_migration_enforces_tenant_relationships_and_downgrades():
    path = Path(__file__).parents[1] / "migrations/versions/012_crm_transactions.py"
    spec = importlib.util.spec_from_file_location("crm_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("CREATE TABLE businesses (id CHAR(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO businesses VALUES ('a'), ('b')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                connection.execute(
                    text(
                        "INSERT INTO crm_contacts (id, business_id, phone) VALUES ('contact-a', 'a', '+919876543210')"
                    )
                )
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "INSERT INTO customers (id, business_id, contact_id) VALUES ('bad', 'b', 'contact-a')"
                        )
                    )
                connection.execute(
                    text(
                        "INSERT INTO customers (id, business_id, contact_id) VALUES ('customer-a', 'a', 'contact-a')"
                    )
                )
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "INSERT INTO orders (id, business_id, customer_id, request_id, request_hash, reference, status, currency, total, items) VALUES ('bad-order', 'b', 'customer-a', 'r', 'h', 'ref', 'confirmed', 'INR', 1, '[]')"
                        )
                    )
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "INSERT INTO crm_contacts (id, business_id, phone) VALUES ('duplicate', 'a', '+919876543210')"
                        )
                    )
                connection.execute(
                    text(
                        "INSERT INTO crm_contacts (id, business_id, phone) VALUES ('contact-b', 'b', '+919876543210')"
                    )
                )
                assert "enquiries" in inspect(connection).get_table_names()
                migration.downgrade()
                assert inspect(connection).get_table_names() == ["businesses"]
                assert connection.execute(text("SELECT count(*) FROM businesses")).scalar_one() == 2
    finally:
        engine.dispose()
