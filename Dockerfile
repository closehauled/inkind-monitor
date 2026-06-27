FROM python:3.12-alpine

# tzdata lets the container honor the TZ env var so cron fires at the intended
# local hour (the base image is UTC-only without it).
RUN apk add --no-cache tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY inkind_monitor.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
