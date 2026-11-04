# Use an official lightweight Python image
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies: MariaDB server & client, Tesseract OCR, LibreOffice, build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    mariadb-server \
    mariadb-client \
    tesseract-ocr \
    libreoffice \
    poppler-utils \
    build-essential \
    default-libmysqlclient-dev && \
    rm -rf /var/lib/apt/lists/*

# Create and set the working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Expose default port
EXPOSE 5000

# Run entrypoint script
CMD ["/bin/bash", "/app/entrypoint.sh"]
