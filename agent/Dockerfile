FROM python:3.12-slim

# Node.js is required by claude-agent-sdk (it drives the Claude Code CLI) and by
# the Perplexity Sonar MCP server (npx server-perplexity-ask).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The Agent SDK shells out to the Claude Code CLI binary.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
