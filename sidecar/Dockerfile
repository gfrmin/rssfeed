FROM debian:bookworm-slim AS tailwind
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -sLo /tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 && \
    chmod +x /tailwindcss && \
    rm -rf /var/lib/apt/lists/*
COPY static/ /build/static/
COPY app/templates/ /build/app/templates/
WORKDIR /build
RUN /tailwindcss -i static/input.css -o static/tailwind.css --minify

FROM python:3.12-slim

WORKDIR /sidecar

COPY pyproject.toml .
RUN pip install --no-cache-dir \
    'fastapi>=0.115' \
    'uvicorn[standard]>=0.34' \
    'httpx>=0.28' \
    'jinja2>=3.1' \
    'psycopg[binary]>=3.2' \
    'trafilatura>=2.0' \
    'readability-lxml>=0.8' \
    'python-multipart>=0.0.18' \
    'humanize>=4.0' \
    'markdownify>=0.14' \
    'ollama>=0.4'

COPY . .
COPY --from=tailwind /build/static/tailwind.css static/tailwind.css

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
