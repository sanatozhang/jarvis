# ---- Test stage: 门票化，默认跳过以加速本地部署；CI/release 用 --build-arg RUN_TESTS=1 ----
# 背景：测试每次都重跑全量 pytest（>120s）+ 重装 pip deps，是本地 deploy 最大单点。
# 改成 ARG 开关：本地 `dc build` 直接跳过；CI 跑 `dc build --build-arg RUN_TESTS=1`。
FROM python:3.12-slim AS test
ARG RUN_TESTS=0

WORKDIR /app

# git needed for crashguard pr_drafter tests (worktree / apply / status)
RUN if [ "$RUN_TESTS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN if [ "$RUN_TESTS" = "1" ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY app/ ./app/
COPY rules/ ./rules/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY pytest.ini .
RUN if [ "$RUN_TESTS" = "1" ]; then \
      echo "Running tests (RUN_TESTS=1)..."; \
      python -m pytest --tb=short -q; \
    else \
      echo "Skipping tests (default; set --build-arg RUN_TESTS=1 to enable)"; \
    fi

# ---- Production stage ----
FROM python:3.12-slim AS production

WORKDIR /app

# Layer 1：稳定系统依赖（重量级，含 git/perl/gnutls/krb5 等 ~120 MB），尽量不动。
# openssh-client: crashguard pr_drafter 走 git@github.com:... ssh 协议，slim 镜像默认不带
RUN apt-get update && apt-get install -y --no-install-recommends \
    grep coreutils unzip git openssh-client curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Layer 2：agent 工具包（轻量，频繁迭代）。
# 把这层放在稳定层之后、彼此独立——下次加 agent 工具不再触发 Layer 1 重拉。
# ripgrep: claude_api 的 grep 工具用 rg 替代 shell grep（更快 + 结构化输出）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
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
RUN git config --system --add safe.directory '*' \
    && git config --system user.email "crashguard-bot@plaud.ai" \
    && git config --system user.name "Crashguard Bot"

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

# Entrypoint: 把 ~/.claude.json 软链到 named volume 内部，让 `claude login`
# 写入的 session config 跟着 ~/.claude/ 目录一起持久化。否则每次 rebuild
# /root/.claude.json 都会回到镜像里的默认（旧 backup）状态，agent 全 401。
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'CFG=/root/.claude/_root_config.json' \
    'LINK=/root/.claude.json' \
    'if [ ! -L "$LINK" ]; then' \
    '  # 第一次启动：若已有 sibling 配置（首次 login 后镜像内残留），迁进 volume' \
    '  if [ -f "$LINK" ] && [ ! -f "$CFG" ]; then mv "$LINK" "$CFG"; fi' \
    '  ln -sfn "$CFG" "$LINK"' \
    'fi' \
    'exec "$@"' \
    > /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
