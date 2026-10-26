# ADR-0004: Incremental architecture and optional companion services

Status: accepted. The [current contract](../CONTRACT.md) governs delivery, retained state and audience policy.

## Working architecture

Artifacts are wiki media on the self-hosted instance. Wikibase statements carry source and process references, and image regions use percentage bounding boxes. Python ingestion produces proposals, review CSVs carry decisions, and the writer applies supported changes. Query templates and the static site provide person pages, timelines, galleries and maps. [ARCHITECTURE.md](../ARCHITECTURE.md) shows the implemented components.

The archive is private by default and exploration serves authorized readers. Publishing a site or sharing an export requires a deliberate audience and disclosure decision. The existing non-privileged export is not automatically suitable for public release. Views may summarize or filter when their scope is clear, material disagreements remain visible and the underlying attributed evidence is accessible.

## Independent capabilities

Deliver useful ingestion, curation and exploration incrementally. Face automation, a richer viewer or a future interface does not gate useful text ingestion or require extraction to stop evolving. Disposable rebuilds and retained ingestion are permanent modes: rebuilds are unratified development artifacts; retained state requires the supported review, identity and recovery safeguards. The current pipeline's one-map restriction still limits incremental writes; [#708](https://github.com/WillHirsch/yizkr/issues/708), [#130](https://github.com/WillHirsch/yizkr/issues/130) and [#142](https://github.com/WillHirsch/yizkr/issues/142) own the missing contracts and workflows.

Use additional services only when a demonstrated workflow needs them:

- **Document viewing and anchoring:** IIIF, an image server and annotation viewers can improve navigation and region anchoring ([#166](https://github.com/WillHirsch/yizkr/issues/166)). They are not deployed requirements of the current citation model.
- **Photo participation and throughput:** a photo-management service such as Immich can supply detection, clustering or enrollment ([#167](https://github.com/WillHirsch/yizkr/issues/167)). Any integration must preserve the affirmative confirmation boundary of [ADR-0005](0005-faces-strategy.md).
- **Exploration:** a richer client can build on the existing query and static-site capabilities ([#165](https://github.com/WillHirsch/yizkr/issues/165)); a fixed progression through frontend technologies is not required.

The W1/W2/W3 labels used elsewhere denote the wiki-native implementation and these optional document/photo capabilities. They are not a mandatory sequence or a promise that every representation survives unchanged. A change of component must preserve evidence and human decisions through an explicit compatibility or migration plan.
