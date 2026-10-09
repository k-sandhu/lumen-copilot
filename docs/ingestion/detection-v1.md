# Content detection and routing v1 (#665)

This internal computation does not change upload MIME allowlists or the live
Python path. Bytes decide the candidate format; declared MIME is only recorded
and compared (parameters/case normalized). Recognition is not parser availability.
The shipped Rust parser registry is empty until format implementation issues land.
A route is native only when a parser is both installed and independently enabled;
otherwise it is Python fallback for the existing six families or typed unsupported.
No new format becomes uploadable merely because the detector recognizes it.

Recognize PDF; DOCX/DOTX/DOCM; PPTX/PPTM; XLSX/XLSM; ODT/ODS/ODP; EPUB;
ZIP/TAR/GZIP; legacy DOC/XLS/PPT/MSG compound files; RTF; HTML/XHTML/MHTML;
TXT/Markdown/source text; CSV/TSV; JSON/JSONL/XML/XBRL/iXBRL; IPYNB; EML/mbox;
and common raster images for later OCR. Container inspection precedes text guesses.
Multiple conflicting document roots in one container fail typed unsupported;
opaque binary or unknown compound roots do too. No scripts/macros/entities run.
XML/HTML, source languages and delimited text may be intrinsically ambiguous:
heuristic evidence is recorded, not a guarantee of semantic validity.

Cap input at 32 MiB, container directory at 10000 entries, inspected metadata at
64 KiB, rendered decoding at 2 million code points; never expand archive members
except bounded format metadata. Truncated/invalid recognized containers fail parse.
Archive/member extraction and malware/DRM handling belong to format parsers.

Decode UTF-8/UTF-16 BOMs, valid UTF-8, conservative BOM-less UTF-16 null patterns,
then chardetng's legacy encoding estimate. Report encoding and decision evidence,
count each decoding error, and retain literal replacement characters separately
from errors. Encoding ambiguity remains heuristic; no normalization is applied.
