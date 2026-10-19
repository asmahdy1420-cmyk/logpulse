FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[api,postgres]"

COPY scripts ./scripts

RUN useradd --create-home --uid 10001 logpulse
USER logpulse

EXPOSE 8000
ENTRYPOINT ["logpulse"]
CMD ["run", "--source", "-", "--window", "60s"]
