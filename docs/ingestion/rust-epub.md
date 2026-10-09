# EPUB candidate (#681)

The candidate validates all ZIP names, CRCs, entry counts, expansion/ratio limits
before reading container.xml, OPF manifest/spine, EPUB 2 NCX or EPUB 3 navigation.
Chapters follow spine order. TOC titles are source metadata; injected TOC headings
have heuristic roles and separate TOC provenance. Missing titles remain unknown.
Every block carries chapter ordinal/member path. Supplied footnotes are retained.
One shared context bounds archive, XML and HTML computation; no chapter resets it.

Encrypted/DRM packages are typed unsupported (including any encryption descriptor
in this conservative profile); malformed packages are typed parse failures.
Traversal entries and references escaping the package root are rejected. No media
is embedded or fetched. NATIVE_EPUB_ENABLED and NATIVE_EPUB_SHADOW default false;
only enabled cutover plus a capable wheel admits application/epub+zip.

Acceptance: EPUB 2/3 spine and TOC order/titles, exact chapter provenance/Unicode
slices, metadata and footnotes, traversal/encryption/malformed/budget negatives.

Merge gate: hold until measured against the baseline evaluation; a human merges.
