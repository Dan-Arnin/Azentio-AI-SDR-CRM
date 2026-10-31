# Build from the repo root so db/ and fixtures/ ship inside the image:
#   docker build -f backend/Dockerfile .
FROM python:3.12-slim
WORKDIR /srv/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY db /srv/db
COPY fixtures /srv/fixtures
# Hosts like Render pass the port in $PORT; locally it defaults to 8000
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "python -m app.db && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
