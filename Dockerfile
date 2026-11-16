FROM python:3.13-alpine

RUN adduser -D -u 10001 bot
WORKDIR /app
COPY bot.py .
RUN mkdir -p /data && chown 10001:10001 /data

USER 10001
ENV STATE_DIR=/data PYTHONUNBUFFERED=1 TZ=Europe/Lisbon
VOLUME /data

CMD ["python", "bot.py"]
