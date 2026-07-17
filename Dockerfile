FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg used for audio processing)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from requirements if present
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy project files
COPY . /app

# Ensure downloads directory exists (mounted from host at runtime)
RUN mkdir -p /app/downloads

EXPOSE 5000

CMD ["python", "web_app.py"]
