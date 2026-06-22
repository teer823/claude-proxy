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

# Explicitly disable debug mode in the container image.
# Override at runtime with -e DEBUG_MODE=true or via --env-file if needed.
ENV DEBUG_MODE=false
ENV DEBUG_LOG_DIR=logs

EXPOSE 8082

CMD ["python", "main.py"]
