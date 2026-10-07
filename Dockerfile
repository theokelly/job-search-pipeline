FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx 2>/dev/null || true

RUN useradd -m -s /bin/bash claude-user

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER claude-user
WORKDIR /home/claude-user

# Install Claude Code to user-local prefix (fixes the npm update issue too)
RUN mkdir -p /home/claude-user/.npm-global /home/claude-user/.claude && \
    npm config set prefix /home/claude-user/.npm-global && \
    npm install -g @anthropic-ai/claude-code

ENV PATH=/home/claude-user/.npm-global/bin:$PATH

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]