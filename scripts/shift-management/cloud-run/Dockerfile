FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY bolt_server.py .

# Cloud Run sets PORT env var (default 8080)
ENV PORT=8080

# Use gunicorn for production (single worker is fine for Bolt)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 120 bolt_server:flask_app
