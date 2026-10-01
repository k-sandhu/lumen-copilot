# Architecture evolution presentation

[Open the HTML slide deck](architecture-evolution.html).

The 71-slide deck explains Lumen Copilot's architecture decisions from June 16 through October 1, 2026. It covers all 23 accepted ADRs, their rationale and tradeoffs, significant supersessions, the merged system topology, and accepted designs that have not shipped. Issue: [#651](https://github.com/k-sandhu/lumen-copilot/issues/651).

## Evidence snapshot

Repository sources are pinned to main commit [`7c9e349b10330535cfb7d4fc94087a9327eaefa5`](https://github.com/k-sandhu/lumen-copilot/commit/7c9e349b10330535cfb7d4fc94087a9327eaefa5), which includes PR #606's direct-media migration and the October 1 merges of PR #603's credential-state lifecycle and PR #605's native embedding contract. The presentation distinguishes decision dates from merge dates, and accepted design from merged implementation. Open PR examples are a September 30 snapshot, not current status promises.

Later accepted ADRs/specifications resolve stale historical summaries. In particular, OD-4 is closed by spec 0004, OpenSearch supersedes the original retrieval-store choice, reusable sandbox sessions supersede the original per-run design, and ADR-0023 records the narrow OpenRouter transcription transport exception. Remaining pgvector code/schema/image residue is explicitly identified. No architecture decisions or application behavior change in this documentation work.

## Use

Open the HTML directly in a modern browser. It embeds all presentation styles, scripts, slide content, and speaker notes, and requires no server or network connection to display. Source links require access to the GitHub repository.

- Arrow keys, Space, and Page Up/Down navigate. Home/End jump to the first/last slide.
- **Contents** (C) searches slide titles, chapters, and subtitles.
- **Notes** (N) shows the active slide's detailed explanation and qualifications.
- **Overview** (O) shows every slide. Selecting a slide returns to presentation mode.
- **Read all** shows the full deck with speaker notes. With JavaScript disabled, the complete slide content remains readable.
- **Fullscreen** uses the browser fullscreen API when supported.
- **Print** uses landscape pages, one slide per page. Enable background graphics and disable browser headers/footers. Speaker notes are omitted from print mode.
- A URL fragment such as `#slide-24` opens a specific slide. The HTML content itself remains editable.

## Verification scope

The presentation is documentation. Verification covers source-file resolution, ADR coverage, keyboard and button navigation, contents search, speaker notes, overview/reading modes, responsive presentation controls, layout fit, and print pagination. It does not certify a live Lumen deployment, complete acceptance of every architectural plan, or external provider transcription conformance.

No application tests or live-stack gates are needed for this standalone documentation artifact. The named `/verify` and `/verify-live` mechanisms and CI remain unresolved in the open-decision registry. See the PR's validation report for checks exercised on this deck.
