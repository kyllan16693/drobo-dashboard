FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY drobo/ drobo/
COPY app.py main.py ./
COPY templates/ templates/
COPY static/ static/

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

ENV WEB_HOST=0.0.0.0
ENV DROBO_DB_PATH=data/history.db

EXPOSE 8765

CMD ["python", "main.py"]
