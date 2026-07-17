# Controlled resumable-download evaluation repository

This deliberately small Python repository models a download service with a
stable `Downloader.download(url, destination)` API and a progress store. The
baseline implementation passes the existing new-download behavior but fails the
two resumable-download acceptance tests.

Initialize it as its own repository before an evaluation:

```bash
git init -b main
git add .
git commit -m "Controlled download-service baseline"
python -m unittest discover -s tests -v
```

The intended evaluation task is:

```text
Add resumable downloads.
Preserve the current public API.
Do not add runtime dependencies.
Existing clients must continue working.
```

An accepted implementation sends a `Range` request when progress exists,
appends a `206 Partial Content` response, and replaces stale partial data when a
server ignores the range and returns `200 OK`.

