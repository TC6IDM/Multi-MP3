FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# uv installs exactly what uv.lock pins.
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never

# spotdl gets its own virtualenv: every release pins fastapi/uvicorn/rich to
# versions years old, and the app only ever runs it as a subprocess.
COPY requirements-spotdl.txt /tmp/requirements-spotdl.txt
RUN uv venv /opt/spotdl && \
    uv pip install --no-cache --python /opt/spotdl/bin/python -r /tmp/requirements-spotdl.txt
ENV SPOTDL_BIN=/opt/spotdl/bin/spotdl

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project --no-dev --no-cache
ENV PATH="/app/.venv/bin:$PATH"

COPY main.py /app/main.py
COPY src/ /app/src/
RUN mkdir -p /music

ENTRYPOINT ["python", "main.py"]
