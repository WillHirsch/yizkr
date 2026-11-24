# ADR-0001: Self-hosted Wikibase Suite as the core knowledge store

Status: accepted. The [current contract](../CONTRACT.md) governs product and engineering policy; this ADR describes the working storage choice and its limits.

## Decision

Use self-hosted Wikibase Suite as the authoritative store of accepted assertions. MediaWiki supplies editing and revision history, Wikibase supplies statements with references, qualifiers and ranks, and the query service supplies SPARQL reads. The Python package supplies ingestion, review tooling and exploration views.

This is the working implementation, not a requirement that every source inventory, proposal, approval record, response cache or job record live in Wikibase. Those records have distinct authority and lifecycle requirements. There must be one authoritative representation of accepted assertions, with explicit ownership of the other state.

## Rationale

- Statements can retain attributed, conflicting values without requiring a preferred value.
- The statement editor, OpenRefine and review CSVs provide useful curation tools. Their availability does not by itself establish that a nontechnical contributor can complete the required curation tasks.
- SPARQL and RDF export support exploration and portability.
- Properties and classes can be added in-instance. New matching, validation or presentation semantics may require code and a documented migration; extensibility does not promise arbitrary new behavior without implementation.
- Self-hosting gives the owner control of access and operation for an archive containing living people's information. It does not make an export suitable for every audience.

## Implementation limits

The stack requires several services and operational maintenance. Artifact regions use the conventions in [ADR-0003](0003-statement-provenance.md); the exploration and face flows use the components described in [ADR-0004](0004-phased-architecture.md) and [ADR-0005](0005-faces-strategy.md).

The map-bound writer still uses one identity map per instance; it finds a cluster's item through its durable creation binding (`core.write_recovery.CreationRegistry`) and adopts no item from a label or description. The [ingestion contract](../INGESTION_CONTRACT.md) defines stable identity. The single write path, in which the assignment writer alone writes retained data and the map-bound writer is retired, is the target of [#822](https://github.com/WillHirsch/yizkr/issues/822), and reviewed correction and split remain under [#142](https://github.com/WillHirsch/yizkr/issues/142); adopting the contract does not implement them. No retained map-written graph exists to migrate ([current contract](../CONTRACT.md#rebuild-and-retained-operation)). Keep the current guards until their supported replacements are verified. A platform replacement is outside this cleanup.
