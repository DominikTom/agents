FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

EXPOSE 8080

# Start dashboard + scheduler together
CMD ["sh", "-c", "uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8080 & python -m src.main schedule"]
