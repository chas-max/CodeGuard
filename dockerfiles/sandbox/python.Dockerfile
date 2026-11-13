FROM python:3.12-slim

RUN groupadd -g 1000 sandbox \
    && useradd -m -u 1000 -g sandbox sandbox \
    && mkdir -p /workspace \
    && chown -R sandbox:sandbox /workspace

RUN pip install --no-cache-dir pytest pytest-cov ruff

WORKDIR /workspace
USER sandbox

ENTRYPOINT ["/bin/sh", "-lc"]
