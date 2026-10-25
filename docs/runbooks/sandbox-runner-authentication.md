# Sandbox runner authentication

All runner capability endpoints require `X-Lumen-Runner-Token` matching the
configured `SANDBOX_RUNNER_TOKEN`. The configured value is stripped and must contain
at least 32 characters; an absent, blank or shorter value prevents startup. This
validation is unchanged by the malformed-header correction in #810.

Missing or invalid credentials, including non-ASCII HTTP header bytes, return
HTTP 401 with the existing generic response:

```json
{"detail":"sandbox runner token missing or invalid"}
```

The response does not echo supplied or configured credentials. Both strings are
encoded to UTF-8 bytes before `secrets.compare_digest`, which retains constant-time
comparison and avoids its exception for non-ASCII string arguments. A valid
configured ASCII token continues to authenticate normally. `/health` remains
public and returns only the runner's liveness status.

Focused HTTP regressions in `sandbox_runner/tests/test_auth.py` bind the production
`require_token` dependency to a protected route. They cover absent, empty, wrong
ASCII, UTF-8 and invalid-UTF-8 header bytes, check the exact 401 response, verify the
protected route was not entered, then exercise a valid credential successfully.
No production credentials are used in these tests.
