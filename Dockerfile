FROM python:3.11-slim

# ffmpeg is not included in the slim image, so install it explicitly
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV OUTPUT_DIR=/app/outputs
ENV PUBLIC_BASE_URL=http://localhost:8000
RUN mkdir -p /app/outputs

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
