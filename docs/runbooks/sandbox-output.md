# Sandbox stdout and stderr

Custom Python execution retains up to **262,144 decoded characters per stream**
(stdout and stderr independently). UTF-8 characters split across Docker chunks are
reassembled using separate decoder state for each stream. Malformed bytes use the
Unicode replacement character; an incomplete sequence remaining at stream end is
also replaced.

When decoded output exceeds that limit, the retained prefix is followed by:

```text
[output truncated at 262144 characters]
```

The notice begins on a new line and is additional to the retained-character limit.
Output exactly at the limit has no notice. The runner continues draining discarded
output so that a full output pipe cannot block execution. Retention stays bounded
by the character cap, one lookahead character, decoder state and a source chunk.
Output-file collection has its separate byte and member budgets; this change does
not alter their byte/member budgets. ADR-0029 (Proposed) reports a stream or
collection overage as `failed` / `sandbox_limit_output` and destroys the session
container; a subsequent run creates a fresh environment.

Regression coverage lives in `sandbox_runner/tests/test_engine.py`: controlled
chunks cover every internal boundary of two-, three- and four-byte UTF-8 characters
on stdout and stderr, interleaved independent streams, malformed and incomplete
sequences, exact-limit output and clipped output. Existing allocation and drain
checks continue to exercise output far beyond the retained limit.
