# Decision: code-defined tools and explicit synchronization

## Scope and ownership

This platform is an agent combining an LLM, matching knowledge units, and matching tools. RAG is one component. The project is named Context Agent and lives in /Users/gokul/Documents/context-agent.

Our developers define and implement tools in application code based on customer requirements. Customers do not create or edit tools. After coding or changing tools, developers explicitly run a sync script to synchronize definitions with PostgreSQL and embeddings with Qdrant.

Code is the source of truth for tools. Executable implementations remain in the application. PostgreSQL stores synchronized definitions for runtime loading, and Qdrant stores their searchable representations. No customer-facing tool-management API is required.

## Tool definitions

Each code definition includes:

- name: function name exposed to the LLM.
- description: capability and when to use it.
- parameters_schema: input JSON schema.
- handler_key: key in the registered backend handler mapping.
- tenant_id: general for shared tools, or the specific customer ID.

## Sync workflow

```text
Develop tool in code
    -> run sync script
    -> insert/update PostgreSQL tool definition
    -> embed new or changed tool discovery text
    -> upsert Qdrant point using stable tool_id
```

- Create missing records, update changed definitions, and preserve existing IDs.
- Embed name plus capability description.
- Reuse vectors when embedding text is unchanged.
- Implementation-only changes do not require re-embedding.
- Schema changes sync to PostgreSQL; re-embedding is needed only if embedding text changes too.
- Track pending, ready, and failed indexing with embedding_status. Retry incomplete indexing even if the definition has not changed.
- Change-detection mechanics and handling tools removed from the registry remain implementation decisions.

## Runtime

Embed the retrieval query, search relevant tool vectors, load matching schemas from PostgreSQL, and provide them to the LLM. Resolve handler_key using an explicit code registry and execute with application-supplied tenant context.

Search includes shared tools plus current-tenant tools. The support-ticket fallback stays available independently of semantic retrieval.

## Embedding lifecycle across the agent

| Trigger | What is embedded | Timing |
|---|---|---|
| PUT document | Titles and content of knowledge units generated from the summary | After knowledge generation, before API success |
| POST knowledge-units | New knowledge-unit titles and content | Before API success |
| PATCH knowledge-units | Only units whose embedding text changed | After preparing the change, before API success |
| Run tool sync | New/changed tool name and capability description | During explicit synchronization |
| Agent message | Retrieval query derived from the user message | Before searching knowledge and tools |
| Natural-language knowledge update | Query representing the requested change | Before retrieving candidate knowledge units |

The summary is first converted by the LLM into self-contained knowledge units. We embed these units, not the entire summary as one vector.

Stored knowledge and tool vectors are reused at runtime. A query vector can be reused for both collections if their embedding model, dimensions, and query configuration are compatible. Otherwise generate the corresponding query embedding for each collection.

Whole-document replacement prepares new knowledge and vectors before switching from the old contents. Changing embedding models or configuration requires rebuilding affected stored vectors.
