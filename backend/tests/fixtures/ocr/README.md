# OCR fixtures — issue #695

`success.json` and `inference_error.json` are synthetic schema examples with
agent-generated Unicode text. They are not recordings.

`recorded_generated_page.json` is a sanitized recording of one OpenRouter
file-parser/mistral-ocr request on 2026-10-09, with a generated raster page.
The chat request returned HTTP 429 while parsed text remained in error metadata.
The adapter recovers that text and removes only the exact separate wrapper
parts. The metadata file describes generation, routing price caps and missing
billing data. No paid retry was made. Usage/cost recovery from a successful
chat response remains covered by synthetic fixtures, not this live recording.

No key, response headers, generation identifier, image URLs, third-party
content or model answer is retained. The annotation hash is replaced; the
filename and recognized text describe only the generated page. Offline tests
use httpx MockTransport and never call the service.
