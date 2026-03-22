# Discord Trivia Bot

A Discord trivia bot built in Python using `discord.py`, the Open Trivia Database API, and SQLite.

This started as a fun personal project inspired by a similar post I saw on LinkedIn. My wife and I have a little tradition of playing trivia while we wait for our food when we go out to dinner, so I decided to build a bot we could actually use ourselves instead of relying on random trivia websites.

What started simple ended up turning into a much more polished project with persistent stats, multiple game modes, a permanent launcher message, admin tools, and a smoother game flow.

## Features

* Slash command and button-based trivia flow
* Category selection
* Difficulty selection
* Single-question mode
* 5-question and 10-question round modes
* Per-channel hourly leaderboard
* All-time leaderboard
* Persistent player stats with SQLite
* Best streak tracking
* Best round score tracking
* Category-specific performance tracking
* Recent performance stats
* Permanent launcher message
* Admin commands for resetting stats and managing sessions
* OpenTDB session token support to reduce repeated questions

## Tech Stack

* Python
* discord.py
* SQLite
* aiohttp
* python-dotenv
* Open Trivia Database API

## Commands

### Player Commands

* `/trivia` — open the trivia setup menu
* `/leaderboard` — show the current hour’s leaderboard for the channel
* `/leaderboard_all_time` — show the all-time leaderboard
* `/stats` — show your personal trivia stats
* `/stats @user` — show another user’s trivia stats

### Admin Commands

* `/setup_trivia_launcher` — create the permanent trivia launcher message
* `/refresh_trivia_launcher` — recreate the stored launcher message
* `/delete_trivia_launcher` — remove the stored launcher message
* `/reset_hourly_scores` — reset the current channel’s hourly scores
* `/reset_user_stats @user` — reset one user’s saved trivia stats
* `/reset_all_trivia_stats` — reset all saved trivia stats
* `/cancel_trivia_session` — force-end the active trivia session in the current channel

## How It Works

Players can launch a trivia game from the permanent launcher message or by using `/trivia`.

From there, they can:

* choose a category
* choose a difficulty
* choose a game mode

After each question, the bot shows the result and gives the round owner the option to continue or end the session. Scores are tracked for the current hour by channel, while long-term player performance is stored in SQLite.

## Stats Tracked

The bot stores long-term player data, including:

* all-time correct answers
* total questions answered
* overall accuracy
* rounds played
* rounds completed
* best round score
* best streak
* category-specific accuracy
* recent performance

## Why I Built It

This project was mainly built for fun and practical use. My wife and I already liked playing trivia while waiting for dinner, so this gave me a way to build something we could actually use while also improving my Python skills.

It also gave me hands-on experience with:

* APIs
* asynchronous Python
* Discord interactions
* SQLite
* persistent state
* debugging real project issues

## Setup

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd discord-trivia-bot
```

### 2. Create and activate a virtual environment

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a `.env` file

```env
DISCORD_TOKEN=your_bot_token_here
```

### 5. Run the bot

```bash
python bot.py
```

## Notes

* Trivia questions come from the Open Trivia Database.
* Player stats are stored locally in SQLite.
* Hourly leaderboard data is stored in memory and resets naturally by hour.
* The bot is designed for use in a dedicated trivia channel.

## Screenshots

### Trivia Launcher

![Trivia launcher](screenshots/Trivia Launcher.jpeg)

### Question Style

![Question style](screenshots/Question Style.jpeg)

### Game Modes

![Game modes](screenshots/Game Modes.jpeg)

### Leaderboard

![Leaderboard](screenshots/Leaderboard.jpeg)

### Stats

![Player stats](screenshots/Stats.jpeg)

## Future Improvements

Possible future improvements:

* split the project into multiple modules
* add more polished error handling
* support alternate trivia sources
* improve deployment options
* add more admin configuration options

## License

This project is for educational and personal portfolio use.
