from uuid import UUID

from sqlalchemy import func, select

from .db import KnowledgeUnit, Tool


def fuse(*rankings):
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), start=1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))


class Retriever:
    def __init__(self, models, vectors, settings):
        self.models, self.vectors, self.settings = models, vectors, settings

    async def search(self, session, tenant, query, kind="knowledge_units", vector=None, limit=None):
        table = KnowledgeUnit if kind == "knowledge_units" else Tool
        scopes = [tenant, "general"] if kind == "tools" else [tenant]
        limit = limit or (
            self.settings.knowledge_limit if kind == "knowledge_units" else self.settings.tool_limit
        )
        vector = vector if vector is not None else await self.models.query(query)
        dense = await self.vectors.search(kind, vector, tenant, self.settings.candidate_limit)
        base = select(table).where(table.tenant_id.in_(scopes), table.embedding_status == "ready")
        body = table.content if kind == "knowledge_units" else table.description
        title = table.title if kind == "knowledge_units" else table.name
        if session.bind.dialect.name == "postgresql":
            doc = func.to_tsvector("simple", title + " " + body)
            terms = func.websearch_to_tsquery("simple", query)
            lexical = list(
                (
                    await session.scalars(
                        base.where(doc.op("@@")(terms))
                        .order_by(func.ts_rank_cd(doc, terms).desc())
                        .limit(self.settings.candidate_limit)
                    )
                ).all()
            )
        else:  # Unit-test portability; deployed storage is PostgreSQL.
            lexical = list(
                (
                    await session.scalars(
                        base.where(body.ilike(f"%{query}%")).limit(self.settings.candidate_limit)
                    )
                ).all()
            )
        ids = fuse(dense, [str(row.id) for row in lexical])
        valid_ids = []
        for value in ids:
            try:
                valid_ids.append(UUID(value))
            except ValueError:
                continue
        rows = (
            list((await session.scalars(base.where(table.id.in_(valid_ids)))).all())
            if valid_ids
            else []
        )
        mapping = {str(row.id): row for row in rows}
        scores = await self.vectors.similarities(kind, vector, tenant, list(mapping))
        return [
            mapping[key]
            for key in ids
            if key in mapping and scores.get(key, -2) >= self.settings.relevance_threshold
        ][:limit]
