FROM python:3.11-slim

WORKDIR /app

# Step 1: Copy only dependency spec (cached unless pyproject.toml changes)
COPY pyproject.toml /app/autohh/pyproject.toml

# Step 2: Create minimal package stub so pip can resolve and install
RUN mkdir -p /app/autohh/autohh && \
    echo '"""autohh"""' > /app/autohh/autohh/__init__.py

# Step 3: Install dependencies (cached when pyproject.toml unchanged)
RUN pip install --no-cache-dir /app/autohh/

# Step 4: Copy actual source code (only this layer rebuilds on code changes)
COPY . /app/autohh/

RUN mkdir -p /app/data

ENV DATA_DIR=/app/data

ENTRYPOINT ["python", "-m", "autohh"]
