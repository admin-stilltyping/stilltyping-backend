import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from super_admin.models import SuperAdmin


def test_super_admin_migration_matches_model_and_round_trips():
    path = Path(__file__).parents[2] / "migrations/versions/006_super_admins.py"
    spec = importlib.util.spec_from_file_location("super_admin_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = inspect(connection)
                columns = {c["name"]: c for c in inspector.get_columns("super_admins")}
                assert set(columns) == set(SuperAdmin.__table__.columns.keys())
                assert columns["password_hash"]["nullable"] is False
                assert columns["locked_until"]["nullable"] is True
                assert inspector.get_unique_constraints("super_admins")[0]["column_names"] == [
                    "username"
                ]
                migration.downgrade()
                assert "super_admins" not in inspect(connection).get_table_names()
                migration.upgrade()
                assert "super_admins" in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_business_migration_matches_models_and_constraints():
    from super_admin.businesses.models import Business, BusinessAdmin

    path = Path(__file__).parents[2] / "migrations/versions/007_businesses.py"
    spec = importlib.util.spec_from_file_location("business_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = inspect(connection)
                for model in [Business, BusinessAdmin]:
                    columns = {c["name"]: c for c in inspector.get_columns(model.__tablename__)}
                    assert set(columns) == set(model.__table__.columns.keys())
                    for column in model.__table__.columns:
                        assert columns[column.name]["nullable"] == column.nullable
                assert inspector.get_unique_constraints("businesses")[0]["column_names"] == ["slug"]
                admin_unique = {
                    tuple(c["column_names"])
                    for c in inspector.get_unique_constraints("business_admins")
                }
                assert admin_unique == {("username",), ("business_id",)}
                fk = inspector.get_foreign_keys("business_admins")[0]
                assert (
                    fk["referred_table"] == "businesses" and fk["options"]["ondelete"] == "CASCADE"
                )
                migration.downgrade()
                assert not set(inspect(connection).get_table_names()) & {
                    "businesses",
                    "business_admins",
                }
                migration.upgrade()
    finally:
        engine.dispose()
