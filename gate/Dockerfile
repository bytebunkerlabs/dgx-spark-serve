# rack gate: python:3.12-slim plus one standard-library script (docs/14-gate.md).
FROM python:3.12-slim
LABEL org.opencontainers.image.title="rack-gate" \
      org.opencontainers.image.description="The rack's own front door: OpenAI and Anthropic APIs, a key per client" \
      org.opencontainers.image.source="https://github.com/bytebunkerlabs/dgx-spark-serve"
COPY rackgate.py /opt/rack-gate/rackgate.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
ENTRYPOINT ["python3", "/opt/rack-gate/rackgate.py"]
CMD ["serve"]
