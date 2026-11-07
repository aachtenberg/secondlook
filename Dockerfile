FROM python:3.13-slim
# what the SQL Server driver (mssql-python) loads at connect time
RUN apt-get update && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# every optional extra: the SQL Server driver, Azure Blob storage and the MCP server (dev tools stay out)
RUN uv sync --frozen --no-dev --no-editable --extra sqlserver --extra azure --extra mcp
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["dbtriage"]
CMD ["--help"]
