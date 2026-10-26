# Requirements

The project helps people preserve, understand, connect and explore historical family records, and improve those interpretations without losing evidence or human work. Judge designs by representative workflows, evidence fidelity, disclosure control, recoverability and the cost of operating and changing the system.

These are intended outcomes, not a claim that every capability is implemented. Deliver useful capabilities incrementally; retaining a database need not wait for extraction development to finish. Make limitations explicit and prevent silent corruption or disclosure. [The current contract](CONTRACT.md) defines cross-cutting policy and outstanding implementation boundaries; [MODELING](MODELING.md) describes the implemented data conventions.

## R1 – Expressive, evolvable representation

Represent entities, relationships, time and uncertainty faithfully, with independent identity where it is useful. This does not require a separate node for every date or value. Ordinary vocabulary additions should preserve existing evidence; new semantics may require code changes or a migration. Unsupported information must remain visible as a limitation or inspectable proposal, rather than being silently discarded or coerced into a misleading form.

## R2 – Traceable evidence and interpretations

Trace historical assertions to their sources and reliable locations within them. Distinguish uncertainty expressed by a source from uncertainty in machine interpretation or identity matching. Genuine disagreements may coexist, including within one source, without requiring a canonical answer.

Use appropriate provenance for each kind of information: source assertions, curator attestations, derived conclusions and operational or schema metadata have different origins. Do not invent precision, citations or independent witnesses. A second machine reading of the same source is an interpretation, not independent corroboration.

## R3 – Automated, recoverable ingestion

Accept supported inputs as supplied and use deterministic processing or models as appropriate. Produce inspectable proposals with source and process provenance, and require scoped human authorization for retained results. Support retries, corrections, incremental additions and reprocessing without losing established identities or human work. Rebuilding a disposable database and extending a retained one are both enduring workflows.

## R4 – Accessible, accountable curation

Nontechnical contributors should be able to inspect evidence, add source information or an explicit personal attestation, correct transcription or attribution, review identities and recover from mistakes. Give these actions an intelligible audit trail. Core tasks must not require CLI or schema knowledge; bulk tools can supplement accessible interfaces.

## R5 – Confirmed face-region associations

Support detection and manual marking of photograph regions, and manual or automated proposals to associate those regions with people. Record how each region was created and who or what proposed and who confirmed each association. Every asserted face-to-person association (`depicts`) needs affirmative human confirmation. Several explicitly displayed associations may be confirmed together; general document or run approval must not silently approve faces. The detection or matching engine is replaceable.

## R6 – Exploration for authorized audiences

Provide timelines, image galleries, maps and other useful views that require no knowledge of the internal model. Begin with private access; disclosure to additional audiences and publication are separate, deliberate capabilities.

Make disagreement visible and attributed detail accessible without requiring a preferred value. Honest summaries, grouping, pagination and filters are permitted; silently manufacturing consensus is not.

## Privacy boundary

Existing privacy controls remain in force. A non-privileged export is not a public-safe copy of the archive. Final wording of the additional privacy requirement and its cutoff remains in [#70](https://github.com/WillHirsch/yizkr/issues/70) and [#72](https://github.com/WillHirsch/yizkr/issues/72); no change to disclosure permissions or the existing cutoff is implied here.
