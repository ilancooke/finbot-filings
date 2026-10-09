FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/finbot_ingestion ./src/finbot_ingestion
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels /wheels/finbot_filings-*.whl \
    && rm -rf /wheels \
    && groupadd --gid 10001 finbot \
    && useradd --uid 10001 --gid finbot --no-create-home finbot
WORKDIR /app
RUN chown 10001:10001 /tmp && chmod 1777 /tmp
VOLUME ["/tmp"]
USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "finbot_ingestion.main", "--health-check"]
ENTRYPOINT ["python", "-m", "finbot_ingestion.main"]
