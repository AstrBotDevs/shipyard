FROM python:3.11-slim

# Install system dependencies
# RUN apt-get update && apt-get install -y \
#     gcc \
#     libc6-dev \
#     libffi-dev \
#     bash \
#     && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy all project files
COPY pyproject.toml requirements.txt alembic.ini run.py ./
COPY app/ ./app/
COPY alembic/ ./alembic/

# Install dependencies and create data directory in one layer
RUN pip install -r requirements.txt --no-cache-dir && \
    mkdir -p /app/data

# Expose port
EXPOSE 8000

# Start the application
CMD ["python", "run.py"]
