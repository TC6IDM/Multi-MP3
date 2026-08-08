FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp for YouTube JS extraction)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

RUN pip install --no-cache-dir spotdl spotipy python-dotenv scdl yt-dlp psutil rich

WORKDIR /app
COPY main.py /app/main.py
COPY src/ /app/src/
RUN mkdir -p /music

ENTRYPOINT ["python", "main.py"]
