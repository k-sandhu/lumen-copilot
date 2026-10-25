# MinIO incomplete multipart reaper

ADR-0023 assigns incomplete multipart cleanup to the Compose MinIO companion.
It removes only incomplete uploads older than `UPLOAD_INCOMPLETE_LIFECYCLE_DAYS`
(default `2` days), then sleeps `UPLOAD_MULTIPART_REAPER_INTERVAL_SECONDS`
(default `900` seconds). Completed objects are outside this command's scope.
Both values remain explicit deployment environment overrides.

Keep the complete `mc rm` invocation on one shell line in the folded Compose
block. YAML preserves newlines inside this more-indented loop; splitting flags
onto adjacent lines makes the shell invoke them as separate commands. A running
container alone does not establish that cleanup works. The existing retry loop
and handling of transient provider errors are unchanged by #804.

Run the resolved-command regression with:

```powershell
python -m unittest discover -s tests -p test_reaper_command.py -v
```

This requires Docker Compose and a POSIX shell (Git for Windows supplies one).
It resolves the real Compose service, handles the serialized dollar escapes,
then executes the shell with a recording `mc`. A test-only sleep function exits
after one iteration. It verifies all flags and the bucket reach the same call,
quoted credentials remain intact, no shell errors occur, and both default and
override age/interval values are passed through. No daemon or live data is used.

For live verification use a unique Compose project, loopback ports and bucket.
Create an incomplete multipart upload with one uploaded part and a separate
completed object with known bytes. Override the reaper's age to `0` days and
interval to `1` second **only in that isolated test service**, making the temporary
orphan immediately eligible without changing the host clock or production policy.
Start the actual companion and poll the provider: the incomplete upload must
become `NoSuchUpload`, the provider's multipart list must be empty, and reading
the completed object must return its exact original bytes. Inspect companion
logs for shell/client errors; an empty success log is not proof of removal.

Delete the retained test object, verify absence, abort any remaining test uploads,
remove the temporary bucket and confirm it is absent. Tear down only that
project's containers and volume. Restore the default environment for normal runs;
never apply the zero-day test override to a shared bucket.
