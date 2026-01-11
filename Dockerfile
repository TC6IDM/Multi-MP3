FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir spotdl spotipy python-dotenv

WORKDIR /app
COPY main.py /app/main.py
RUN mkdir -p /music

ENTRYPOINT ["python", "main.py"]
