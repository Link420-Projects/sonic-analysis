FROM python:3.11-slim

RUN pip install --no-cache-dir essentia numpy mutagen bencodepy requests

COPY sonic_analysis.py /app/sonic_analysis.py

WORKDIR /output

ENTRYPOINT ["python", "/app/sonic_analysis.py"]
