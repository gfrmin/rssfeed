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
    'humanize>=4.0'

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
