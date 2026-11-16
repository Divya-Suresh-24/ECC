# Optional container build (use if ISU IT prefers Docker/Podman over a bare VM install).
#   docker build -t ecc-assistant .
#   docker run -d -p 8010:8010 --env-file .env -v /srv/ecc-assistant/data:/data ecc-assistant
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8010
RUN useradd -r -u 1001 ecc && mkdir -p /data && chown ecc /data
USER ecc
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "2", "--proxy-headers"]
