FROM python:3.12-slim

# Create a non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py .
COPY routers/ routers/
COPY schemas/ schemas/
COPY services/ services/

# Switch to non-root user
USER appuser

EXPOSE 8082

CMD ["python", "main.py"]