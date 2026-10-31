FROM python:3.12-slim
WORKDIR /srv/backend
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# db/ and fixtures/ are mounted at /srv/db and /srv/fixtures by docker-compose
CMD ["sh", "-c", "python -m app.db && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
