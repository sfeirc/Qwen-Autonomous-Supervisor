FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @qwen-code/qwen-code@0.21.10

WORKDIR /opt/qas
COPY . /opt/qas
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /opt/qas \
    && mkdir -p /workspace /runtime \
    && chown -R node:node /workspace /runtime

USER node
ENV PATH="/opt/venv/bin:${PATH}" \
    QAS_CONFIG=/config/supervisor.yml \
    NO_BROWSER=1
ENTRYPOINT ["/usr/bin/tini", "--", "qas"]
CMD ["loop"]
