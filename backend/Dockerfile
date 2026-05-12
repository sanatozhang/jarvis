# ---- Test stage: tests must pass before building production image ----
FROM python:3.12-slim AS test

WORKDIR /app

# git needed for crashguard pr_drafter tests (worktree / apply / status)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY rules/ ./rules/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY pytest.ini .
RUN python -m pytest --tb=short -q

# ---- Production stage ----
FROM python:3.12-slim AS production

WORKDIR /app

# System deps for grep, unzip, git, ssh (git push via ssh), node (for codex CLI)
# openssh-client: crashguard pr_drafter 走 git@github.com:... ssh 协议，slim 镜像默认不带
RUN apt-get update && apt-get install -y --no-install-recommends \
    grep coreutils unzip git openssh-client curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (needed for Codex CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh) — crashguard pr_drafter calls `gh pr create --draft`
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# git safe.directory：容器 root 访问宿主用户拥有的 bind-mounted 仓库会触发
# "fatal: detected dubious ownership"。crashguard pr_drafter 在 /Users/mac/... 等
# 宿主路径上跑 git——加通配把这层防御关掉（容器内本来就只有 root，没有权限边界）。
RUN git config --system --add safe.directory '*'

# Install Codex CLI globally
RUN npm install -g @openai/codex 2>/dev/null || echo "Codex install skipped (optional)"

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || echo "Claude install skipped (optional)"

# Install Feishu CLI (for Bitable read/write)
RUN npm install -g @larksuite/cli 2>/dev/null || echo "Feishu CLI install skipped (optional)"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code (no tests in production)
COPY app/ ./app/
COPY rules/ ./rules/
# scripts/ is imported at startup (check_crash_decoupling)
COPY scripts/ ./scripts/

# Create data dirs
RUN mkdir -p /app/data /app/workspaces

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
