FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip megatools ffmpeg curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip install --no-cache-dir cryptg || echo "cryptg not available - continuing without it")
COPY . .
VOLUME /app/data
ENV DATA_DIR=/app/data
CMD ["python", "main.py"]
