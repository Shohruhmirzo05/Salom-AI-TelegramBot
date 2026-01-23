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

You can also run the bot using Docker:

```bash
docker build -t salom-ai-telegram-bot .
docker run -d --env-file .env salom-ai-telegram-bot
```

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
