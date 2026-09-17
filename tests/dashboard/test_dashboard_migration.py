import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from dashboard.models import DashboardConfig


def test_dashboard_migration_round_trip_matches_model():
    path = Path(__file__).parents[2] / "migrations/versions/008_dashboard_configs.py"
    spec = importlib.util.spec_from_file_location("dashboard_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = inspect(connection)
                columns = {
                    column["name"]: column for column in inspector.get_columns("dashboard_configs")
                }
                assert set(columns) == set(DashboardConfig.__table__.columns.keys())
                for column in DashboardConfig.__table__.columns:
                    assert columns[column.name]["nullable"] == column.nullable
                assert inspector.get_pk_constraint("dashboard_configs")["constrained_columns"] == [
                    "business_id"
                ]
                fk = inspector.get_foreign_keys("dashboard_configs")[0]
                assert (
                    fk["referred_table"] == "businesses" and fk["options"]["ondelete"] == "CASCADE"
                )
                migration.downgrade()
                assert "dashboard_configs" not in inspect(connection).get_table_names()
                migration.upgrade()
    finally:
        engine.dispose()
