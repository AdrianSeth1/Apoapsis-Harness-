# Pilot controller image, built from a pinned commit and nothing else.
#
# The previous controller (`apoapsis-live-controller:slice5c`) was built as
# `FROM apoapsis-live-controller:slice2c` plus `COPY src /opt/apoapsis/src`
# from a working tree, and carries no labels at all. Its image id is real; its
# provenance is not recoverable from it. That chain is exactly what this file
# replaces.
#
# Two rules make provenance checkable rather than remembered:
#
#   1. The build context is a `git archive` of one commit. There is no path by
#      which an uncommitted edit reaches the image, because the working tree is
#      never the context.
#   2. The commit is written into the image as a label, so the built artefact
#      answers "which source?" without reference to a separate note that can be
#      lost or contradicted.
#
# Base pins are digest-pinned by the caller via build args so a moved tag
# cannot silently change the image.

ARG PYTHON_BASE=ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90
ARG DOCKER_CLI=docker:29-cli

FROM ${DOCKER_CLI} AS docker_cli

FROM ${PYTHON_BASE}

ARG SOURCE_COMMIT
ARG SOURCE_TREE
ARG BUILD_CONTEXT_SHA256

LABEL org.apoapsis.role="pilot-controller" \
      org.apoapsis.source-commit="${SOURCE_COMMIT}" \
      org.apoapsis.source-tree="${SOURCE_TREE}" \
      org.apoapsis.build-context-sha256="${BUILD_CONTEXT_SHA256}" \
      org.apoapsis.pilot="crisis-atlas"

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates git libgomp1 python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/apoapsis
COPY . /opt/apoapsis
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir .

# Refuse to be a controller that cannot say where it came from.
RUN test -n "${SOURCE_COMMIT}" || (echo "SOURCE_COMMIT build arg is required" && false)

ENTRYPOINT ["/opt/venv/bin/python"]
