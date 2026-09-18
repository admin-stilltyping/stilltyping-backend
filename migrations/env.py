import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from appointments import models as appointment_models  # noqa: F401
from context_agent import notification_models  # noqa: F401
from context_agent.config import Settings
from context_agent.db import Base
from crm import models as crm_models  # noqa: F401
from custom_fields import models as custom_field_models  # noqa: F401 - register ORM metadata
from dashboard import models as dashboard_models  # noqa: F401 - register ORM metadata
from modules import models as module_models  # noqa: F401 - register ORM metadata
from orders import models as order_models  # noqa: F401
from products import models as product_models  # noqa: F401 - register ORM metadata
from public_chat import models as public_chat_models  # noqa: F401
from services import models as service_models  # noqa: F401
from super_admin import models as super_admin_models  # noqa: F401 - register ORM metadata
from super_admin.businesses import models as business_models  # noqa: F401 - register ORM metadata


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            # Serialize startup migrations across concurrently starting containers.
            connection.execute(text("SELECT pg_advisory_xact_lock(784330149221)"))
        context.run_migrations()


async def run():
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run())
