import os
import json
import html
import random
import sqlite3
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

TRIVIA_API_URL = "https://opentdb.com/api.php"
CATEGORY_API_URL = "https://opentdb.com/api_category.php"
TOKEN_REQUEST_URL = "https://opentdb.com/api_token.php"
LAUNCHER_FILE = "trivia_launcher.json"
DB_FILE = "trivia.db"

CATEGORIES_PER_PAGE = 20
FINAL_MESSAGE_TIMEOUT_SECONDS = 5
MID_ROUND_TIMEOUT_SECONDS = 15
STATS_MESSAGE_DELETE_SECONDS = 12
CATEGORY_MIN_SAMPLE = 5

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

category_cache: dict[str, int] = {}
scores_by_channel: dict[int, dict[str, dict[int, int]]] = {}
active_sessions: dict[int, "TriviaSession"] = {}
trivia_api_session_token: Optional[str] = None


@dataclass
class TriviaSession:
    owner_id: int
    owner_name: str
    category_name: Optional[str]
    difficulty: str
    total_questions: int
    current_question_number: int = 0
    correct_in_round: int = 0
    question_message: Optional[discord.Message] = None
    result_message: Optional[discord.Message] = None
    round_recorded: bool = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_session_key() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:00")


def current_session_label() -> str:
    now = datetime.now()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start.replace(minute=59, second=59)
    return f"{hour_start.strftime('%I:%M %p')} - {hour_end.strftime('%I:%M %p')}"


def decode(s: str) -> str:
    return html.unescape(s)


def format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def format_mode_label(total_questions: int) -> str:
    return "Single Question" if total_questions == 1 else f"{total_questions} Questions"


async def safe_delete_message(message: Optional[discord.Message]) -> None:
    if not message:
        return

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def safe_edit_message(message: Optional[discord.Message], **kwargs) -> bool:
    if not message:
        return False

    try:
        await message.edit(**kwargs)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


def get_active_session(channel_id: int) -> Optional["TriviaSession"]:
    return active_sessions.get(channel_id)


def create_session(
    *,
    channel_id: int,
    owner_id: int,
    owner_name: str,
    category_name: Optional[str],
    difficulty: str,
    total_questions: int,
) -> "TriviaSession":
    session = TriviaSession(
        owner_id=owner_id,
        owner_name=owner_name,
        category_name=category_name,
        difficulty=difficulty,
        total_questions=total_questions,
    )
    active_sessions[channel_id] = session
    return session


async def cleanup_session_messages(session: "TriviaSession") -> None:
    await safe_delete_message(session.question_message)
    await safe_delete_message(session.result_message)
    session.question_message = None
    session.result_message = None


async def end_session(channel_id: int, *, delete_messages: bool = True) -> None:
    session = get_active_session(channel_id)
    if not session:
        return

    if delete_messages:
        await cleanup_session_messages(session)

    active_sessions.pop(channel_id, None)


def record_round_if_needed(session: "TriviaSession", *, completed: bool) -> None:
    if session.round_recorded:
        return

    record_round_result(
        session.owner_id,
        session.owner_name,
        session.correct_in_round,
        completed=completed,
    )
    session.round_recorded = True


def get_channel_session_scores(channel_id: int) -> dict[int, int]:
    session_key = current_session_key()

    if channel_id not in scores_by_channel:
        scores_by_channel[channel_id] = {}

    if session_key not in scores_by_channel[channel_id]:
        scores_by_channel[channel_id][session_key] = {}

    old_keys = [key for key in scores_by_channel[channel_id] if key != session_key]
    for key in old_keys:
        del scores_by_channel[channel_id][key]

    return scores_by_channel[channel_id][session_key]


def add_point(channel_id: int, user_id: int) -> int:
    session_scores = get_channel_session_scores(channel_id)
    session_scores[user_id] = session_scores.get(user_id, 0) + 1
    return session_scores[user_id]


def reset_channel_hourly_scores(channel_id: int) -> None:
    scores_by_channel[channel_id] = {current_session_key(): {}}


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column_exists(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = {row["name"] for row in cur.fetchall()}
    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db() -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            total_correct INTEGER NOT NULL DEFAULT 0,
            questions_answered INTEGER NOT NULL DEFAULT 0,
            rounds_played INTEGER NOT NULL DEFAULT 0,
            rounds_completed INTEGER NOT NULL DEFAULT 0,
            total_round_score INTEGER NOT NULL DEFAULT 0,
            best_round_score INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT
        )
        """
    )

    ensure_column_exists(conn, "players", "current_streak", "INTEGER NOT NULL DEFAULT 0")
    ensure_column_exists(conn, "players", "best_streak", "INTEGER NOT NULL DEFAULT 0")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS category_stats (
            user_id INTEGER NOT NULL,
            category_name TEXT NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            answered_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, category_name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            category_name TEXT NOT NULL,
            correct INTEGER NOT NULL,
            answered_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def ensure_player_exists(user_id: int, username: str) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM players WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()

    now_iso = utc_now_iso()

    if existing:
        cur.execute(
            """
            UPDATE players
            SET username = ?, last_seen = ?
            WHERE user_id = ?
            """,
            (username, now_iso, user_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO players (
                user_id,
                username,
                total_correct,
                questions_answered,
                rounds_played,
                rounds_completed,
                total_round_score,
                best_round_score,
                current_streak,
                best_streak,
                last_seen
            )
            VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?)
            """,
            (user_id, username, now_iso),
        )

    conn.commit()
    conn.close()


def record_answer(user_id: int, username: str, category_name: str, correct: bool) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    now_iso = utc_now_iso()

    cur.execute(
        """
        SELECT total_correct, questions_answered, current_streak, best_streak
        FROM players
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()

    if row is None:
        current_streak = 1 if correct else 0
        best_streak = current_streak
        cur.execute(
            """
            INSERT INTO players (
                user_id,
                username,
                total_correct,
                questions_answered,
                rounds_played,
                rounds_completed,
                total_round_score,
                best_round_score,
                current_streak,
                best_streak,
                last_seen
            )
            VALUES (?, ?, ?, 1, 0, 0, 0, 0, ?, ?, ?)
            """,
            (
                user_id,
                username,
                1 if correct else 0,
                current_streak,
                best_streak,
                now_iso,
            ),
        )
    else:
        current_streak = (row["current_streak"] + 1) if correct else 0
        best_streak = max(row["best_streak"], current_streak)
        cur.execute(
            """
            UPDATE players
            SET username = ?,
                total_correct = total_correct + ?,
                questions_answered = questions_answered + 1,
                current_streak = ?,
                best_streak = ?,
                last_seen = ?
            WHERE user_id = ?
            """,
            (
                username,
                1 if correct else 0,
                current_streak,
                best_streak,
                now_iso,
                user_id,
            ),
        )

    cur.execute(
        """
        SELECT correct_count, answered_count
        FROM category_stats
        WHERE user_id = ? AND category_name = ?
        """,
        (user_id, category_name),
    )
    category_row = cur.fetchone()

    if category_row is None:
        cur.execute(
            """
            INSERT INTO category_stats (user_id, category_name, correct_count, answered_count)
            VALUES (?, ?, ?, 1)
            """,
            (user_id, category_name, 1 if correct else 0),
        )
    else:
        cur.execute(
            """
            UPDATE category_stats
            SET correct_count = correct_count + ?,
                answered_count = answered_count + 1
            WHERE user_id = ? AND category_name = ?
            """,
            (1 if correct else 0, user_id, category_name),
        )

    cur.execute(
        """
        INSERT INTO answer_history (user_id, username, category_name, correct, answered_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, username, category_name, 1 if correct else 0, now_iso),
    )

    conn.commit()
    conn.close()


def record_round_result(user_id: int, username: str, round_score: int, completed: bool) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    now_iso = utc_now_iso()

    cur.execute("SELECT user_id, best_round_score FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO players (
                user_id,
                username,
                total_correct,
                questions_answered,
                rounds_played,
                rounds_completed,
                total_round_score,
                best_round_score,
                current_streak,
                best_streak,
                last_seen
            )
            VALUES (?, ?, 0, 0, 1, ?, ?, ?, 0, 0, ?)
            """,
            (
                user_id,
                username,
                1 if completed else 0,
                round_score,
                round_score,
                now_iso,
            ),
        )
    else:
        best_round_score = max(row["best_round_score"], round_score)
        cur.execute(
            """
            UPDATE players
            SET username = ?,
                rounds_played = rounds_played + 1,
                rounds_completed = rounds_completed + ?,
                total_round_score = total_round_score + ?,
                best_round_score = ?,
                last_seen = ?
            WHERE user_id = ?
            """,
            (
                username,
                1 if completed else 0,
                round_score,
                best_round_score,
                now_iso,
                user_id,
            ),
        )

    conn.commit()
    conn.close()


def get_all_time_leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            user_id,
            username,
            total_correct,
            questions_answered,
            rounds_played,
            rounds_completed,
            best_round_score
        FROM players
        WHERE total_correct > 0 OR questions_answered > 0 OR rounds_played > 0
        ORDER BY total_correct DESC, questions_answered DESC, username COLLATE NOCASE ASC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def get_player_stats(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            user_id,
            username,
            total_correct,
            questions_answered,
            rounds_played,
            rounds_completed,
            total_round_score,
            best_round_score,
            current_streak,
            best_streak,
            last_seen
        FROM players
        WHERE user_id = ?
        """,
        (user_id,),
    )

    row = cur.fetchone()
    conn.close()
    return row


def get_category_stats_for_user(user_id: int) -> list[sqlite3.Row]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            category_name,
            correct_count,
            answered_count
        FROM category_stats
        WHERE user_id = ?
        ORDER BY answered_count DESC, correct_count DESC, category_name COLLATE NOCASE ASC
        """,
        (user_id,),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def get_recent_performance(user_id: int, limit: int) -> tuple[int, int]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT correct
        FROM answer_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )

    rows = cur.fetchall()
    conn.close()

    total = len(rows)
    correct = sum(row["correct"] for row in rows)
    return correct, total


def reset_user_stats(user_id: int) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM players WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM category_stats WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM answer_history WHERE user_id = ?", (user_id,))

    conn.commit()
    conn.close()


def reset_all_stats() -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM players")
    cur.execute("DELETE FROM category_stats")
    cur.execute("DELETE FROM answer_history")

    conn.commit()
    conn.close()


async def fetch_json(url: str, params: dict | None = None) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=15) as response:
            response.raise_for_status()
            return await response.json()


async def load_categories() -> dict[str, int]:
    global category_cache
    if category_cache:
        return category_cache

    data = await fetch_json(CATEGORY_API_URL)
    categories = data.get("trivia_categories", [])
    category_cache = {item["name"]: item["id"] for item in categories}
    return category_cache


async def ensure_trivia_api_session_token(reset: bool = False) -> Optional[str]:
    global trivia_api_session_token

    params = {"command": "reset" if reset and trivia_api_session_token else "request"}
    if reset and trivia_api_session_token:
        params["token"] = trivia_api_session_token

    data = await fetch_json(TOKEN_REQUEST_URL, params=params)

    response_code = data.get("response_code")
    token = data.get("token")

    if response_code in (0, 1, 2, 3, 4) and token:
        trivia_api_session_token = token
        return token

    return trivia_api_session_token


async def fetch_trivia_question(
    *,
    category_name: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> Optional[dict]:
    params = {"amount": 1, "type": "multiple"}

    if category_name:
        cats = await load_categories()
        category_id = cats.get(category_name)
        if not category_id:
            return None
        params["category"] = category_id

    if difficulty and difficulty != "any":
        params["difficulty"] = difficulty

    token = await ensure_trivia_api_session_token()
    if token:
        params["token"] = token

    data = await fetch_json(TRIVIA_API_URL, params=params)
    response_code = data.get("response_code")

    if response_code == 4:
        token = await ensure_trivia_api_session_token(reset=True)
        if token:
            params["token"] = token
        data = await fetch_json(TRIVIA_API_URL, params=params)
        response_code = data.get("response_code")
    elif response_code == 3:
        token = await ensure_trivia_api_session_token(reset=False)
        if token:
            params["token"] = token
        data = await fetch_json(TRIVIA_API_URL, params=params)
        response_code = data.get("response_code")

    if response_code != 0 or not data.get("results"):
        return None

    q = data["results"][0]

    question = decode(q["question"])
    correct = decode(q["correct_answer"])
    incorrect = [decode(x) for x in q["incorrect_answers"]]
    answers = incorrect + [correct]
    random.shuffle(answers)

    return {
        "question": question,
        "correct_answer": correct,
        "answers": answers,
        "category": decode(q["category"]),
        "difficulty": decode(q["difficulty"]).title(),
    }


async def send_next_question(channel: discord.abc.Messageable, channel_id: int) -> bool:
    session = get_active_session(channel_id)
    if not session:
        return False

    question_data = await fetch_trivia_question(
        category_name=session.category_name,
        difficulty=session.difficulty,
    )

    if not question_data:
        await channel.send("Could not fetch a trivia question with those settings right now.")
        await end_session(channel_id)
        return False

    session.current_question_number += 1

    embed = discord.Embed(
        title=f"Trivia — Question {session.current_question_number}/{session.total_questions}",
        description=question_data["question"],
    )
    embed.add_field(name="Category", value=question_data["category"], inline=True)
    embed.add_field(name="Difficulty", value=question_data["difficulty"], inline=True)

    view = TriviaAnswerView(
        channel_id=channel_id,
        correct_answer=question_data["correct_answer"],
        answers=question_data["answers"],
        category_name=question_data["category"],
    )

    message = await channel.send(embed=embed, view=view)
    view.message = message
    session.question_message = message
    return True


async def start_new_round_from_settings(
    *,
    channel: discord.abc.Messageable,
    channel_id: int,
    owner_id: int,
    owner_name: str,
    category_name: Optional[str],
    difficulty: str,
    total_questions: int,
) -> None:
    if get_active_session(channel_id):
        return

    create_session(
        channel_id=channel_id,
        owner_id=owner_id,
        owner_name=owner_name,
        category_name=category_name,
        difficulty=difficulty,
        total_questions=total_questions,
    )
    await send_next_question(channel, channel_id)


async def open_trivia_menu(interaction: discord.Interaction) -> None:
    channel_id = interaction.channel_id

    if get_active_session(channel_id):
        await interaction.response.send_message(
            "There is already an active trivia session in this channel.",
            ephemeral=True,
        )
        return

    cats = await load_categories()
    names = sorted(cats.keys())

    view = CategoryMenuView(names, page=0)
    await interaction.response.send_message(
        content=view.build_message(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


async def show_round_controls(
    *,
    channel: discord.abc.Messageable,
    channel_id: int,
    result_text: str,
) -> None:
    session = get_active_session(channel_id)
    if not session:
        return

    await safe_delete_message(session.result_message)
    session.result_message = None

    if session.current_question_number < session.total_questions:
        view = RoundControlView(channel_id=channel_id)
        result_message = await channel.send(
            f"{result_text}\n\nRound progress: {session.current_question_number}/{session.total_questions}\n"
            f"Round score: {session.correct_in_round}/{session.current_question_number}",
            view=view,
        )
        view.message = result_message
        session.result_message = result_message
    else:
        owner_id = session.owner_id
        owner_name = session.owner_name
        category_name = session.category_name
        difficulty = session.difficulty
        total_questions = session.total_questions

        summary = (
            f"{result_text}\n\n"
            f"Round finished.\n"
            f"Final score: {session.correct_in_round}/{session.total_questions}"
        )

        record_round_if_needed(session, completed=True)

        await safe_delete_message(session.question_message)
        await safe_delete_message(session.result_message)
        await end_session(channel_id, delete_messages=False)

        play_again_view = PlayAgainView(
            channel_id=channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            category_name=category_name,
            difficulty=difficulty,
            total_questions=total_questions,
        )
        result_message = await channel.send(summary, view=play_again_view)
        play_again_view.message = result_message


def save_launcher_info(channel_id: int, message_id: int) -> None:
    with open(LAUNCHER_FILE, "w", encoding="utf-8") as f:
        json.dump({"channel_id": channel_id, "message_id": message_id}, f)


def load_launcher_info() -> Optional[dict]:
    if not os.path.exists(LAUNCHER_FILE):
        return None

    try:
        with open(LAUNCHER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "channel_id" in data and "message_id" in data:
            return data
    except (OSError, json.JSONDecodeError):
        return None

    return None


def clear_launcher_info() -> None:
    try:
        if os.path.exists(LAUNCHER_FILE):
            os.remove(LAUNCHER_FILE)
    except OSError:
        pass


def build_stats_embed(target: discord.abc.User, stats_row: sqlite3.Row, category_rows: list[sqlite3.Row]) -> discord.Embed:
    display_name = getattr(target, "display_name", target.name)

    total_correct = stats_row["total_correct"]
    questions_answered = stats_row["questions_answered"]
    rounds_played = stats_row["rounds_played"]
    rounds_completed = stats_row["rounds_completed"]
    best_round_score = stats_row["best_round_score"]
    best_streak = stats_row["best_streak"]

    overall_accuracy = format_percent(total_correct, questions_answered)

    categories_with_min_sample = [row for row in category_rows if row["answered_count"] >= CATEGORY_MIN_SAMPLE]

    best_category_text = "Not enough data yet"
    worst_category_text = "Not enough data yet"

    if categories_with_min_sample:
        best_category = max(
            categories_with_min_sample,
            key=lambda row: (
                row["correct_count"] / row["answered_count"],
                row["answered_count"],
                row["correct_count"],
            ),
        )
        worst_category = min(
            categories_with_min_sample,
            key=lambda row: (
                row["correct_count"] / row["answered_count"],
                -row["answered_count"],
                -row["correct_count"],
            ),
        )

        best_category_text = (
            f"{best_category['category_name']} — "
            f"{best_category['correct_count']}/{best_category['answered_count']} "
            f"({format_percent(best_category['correct_count'], best_category['answered_count'])})"
        )
        worst_category_text = (
            f"{worst_category['category_name']} — "
            f"{worst_category['correct_count']}/{worst_category['answered_count']} "
            f"({format_percent(worst_category['correct_count'], worst_category['answered_count'])})"
        )

    top_category_lines = []
    for row in category_rows[:4]:
        top_category_lines.append(
            f"{row['category_name']}: {row['correct_count']}/{row['answered_count']} "
            f"({format_percent(row['correct_count'], row['answered_count'])})"
        )

    if not top_category_lines:
        top_category_lines = ["No category stats yet"]

    recent_10_correct, recent_10_total = get_recent_performance(target.id, 10)
    recent_25_correct, recent_25_total = get_recent_performance(target.id, 25)

    embed = discord.Embed(
        title=f"Trivia Stats — {display_name}",
        description="All-time profile",
    )

    avatar = target.display_avatar.url if hasattr(target, "display_avatar") else None
    if avatar:
        embed.set_thumbnail(url=avatar)

    embed.add_field(
        name="Overall",
        value=(
            f"Correct: {total_correct}\n"
            f"Answered: {questions_answered}\n"
            f"Accuracy: {overall_accuracy}\n"
            f"Best streak: {best_streak}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Rounds",
        value=(
            f"Played: {rounds_played}\n"
            f"Completed: {rounds_completed}\n"
            f"Best round score: {best_round_score}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Categories",
        value=(
            f"Best category: {best_category_text}\n"
            f"Worst category: {worst_category_text}\n\n"
            + "\n".join(top_category_lines)
        ),
        inline=False,
    )

    embed.add_field(
        name="Recent Performance",
        value=(
            f"Last 10: {recent_10_correct}/{recent_10_total}\n"
            f"Last 25: {recent_25_correct}/{recent_25_total}"
        ),
        inline=False,
    )

    return embed


class TriviaAnswerView(discord.ui.View):
    def __init__(
        self,
        channel_id: int,
        correct_answer: str,
        answers: list[str],
        category_name: str,
        timeout: int = 30,
    ):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.correct_answer = correct_answer
        self.category_name = category_name
        self.answered = False
        self.message: Optional[discord.Message] = None

        for answer in answers:
            self.add_item(TriviaAnswerButton(answer))

    async def finish_question(self, interaction: Optional[discord.Interaction], selected: Optional[str]) -> None:
        if self.answered:
            return

        self.answered = True

        session = get_active_session(self.channel_id)
        if not session:
            return

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                if item.label == self.correct_answer[:80]:
                    item.style = discord.ButtonStyle.success
                elif selected is not None and item.label == selected[:80] and selected != self.correct_answer:
                    item.style = discord.ButtonStyle.danger

        if interaction is not None:
            edited = await safe_edit_message(interaction.message, view=self)
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            if not edited:
                return
            channel = interaction.channel
            user = interaction.user
        else:
            if self.message:
                await safe_edit_message(self.message, view=self)
                channel = self.message.channel
            else:
                return
            user = None

        if selected is None:
            result_text = f"⏰ Time's up! Correct answer: {self.correct_answer}"
        elif selected == self.correct_answer:
            if user is None:
                result_text = f"✅ Correct! Answer: {self.correct_answer}"
            else:
                new_score = add_point(self.channel_id, user.id)
                record_answer(user.id, user.display_name, self.category_name, True)
                session.correct_in_round += 1
                result_text = (
                    f"✅ Correct! {user.mention} now has "
                    f"**{new_score}** point{'s' if new_score != 1 else ''} this hour."
                )
        else:
            if user is not None:
                record_answer(user.id, user.display_name, self.category_name, False)
            result_text = f"❌ Wrong! Correct answer: {self.correct_answer}"

        await show_round_controls(
            channel=channel,
            channel_id=self.channel_id,
            result_text=result_text,
        )

    async def on_timeout(self):
        if self.answered:
            return
        await self.finish_question(None, None)


class TriviaAnswerButton(discord.ui.Button):
    def __init__(self, answer: str):
        super().__init__(
            label=answer[:80],
            style=discord.ButtonStyle.primary,
        )
        self.answer = answer

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, TriviaAnswerView):
            return

        if view.answered:
            await interaction.response.send_message(
                "This question has already been answered.",
                ephemeral=True,
            )
            return

        await view.finish_question(interaction, self.answer)


class RoundControlView(discord.ui.View):
    def __init__(self, channel_id: int, timeout: int = MID_ROUND_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.message: Optional[discord.Message] = None
        self.handled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.handled:
            await interaction.response.send_message(
                "This control is no longer active.",
                ephemeral=True,
            )
            return False

        session = get_active_session(self.channel_id)
        if not session:
            await interaction.response.send_message(
                "There is no active trivia session in this channel.",
                ephemeral=True,
            )
            return False

        if interaction.user.id != session.owner_id:
            await interaction.response.send_message(
                "Only the person who started this round can continue or end it.",
                ephemeral=True,
            )
            return False

        return True

    async def disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        if self.message:
            await safe_edit_message(self.message, view=self)

    @discord.ui.button(label="Next Question", style=discord.ButtonStyle.primary)
    async def next_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = get_active_session(self.channel_id)
        if not session:
            await interaction.response.send_message(
                "There is no active trivia session in this channel.",
                ephemeral=True,
            )
            return

        self.handled = True
        await self.disable_buttons()
        await interaction.response.defer()

        await safe_delete_message(session.question_message)
        await safe_delete_message(session.result_message)
        session.question_message = None
        session.result_message = None

        await send_next_question(interaction.channel, self.channel_id)

    @discord.ui.button(label="End Session", style=discord.ButtonStyle.danger)
    async def end_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = get_active_session(self.channel_id)
        if not session:
            await interaction.response.send_message(
                "There is no active trivia session in this channel.",
                ephemeral=True,
            )
            return

        self.handled = True
        await self.disable_buttons()

        owner_id = session.owner_id
        owner_name = session.owner_name
        category_name = session.category_name
        difficulty = session.difficulty
        total_questions = session.total_questions
        final_score = session.correct_in_round

        record_round_if_needed(session, completed=False)

        await interaction.response.defer()

        await safe_delete_message(session.question_message)
        await safe_delete_message(session.result_message)
        await end_session(self.channel_id, delete_messages=False)

        play_again_view = PlayAgainView(
            channel_id=self.channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            category_name=category_name,
            difficulty=difficulty,
            total_questions=total_questions,
        )
        message = await interaction.channel.send(
            f"Session ended.\nFinal score: {final_score}/{total_questions}",
            view=play_again_view,
        )
        play_again_view.message = message

    async def on_timeout(self):
        if self.handled:
            return

        session = get_active_session(self.channel_id)

        if self.message:
            await safe_delete_message(self.message)

        if session:
            record_round_if_needed(session, completed=False)
            await safe_delete_message(session.question_message)
            session.question_message = None
            session.result_message = None
            await end_session(self.channel_id, delete_messages=False)


class PlayAgainView(discord.ui.View):
    def __init__(
        self,
        *,
        channel_id: int,
        owner_id: int,
        owner_name: str,
        category_name: Optional[str],
        difficulty: str,
        total_questions: int,
        timeout: int = FINAL_MESSAGE_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.category_name = category_name
        self.difficulty = difficulty
        self.total_questions = total_questions
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who started the last round can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.success)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_active_session(self.channel_id):
            await interaction.response.send_message(
                "There is already an active trivia session in this channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if self.message:
            await safe_delete_message(self.message)

        await start_new_round_from_settings(
            channel=interaction.channel,
            channel_id=self.channel_id,
            owner_id=self.owner_id,
            owner_name=self.owner_name,
            category_name=self.category_name,
            difficulty=self.difficulty,
            total_questions=self.total_questions,
        )

    @discord.ui.button(label="New Setup", style=discord.ButtonStyle.secondary)
    async def new_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_active_session(self.channel_id):
            await interaction.response.send_message(
                "There is already an active trivia session in this channel.",
                ephemeral=True,
            )
            return

        if self.message:
            await safe_delete_message(self.message)

        await open_trivia_menu(interaction)

    async def on_timeout(self):
        if self.message:
            await safe_delete_message(self.message)


class CategorySelect(discord.ui.Select):
    def __init__(self, parent_view: "CategoryMenuView"):
        self.parent_view = parent_view
        categories = parent_view.get_page_categories()

        options = [
            discord.SelectOption(label=name[:100], value=name)
            for name in categories
        ]

        super().__init__(
            placeholder=f"Choose a category (Page {parent_view.page + 1}/{parent_view.total_pages})",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_category = self.values[0]
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.build_message(),
            view=self.parent_view,
        )


class DifficultySelect(discord.ui.Select):
    def __init__(self, parent_view: "CategoryMenuView"):
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label="Any Difficulty",
                value="any",
                default=parent_view.selected_difficulty == "any",
            ),
            discord.SelectOption(
                label="Easy",
                value="easy",
                default=parent_view.selected_difficulty == "easy",
            ),
            discord.SelectOption(
                label="Medium",
                value="medium",
                default=parent_view.selected_difficulty == "medium",
            ),
            discord.SelectOption(
                label="Hard",
                value="hard",
                default=parent_view.selected_difficulty == "hard",
            ),
        ]

        super().__init__(
            placeholder="Choose difficulty",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_difficulty = self.values[0]
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.build_message(),
            view=self.parent_view,
        )


class ModeSelect(discord.ui.Select):
    def __init__(self, parent_view: "CategoryMenuView"):
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label="Single Question",
                value="1",
                default=parent_view.selected_total_questions == 1,
            ),
            discord.SelectOption(
                label="5 Questions",
                value="5",
                default=parent_view.selected_total_questions == 5,
            ),
            discord.SelectOption(
                label="10 Questions",
                value="10",
                default=parent_view.selected_total_questions == 10,
            ),
        ]

        super().__init__(
            placeholder="Choose mode",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_total_questions = int(self.values[0])
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.build_message(),
            view=self.parent_view,
        )


class CategoryMenuView(discord.ui.View):
    def __init__(self, all_categories: list[str], page: int = 0, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.all_categories = all_categories
        self.page = page
        self.selected_category: Optional[str] = None
        self.selected_difficulty = "any"
        self.selected_total_questions = 1
        self.message: Optional[discord.Message] = None
        self.total_pages = max(
            1,
            (len(all_categories) + CATEGORIES_PER_PAGE - 1) // CATEGORIES_PER_PAGE
        )
        self.refresh_components()

    def get_page_categories(self) -> list[str]:
        start = self.page * CATEGORIES_PER_PAGE
        end = start + CATEGORIES_PER_PAGE
        return self.all_categories[start:end]

    def build_message(self) -> str:
        category_text = self.selected_category if self.selected_category else "Random"
        difficulty_text = (
            self.selected_difficulty.title()
            if self.selected_difficulty != "any"
            else "Any"
        )
        mode_text = format_mode_label(self.selected_total_questions)

        return (
            f"Choose a trivia category, difficulty, and mode.\n"
            f"Page {self.page + 1}/{self.total_pages}\n"
            f"Selected category: {category_text}\n"
            f"Selected difficulty: {difficulty_text}\n"
            f"Selected mode: {mode_text}"
        )

    def refresh_components(self):
        self.clear_items()

        self.add_item(CategorySelect(self))
        self.add_item(DifficultySelect(self))
        self.add_item(ModeSelect(self))

        prev_button = discord.ui.Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=3,
        )
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1,
            row=3,
        )
        start_button = discord.ui.Button(
            label="Start",
            style=discord.ButtonStyle.primary,
            row=3,
        )
        random_button = discord.ui.Button(
            label="Random",
            style=discord.ButtonStyle.success,
            row=3,
        )

        async def prev_callback(interaction: discord.Interaction):
            self.page -= 1
            self.refresh_components()
            await interaction.response.edit_message(
                content=self.build_message(),
                view=self,
            )

        async def next_callback(interaction: discord.Interaction):
            self.page += 1
            self.refresh_components()
            await interaction.response.edit_message(
                content=self.build_message(),
                view=self,
            )

        async def start_callback(interaction: discord.Interaction):
            channel_id = interaction.channel_id

            if get_active_session(channel_id):
                await interaction.response.send_message(
                    "There is already an active trivia session in this channel.",
                    ephemeral=True,
                )
                return

            create_session(
                channel_id=channel_id,
                owner_id=interaction.user.id,
                owner_name=interaction.user.display_name,
                category_name=self.selected_category,
                difficulty=self.selected_difficulty,
                total_questions=self.selected_total_questions,
            )

            ensure_player_exists(interaction.user.id, interaction.user.display_name)

            await interaction.response.defer()
            await safe_delete_message(self.message)
            await send_next_question(interaction.channel, channel_id)

        async def random_callback(interaction: discord.Interaction):
            channel_id = interaction.channel_id

            if get_active_session(channel_id):
                await interaction.response.send_message(
                    "There is already an active trivia session in this channel.",
                    ephemeral=True,
                )
                return

            self.selected_category = None

            create_session(
                channel_id=channel_id,
                owner_id=interaction.user.id,
                owner_name=interaction.user.display_name,
                category_name=None,
                difficulty=self.selected_difficulty,
                total_questions=self.selected_total_questions,
            )

            ensure_player_exists(interaction.user.id, interaction.user.display_name)

            await interaction.response.defer()
            await safe_delete_message(self.message)
            await send_next_question(interaction.channel, channel_id)

        prev_button.callback = prev_callback
        next_button.callback = next_callback
        start_button.callback = start_callback
        random_button.callback = random_callback

        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(start_button)
        self.add_item(random_button)

    async def on_timeout(self):
        await safe_delete_message(self.message)


class LauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Trivia", style=discord.ButtonStyle.success, custom_id="trivia_launcher_start")
    async def start_trivia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_trivia_menu(interaction)


async def create_or_replace_launcher(channel: discord.TextChannel) -> discord.Message:
    existing = load_launcher_info()

    if existing:
        try:
            old_channel = bot.get_channel(existing["channel_id"])
            if old_channel is None:
                old_channel = await bot.fetch_channel(existing["channel_id"])
            old_message = await old_channel.fetch_message(existing["message_id"])
            await safe_delete_message(old_message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    embed = discord.Embed(
        title="Trivia Launcher",
        description="Click the button below to start a trivia game.",
    )
    embed.add_field(name="Modes", value="Single Question, 5 Questions, 10 Questions", inline=False)
    embed.add_field(name="Extras", value="Uses anti-repeat question sessions.", inline=False)

    view = LauncherView()
    message = await channel.send(embed=embed, view=view)
    save_launcher_info(channel.id, message.id)
    return message


async def delete_saved_launcher_message() -> bool:
    existing = load_launcher_info()
    if not existing:
        return False

    try:
        old_channel = bot.get_channel(existing["channel_id"])
        if old_channel is None:
            old_channel = await bot.fetch_channel(existing["channel_id"])
        old_message = await old_channel.fetch_message(existing["message_id"])
        await safe_delete_message(old_message)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

    clear_launcher_info()
    return True


@bot.event
async def on_ready():
    init_db()
    await ensure_trivia_api_session_token()
    await load_categories()
    bot.add_view(LauncherView())
    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    print(f"Synced {len(synced)} slash command(s)")


@bot.tree.command(name="trivia", description="Open the trivia menu")
async def trivia(interaction: discord.Interaction):
    await open_trivia_menu(interaction)


@bot.tree.command(name="leaderboard", description="Show the current hour's trivia leaderboard")
async def leaderboard(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    session_scores = get_channel_session_scores(channel_id)

    if not session_scores:
        await interaction.response.send_message(
            f"No scores yet for this hour.\nSession: {current_session_label()}"
        )
        leaderboard_message = await interaction.original_response()
        await asyncio.sleep(5)
        await safe_delete_message(leaderboard_message)
        return

    sorted_scores = sorted(
        session_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    lines = []
    medals = ["🥇", "🥈", "🥉"]

    for index, (user_id, score) in enumerate(sorted_scores[:10], start=1):
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(f"{prefix} <@{user_id}> — {score}")

    embed = discord.Embed(
        title="Trivia Leaderboard",
        description="\n".join(lines),
    )
    embed.add_field(name="Session", value=current_session_label(), inline=False)

    await interaction.response.send_message(embed=embed)
    leaderboard_message = await interaction.original_response()
    await asyncio.sleep(5)
    await safe_delete_message(leaderboard_message)


@bot.tree.command(name="leaderboard_all_time", description="Show the all-time trivia leaderboard")
async def leaderboard_all_time(interaction: discord.Interaction):
    rows = get_all_time_leaderboard(limit=10)

    if not rows:
        await interaction.response.send_message("No all-time trivia stats yet.")
        leaderboard_message = await interaction.original_response()
        await asyncio.sleep(5)
        await safe_delete_message(leaderboard_message)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []

    for index, row in enumerate(rows, start=1):
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(
            f"{prefix} <@{row['user_id']}> — {row['total_correct']} correct "
            f"(answered: {row['questions_answered']}, rounds: {row['rounds_played']}, best round: {row['best_round_score']})"
        )

    embed = discord.Embed(
        title="All-Time Trivia Leaderboard",
        description="\n".join(lines),
    )

    await interaction.response.send_message(embed=embed)
    leaderboard_message = await interaction.original_response()
    await asyncio.sleep(5)
    await safe_delete_message(leaderboard_message)


@bot.tree.command(name="stats", description="Show trivia stats for yourself or another user")
async def stats(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    target_name = getattr(target, "display_name", target.name)

    stats_row = get_player_stats(target.id)
    if not stats_row:
        await interaction.response.send_message(f"No trivia stats yet for {target_name}.")
        stats_message = await interaction.original_response()
        await asyncio.sleep(STATS_MESSAGE_DELETE_SECONDS)
        await safe_delete_message(stats_message)
        return

    category_rows = get_category_stats_for_user(target.id)
    embed = build_stats_embed(target, stats_row, category_rows)

    await interaction.response.send_message(embed=embed)
    stats_message = await interaction.original_response()
    await asyncio.sleep(STATS_MESSAGE_DELETE_SECONDS)
    await safe_delete_message(stats_message)


@bot.tree.command(name="setup_trivia_launcher", description="Create or replace the permanent trivia launcher message")
async def setup_trivia_launcher(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "This command must be used in a server text channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    await create_or_replace_launcher(interaction.channel)
    await interaction.followup.send(
        f"Trivia launcher created in {interaction.channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="refresh_trivia_launcher", description="Recreate the stored trivia launcher message")
async def refresh_trivia_launcher(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    launcher_info = load_launcher_info()
    if not launcher_info:
        await interaction.response.send_message(
            "No saved trivia launcher was found yet. Use /setup_trivia_launcher first.",
            ephemeral=True,
        )
        return

    try:
        channel = bot.get_channel(launcher_info["channel_id"])
        if channel is None:
            channel = await bot.fetch_channel(launcher_info["channel_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        clear_launcher_info()
        await interaction.response.send_message(
            "I couldn't access the saved launcher channel anymore. Run /setup_trivia_launcher again.",
            ephemeral=True,
        )
        return

    if not isinstance(channel, discord.TextChannel):
        clear_launcher_info()
        await interaction.response.send_message(
            "The saved launcher channel is no longer a normal text channel. Run /setup_trivia_launcher again.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    await create_or_replace_launcher(channel)
    await interaction.followup.send(
        f"Trivia launcher refreshed in {channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="delete_trivia_launcher", description="Delete the saved trivia launcher message")
async def delete_trivia_launcher(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await delete_saved_launcher_message()

    if deleted:
        await interaction.followup.send("Trivia launcher deleted.", ephemeral=True)
    else:
        await interaction.followup.send("No saved trivia launcher was found.", ephemeral=True)


@bot.tree.command(name="reset_hourly_scores", description="Reset this channel's current hourly trivia scores")
async def reset_hourly_scores(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    reset_channel_hourly_scores(interaction.channel_id)
    await interaction.response.send_message("This channel's hourly trivia scores were reset.", ephemeral=True)


@bot.tree.command(name="reset_user_stats", description="Reset all stored trivia stats for one user")
async def admin_reset_user_stats(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    reset_user_stats(user.id)
    await interaction.response.send_message(
        f"Reset stored trivia stats for {user.display_name}.",
        ephemeral=True,
    )


@bot.tree.command(name="reset_all_trivia_stats", description="Reset all stored trivia stats for everyone")
async def reset_all_trivia_stats(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    reset_all_stats()
    scores_by_channel.clear()
    await interaction.response.send_message(
        "Reset all stored trivia stats and hourly scores.",
        ephemeral=True,
    )


@bot.tree.command(name="cancel_trivia_session", description="Force-end the active trivia session in this channel")
async def cancel_trivia_session(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    session = get_active_session(interaction.channel_id)
    if not session:
        await interaction.response.send_message("There is no active trivia session in this channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await safe_delete_message(session.question_message)
    await safe_delete_message(session.result_message)
    await end_session(interaction.channel_id, delete_messages=False)
    await interaction.followup.send("Active trivia session cancelled.", ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

bot.run(TOKEN)
