# rack monitor: python:3.12-slim plus one standard-library script.
# nvidia-smi is not in the image; the NVIDIA container runtime injects it at
# run time (--gpus all with NVIDIA_DRIVER_CAPABILITIES=utility: query only,
# no compute). `rack monitor up` builds this and runs it on every node.
FROM python:3.12-slim
LABEL org.opencontainers.image.title="rack-monitor" \
      org.opencontainers.image.description="Read-only node and engine telemetry for the rack" \
      org.opencontainers.image.source="https://github.com/bytebunkerlabs/dgx-spark-serve"
COPY rackmon.py /opt/rack-monitor/rackmon.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NVIDIA_DRIVER_CAPABILITIES=utility
USER 65534:65534
EXPOSE 9177
ENTRYPOINT ["python3", "/opt/rack-monitor/rackmon.py"]
CMD ["serve"]
