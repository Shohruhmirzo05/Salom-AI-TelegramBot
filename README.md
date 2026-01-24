# Salom AI Telegram Bot

Official Telegram bot for Salom AI.

## Overview

This bot provides Telegram users access to Salom AI's conversational AI capabilities directly through Telegram messenger.

## Requirements

- Python 3.9+
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Access to Salom AI Backend API

## Installation

1. Clone this repository:
```bash
git clone https://github.com/Shohruhmirzo05/Salom-AI-TelegramBot.git
cd Salom-AI-TelegramBot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables:
Create a `.env` file with the following:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
API_BASE_URL=https://your-backend-api.com
# Add other required environment variables
```

4. Run the bot:
```bash
python bot.py
```

## Docker Deployment

### Local Development

```bash
# Copy example env and configure
cp .env.example .env
# Edit .env with your TELEGRAM_TOKEN

# Build and run with Docker
docker build -t salom-ai-telegram-bot .
docker run -d --env-file .env salom-ai-telegram-bot

# Or use Docker Compose
docker-compose up -d
```

### Production Deployment (CI/CD)

This repository includes automated CI/CD pipelines that deploy to the production server.

#### GitHub Secrets Required

Add these secrets to your GitHub repository (`Settings > Secrets and variables > Actions`):

| Secret | Description |
|--------|-------------|
| `SSH_PRIVATE_KEY` | SSH private key for server access |
| `SERVER_IP` | Production server IP (104.248.34.19) |
| `SERVER_USER` | SSH user (root) |
| `TELEGRAM_TOKEN` | *(Optional in secrets, can be set on server)* |

#### Server Setup (One-time)

On the production server, create the environment file:

```bash
# Create directory and env file
mkdir -p /opt/salom-ai-telegram-bot
cat > /opt/salom-ai-telegram-bot/.env.production << 'EOF'
TELEGRAM_TOKEN=your_bot_token_here
BACKEND_URL=http://salom-ai-api-1:8000
DEFAULT_MODEL=gpt-4o-mini
EOF
```

#### How It Works

1. **CI Pipeline** (`.github/workflows/ci.yml`):
   - Runs on all pushes and PRs
   - Linting with flake8, black, isort
   - Docker image build test
   - Security vulnerability scanning

2. **CD Pipeline** (`.github/workflows/cd.yml`):
   - Runs on push to `main`/`master` branch
   - Copies code to server via SSH
   - Builds Docker image on server
   - Connects container to `salom-ai_salom-network`
   - Verifies deployment

#### Docker Network

The bot container joins the existing `salom-ai_salom-network` to communicate with:
- `salom-ai-api-1` - Backend API (port 8000)
- Other Salom AI services

## Features

- AI-powered conversations
- Integration with Salom AI backend
- User authentication and management
- Multi-language support

## Configuration

The bot connects to the Salom AI backend API. Make sure your backend is running and accessible before starting the bot.

## Related Repositories

- [Salom-AI Backend](https://github.com/Shohruhmirzo05/Salom-AI) - Backend, Web, and Admin Panel
- [Salom-AI Mobile](https://github.com/Shohruhmirzo05/Salom-AI-Mobile) - iOS and Android apps

## License

See the main Salom AI repository for license information.
