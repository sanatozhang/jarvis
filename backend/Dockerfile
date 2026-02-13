FROM python:3.12-slim

WORKDIR /app

# System deps for grep, unzip, git, node (for codex CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    grep coreutils unzip git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (needed for Codex CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Codex CLI globally
RUN npm install -g @openai/codex 2>/dev/null || echo "Codex install skipped (optional)"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY app/ ./app/
COPY rules/ ./rules/

# Create data dirs
RUN mkdir -p /app/data /app/workspaces

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
