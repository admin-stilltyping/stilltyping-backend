import argparse
import asyncio
import json
from uuid import UUID, uuid4

from qdrant_client import models as qm
from sqlalchemy import select

from .channels import ADAPTERS
from .config import Settings
from .db import ChannelAccount, Database, KnowledgeUnit, embedding_text
from .models import Models
from .tools import sync_tools
from .vectors import Vectors


async def reconcile(db, models, vectors, tenant):
    # Canonical data controls visibility. Rebuild under the same lock used by mutations.
    async with db.transaction(tenant) as session:
        rows = list(
            (
                await session.scalars(
                    select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == tenant)
                )
            ).all()
        )
        for start in range(0, len(rows), 32):
            batch = rows[start : start + 32]
            embeddings = await models.embed([embedding_text(x.title, x.content) for x in batch])
            await vectors.upsert("knowledge_units", batch, embeddings)
            for row in batch:
                row.embedding_status = "ready"
        canonical = {row.id for row in rows}
        offset = None
        removed = 0
        while True:
            points, offset = await vectors.client.scroll(
                vectors.collection("knowledge_units"),
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=tenant))]
                ),
                offset=offset,
                limit=100,
                with_payload=False,
                with_vectors=False,
            )
            orphans = [point.id for point in points if UUID(str(point.id)) not in canonical]
            await vectors.delete("knowledge_units", orphans)
            removed += len(orphans)
            if offset is None:
                break
    return {"indexed": len(rows), "orphan_vectors_removed": removed}


async def register_channel(db, tenant, channel, account_id, config):
    async with db.transaction(tenant) as session:
        row = await session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.channel == channel, ChannelAccount.account_id == account_id
            )
        )
        if row is None:
            row = ChannelAccount(
                id=uuid4(),
                tenant_id=tenant,
                channel=channel,
                account_id=account_id,
                config=config,
            )
            session.add(row)
        else:
            row.tenant_id, row.config = tenant, config
    return {"tenant": tenant, "channel": channel, "account_id": account_id}


async def run(args):
    settings = Settings()
    db, vectors = Database(settings.database_url), Vectors(settings)
    models = None
    try:
        if args.command == "init-index":
            await vectors.initialize()
            result = {"initialized": True}
        elif args.command == "register-channel":
            result = await register_channel(
                db, args.tenant, args.channel, args.account_id, json.loads(args.config)
            )
        else:
            models = Models(settings)
            if args.command == "sync-tools":
                result = await sync_tools(db, models, vectors, force_reembed=args.reembed)
            else:
                result = await reconcile(db, models, vectors, args.tenant)
        print(json.dumps(result))
    finally:
        if models is not None:
            await models.close()
        await vectors.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Context Agent maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-index")
    sync = commands.add_parser("sync-tools")
    sync.add_argument("--reembed", action="store_true", help="Rebuild all tool vectors")
    repair = commands.add_parser("reconcile")
    repair.add_argument("--tenant", required=True)
    channel = commands.add_parser("register-channel", help="Route a messaging account to a tenant")
    channel.add_argument("--tenant", required=True)
    channel.add_argument("--channel", required=True, choices=sorted(ADAPTERS))
    channel.add_argument(
        "--account-id",
        required=True,
        help="WhatsApp phone_number_id, Instagram account id, or Telegram webhook secret",
    )
    channel.add_argument(
        "--config", required=True, help="JSON credentials object stored for this account"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
