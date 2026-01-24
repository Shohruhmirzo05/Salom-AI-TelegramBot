FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (ffmpeg for pydub audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot.py .

# Create data directory for persistent state
RUN mkdir -p /app/data

# Create non-root user for security
RUN useradd -m -r botuser && \
    chown -R botuser:botuser /app
USER botuser

# Environment variables with defaults
ENV TELEGRAM_TOKEN=""
ENV BACKEND_URL="http://salom-ai-api-1:8000"
ENV DEFAULT_MODEL="gpt-4o-mini"
ENV REQUEST_TIMEOUT="30"
ENV STATE_FILE="/app/data/bot_state.pickle"
ENV LOG_FILE="/app/data/bot.log"

# Health check - verify the bot process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f "python bot.py" || exit 1

# Run the bot
CMD ["python", "-u", "bot.py"]
