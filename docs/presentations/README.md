# Architecture evolution presentation

[Open the HTML slide deck](architecture-evolution.html).

The 74-slide deck explains Lumen Copilot's architecture decisions from June 16 through October 2, 2026. It covers all 23 accepted ADRs, their rationale and tradeoffs, significant supersessions, the merged system topology, and accepted designs that have not shipped. Issue: [#651](https://github.com/k-sandhu/lumen-copilot/issues/651).

## Evidence snapshot

Repository sources are pinned to current main commit [`8c3c87b5f69bf30d43ffc8887ac54d6cfe2c2660`](https://github.com/k-sandhu/lumen-copilot/commit/8c3c87b5f69bf30d43ffc8887ac54d6cfe2c2660), inspected October 2, 2026. It includes PR #559’s durable T2 pre-approval evidence, #606’s direct media, #603’s credential lifecycle, #605’s embedding contract, #477’s connector conformance, #604’s durable denial ownership and collection deletion, and #570’s same-session transcript recall with immutable message provenance. ADR-0024 (#609) and ADR-0025 (#637) are merged documents that remain **Proposed**, with runtime designs unimplemented. The deck distinguishes decision dates, document merges, and runtime implementation. Open PR examples are dated October 2 observations, not future status promises.

Later accepted ADRs/specifications resolve stale historical summaries. In particular, OD-4 is closed by spec 0004, OpenSearch supersedes the original retrieval-store choice, reusable sandbox sessions supersede the original per-run design, and ADR-0023 records the narrow OpenRouter transcription transport exception. Remaining pgvector code/schema/image residue is explicitly identified. No architecture decisions or application behavior change in this documentation work.

## Use

Open the HTML directly in a modern browser. It embeds all presentation styles, scripts, slide content, and speaker notes, and requires no server or network connection to display. Source links require access to the GitHub repository.

- Arrow keys, Space, and Page Up/Down navigate. Home/End jump to the first/last slide.
- **Contents** (C) searches slide titles, chapters, and subtitles. A selection opens the chosen slide in presentation mode or scrolls it into view while retaining Read all or Overview. Toolbar Previous/Next follows the same rule.
- **Notes** (N) shows the active slide's detailed explanation and qualifications.
- **Overview** (O) shows every slide. Selecting a slide returns to presentation mode.
- **Read all** shows the full deck with speaker notes. With JavaScript disabled, the complete slide content remains readable.
- **Fullscreen** uses the browser fullscreen API when supported.
- **Print** uses landscape pages, one slide per page. Enable background graphics and disable browser headers/footers. Speaker notes are omitted from print mode.
- A URL fragment such as `#slide-24` opens a specific slide. The HTML content itself remains editable.

## Verification scope

The presentation is documentation. The initial deck validation covered source-file resolution, ADR coverage, navigation, layout fit, and print pagination for 71 slides. R2 uses deterministic built-in Node checks for content/source coverage, markup and counts, and navigation from every mode. The three added slides reuse the existing layouts. Browser visual fit and the updated 74-page print output have not been rerun under the owner’s no-browser requirement. It does not certify a live Lumen deployment, complete acceptance of every architectural plan, or external provider transcription conformance.

No application tests or live-stack gates are needed for this standalone documentation artifact. The named `/verify` and `/verify-live` mechanisms and CI remain unresolved in the open-decision registry. See the PR's validation report for checks exercised on this deck.
