FROM python:3.12-slim

WORKDIR /app

# System deps needed for some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directory for SQLite DB
RUN mkdir -p /data

# Default mode: paper trading
ENV TRADING_MODE=paper
ENV DATABASE_URL=sqlite:////data/trade_bot.db
ENV LOG_LEVEL=INFO

# Expose Streamlit port (optional, only if running dashboard)
EXPOSE 8501

CMD ["python", "main.py", "--mode", "paper"]
