# BuildKit verifies every download before committing its layer. Keep hashes in
# source (not overridable build args). Official upstream binaries, same server
# release as the former image; amd64 and arm64 are supported explicitly.
FROM alpine:3.20.3@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a AS binaries-amd64
ADD --checksum=sha256:1126bbb3276321e7fed0167682d92a03572862f73802658f616af43d9a951e6a https://github.com/minio/minio/releases/download/RELEASE.2024-10-13T13-34-11Z/minio.linux-amd64.RELEASE.2024-10-13T13-34-11Z /usr/bin/minio
ADD --checksum=sha256:b1f0ef2319c0428f10c04e2338a9a84f686bf73c46fc7823c1bffb4fc1c6d45f https://github.com/minio/mc/releases/download/RELEASE.2024-10-08T09-37-26Z/mc.linux-amd64.RELEASE.2024-10-08T09-37-26Z /usr/bin/mc

FROM alpine:3.20.3@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a AS binaries-arm64
ADD --checksum=sha256:d89a1c767182fb599b975b6177b517b17b3bee2d0948dcef4adfd0b886a09354 https://github.com/minio/minio/releases/download/RELEASE.2024-10-13T13-34-11Z/minio.linux-arm64.RELEASE.2024-10-13T13-34-11Z /usr/bin/minio
ADD --checksum=sha256:4c970b9da5a88b203ec685e025f728b6cf9cf5650baca60f55b57974fd013eb6 https://github.com/minio/mc/releases/download/RELEASE.2024-10-08T09-37-26Z/mc.linux-arm64.RELEASE.2024-10-08T09-37-26Z /usr/bin/mc

FROM binaries-${TARGETARCH} AS runtime
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.version="RELEASE.2024-10-13T13-34-11Z" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
RUN chmod 0555 /usr/bin/minio /usr/bin/mc
EXPOSE 9000 9001
ENTRYPOINT ["/usr/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
