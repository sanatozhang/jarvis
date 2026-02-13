FROM python:3.12-slim

WORKDIR /app

# System deps for grep, unzip, git (codex needs git)
RUN apt-get update && apt-get install -y --no-install-recommends \
    grep coreutils unzip git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY app/ ./app/
COPY rules/ ./rules/

# Create data dirs
RUN mkdir -p /app/data /app/workspaces

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
