# Use an official lightweight Python image
FROM python:3.10-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies for tesseract, libreoffice (docx2pdf), and general build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libreoffice \
    poppler-utils \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# Create and set the working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the default port (Render will override if needed)
EXPOSE 5000

# Default command using gunicorn with eventlet for Socket.IO support
CMD ["sh", "-c", "gunicorn --worker-class eventlet -w 1 -b 0.0.0.0:${PORT:-5000} app:app"]
