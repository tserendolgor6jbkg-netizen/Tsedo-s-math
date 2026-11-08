#!/usr/bin/env python3
"""Local-first HTTP/API server for the Tsedo's math learning platform."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import base64
import binascii
import re
import secrets
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import uuid
import zipfile
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parent
INSTANCE = ROOT / "instance"
DB_PATH = Path(os.environ.get("DATABASE_PATH", INSTANCE / "platform.sqlite3")).resolve()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
APP_ENV = os.environ.get("APP_ENV", "development").lower()
ALLOW_CONSOLE_RESET = os.environ.get("ALLOW_CONSOLE_PASSWORD_RESET", "1" if APP_ENV != "production" else "0").lower() in {"1", "true", "yes"}
SESSION_DAYS = 7
PASSWORD_ITERATIONS = 310_000
MAX_PROFILE_PHOTO_BYTES = 2 * 1024 * 1024
MAX_COVER_IMAGE_BYTES = 4 * 1024 * 1024
MAX_BODY = (MAX_COVER_IMAGE_BYTES * 4 // 3) + 32 * 1024
MAX_LESSON_MEDIA_BYTES = 100 * 1024 * 1024
PROFILE_PHOTO_DIR = DB_PATH.parent / "uploads" / "profiles"
COVER_IMAGE_DIR = DB_PATH.parent / "uploads" / "cover"
LESSON_MEDIA_DIR = DB_PATH.parent / "uploads" / "lessons"
STATIC_FILES = {"index.html", "styles.css", "curriculum.js", "app.js"}
ASSESSMENT_MINUTES = 20
LEVELS = {"basic", "standard", "advanced"}
RATE_LIMITS: dict[tuple[str, str], list[float]] = {}
RATE_LOCK = threading.Lock()
SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def normalize_phone(value: str) -> str:
    """Return Mongolian mobile numbers in a single, searchable format."""
    compact = re.sub(r"[\s()\-]", "", value.strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("8") and len(compact) == 8:
        compact = "+976" + compact
    if not re.fullmatch(r"\+9768\d{7}", compact):
        return ""
    return compact


def normalize_login_identifier(value: str) -> tuple[str, str]:
    """Return (kind, normalized value) for an email address or phone number."""
    identifier = value.strip()
    if "@" in identifier:
        return "email", identifier.lower()
    return "phone", normalize_phone(identifier)


@contextmanager
def connect_db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_curriculum() -> dict:
    source = (ROOT / "curriculum.js").read_text(encoding="utf-8")
    match = re.fullmatch(r"\s*window\.curriculum\s*=\s*(\{.*\})\s*;\s*", source, re.S)
    if not match:
        raise RuntimeError("curriculum.js нь хүлээгдсэн өгөгдлийн бүтэцтэй биш байна.")
    literal = match.group(1)
    literal = re.sub(r'([{\s,])([A-Za-z_$][\w$]*)(\s*:)', r'\1"\2"\3', literal)
    try:
        return json.loads(literal)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"curriculum.js JSON болгон уншигдсангүй: {error}") from error


def load_lessons() -> list[dict]:
    records = json.loads((ROOT / "data" / "lessons.json").read_text(encoding="utf-8"))
    if not isinstance(records, dict) or not isinstance(records.get("lessons"), list):
        raise RuntimeError("data/lessons.json хичээлийн жагсаалт агуулах ёстой.")
    curriculum = load_curriculum()
    chapter_index = {chapter["id"]: chapter for chapter in curriculum["chapters"]}
    lesson_ids: set[str] = set()
    quiz_ids: set[str] = set()
    for lesson in records["lessons"]:
        chapter = chapter_index.get(lesson.get("chapter_id"))
        if not chapter or not re.fullmatch(r"[a-z0-9-]+", lesson.get("id", "")):
            raise RuntimeError("Цахим хичээлийн бүлэг эсвэл ID буруу байна.")
        objective_codes = {item["code"] for item in chapter["objectives"]}
        criteria_ids = {item["id"] for item in chapter["criteria"]}
        if (
            lesson.get("topic") not in chapter["topics"]
            or not set(lesson.get("objective_codes", [])).issubset(objective_codes)
            or not set(lesson.get("criteria", [])).issubset(criteria_ids)
        ):
            raise RuntimeError(f"Цахим хичээлийн сэдэв, зорилт, шалгуур хөтөлбөртэй таарахгүй байна: {lesson['id']}")
        if lesson["id"] in lesson_ids:
            raise RuntimeError(f"Цахим хичээлийн ID давхардсан байна: {lesson['id']}")
        lesson_ids.add(lesson["id"])
        for question in lesson.get("quiz", []):
            if not re.fullmatch(r"[a-z0-9-]+", question.get("id", "")) or question["id"] in quiz_ids:
                raise RuntimeError(f"Мини сорилын ID буруу эсвэл давхардсан байна: {question.get('id')}")
            if not question.get("answer") or not question.get("explanation") or question.get("points", 0) < 1:
                raise RuntimeError(f"Мини сорилын хариу, тайлбар, оноо дутуу байна: {question['id']}")
            quiz_ids.add(question["id"])
    for lesson in records["lessons"]:
        if any(link["lesson_id"] not in lesson_ids for link in lesson.get("connections", [])):
            raise RuntimeError(f"Сэдэв хоорондын холбоос олдсонгүй: {lesson['id']}")
    return records["lessons"]


def load_eesh_quizzes() -> list[dict]:
    path = ROOT / "data" / "eesh_quizzes.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, dict) or not isinstance(records.get("quizzes"), list):
        raise RuntimeError("data/eesh_quizzes.json нь quizzes жагсаалт агуулах ёстой.")
    quiz_ids: set[str] = set()
    for quiz in records["quizzes"]:
        if not isinstance(quiz, dict):
            raise RuntimeError("ЭЕШ сорилын мэдээлэл объект байх ёстой.")
        quiz_id = quiz.get("id", "")
        if (
            not isinstance(quiz_id, str)
            or not re.fullmatch(r"eesh-[a-z0-9-]+", quiz_id)
            or quiz_id in quiz_ids
            or type(quiz.get("year")) is not int
            or not 2000 <= quiz["year"] <= 2100
            or not isinstance(quiz.get("variant"), str)
            or not re.fullmatch(r"[А-ЯA-Z0-9-]{1,12}", quiz.get("variant", ""))
            or type(quiz.get("duration_minutes")) is not int
            or not 1 <= quiz["duration_minutes"] <= 300
            or not isinstance(quiz.get("questions"), list)
            or not quiz["questions"]
        ):
            raise RuntimeError(f"ЭЕШ сорилын мэдээлэл буруу эсвэл асуултгүй байна: {quiz_id}")
        quiz_ids.add(quiz_id)
        question_ids: set[str] = set()
        for question in quiz["questions"]:
            if not isinstance(question, dict):
                raise RuntimeError(f"ЭЕШ сорилын асуулт объект байх ёстой: {quiz_id}")
            question_id = question.get("id", "")
            if (
                not isinstance(question_id, str)
                or not isinstance(question.get("prompt"), str)
                or not isinstance(question.get("answer"), str)
                or not isinstance(question.get("explanation"), str)
                or not re.fullmatch(r"[a-z0-9-]+", question_id)
                or question_id in question_ids
                or not question.get("prompt")
                or not question.get("answer")
                or not question.get("explanation")
                or type(question.get("points")) is not int
                or question["points"] < 1
            ):
                raise RuntimeError(f"ЭЕШ сорилын асуултын мэдээлэл дутуу эсвэл буруу байна: {quiz_id}/{question_id}")
            question_ids.add(question_id)
    return records["quizzes"]


def load_eesh_materials() -> list[dict]:
    path = ROOT / "data" / "eesh_materials.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, dict) or not isinstance(records.get("materials"), list):
        raise RuntimeError("data/eesh_materials.json нь materials жагсаалт агуулах ёстой.")
    material_ids: set[str] = set()
    filenames: set[str] = set()
    for material in records["materials"]:
        if not isinstance(material, dict):
            raise RuntimeError("ЭЕШ материалын мэдээлэл объект байх ёстой.")
        material_id = material.get("id", "")
        filename = material.get("filename", "")
        year = material.get("year")
        variant = material.get("variant", "")
        variant_slug = {"А": "a", "Б": "b", "В": "v", "Г": "g", "Д": "d"}.get(
            variant, variant.lower() if isinstance(variant, str) else ""
        )
        if (
            not isinstance(material_id, str)
            or not re.fullmatch(r"eesh-material-(?:19|20)\d{2}-[a-z]", material_id)
            or material_id in material_ids
            or type(year) is not int
            or not 1900 <= year <= 2100
            or not isinstance(variant, str)
            or not re.fullmatch(r"[А-ЯA-Z]", variant)
            or not isinstance(filename, str)
            or not re.fullmatch(r"(?:19|20)\d{2}-[a-z]\.pptx", filename)
            or filename in filenames
            or material_id != f"eesh-material-{year}-{variant_slug}"
            or filename != f"{year}-{variant_slug}.pptx"
            or not isinstance(material.get("title"), str)
            or not material["title"].strip()
            or type(material.get("slide_count")) is not int
            or not 1 <= material["slide_count"] <= 200
        ):
            raise RuntimeError(f"ЭЕШ материалын мэдээлэл буруу эсвэл давхардсан байна: {material_id}")
        file_path = (ROOT / "data" / "eesh_materials" / filename).resolve()
        if file_path.parent != (ROOT / "data" / "eesh_materials").resolve() or not file_path.is_file():
            raise RuntimeError(f"ЭЕШ материалын файл олдсонгүй: {filename}")
        material_ids.add(material_id)
        filenames.add(filename)
    return records["materials"]


def public_eesh_quiz(quiz: dict, include_answers: bool = False) -> dict:
    return {
        key: value for key, value in quiz.items() if key != "questions"
    } | {
        "questions": [
            {key: value for key, value in question.items() if include_answers or key not in {"answer", "explanation"}}
            for question in quiz["questions"]
        ]
    }


def public_lesson(lesson: dict) -> dict:
    return {
        **lesson,
        "quiz": [
            {key: value for key, value in question.items() if key != "answer"}
            for question in lesson["quiz"]
        ],
    }


def lesson_media_catalog(user_id: str | None = None) -> dict[str, list[dict]]:
    with connect_db() as db:
        rows = db.execute(
            """SELECT id, lesson_id, title, original_name, extension, media_type,
                      size_bytes, uploaded_by, created_at
               FROM lesson_media ORDER BY created_at DESC"""
        ).fetchall()
    catalog: dict[str, list[dict]] = {}
    for row in rows:
        catalog.setdefault(row["lesson_id"], []).append({
            "id": row["id"],
            "title": row["title"],
            "original_name": row["original_name"],
            "extension": row["extension"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "url": f"/media/lessons/{row['id']}.{row['extension']}",
            "can_delete": bool(user_id and row["uploaded_by"] == user_id),
        })
    return catalog


def lesson_media_records(lesson_id: str, user_id: str | None = None) -> list[dict]:
    return lesson_media_catalog(user_id).get(lesson_id, [])


def schema() -> None:
    INSTANCE.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                phone TEXT UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('teacher', 'student')),
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS site_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chapters (
                id TEXT PRIMARY KEY,
                number TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                hours INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topics (
                id TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS objectives (
                code TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS criteria (
                id TEXT NOT NULL,
                chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                PRIMARY KEY (chapter_id, id)
            );
            CREATE TABLE IF NOT EXISTS criterion_objectives (
                chapter_id TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                objective_code TEXT NOT NULL REFERENCES objectives(code) ON DELETE CASCADE,
                PRIMARY KEY (chapter_id, criterion_id, objective_code),
                FOREIGN KEY (chapter_id, criterion_id) REFERENCES criteria(chapter_id, id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS learning_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exercises (
                id TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL REFERENCES chapters(id),
                topic TEXT NOT NULL,
                level TEXT NOT NULL CHECK (level IN ('basic', 'standard', 'advanced')),
                prompt TEXT NOT NULL,
                answer_key TEXT NOT NULL,
                explanation TEXT NOT NULL,
                hint TEXT NOT NULL,
                points INTEGER NOT NULL CHECK (points > 0),
                criteria_json TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS exercises_chapter_idx ON exercises(chapter_id, level);
            CREATE TABLE IF NOT EXISTS exercise_attempts (
                id TEXT PRIMARY KEY,
                exercise_id TEXT NOT NULL REFERENCES exercises(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                answer TEXT NOT NULL,
                correct INTEGER NOT NULL,
                points_earned INTEGER NOT NULL,
                elapsed_seconds INTEGER,
                feedback TEXT,
                reviewed_points INTEGER,
                review_comment TEXT,
                reviewed_by TEXT REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS exercise_attempt_user_idx ON exercise_attempts(user_id, created_at);
            CREATE TABLE IF NOT EXISTS learning_progress (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, chapter_id)
            );
            CREATE TABLE IF NOT EXISTS lesson_progress (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                lesson_id TEXT NOT NULL,
                viewed_at TEXT,
                completed_at TEXT,
                last_quiz_score INTEGER,
                last_quiz_max INTEGER,
                quiz_attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, lesson_id)
            );
            CREATE INDEX IF NOT EXISTS lesson_progress_user_idx ON lesson_progress(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS lesson_media (
                id TEXT PRIMARY KEY,
                lesson_id TEXT NOT NULL,
                title TEXT NOT NULL,
                original_name TEXT NOT NULL,
                extension TEXT NOT NULL CHECK (
                    extension IN ('ppt', 'pptx', 'pdf', 'mp4', 'm4v', 'mov', 'webm', 'mkv')
                ),
                media_type TEXT NOT NULL CHECK (media_type IN ('presentation', 'document', 'video')),
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                uploaded_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lesson_media_lesson_idx ON lesson_media(lesson_id, created_at);
            CREATE TABLE IF NOT EXISTS lesson_quiz_attempts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                lesson_id TEXT NOT NULL,
                quiz_id TEXT NOT NULL,
                quiz_type TEXT NOT NULL CHECK (quiz_type IN ('mini', 'daily')),
                points_earned INTEGER NOT NULL,
                max_points INTEGER NOT NULL,
                submitted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lesson_quiz_user_idx ON lesson_quiz_attempts(user_id, submitted_at);
            CREATE TABLE IF NOT EXISTS lesson_quiz_answers (
                attempt_id TEXT NOT NULL REFERENCES lesson_quiz_attempts(id) ON DELETE CASCADE,
                question_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL,
                expected_answer TEXT NOT NULL,
                correct INTEGER NOT NULL,
                explanation TEXT NOT NULL,
                PRIMARY KEY (attempt_id, question_id)
            );
            CREATE INDEX IF NOT EXISTS lesson_quiz_errors_idx ON lesson_quiz_answers(correct, attempt_id);
            CREATE TABLE IF NOT EXISTS classes (
                id TEXT PRIMARY KEY,
                teacher_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                join_code_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS class_memberships (
                class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                student_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (class_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                teacher_id TEXT NOT NULL REFERENCES users(id),
                exercise_id TEXT NOT NULL REFERENCES exercises(id),
                due_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL REFERENCES chapters(id),
                title TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                question_ids TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS assessment_assignments (
                id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                assessment_id TEXT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                teacher_id TEXT NOT NULL REFERENCES users(id),
                due_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(class_id, assessment_id)
            );
            CREATE TABLE IF NOT EXISTS assessment_attempts (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL REFERENCES assessments(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                submitted_at TEXT,
                answers_json TEXT,
                points_earned INTEGER,
                max_points INTEGER NOT NULL,
                UNIQUE (assessment_id, user_id, id)
            );
            CREATE INDEX IF NOT EXISTS assessment_user_idx ON assessment_attempts(user_id, started_at);
            CREATE TABLE IF NOT EXISTS eesh_quiz_attempts (
                id TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                submitted_at TEXT,
                answers_json TEXT,
                points_earned INTEGER,
                max_points INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS eesh_quiz_user_idx ON eesh_quiz_attempts(user_id, started_at);
            CREATE TABLE IF NOT EXISTS eesh_material_responses (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                material_id TEXT NOT NULL,
                slide_number INTEGER NOT NULL CHECK (slide_number > 0),
                answer TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, material_id, slide_number)
            );
            CREATE TABLE IF NOT EXISTS game_scores (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                class_id TEXT REFERENCES classes(id) ON DELETE SET NULL,
                exercise_id TEXT NOT NULL REFERENCES exercises(id),
                points INTEGER NOT NULL,
                elapsed_seconds INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS badges (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                badge_code TEXT NOT NULL,
                awarded_at TEXT NOT NULL,
                PRIMARY KEY (user_id, badge_code)
            );
            """
        )
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "phone" not in user_columns or any(row["name"] == "email" and row["notnull"] for row in db.execute("PRAGMA table_info(users)")):
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                """CREATE TABLE users_new (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE,
                    phone TEXT UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('teacher', 'student')),
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    profile_image TEXT
                )"""
            )
            existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            db.execute(
                f"""INSERT INTO users_new
                    (id, email, phone, display_name, role, password_hash, created_at, profile_image)
                    SELECT id, email, NULL, display_name, role, password_hash, created_at,
                           {"profile_image" if "profile_image" in existing_columns else "NULL"}
                    FROM users"""
            )
            db.execute("DROP TABLE users")
            db.execute("ALTER TABLE users_new RENAME TO users")
            db.execute("PRAGMA foreign_keys=ON")
            user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "profile_image" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN profile_image TEXT")
        seed_curriculum(db)
        seed_exercises(db)
        seed_assessment(db)


def seed_curriculum(db: sqlite3.Connection) -> None:
    data = load_curriculum()
    for chapter in data["chapters"]:
        db.execute(
            """INSERT OR IGNORE INTO chapters(id, number, title, goal, hours)
               VALUES(?, ?, ?, ?, ?)""",
            (chapter["id"], chapter["number"], chapter["title"], chapter["goal"], chapter["hours"]),
        )
        for position, title in enumerate(chapter["topics"]):
            db.execute(
                "INSERT OR IGNORE INTO topics(id, chapter_id, title, position) VALUES(?, ?, ?, ?)",
                (f'{chapter["id"]}:{position + 1}', chapter["id"], title, position),
            )
        for objective in chapter["objectives"]:
            code = objective["code"]
            db.execute(
                "INSERT OR IGNORE INTO objectives(code, chapter_id, text, starred) VALUES(?, ?, ?, ?)",
                (code, chapter["id"], objective["text"], int(code.endswith("*"))),
            )
        for criterion in chapter["criteria"]:
            db.execute(
                "INSERT OR IGNORE INTO criteria(id, chapter_id, text) VALUES(?, ?, ?)",
                (criterion["id"], chapter["id"], criterion["text"]),
            )
            for code in criterion.get("objectives", []):
                db.execute(
                    """INSERT OR IGNORE INTO criterion_objectives(chapter_id, criterion_id, objective_code)
                       VALUES(?, ?, ?)""",
                    (chapter["id"], criterion["id"], code),
                )
        for position, outcome in enumerate(chapter["outcomes"]):
            db.execute(
                """INSERT INTO learning_outcomes(chapter_id, text, position)
                   SELECT ?, ?, ? WHERE NOT EXISTS(
                     SELECT 1 FROM learning_outcomes WHERE chapter_id = ? AND position = ?
                   )""",
                (chapter["id"], outcome, position, chapter["id"], position),
            )


def seed_exercises(db: sqlite3.Connection) -> None:
    records = json.loads((ROOT / "data" / "exercises.json").read_text(encoding="utf-8"))
    identifiers = [record["id"] for record in records]
    if not records or len(set(identifiers)) != len(identifiers):
        raise RuntimeError("Дасгалын ID давхардсан байна.")
    chapters = {chapter["id"]: chapter for chapter in load_curriculum()["chapters"]}
    for record in records:
        chapter = chapters.get(record.get("chapter_id"))
        if (
            not chapter
            or record.get("topic") not in chapter["topics"]
            or record.get("level") not in LEVELS
            or not record.get("criteria")
            or not set(record["criteria"]).issubset({item["id"] for item in chapter["criteria"]})
            or record.get("points", 0) < 1
        ):
            raise RuntimeError(f"Дасгалын бүлэг, сэдэв, түвшин эсвэл шалгуур буруу байна: {record.get('id')}")
        db.execute(
            """INSERT OR IGNORE INTO exercises
               (id, chapter_id, topic, level, prompt, answer_key, explanation, hint, points, criteria_json)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["id"], record["chapter_id"], record["topic"], record["level"],
                record["prompt"], record["answer"], record["explanation"], record["hint"],
                record["points"], json.dumps(record["criteria"], ensure_ascii=False),
            ),
        )


def seed_assessment(db: sqlite3.Connection) -> None:
    existing_questions = {
        "absolute": [
            "absolute-basic-01", "absolute-basic-04", "absolute-basic-07", "absolute-basic-10",
            "absolute-standard-01", "absolute-standard-03", "absolute-standard-05",
            "absolute-standard-08", "absolute-advanced-04", "absolute-advanced-06",
        ]
    }
    question_counts = {"basic": 4, "standard": 4, "advanced": 2}
    for chapter in load_curriculum()["chapters"]:
        question_ids = existing_questions.get(chapter["id"])
        if question_ids is None:
            rows = db.execute(
                """SELECT id,level,topic FROM exercises
                   WHERE chapter_id=? AND published=1 ORDER BY id""",
                (chapter["id"],),
            ).fetchall()
            question_ids = []
            for level, count in question_counts.items():
                level_rows = [row for row in rows if row["level"] == level]
                selected = []
                for topic in chapter["topics"]:
                    match = next((row for row in level_rows if row["topic"] == topic and row["id"] not in selected), None)
                    if match:
                        selected.append(match["id"])
                    if len(selected) == count:
                        break
                selected.extend(row["id"] for row in level_rows if row["id"] not in selected)
                question_ids.extend(selected[:count])
            if len(question_ids) != sum(question_counts.values()):
                raise RuntimeError(f"{chapter['title']} бүлэгт нэгжийн сорил үүсгэх дасгал хүрэлцэхгүй байна.")

        db.execute(
            """INSERT OR IGNORE INTO assessments(id, chapter_id, title, duration_minutes, question_ids)
               VALUES(?, ?, ?, ?, ?)""",
            (
                f'{chapter["id"]}-unit-test', chapter["id"],
                f'{chapter["title"]} · нэгжийн сорил', ASSESSMENT_MINUTES, json.dumps(question_ids),
            ),
        )


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "email": row["email"], "phone": row["phone"], "display_name": row["display_name"],
        "role": row["role"], "profile_image": row["profile_image"],
    }


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def xlsx_workbook(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_types = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.register_namespace("", spreadsheet)
    ET.register_namespace("r", office_rel)

    def column_name(number: int) -> str:
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        types = ET.Element(f"{{{content_types}}}Types")
        ET.SubElement(types, f"{{{content_types}}}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
        ET.SubElement(types, f"{{{content_types}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
        ET.SubElement(types, f"{{{content_types}}}Override", {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
        for index in range(len(sheets)):
            ET.SubElement(types, f"{{{content_types}}}Override", {"PartName": f"/xl/worksheets/sheet{index + 1}.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})
        archive.writestr("[Content_Types].xml", ET.tostring(types, encoding="utf-8", xml_declaration=True))

        root_rels = ET.Element(f"{{{package_rel}}}Relationships")
        ET.SubElement(root_rels, f"{{{package_rel}}}Relationship", {"Id": "rId1", "Type": f"{office_rel}/officeDocument", "Target": "xl/workbook.xml"})
        archive.writestr("_rels/.rels", ET.tostring(root_rels, encoding="utf-8", xml_declaration=True))

        workbook = ET.Element(f"{{{spreadsheet}}}workbook")
        sheet_nodes = ET.SubElement(workbook, f"{{{spreadsheet}}}sheets")
        rels = ET.Element(f"{{{package_rel}}}Relationships")
        for index, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(sheet_nodes, f"{{{spreadsheet}}}sheet", {"name": name[:31], "sheetId": str(index), f"{{{office_rel}}}id": f"rId{index}"})
            ET.SubElement(rels, f"{{{package_rel}}}Relationship", {
                "Id": f"rId{index}",
                "Type": f"{office_rel}/worksheet",
                "Target": f"worksheets/sheet{index}.xml",
            })
            worksheet = ET.Element(f"{{{spreadsheet}}}worksheet")
            data = ET.SubElement(worksheet, f"{{{spreadsheet}}}sheetData")
            for row_number, values in enumerate(rows, 1):
                row = ET.SubElement(data, f"{{{spreadsheet}}}row", {"r": str(row_number)})
                for column_number, value in enumerate(values, 1):
                    cell = ET.SubElement(row, f"{{{spreadsheet}}}c", {"r": f"{column_name(column_number)}{row_number}", "t": "inlineStr"})
                    inline = ET.SubElement(cell, f"{{{spreadsheet}}}is")
                    text = ET.SubElement(inline, f"{{{spreadsheet}}}t")
                    text.text = "" if value is None else str(value)
            archive.writestr(f"xl/worksheets/sheet{index}.xml", ET.tostring(worksheet, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(rels, encoding="utf-8", xml_declaration=True))
    return output.getvalue()


def safe_exercise(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "chapter_id": row["chapter_id"], "topic": row["topic"],
        "level": row["level"], "prompt": row["prompt"], "points": row["points"],
        "criteria": json.loads(row["criteria_json"]),
    }


def normalize_answer(value: str) -> str:
    normalized = value.strip().lower().replace("−", "-").replace("≤", "<=").replace("≥", ">=")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(
        r"([a-z])([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",
        lambda match: f"{match.group(1)}^{match.group(2).translate(SUPERSCRIPT_DIGITS)}",
        normalized,
    )
    normalized = normalized.replace("эсвэл", "or").replace("∨", "or")
    normalized = normalized.replace("∪", "or")
    return normalized


def answer_matches(submitted: str, expected: str) -> bool:
    normalized = normalize_answer(submitted)
    target = normalize_answer(expected)
    if normalized == target:
        return True
    if "," in normalized and "," in target:
        submitted_parts = [part for part in normalized.split(",") if part]
        target_parts = [part for part in target.split(",") if part]
        if len(submitted_parts) == len(target_parts):
            try:
                as_numbers = lambda values: sorted(float(value) for value in values)
                return as_numbers(submitted_parts) == as_numbers(target_parts)
            except ValueError:
                return False
    return False


def check_rate_limit(key: tuple[str, str], limit: int, window_seconds: int) -> bool:
    now = time.monotonic()
    with RATE_LOCK:
        attempts = [instant for instant in RATE_LIMITS.get(key, []) if now - instant < window_seconds]
        if len(attempts) >= limit:
            RATE_LIMITS[key] = attempts
            return False
        attempts.append(now)
        RATE_LIMITS[key] = attempts
        return True


def send_password_reset(email: str, token: str) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        if ALLOW_CONSOLE_RESET:
            print(f"[НУУЦ ҮГ СЭРГЭЭХ] {email}: код {token} (30 минут хүчинтэй)", flush=True)
            return
        raise RuntimeError("Нууц үг сэргээх имэйл тохируулагдаагүй байна.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ.get("SMTP_FROM")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    if not sender or not username or not password:
        raise RuntimeError("SMTP_FROM, SMTP_USERNAME, SMTP_PASSWORD тохиргоо дутуу байна.")
    message = EmailMessage()
    message["Subject"] = "Tsedo's math · нууц үг сэргээх"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Нууц үгээ шинэчлэх код: {token}\n"
        "Энэ код 30 минут хүчинтэй. Та хүсэлт гаргаагүй бол энэ имэйлийг үл тооно уу."
    )
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.send_message(message)


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = "request_failed"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class Handler(BaseHTTPRequestHandler):
    server_version = "Tsedo's-math/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{timestamp()}] {self.address_string()} {fmt % args}\n")

    def send_json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(
        self, status: int, filename: str, content_type: str, data: bytes, inline: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise APIError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON хүсэлт илгээх шаардлагатай.", "json_required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "Хүсэлтийн хэмжээ буруу байна.") from error
        if length < 1 or length > MAX_BODY:
            raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Хүсэлтийн хэмжээ зөвшөөрөгдөхгүй байна.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "JSON өгөгдөл буруу байна.") from error
        if not isinstance(payload, dict):
            raise APIError(HTTPStatus.BAD_REQUEST, "JSON объект шаардлагатай.")
        return payload

    def check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urlsplit(origin)
        host = self.headers.get("Host", "")
        if parsed.netloc.lower() != host.lower() or parsed.scheme not in {"http", "https"}:
            raise APIError(HTTPStatus.FORBIDDEN, "Хүсэлтийн эх сурвалж зөвшөөрөгдөөгүй.", "origin_not_allowed")

    def current_user(self, required: bool = True) -> sqlite3.Row | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            if required:
                raise APIError(HTTPStatus.UNAUTHORIZED, "Нэвтэрч үргэлжлүүлнэ үү.", "authentication_required")
            return None
        morsel = cookie.get("toonoos_session")
        if not morsel:
            if required:
                raise APIError(HTTPStatus.UNAUTHORIZED, "Нэвтэрч үргэлжлүүлнэ үү.", "authentication_required")
            return None
        with connect_db() as db:
            row = db.execute(
                """SELECT users.* FROM sessions
                   JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
                (token_digest(morsel.value), timestamp()),
            ).fetchone()
        if row is None and required:
            raise APIError(HTTPStatus.UNAUTHORIZED, "Сесс дууссан байна. Дахин нэвтэрнэ үү.", "session_expired")
        return row

    def require_role(self, role: str) -> sqlite3.Row:
        user = self.current_user()
        if user["role"] != role:
            raise APIError(HTTPStatus.FORBIDDEN, "Энэ үйлдлийг хийх эрхгүй байна.", "permission_denied")
        return user

    def do_GET(self) -> None:
        try:
            self.route_get(urlsplit(self.path))
        except APIError as error:
            self.send_json(error.status, {"error": error.code, "message": error.message})
        except Exception as error:
            print(f"[СЕРВЕРИЙН АЛДАА] {error!r}", file=sys.stderr, flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "Сервер хүсэлтийг боловсруулахад алдаа гарлаа."})

    def do_POST(self) -> None:
        try:
            self.check_origin()
            parsed = urlsplit(self.path)
            lesson_media_match = re.fullmatch(
                r"/api/learning/lessons/([a-z0-9-]+)/media", unquote(parsed.path)
            )
            if lesson_media_match:
                self.upload_lesson_media(lesson_media_match.group(1), parse_qs(parsed.query))
                return
            payload = self.read_json()
            self.route_post(parsed, payload)
        except APIError as error:
            self.send_json(error.status, {"error": error.code, "message": error.message})
        except Exception as error:
            print(f"[СЕРВЕРИЙН АЛДАА] {error!r}", file=sys.stderr, flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "Сервер хүсэлтийг боловсруулахад алдаа гарлаа."})

    def do_DELETE(self) -> None:
        try:
            self.check_origin()
            match = re.fullmatch(r"/api/learning/media/([a-f0-9]{32})", unquote(urlsplit(self.path).path))
            if match:
                self.delete_lesson_media(match.group(1))
                return
            self.do_POST()
        except APIError as error:
            self.send_json(error.status, {"error": error.code, "message": error.message})
        except Exception as error:
            print(f"[СЕРВЕРИЙН АЛДАА] {error!r}", file=sys.stderr, flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "Сервер хүсэлтийг боловсруулахад алдаа гарлаа."})

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/"):
            self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed", "message": "Энэ хүсэлт зөвшөөрөгдөөгүй."})
            return
        try:
            self.serve_static(urlsplit(self.path).path, head_only=True)
        except APIError as error:
            self.send_json(error.status, {"error": error.code, "message": error.message})

    def route_get(self, parsed) -> None:
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            if path == "/api/health":
                with connect_db() as db:
                    summary = {
                        "chapters": db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
                        "topics": db.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
                        "criteria": db.execute("SELECT COUNT(*) FROM criteria").fetchone()[0],
                        "exercises": db.execute("SELECT COUNT(*) FROM exercises WHERE published=1").fetchone()[0],
                        "database": "SQLite",
                    }
                self.send_json(HTTPStatus.OK, {"status": "ok", **summary})
                return
            if path == "/api/site/cover":
                with connect_db() as db:
                    row = db.execute(
                        "SELECT setting_value FROM site_settings WHERE setting_key='cover_image'"
                    ).fetchone()
                self.send_json(HTTPStatus.OK, {"cover_image": row["setting_value"] if row else None})
                return
            if path == "/api/site/logo":
                with connect_db() as db:
                    row = db.execute(
                        "SELECT setting_value FROM site_settings WHERE setting_key='logo_image'"
                    ).fetchone()
                self.send_json(HTTPStatus.OK, {"logo_image": row["setting_value"] if row else None})
                return
            if path == "/api/learning/lessons":
                lessons = load_lessons()
                user = self.current_user(required=False)
                media_catalog = lesson_media_catalog(user["id"] if user else None)
                available_by_chapter: dict[str, int] = {}
                for lesson in lessons:
                    available_by_chapter[lesson["chapter_id"]] = available_by_chapter.get(lesson["chapter_id"], 0) + 1
                chapter_list = [
                    {
                        "id": chapter["id"], "number": chapter["number"], "title": chapter["title"],
                        "topics": chapter["topics"], "lesson_count": available_by_chapter.get(chapter["id"], 0),
                    }
                    for chapter in load_curriculum()["chapters"]
                ]
                self.send_json(HTTPStatus.OK, {
                    "chapters": chapter_list,
                    "lessons": [
                        {
                            **public_lesson(lesson),
                            "media": media_catalog.get(lesson["id"], []),
                        }
                        for lesson in lessons
                    ],
                })
                return
            lesson_match = re.fullmatch(r"/api/learning/lessons/([a-z0-9-]+)", path)
            if lesson_match:
                lesson = next((item for item in load_lessons() if item["id"] == lesson_match.group(1)), None)
                if not lesson:
                    raise APIError(HTTPStatus.NOT_FOUND, "Цахим хичээл олдсонгүй.")
                user = self.current_user(required=False)
                self.send_json(HTTPStatus.OK, {
                    "lesson": {
                        **public_lesson(lesson),
                        "media": lesson_media_records(lesson["id"], user["id"] if user else None),
                    },
                })
                return
            if path == "/api/learning/quizzes/daily":
                quiz = self.daily_quiz()
                quiz["questions"] = [
                    {key: value for key, value in question.items() if key != "answer"}
                    for question in quiz["questions"]
                ]
                self.send_json(HTTPStatus.OK, quiz)
                return
            if path == "/api/eesh/materials":
                materials = load_eesh_materials()
                self.send_json(HTTPStatus.OK, {"materials": [
                    {key: value for key, value in material.items() if key != "filename"}
                    | {"download_url": f"/api/eesh/materials/{material['id']}/file"}
                    for material in materials
                ]})
                return
            eesh_responses_match = re.fullmatch(
                r"/api/eesh/materials/(eesh-material-(?:19|20)\d{2}-[a-z])/responses", path
            )
            if eesh_responses_match:
                material_id = eesh_responses_match.group(1)
                material = next(
                    (item for item in load_eesh_materials() if item["id"] == material_id),
                    None,
                )
                if not material:
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материал олдсонгүй.")
                student = self.require_role("student")
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT slide_number,answer FROM eesh_material_responses
                           WHERE user_id=? AND material_id=? ORDER BY slide_number""",
                        (student["id"], material_id),
                    ).fetchall()
                self.send_json(HTTPStatus.OK, {
                    "responses": {str(row["slide_number"]): row["answer"] for row in rows},
                })
                return
            eesh_slide_match = re.fullmatch(
                r"/api/eesh/materials/(eesh-material-(?:19|20)\d{2}-[a-z])/slides/(\d+)", path
            )
            if eesh_slide_match:
                material_id, slide_text = eesh_slide_match.groups()
                material = next(
                    (item for item in load_eesh_materials() if item["id"] == material_id),
                    None,
                )
                if not material:
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материал олдсонгүй.")
                self.require_role("student")
                slide_number = int(slide_text)
                if not 1 <= slide_number <= material["slide_count"]:
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материалын слайд олдсонгүй.")
                slide_path = (
                    ROOT / "data" / "eesh_materials" / "slides" / material_id
                    / f"slide-{slide_number:03d}.jpg"
                )
                if not slide_path.is_file():
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материалын слайд олдсонгүй.")
                self.send_file(
                    HTTPStatus.OK,
                    f"{material['filename'][:-5]}-slide-{slide_number:03d}.jpg",
                    "image/jpeg",
                    slide_path.read_bytes(),
                    inline=True,
                )
                return
            eesh_material_match = re.fullmatch(
                r"/api/eesh/materials/(eesh-material-(?:19|20)\d{2}-[a-z])/file", path
            )
            if eesh_material_match:
                material = next(
                    (item for item in load_eesh_materials() if item["id"] == eesh_material_match.group(1)),
                    None,
                )
                if not material:
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материал олдсонгүй.")
                file_path = ROOT / "data" / "eesh_materials" / material["filename"]
                self.send_file(
                    HTTPStatus.OK,
                    material["filename"],
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    file_path.read_bytes(),
                )
                return
            if path == "/api/eesh/quizzes":
                quizzes = load_eesh_quizzes()
                self.send_json(HTTPStatus.OK, {
                    "quizzes": [public_eesh_quiz(quiz) for quiz in quizzes],
                })
                return
            eesh_quiz_match = re.fullmatch(r"/api/eesh/quizzes/(eesh-[a-z0-9-]+)", path)
            if eesh_quiz_match:
                quiz = next((item for item in load_eesh_quizzes() if item["id"] == eesh_quiz_match.group(1)), None)
                if not quiz:
                    raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ сорил олдсонгүй.")
                self.send_json(HTTPStatus.OK, {"quiz": public_eesh_quiz(quiz)})
                return
            if path == "/api/student/learning":
                student = self.require_role("student")
                self.send_json(HTTPStatus.OK, self.student_learning(student["id"]))
                return
            if path == "/api/teacher/learning":
                teacher = self.require_role("teacher")
                self.send_json(HTTPStatus.OK, self.teacher_learning(teacher["id"]))
                return
            if path == "/api/session":
                user = self.current_user(required=False)
                if user is None:
                    self.send_json(HTTPStatus.OK, {"user": None})
                    return
                with connect_db() as db:
                    classes = db.execute(
                        """SELECT classes.id, classes.name, classes.teacher_id, users.display_name AS teacher_name
                           FROM class_memberships JOIN classes ON classes.id=class_memberships.class_id
                           JOIN users ON users.id=classes.teacher_id WHERE class_memberships.student_id=?""",
                        (user["id"],),
                    ).fetchall()
                self.send_json(HTTPStatus.OK, {"user": public_user(user), "classes": [dict(row) for row in classes]})
                return
            if path == "/api/curriculum":
                self.current_user()
                with connect_db() as db:
                    chapters = db.execute("SELECT * FROM chapters ORDER BY rowid").fetchall()
                    result = []
                    for chapter in chapters:
                        item = dict(chapter)
                        item["topics"] = [dict(row) for row in db.execute("SELECT id,title,position FROM topics WHERE chapter_id=? ORDER BY position", (chapter["id"],))]
                        item["objectives"] = [dict(row) for row in db.execute("SELECT code,text,starred FROM objectives WHERE chapter_id=? ORDER BY rowid", (chapter["id"],))]
                        item["criteria"] = [dict(row) for row in db.execute("SELECT id,text FROM criteria WHERE chapter_id=? ORDER BY rowid", (chapter["id"],))]
                        item["outcomes"] = [row["text"] for row in db.execute("SELECT text FROM learning_outcomes WHERE chapter_id=? ORDER BY position", (chapter["id"],))]
                        result.append(item)
                self.send_json(HTTPStatus.OK, {"chapters": result})
                return
            if path == "/api/exercises":
                self.current_user()
                chapter_id = query.get("chapter_id", ["absolute"])[0]
                level = query.get("level", [None])[0]
                if level and level not in LEVELS:
                    raise APIError(HTTPStatus.BAD_REQUEST, "Түвшин basic, standard, advanced-ийн аль нэг байна.")
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT * FROM exercises WHERE chapter_id=? AND published=1
                           AND (? IS NULL OR level=?) ORDER BY
                             CASE level WHEN 'basic' THEN 1 WHEN 'standard' THEN 2 ELSE 3 END, id""",
                        (chapter_id, level, level),
                    ).fetchall()
                self.send_json(HTTPStatus.OK, {"exercises": [safe_exercise(row) for row in rows]})
                return
            if path == "/api/attempts":
                user = self.current_user()
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT a.id,a.exercise_id,a.correct,a.points_earned,a.reviewed_points,a.created_at,
                                  e.chapter_id,e.topic,e.level,e.points,e.criteria_json
                           FROM exercise_attempts a JOIN exercises e ON e.id=a.exercise_id
                           WHERE a.user_id=? ORDER BY a.created_at DESC LIMIT 200""",
                        (user["id"],),
                    ).fetchall()
                attempts = [dict(row) | {"criteria": json.loads(row["criteria_json"])} for row in rows]
                self.send_json(HTTPStatus.OK, {"attempts": attempts})
                return
            if path == "/api/student/dashboard":
                student = self.require_role("student")
                self.send_json(HTTPStatus.OK, self.student_dashboard(student["id"]))
                return
            if path == "/api/student/errors":
                student = self.require_role("student")
                self.send_json(HTTPStatus.OK, self.student_errors(student["id"]))
                return
            if path == "/api/assessments":
                self.current_user()
                with connect_db() as db:
                    rows = db.execute("SELECT id,chapter_id,title,duration_minutes FROM assessments WHERE published=1 ORDER BY title").fetchall()
                self.send_json(HTTPStatus.OK, {"assessments": [dict(row) for row in rows]})
                return
            assessment_match = re.fullmatch(r"/api/assessments/([a-z0-9-]+)", path)
            if assessment_match:
                self.current_user()
                assessment_id = assessment_match.group(1)
                with connect_db() as db:
                    assessment = db.execute("SELECT * FROM assessments WHERE id=? AND published=1", (assessment_id,)).fetchone()
                    if not assessment:
                        raise APIError(HTTPStatus.NOT_FOUND, "Шалгалт олдсонгүй.")
                    ids = json.loads(assessment["question_ids"])
                    placeholders = ",".join("?" for _ in ids)
                    rows = db.execute(f"SELECT * FROM exercises WHERE id IN ({placeholders})", ids).fetchall()
                by_id = {row["id"]: row for row in rows}
                questions = [safe_exercise(by_id[question_id]) for question_id in ids if question_id in by_id]
                self.send_json(HTTPStatus.OK, {"assessment": {
                    "id": assessment["id"], "title": assessment["title"], "chapter_id": assessment["chapter_id"],
                    "duration_minutes": assessment["duration_minutes"], "questions": questions,
                }})
                return
            if path == "/api/teacher/dashboard":
                teacher = self.require_role("teacher")
                self.send_json(HTTPStatus.OK, self.teacher_dashboard(teacher["id"]))
                return
            if path == "/api/teacher/report.xlsx":
                teacher = self.require_role("teacher")
                report = self.teacher_dashboard(teacher["id"])
                sheets = [
                    ("Анги", [["Анги", "Сурагч", "Даалгавар", "Дундаж хувь", "Үнэлэх хариулт"], *[
                        [row["name"], row["student_count"], row["assignment_count"], row["average_percent"], row["pending_review"]]
                        for row in report["classes"]
                    ]]),
                    ("Сурагч", [["Анги", "Сурагч", "Имэйл", "Оролдлого", "Оноо", "Алдсан бодлого", "Багшийн үнэлгээ"], *[
                        [row["class_name"], row["display_name"], row["email"], row["attempts"], row["points"], row["error_questions"], row["teacher_reviews"]]
                        for row in report["students"]
                    ]]),
                    ("Бодлогын хариу", [["Сурагч", "Сэдэв", "Түвшин", "Хариулт", "Автомат оноо", "Багшийн оноо", "Огноо"], *[
                        [row["display_name"], row["topic"], row["level"], row["answer"], row["points_earned"], row["reviewed_points"], row["created_at"]]
                        for row in report["attempts"]
                    ]]),
                    ("Шалгалт", [["Сурагч", "Шалгалт", "Оноо", "Боломжит оноо", "Эхэлсэн", "Илгээсэн"], *[
                        [row["display_name"], row["title"], row["points_earned"], row["max_points"], row["started_at"], row["submitted_at"]]
                        for row in report["assessments"]
                    ]]),
                ]
                self.send_file(
                    HTTPStatus.OK, "tsedo-math-report.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    xlsx_workbook(sheets),
                )
                return
            if path == "/api/classes/my":
                user = self.current_user()
                self.send_json(HTTPStatus.OK, self.classes_for(user))
                return
            if path == "/api/assignments":
                student = self.require_role("student")
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT a.id,a.exercise_id,a.due_at,a.created_at,c.name AS class_name,
                                  e.topic,e.level,e.prompt,
                                  (SELECT COUNT(*) FROM exercise_attempts x
                                   WHERE x.user_id=? AND x.exercise_id=a.exercise_id) AS attempts
                           FROM assignments a
                           JOIN class_memberships m ON m.class_id=a.class_id AND m.student_id=?
                           JOIN classes c ON c.id=a.class_id
                           JOIN exercises e ON e.id=a.exercise_id
                           ORDER BY a.created_at DESC""",
                        (student["id"], student["id"]),
                    ).fetchall()
                self.send_json(HTTPStatus.OK, {"assignments": [dict(row) for row in rows]})
                return
            if path == "/api/assessment-assignments":
                student = self.require_role("student")
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT aa.id,aa.assessment_id,aa.due_at,aa.created_at,c.name AS class_name,
                                  s.title,s.duration_minutes,
                                  (SELECT COUNT(*) FROM assessment_attempts a
                                   WHERE a.user_id=? AND a.assessment_id=aa.assessment_id
                                     AND a.submitted_at IS NOT NULL) AS submitted_attempts
                           FROM assessment_assignments aa
                           JOIN class_memberships m ON m.class_id=aa.class_id AND m.student_id=?
                           JOIN classes c ON c.id=aa.class_id
                           JOIN assessments s ON s.id=aa.assessment_id
                           ORDER BY aa.created_at DESC""",
                        (student["id"], student["id"]),
                    ).fetchall()
                self.send_json(HTTPStatus.OK, {"assignments": [dict(row) for row in rows]})
                return
            if path == "/api/leaderboard":
                user = self.current_user()
                class_id = query.get("class_id", [None])[0]
                self.send_json(HTTPStatus.OK, self.leaderboard(user, class_id))
                return
            raise APIError(HTTPStatus.NOT_FOUND, "API хүсэлт олдсонгүй.", "not_found")
        self.serve_static(path)

    def route_post(self, parsed, payload: dict) -> None:
        path = unquote(parsed.path)
        if path == "/api/auth/register":
            self.register(payload)
            return
        if path == "/api/auth/login":
            self.login(payload)
            return
        if path == "/api/auth/logout":
            self.logout()
            return
        if path == "/api/auth/password-reset/request":
            self.request_password_reset(payload)
            return
        if path == "/api/auth/password-reset/confirm":
            self.confirm_password_reset(payload)
            return
        if path == "/api/profile/photo":
            self.update_profile_photo(payload)
            return
        if path == "/api/profile/email":
            self.update_profile_email(payload)
            return
        if path == "/api/profile/name":
            self.update_profile_name(payload)
            return
        if path == "/api/profile/password":
            self.update_profile_password(payload)
            return
        if path == "/api/site/cover":
            self.update_site_cover(payload)
            return
        if path == "/api/site/logo":
            self.update_site_image(payload, "logo_image", "logo", "site-logo", "Лого зураг")
            return
        lesson_view_match = re.fullmatch(r"/api/learning/lessons/([a-z0-9-]+)/view", path)
        if lesson_view_match:
            self.record_lesson_view(lesson_view_match.group(1))
            return
        lesson_quiz_match = re.fullmatch(r"/api/learning/quizzes/([a-z0-9-]+)/submit", path)
        if lesson_quiz_match:
            self.submit_lesson_quiz(lesson_quiz_match.group(1), payload)
            return
        eesh_start_match = re.fullmatch(r"/api/eesh/quizzes/(eesh-[a-z0-9-]+)/start", path)
        if eesh_start_match:
            self.start_eesh_quiz(eesh_start_match.group(1))
            return
        eesh_submit_match = re.fullmatch(r"/api/eesh/attempts/([a-f0-9-]+)/submit", path)
        if eesh_submit_match:
            self.submit_eesh_quiz(eesh_submit_match.group(1), payload)
            return
        eesh_responses_match = re.fullmatch(
            r"/api/eesh/materials/(eesh-material-(?:19|20)\d{2}-[a-z])/responses", path
        )
        if eesh_responses_match:
            self.save_eesh_material_response(eesh_responses_match.group(1), payload)
            return
        exercise_match = re.fullmatch(r"/api/exercises/([a-z0-9-]+)/attempts", path)
        if exercise_match:
            self.submit_exercise(exercise_match.group(1), payload)
            return
        if path == "/api/classes":
            self.create_class(payload)
            return
        if path == "/api/classes/join":
            self.join_class(payload)
            return
        class_students_match = re.fullmatch(r"/api/classes/([a-f0-9-]+)/students", path)
        if class_students_match:
            self.add_student_to_class(class_students_match.group(1), payload)
            return
        remove_student_match = re.fullmatch(r"/api/classes/([a-f0-9-]+)/students/([a-f0-9-]+)/remove", path)
        if remove_student_match:
            self.remove_student_from_class(remove_student_match.group(1), remove_student_match.group(2))
            return
        assignment_match = re.fullmatch(r"/api/classes/([a-z0-9-]+)/assignments", path)
        if assignment_match:
            self.create_assignment(assignment_match.group(1), payload)
            return
        assessment_assignment_match = re.fullmatch(
            r"/api/classes/([a-z0-9-]+)/assessment-assignments", path
        )
        if assessment_assignment_match:
            self.assign_assessment(assessment_assignment_match.group(1), payload)
            return
        review_match = re.fullmatch(r"/api/teacher/attempts/([a-z0-9-]+)/review", path)
        if review_match:
            self.review_attempt(review_match.group(1), payload)
            return
        start_match = re.fullmatch(r"/api/assessments/([a-z0-9-]+)/start", path)
        if start_match:
            self.start_assessment(start_match.group(1))
            return
        submit_match = re.fullmatch(r"/api/assessments/attempts/([a-z0-9-]+)/submit", path)
        if submit_match:
            self.submit_assessment(submit_match.group(1), payload)
            return
        if path == "/api/games/quiz":
            self.submit_game(payload)
            return
        raise APIError(HTTPStatus.NOT_FOUND, "API хүсэлт олдсонгүй.", "not_found")

    def register(self, payload: dict) -> None:
        ip = self.client_address[0]
        if not check_rate_limit((ip, "register"), 8, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Олон бүртгэл илгээсэн байна. Дараа дахин оролдоно уу.")
        email = str(payload.get("email", "")).strip().lower()
        phone = normalize_phone(str(payload.get("phone", "")))
        name = str(payload.get("display_name", "")).strip()
        password = str(payload.get("password", ""))
        requested_role = payload.get("role", "student")
        if requested_role == "student" and not email and not phone:
            raise APIError(HTTPStatus.BAD_REQUEST, "Имэйл эсвэл 8 оронтой утасны дугаар оруулна уу.")
        if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
            raise APIError(HTTPStatus.BAD_REQUEST, "Имэйл хаягаа шалгана уу.")
        if requested_role == "teacher" and not email:
            raise APIError(HTTPStatus.BAD_REQUEST, "Багшийн бүртгэлд имэйл заавал оруулна уу.")
        if phone and not re.fullmatch(r"\+9768\d{7}", phone):
            raise APIError(HTTPStatus.BAD_REQUEST, "Монголын 8 оронтой гар утасны дугаар оруулна уу.")
        if not 2 <= len(name) <= 80:
            raise APIError(HTTPStatus.BAD_REQUEST, "Нэр 2–80 тэмдэгт байна.")
        if len(password) < 12 or len(password) > 128:
            raise APIError(HTTPStatus.BAD_REQUEST, "Нууц үг 12–128 тэмдэгт байх шаардлагатай.")
        if requested_role not in {"student", "teacher"}:
            raise APIError(HTTPStatus.BAD_REQUEST, "Хэрэглэгчийн төрөл буруу байна.")
        role = "student"
        if requested_role == "teacher":
            invite = os.environ.get("TEACHER_INVITE_CODE", self.server.teacher_invite_code)
            supplied = str(payload.get("teacher_invite_code", ""))
            if not hmac.compare_digest(supplied, invite):
                raise APIError(HTTPStatus.FORBIDDEN, "Багшийн урилгын код буруу байна.", "invalid_teacher_invite")
            role = "teacher"
        user_id = str(uuid.uuid4())
        try:
            with connect_db() as db:
                db.execute(
                    "INSERT INTO users(id,email,phone,display_name,role,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, email or None, phone or None, name, role, password_hash(password), timestamp()),
                )
        except sqlite3.IntegrityError as error:
            if "users.email" in str(error) or "UNIQUE constraint failed" in str(error):
                raise APIError(HTTPStatus.CONFLICT, "Энэ имэйлээр бүртгэл үүссэн байна.", "email_exists") from error
            raise
        self.create_session(user_id)
        self.send_json(HTTPStatus.CREATED, {"user": {"id": user_id, "email": email or None, "phone": phone or None, "display_name": name, "role": role}})

    def login(self, payload: dict) -> None:
        ip = self.client_address[0]
        raw_identifier = str(payload.get("identifier") or payload.get("email") or payload.get("phone") or "")
        identifier_kind, identifier = normalize_login_identifier(raw_identifier)
        if not identifier:
            raise APIError(HTTPStatus.BAD_REQUEST, "Имэйл эсвэл утасны дугаараа оруулна уу.")
        if not check_rate_limit((ip, "login"), 12, 300) or not check_rate_limit((identifier, "login"), 8, 300):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Нэвтрэх оролдлого хэтэрсэн байна. 5 минутын дараа дахин оролдоно уу.")
        password = str(payload.get("password", ""))
        with connect_db() as db:
            user = db.execute(f"SELECT * FROM users WHERE {identifier_kind}=?", (identifier,)).fetchone()
        encoded = user["password_hash"] if user else self.server.dummy_password_hash
        valid = verify_password(password, encoded)
        if user is None or not valid:
            raise APIError(HTTPStatus.UNAUTHORIZED, "Имэйл эсвэл нууц үг буруу байна.", "invalid_credentials")
        self.create_session(user["id"])
        self.send_json(HTTPStatus.OK, {"user": public_user(user)})

    def create_session(self, user_id: str) -> None:
        token = secrets.token_urlsafe(32)
        expires = utc_now() + timedelta(days=SESSION_DAYS)
        with connect_db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp(),))
            db.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_digest(token), user_id, timestamp(expires), timestamp()),
            )
        cookie = f"toonoos_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
        if COOKIE_SECURE:
            cookie += "; Secure"
        self.pending_cookie = cookie
        original_send_json = self.send_json

        def send_json_with_cookie(status: int, payload: dict, headers: dict[str, str] | None = None):
            merged = dict(headers or {})
            merged["Set-Cookie"] = self.pending_cookie
            original_send_json(status, payload, merged)

        self.send_json = send_json_with_cookie

    def logout(self) -> None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("toonoos_session")
        if morsel:
            with connect_db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(morsel.value),))
        expired = "toonoos_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        if COOKIE_SECURE:
            expired += "; Secure"
        self.send_json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": expired})

    def update_profile_photo(self, payload: dict) -> None:
        user = self.current_user()
        if not check_rate_limit((user["id"], "profile-photo"), 10, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Зургийг олон удаа сольсон байна. Дараа дахин оролдоно уу.")
        data_url = payload.get("data_url")
        if not isinstance(data_url, str) or len(data_url) > (MAX_PROFILE_PHOTO_BYTES * 4 // 3) + 128:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зургийн өгөгдөл буруу эсвэл хэт том байна.")
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
        if not match:
            raise APIError(HTTPStatus.BAD_REQUEST, "PNG, JPEG эсвэл WebP зураг сонгоно уу.")
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зургийн кодчилол буруу байна.") from error
        if not image or len(image) > MAX_PROFILE_PHOTO_BYTES:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зургийн хэмжээ 2 MB-аас хэтрэхгүй байна.")
        formats = {
            "png": (b"\x89PNG\r\n\x1a\n", ".png"),
            "jpeg": (b"\xff\xd8\xff", ".jpg"),
            "webp": (b"RIFF", ".webp"),
        }
        signature, suffix = formats[match.group(1)]
        if not image.startswith(signature) or (match.group(1) == "webp" and image[8:12] != b"WEBP"):
            raise APIError(HTTPStatus.BAD_REQUEST, "Файлын бодит төрөл сонгосон зурагтай тохирохгүй байна.")

        PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"profile-{secrets.token_hex(16)}{suffix}"
        image_path = PROFILE_PHOTO_DIR / filename
        relative_path = f"/media/profiles/{filename}"
        old_image = user["profile_image"]
        try:
            image_path.write_bytes(image)
            with connect_db() as db:
                db.execute("UPDATE users SET profile_image=? WHERE id=?", (relative_path, user["id"]))
        except (OSError, sqlite3.Error) as error:
            try:
                image_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise APIError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Зураг хадгалах үед алдаа гарлаа; түр файлыг цэвэрлэж чадсангүй.",
                ) from cleanup_error
            raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, "Зургийг хадгалж чадсангүй.") from error
        if old_image:
            old_match = re.fullmatch(r"/media/profiles/(profile-[a-f0-9]{32}\.(?:png|jpg|webp))", old_image)
            if old_match:
                (PROFILE_PHOTO_DIR / old_match.group(1)).unlink(missing_ok=True)
        with connect_db() as db:
            updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        self.send_json(HTTPStatus.OK, {"user": public_user(updated)})

    def upload_lesson_media(self, lesson_id: str, query: dict[str, list[str]]) -> None:
        teacher = self.require_role("teacher")
        lesson = next((item for item in load_lessons() if item["id"] == lesson_id), None)
        if not lesson:
            raise APIError(HTTPStatus.NOT_FOUND, "Цахим хичээл олдсонгүй.")
        if not check_rate_limit((teacher["id"], "lesson-media"), 30, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Файл байршуулалт хэтэрсэн байна. Дараа дахин оролдоно уу.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "Файлын хэмжээ буруу байна.") from error
        if length < 1 or length > MAX_LESSON_MEDIA_BYTES:
            raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Файлын хэмжээ 100 MB-аас хэтрэхгүй байна.")

        supplied_name = query.get("filename", [""])[0]
        original_name = re.split(r"[\\/]", supplied_name)[-1].strip()
        original_name = "".join(char for char in original_name if char.isprintable())
        if not original_name or len(original_name) > 255:
            raise APIError(HTTPStatus.BAD_REQUEST, "Файлын нэр буруу байна.")
        extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        media_formats = {
            "ppt": ("presentation", "application/vnd.ms-powerpoint"),
            "pptx": ("presentation", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "pdf": ("document", "application/pdf"),
            "mp4": ("video", "video/mp4"),
            "m4v": ("video", "video/mp4"),
            "mov": ("video", "video/quicktime"),
            "webm": ("video", "video/webm"),
            "mkv": ("video", "video/x-matroska"),
        }
        format_info = media_formats.get(extension)
        if not format_info:
            raise APIError(HTTPStatus.BAD_REQUEST, "PPT, PPTX, PDF эсвэл MP4, MOV, WebM, MKV видео сонгоно уу.")
        title = query.get("title", [""])[0].strip()
        if not title or len(title) > 120 or any(not char.isprintable() for char in title):
            raise APIError(HTTPStatus.BAD_REQUEST, "Хичээлийн материалын нэр 1–120 тэмдэгт байна.")

        media_id = uuid.uuid4().hex
        temporary_path = LESSON_MEDIA_DIR / f".lesson-{media_id}.tmp"
        media_path = LESSON_MEDIA_DIR / f"lesson-{media_id}.{extension}"
        LESSON_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            remaining = length
            with temporary_path.open("xb") as target:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise APIError(HTTPStatus.BAD_REQUEST, "Файл бүрэн ирсэнгүй.")
                    target.write(chunk)
                    remaining -= len(chunk)
            self.validate_lesson_media(temporary_path, extension)
            temporary_path.replace(media_path)
            created_at = timestamp()
            try:
                with connect_db() as db:
                    db.execute(
                        """INSERT INTO lesson_media
                           (id, lesson_id, title, original_name, extension, media_type,
                            size_bytes, uploaded_by, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            media_id, lesson_id, title, original_name, extension,
                            format_info[0], length, teacher["id"], created_at,
                        ),
                    )
            except sqlite3.Error as error:
                media_path.unlink(missing_ok=True)
                raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, "Материалын мэдээллийг хадгалж чадсангүй.") from error
        finally:
            temporary_path.unlink(missing_ok=True)

        self.send_json(HTTPStatus.CREATED, {
            "media": {
                "id": media_id,
                "title": title,
                "original_name": original_name,
                "extension": extension,
                "media_type": format_info[0],
                "size_bytes": length,
                "url": f"/media/lessons/{media_id}.{extension}",
                "can_delete": True,
            },
        })

    @staticmethod
    def validate_lesson_media(path: Path, extension: str) -> None:
        with path.open("rb") as source:
            header = source.read(16)
        valid = False
        if extension == "pdf":
            valid = header.startswith(b"%PDF-")
        elif extension == "ppt":
            valid = header.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        elif extension == "pptx":
            try:
                with zipfile.ZipFile(path) as archive:
                    members = set(archive.namelist())
                valid = "[Content_Types].xml" in members and "ppt/presentation.xml" in members
            except (OSError, zipfile.BadZipFile):
                valid = False
        elif extension in {"mp4", "m4v", "mov"}:
            valid = len(header) >= 8 and header[4:8] == b"ftyp"
        elif extension in {"webm", "mkv"}:
            valid = header.startswith(bytes.fromhex("1A45DFA3"))
        if not valid:
            raise APIError(HTTPStatus.BAD_REQUEST, "Файлын агуулга өргөтгөлтэйгээ тохирохгүй байна.")

    def delete_lesson_media(self, media_id: str) -> None:
        teacher = self.require_role("teacher")
        with connect_db() as db:
            media = db.execute("SELECT * FROM lesson_media WHERE id=?", (media_id,)).fetchone()
        if not media:
            raise APIError(HTTPStatus.NOT_FOUND, "Материал олдсонгүй.")
        if media["uploaded_by"] != teacher["id"]:
            raise APIError(HTTPStatus.FORBIDDEN, "Зөвхөн өөрийн байршуулсан материалыг устгана.")
        media_path = LESSON_MEDIA_DIR / f"lesson-{media_id}.{media['extension']}"
        try:
            media_path.unlink()
        except OSError as error:
            raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, "Материалын файлыг устгаж чадсангүй.") from error
        with connect_db() as db:
            db.execute("DELETE FROM lesson_media WHERE id=?", (media_id,))
        self.send_json(HTTPStatus.OK, {"ok": True})

    def update_profile_email(self, payload: dict) -> None:
        user = self.current_user()
        if not check_rate_limit((user["id"], "profile-email"), 5, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Имэйл солих оролдлого хэтэрсэн байна. Дараа дахин оролдоно уу.")
        new_email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))
        if len(new_email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_email):
            raise APIError(HTTPStatus.BAD_REQUEST, "Шинэ имэйл хаягаа шалгана уу.")
        if not verify_password(password, user["password_hash"]):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Одоогийн нууц үг буруу байна.", "invalid_password")
        if new_email == user["email"]:
            raise APIError(HTTPStatus.BAD_REQUEST, "Шинэ имэйл нь одоогийн имэйлтэй ижил байна.")
        try:
            with connect_db() as db:
                db.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
                updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        except sqlite3.IntegrityError as error:
            if "users.email" in str(error) or "UNIQUE constraint failed" in str(error):
                raise APIError(HTTPStatus.CONFLICT, "Энэ имэйлээр бүртгэл үүссэн байна.", "email_exists") from error
            raise
        self.send_json(HTTPStatus.OK, {"user": public_user(updated)})

    def update_profile_name(self, payload: dict) -> None:
        user = self.current_user()
        if not check_rate_limit((user["id"], "profile-name"), 10, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Нэр солих оролдлого хэтэрсэн байна. Дараа дахин оролдоно уу.")
        name = str(payload.get("display_name", "")).strip()
        password = str(payload.get("password", ""))
        if not 2 <= len(name) <= 80:
            raise APIError(HTTPStatus.BAD_REQUEST, "Нэр 2–80 тэмдэгт байна.")
        if not verify_password(password, user["password_hash"]):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Одоогийн нууц үг буруу байна.", "invalid_password")
        with connect_db() as db:
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name, user["id"]))
            updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        self.send_json(HTTPStatus.OK, {"user": public_user(updated)})

    def update_profile_password(self, payload: dict) -> None:
        user = self.current_user()
        if not check_rate_limit((user["id"], "profile-password"), 5, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Нууц үг солих оролдлого хэтэрсэн байна. Дараа дахин оролдоно уу.")
        current_password = str(payload.get("current_password", ""))
        new_password = str(payload.get("new_password", ""))
        if len(new_password) < 12 or len(new_password) > 128:
            raise APIError(HTTPStatus.BAD_REQUEST, "Нууц үг 12–128 тэмдэгт байх шаардлагатай.")
        if not verify_password(current_password, user["password_hash"]):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Одоогийн нууц үг буруу байна.", "invalid_password")
        if verify_password(new_password, user["password_hash"]):
            raise APIError(HTTPStatus.BAD_REQUEST, "Шинэ нууц үг одоогийн нууц үгтэй ижил байна.")
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        session = cookie.get("toonoos_session")
        if not session:
            raise APIError(HTTPStatus.UNAUTHORIZED, "Сесс дууссан байна. Дахин нэвтэрнэ үү.", "session_expired")
        current_session_hash = token_digest(session.value)
        with connect_db() as db:
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (password_hash(new_password), user["id"]),
            )
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                (user["id"], current_session_hash),
            )
        self.send_json(HTTPStatus.OK, {"message": "Нууц үг солигдлоо. Бусад төхөөрөмжөөс гаргалаа."})

    def update_site_cover(self, payload: dict) -> None:
        self.update_site_image(payload, "cover_image", "cover", "site-cover", "Нүүр зураг")

    def update_site_image(self, payload: dict, setting_key: str, filename_prefix: str, rate_limit_key: str, image_name: str) -> None:
        teacher = self.require_role("teacher")
        if not check_rate_limit((teacher["id"], rate_limit_key), 5, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, f"{image_name} олон удаа сольсон байна. Дараа дахин оролдоно уу.")
        data_url = payload.get("data_url")
        if not isinstance(data_url, str) or len(data_url) > (MAX_COVER_IMAGE_BYTES * 4 // 3) + 128:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зургийн өгөгдөл буруу эсвэл хэт том байна.")
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
        if not match:
            raise APIError(HTTPStatus.BAD_REQUEST, "PNG, JPEG эсвэл WebP зураг сонгоно уу.")
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зургийн кодчилол буруу байна.") from error
        if not image or len(image) > MAX_COVER_IMAGE_BYTES:
            raise APIError(HTTPStatus.BAD_REQUEST, f"{image_name} хэмжээ 4 MB-аас хэтрэхгүй байна.")
        formats = {
            "png": (b"\x89PNG\r\n\x1a\n", ".png"),
            "jpeg": (b"\xff\xd8\xff", ".jpg"),
            "webp": (b"RIFF", ".webp"),
        }
        signature, suffix = formats[match.group(1)]
        if not image.startswith(signature) or (match.group(1) == "webp" and image[8:12] != b"WEBP"):
            raise APIError(HTTPStatus.BAD_REQUEST, "Файлын бодит төрөл сонгосон зурагтай тохирохгүй байна.")

        COVER_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{filename_prefix}-{secrets.token_hex(16)}{suffix}"
        image_path = COVER_IMAGE_DIR / filename
        relative_path = f"/media/cover/{filename}"
        with connect_db() as db:
            old_row = db.execute(
                "SELECT setting_value FROM site_settings WHERE setting_key=?",
                (setting_key,),
            ).fetchone()
        old_image = old_row["setting_value"] if old_row else None
        try:
            image_path.write_bytes(image)
            with connect_db() as db:
                db.execute(
                    """INSERT INTO site_settings(setting_key,setting_value) VALUES(?,?)
                       ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                    (setting_key, relative_path),
                )
        except (OSError, sqlite3.Error) as error:
            try:
                image_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise APIError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"{image_name} хадгалах үед алдаа гарлаа; түр файлыг цэвэрлэж чадсангүй.",
                ) from cleanup_error
            raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, f"{image_name} хадгалж чадсангүй.") from error
        if old_image:
            old_match = re.fullmatch(
                rf"/media/cover/({re.escape(filename_prefix)}-[a-f0-9]{{32}}\.(?:png|jpg|webp))", old_image
            )
            if old_match:
                (COVER_IMAGE_DIR / old_match.group(1)).unlink(missing_ok=True)
        response_key = "cover_image" if setting_key == "cover_image" else "logo_image"
        self.send_json(HTTPStatus.OK, {response_key: relative_path})

    def request_password_reset(self, payload: dict) -> None:
        ip = self.client_address[0]
        if not check_rate_limit((ip, "password-reset"), 5, 3600):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "Сэргээх хүсэлт хэтэрсэн байна. Дараа дахин оролдоно уу.")
        email = str(payload.get("email", "")).strip().lower()
        with connect_db() as db:
            user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if user:
                token = secrets.token_urlsafe(32)
                expires = utc_now() + timedelta(minutes=30)
                db.execute(
                    "INSERT INTO password_reset_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (token_digest(token), user["id"], timestamp(expires), timestamp()),
                )
            else:
                token = None
        if token:
            try:
                send_password_reset(email, token)
            except Exception:
                with connect_db() as db:
                    db.execute("DELETE FROM password_reset_tokens WHERE token_hash=?", (token_digest(token),))
                raise APIError(HTTPStatus.SERVICE_UNAVAILABLE, "Нууц үг сэргээх мэдээлэл илгээж чадсангүй. Серверийн тохиргоог шалгана уу.", "reset_delivery_failed")
        self.send_json(HTTPStatus.OK, {"message": "Хаяг бүртгэлтэй бол нууц үг сэргээх заавар илгээгдэнэ."})

    def confirm_password_reset(self, payload: dict) -> None:
        token = str(payload.get("token", ""))
        password = str(payload.get("new_password", ""))
        if len(password) < 12 or len(password) > 128:
            raise APIError(HTTPStatus.BAD_REQUEST, "Нууц үг 12–128 тэмдэгт байх шаардлагатай.")
        digest = token_digest(token) if token else ""
        with connect_db() as db:
            row = db.execute(
                """SELECT user_id FROM password_reset_tokens
                   WHERE token_hash=? AND used_at IS NULL AND expires_at>?""",
                (digest, timestamp()),
            ).fetchone()
            if not row:
                raise APIError(HTTPStatus.BAD_REQUEST, "Сэргээх код буруу эсвэл хугацаа дууссан байна.", "invalid_reset_token")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), row["user_id"]))
            db.execute("UPDATE password_reset_tokens SET used_at=? WHERE token_hash=?", (timestamp(), digest))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
        self.send_json(HTTPStatus.OK, {"message": "Нууц үг шинэчлэгдлээ. Дахин нэвтэрнэ үү."}, {
            "Set-Cookie": "toonoos_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" + ("; Secure" if COOKIE_SECURE else "")
        })

    def submit_exercise(self, exercise_id: str, payload: dict) -> None:
        user = self.current_user()
        answer = str(payload.get("answer", ""))[:1000]
        if not answer.strip():
            raise APIError(HTTPStatus.BAD_REQUEST, "Хариултаа оруулна уу.")
        elapsed = payload.get("elapsed_seconds")
        if elapsed is not None:
            try:
                elapsed = max(0, min(int(elapsed), 86400))
            except (TypeError, ValueError):
                elapsed = None
        with connect_db() as db:
            exercise = db.execute("SELECT * FROM exercises WHERE id=? AND published=1", (exercise_id,)).fetchone()
            if not exercise:
                raise APIError(HTTPStatus.NOT_FOUND, "Дасгал олдсонгүй.")
            correct = answer_matches(answer, exercise["answer_key"])
            points = exercise["points"] if correct else 0
            attempt_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO exercise_attempts(id,exercise_id,user_id,answer,correct,points_earned,elapsed_seconds,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (attempt_id, exercise_id, user["id"], answer, int(correct), points, elapsed, timestamp()),
            )
            db.execute(
                """INSERT INTO learning_progress(user_id,chapter_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(user_id,chapter_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (user["id"], exercise["chapter_id"], timestamp()),
            )
        self.send_json(HTTPStatus.CREATED, {
            "attempt_id": attempt_id, "correct": correct, "points_earned": points,
            "points_possible": exercise["points"],
            "message": "Зөв хариуллаа!" if correct else "Одоогоор буруу байна. Алхамтай тайлбарыг үзээд дахин оролдоорой.",
            "explanation": exercise["explanation"], "hint": exercise["hint"],
        })

    def create_class(self, payload: dict) -> None:
        teacher = self.require_role("teacher")
        name = str(payload.get("name", "")).strip()
        if not 2 <= len(name) <= 80:
            raise APIError(HTTPStatus.BAD_REQUEST, "Ангийн нэр 2–80 тэмдэгт байна.")
        class_id = str(uuid.uuid4())
        with connect_db() as db:
            while True:
                code = secrets.token_hex(4).upper()
                digest = token_digest(code)
                if not db.execute("SELECT 1 FROM classes WHERE join_code_hash=?", (digest,)).fetchone():
                    break
            db.execute(
                "INSERT INTO classes(id,teacher_id,name,join_code_hash,created_at) VALUES(?,?,?,?,?)",
                (class_id, teacher["id"], name, digest, timestamp()),
            )
        self.send_json(HTTPStatus.CREATED, {"class": {"id": class_id, "name": name, "join_code": code}})

    def join_class(self, payload: dict) -> None:
        student = self.require_role("student")
        code = re.sub(r"\s+", "", str(payload.get("join_code", ""))).upper()
        if not re.fullmatch(r"[A-F0-9]{8}", code):
            raise APIError(HTTPStatus.BAD_REQUEST, "Ангийн код 8 тэмдэгттэй байна.")
        with connect_db() as db:
            classroom = db.execute("SELECT id,name FROM classes WHERE join_code_hash=?", (token_digest(code),)).fetchone()
            if not classroom:
                raise APIError(HTTPStatus.NOT_FOUND, "Ангийн код буруу байна.", "class_not_found")
            db.execute(
                "INSERT OR IGNORE INTO class_memberships(class_id,student_id,joined_at) VALUES(?,?,?)",
                (classroom["id"], student["id"], timestamp()),
            )
        self.send_json(HTTPStatus.OK, {"class": dict(classroom)})

    def add_student_to_class(self, class_id: str, payload: dict) -> None:
        teacher = self.require_role("teacher")
        raw_identifier = str(payload.get("identifier") or payload.get("email") or payload.get("phone") or "")
        identifier_kind, identifier = normalize_login_identifier(raw_identifier)
        if not identifier:
            raise APIError(HTTPStatus.BAD_REQUEST, "Зөв имэйл эсвэл утасны дугаар оруулна уу.")
        if False:
            raise APIError(HTTPStatus.BAD_REQUEST, "Сурагчийн бүртгэлтэй имэйл хаягийг оруулна уу.")
        with connect_db() as db:
            classroom = db.execute(
                "SELECT id,name FROM classes WHERE id=? AND teacher_id=?",
                (class_id, teacher["id"]),
            ).fetchone()
            if not classroom:
                raise APIError(HTTPStatus.NOT_FOUND, "Таны удирдах анги олдсонгүй.")
            student = db.execute(
                f"SELECT id,display_name,email,phone,role FROM users WHERE {identifier_kind}=?",
                (identifier,),
            ).fetchone()
            if not student or student["role"] != "student":
                raise APIError(HTTPStatus.NOT_FOUND, "Энэ имэйлээр бүртгэлтэй сурагч олдсонгүй.")
            existing = db.execute(
                "SELECT 1 FROM class_memberships WHERE class_id=? AND student_id=?",
                (class_id, student["id"]),
            ).fetchone()
            if existing:
                raise APIError(HTTPStatus.CONFLICT, "Сурагч энэ ангид өмнө нь нэмэгдсэн байна.")
            db.execute(
                "INSERT INTO class_memberships(class_id,student_id,joined_at) VALUES(?,?,?)",
                (class_id, student["id"], timestamp()),
            )
        self.send_json(HTTPStatus.CREATED, {
            "student": {"id": student["id"], "display_name": student["display_name"], "email": student["email"], "phone": student["phone"]},
            "class": {"id": classroom["id"], "name": classroom["name"]},
        })

    def remove_student_from_class(self, class_id: str, student_id: str) -> None:
        teacher = self.require_role("teacher")
        with connect_db() as db:
            membership = db.execute(
                """SELECT c.name,u.display_name
                   FROM classes c
                   JOIN class_memberships m ON m.class_id=c.id
                   JOIN users u ON u.id=m.student_id
                   WHERE c.id=? AND c.teacher_id=? AND u.id=? AND u.role='student'""",
                (class_id, teacher["id"], student_id),
            ).fetchone()
            if not membership:
                raise APIError(HTTPStatus.NOT_FOUND, "Таны ангид бүртгэлтэй сурагч олдсонгүй.")
            db.execute(
                "DELETE FROM class_memberships WHERE class_id=? AND student_id=?",
                (class_id, student_id),
            )
        self.send_json(HTTPStatus.OK, {
            "message": "Сурагчийг ангиас хаслаа. Бүртгэл болон сурлагын мэдээлэл хэвээр үлдэнэ.",
            "student": {"id": student_id, "display_name": membership["display_name"]},
            "class": {"id": class_id, "name": membership["name"]},
        })

    def classes_for(self, user: sqlite3.Row) -> dict:
        with connect_db() as db:
            if user["role"] == "teacher":
                rows = db.execute(
                    """SELECT c.id,c.name,c.created_at,
                              (SELECT COUNT(*) FROM class_memberships m WHERE m.class_id=c.id) AS student_count
                       FROM classes c WHERE c.teacher_id=? ORDER BY c.created_at DESC""",
                    (user["id"],),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT c.id,c.name,c.created_at,u.display_name AS teacher_name
                       FROM class_memberships m JOIN classes c ON c.id=m.class_id
                       JOIN users u ON u.id=c.teacher_id WHERE m.student_id=? ORDER BY c.created_at DESC""",
                    (user["id"],),
                ).fetchall()
        return {"classes": [dict(row) for row in rows]}

    def create_assignment(self, class_id: str, payload: dict) -> None:
        teacher = self.require_role("teacher")
        exercise_id = str(payload.get("exercise_id", ""))
        due_at = payload.get("due_at")
        if due_at is not None:
            if not isinstance(due_at, str) or len(due_at) > 40:
                raise APIError(HTTPStatus.BAD_REQUEST, "Дуусах хугацааны формат буруу байна.")
            try:
                due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError as error:
                raise APIError(HTTPStatus.BAD_REQUEST, "Дуусах хугацааны формат буруу байна.") from error
        assignment_id = str(uuid.uuid4())
        with connect_db() as db:
            classroom = db.execute("SELECT id FROM classes WHERE id=? AND teacher_id=?", (class_id, teacher["id"])).fetchone()
            exercise = db.execute("SELECT id FROM exercises WHERE id=? AND published=1", (exercise_id,)).fetchone()
            if not classroom:
                raise APIError(HTTPStatus.NOT_FOUND, "Таны ангид энэ анги олдсонгүй.")
            if not exercise:
                raise APIError(HTTPStatus.NOT_FOUND, "Дасгал олдсонгүй.")
            db.execute(
                "INSERT INTO assignments(id,class_id,teacher_id,exercise_id,due_at,created_at) VALUES(?,?,?,?,?,?)",
                (assignment_id, class_id, teacher["id"], exercise_id, due_at, timestamp()),
            )
        self.send_json(HTTPStatus.CREATED, {"assignment_id": assignment_id})

    def assign_assessment(self, class_id: str, payload: dict) -> None:
        teacher = self.require_role("teacher")
        assessment_id = str(payload.get("assessment_id", ""))
        due_at = payload.get("due_at")
        if due_at is not None:
            if not isinstance(due_at, str) or len(due_at) > 40:
                raise APIError(HTTPStatus.BAD_REQUEST, "Дуусах хугацааны формат буруу байна.")
            try:
                due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError as error:
                raise APIError(HTTPStatus.BAD_REQUEST, "Дуусах хугацааны формат буруу байна.") from error
        with connect_db() as db:
            classroom = db.execute(
                "SELECT id FROM classes WHERE id=? AND teacher_id=?", (class_id, teacher["id"])
            ).fetchone()
            assessment = db.execute(
                "SELECT id FROM assessments WHERE id=? AND published=1", (assessment_id,)
            ).fetchone()
            if not classroom:
                raise APIError(HTTPStatus.NOT_FOUND, "Таны удирдах ангид энэ анги олдсонгүй.")
            if not assessment:
                raise APIError(HTTPStatus.NOT_FOUND, "Нээлттэй шалгалт олдсонгүй.")
            assignment_id = str(uuid.uuid4())
            try:
                db.execute(
                    """INSERT INTO assessment_assignments
                       (id,class_id,assessment_id,teacher_id,due_at,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (assignment_id, class_id, assessment_id, teacher["id"], due_at, timestamp()),
                )
            except sqlite3.IntegrityError as error:
                raise APIError(HTTPStatus.CONFLICT, "Энэ шалгалтыг тухайн ангид өмнө нь оноосон байна.") from error
        self.send_json(HTTPStatus.CREATED, {"assignment_id": assignment_id})

    def review_attempt(self, attempt_id: str, payload: dict) -> None:
        teacher = self.require_role("teacher")
        try:
            points = int(payload.get("points"))
        except (TypeError, ValueError) as error:
            raise APIError(HTTPStatus.BAD_REQUEST, "Оноог бүхэл тоогоор оруулна уу.") from error
        comment = str(payload.get("comment", "")).strip()[:2000]
        with connect_db() as db:
            attempt = db.execute(
                """SELECT a.id,e.points,c.teacher_id FROM exercise_attempts a
                   JOIN exercises e ON e.id=a.exercise_id
                   JOIN class_memberships m ON m.student_id=a.user_id
                   JOIN classes c ON c.id=m.class_id
                   WHERE a.id=? AND c.teacher_id=?""",
                (attempt_id, teacher["id"]),
            ).fetchone()
            if not attempt:
                raise APIError(HTTPStatus.NOT_FOUND, "Танд хамаарах сурагчийн хариулт олдсонгүй.")
            if points < 0 or points > attempt["points"]:
                raise APIError(HTTPStatus.BAD_REQUEST, f"Оноо 0–{attempt['points']} хооронд байна.")
            db.execute(
                """UPDATE exercise_attempts SET reviewed_points=?,review_comment=?,reviewed_by=?,
                   points_earned=?,correct=? WHERE id=?""",
                (points, comment, teacher["id"], points, int(points == attempt["points"]), attempt_id),
            )
        self.send_json(HTTPStatus.OK, {"ok": True})

    def start_eesh_quiz(self, quiz_id: str) -> None:
        student = self.require_role("student")
        quiz = next((item for item in load_eesh_quizzes() if item["id"] == quiz_id), None)
        if not quiz:
            raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ сорил олдсонгүй.")
        max_points = sum(question["points"] for question in quiz["questions"])
        with connect_db() as db:
            existing = db.execute(
                """SELECT * FROM eesh_quiz_attempts WHERE quiz_id=? AND user_id=?
                   AND submitted_at IS NULL AND expires_at>? ORDER BY started_at DESC LIMIT 1""",
                (quiz_id, student["id"], timestamp()),
            ).fetchone()
            if existing:
                attempt_id = existing["id"]
                started = existing["started_at"]
                expires = existing["expires_at"]
            else:
                attempt_id = str(uuid.uuid4())
                started_dt = utc_now()
                expires_dt = started_dt + timedelta(minutes=quiz["duration_minutes"])
                started, expires = timestamp(started_dt), timestamp(expires_dt)
                db.execute(
                    """INSERT INTO eesh_quiz_attempts
                       (id,quiz_id,user_id,started_at,expires_at,max_points)
                       VALUES(?,?,?,?,?,?)""",
                    (attempt_id, quiz_id, student["id"], started, expires, max_points),
                )
        self.send_json(HTTPStatus.CREATED, {
            "attempt_id": attempt_id, "started_at": started, "expires_at": expires,
            "duration_minutes": quiz["duration_minutes"], "question_count": len(quiz["questions"]),
        })

    def save_eesh_material_response(self, material_id: str, payload: dict) -> None:
        student = self.require_role("student")
        material = next(
            (item for item in load_eesh_materials() if item["id"] == material_id),
            None,
        )
        if not material:
            raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ материал олдсонгүй.")
        slide_number = payload.get("slide_number")
        answer = payload.get("answer")
        if (
            type(slide_number) is not int
            or not 1 <= slide_number <= material["slide_count"]
            or not isinstance(answer, str)
            or len(answer) > 5000
        ):
            raise APIError(HTTPStatus.BAD_REQUEST, "Слайдын дугаар эсвэл хариултын хэмжээ буруу байна.")
        with connect_db() as db:
            db.execute(
                """INSERT INTO eesh_material_responses
                   (user_id,material_id,slide_number,answer,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id,material_id,slide_number) DO UPDATE SET
                   answer=excluded.answer,updated_at=excluded.updated_at""",
                (student["id"], material_id, slide_number, answer, timestamp()),
            )
        self.send_json(HTTPStatus.OK, {"saved": True, "slide_number": slide_number})

    def submit_eesh_quiz(self, attempt_id: str, payload: dict) -> None:
        student = self.require_role("student")
        answers = payload.get("answers")
        if not isinstance(answers, dict) or len(answers) > 100:
            raise APIError(HTTPStatus.BAD_REQUEST, "ЭЕШ сорилын хариултын бүтэц буруу байна.")
        with connect_db() as db:
            attempt = db.execute(
                "SELECT * FROM eesh_quiz_attempts WHERE id=? AND user_id=?",
                (attempt_id, student["id"]),
            ).fetchone()
            if not attempt:
                raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ сорилын оролдлого олдсонгүй.")
            if attempt["submitted_at"]:
                raise APIError(HTTPStatus.CONFLICT, "Энэ ЭЕШ сорилыг өмнө нь илгээсэн байна.")
            if attempt["expires_at"] < timestamp():
                raise APIError(HTTPStatus.CONFLICT, "ЭЕШ сорилын хугацаа дууссан байна.")
            quiz = next((item for item in load_eesh_quizzes() if item["id"] == attempt["quiz_id"]), None)
            if not quiz:
                raise APIError(HTTPStatus.NOT_FOUND, "ЭЕШ сорил олдсонгүй.")
            question_ids = {question["id"] for question in quiz["questions"]}
            if not set(answers).issubset(question_ids):
                raise APIError(HTTPStatus.BAD_REQUEST, "ЭЕШ сорилд байхгүй асуултын хариу илгээгдсэн байна.")
            total = 0
            marked_answers = {}
            for question in quiz["questions"]:
                value = str(answers.get(question["id"], ""))[:1000]
                correct = answer_matches(value, question["answer"])
                points = question["points"] if correct else 0
                total += points
                marked_answers[question["id"]] = {
                    "answer": value, "correct": correct, "points": points,
                    "expected_answer": question["answer"], "explanation": question["explanation"],
                }
            db.execute(
                """UPDATE eesh_quiz_attempts SET submitted_at=?,answers_json=?,points_earned=?
                   WHERE id=? AND submitted_at IS NULL""",
                (timestamp(), json.dumps(marked_answers, ensure_ascii=False), total, attempt_id),
            )
        self.send_json(HTTPStatus.OK, {
            "attempt_id": attempt_id, "points_earned": total, "points_possible": attempt["max_points"],
            "percentage": round(total / max(1, attempt["max_points"]) * 100),
            "answers": marked_answers,
        })

    def start_assessment(self, assessment_id: str) -> None:
        user = self.current_user()
        with connect_db() as db:
            assessment = db.execute(
                "SELECT * FROM assessments WHERE id=? AND published=1", (assessment_id,)
            ).fetchone()
            if not assessment:
                raise APIError(HTTPStatus.NOT_FOUND, "Шалгалт олдсонгүй.")
            questions = json.loads(assessment["question_ids"])
            placeholders = ",".join("?" for _ in questions)
            max_points = db.execute(
                f"SELECT COALESCE(SUM(points),0) FROM exercises WHERE id IN ({placeholders})",
                questions,
            ).fetchone()[0]
            existing = db.execute(
                """SELECT * FROM assessment_attempts WHERE assessment_id=? AND user_id=?
                   AND submitted_at IS NULL AND expires_at>? ORDER BY started_at DESC LIMIT 1""",
                (assessment_id, user["id"], timestamp()),
            ).fetchone()
            if existing:
                attempt_id = existing["id"]
                started = existing["started_at"]
                expires = existing["expires_at"]
            else:
                attempt_id = str(uuid.uuid4())
                started_dt = utc_now()
                expires_dt = started_dt + timedelta(minutes=assessment["duration_minutes"])
                started, expires = timestamp(started_dt), timestamp(expires_dt)
                db.execute(
                    """INSERT INTO assessment_attempts(id,assessment_id,user_id,started_at,expires_at,max_points)
                       VALUES(?,?,?,?,?,?)""",
                    (attempt_id, assessment_id, user["id"], started, expires, max_points),
                )
        self.send_json(HTTPStatus.CREATED, {
            "attempt_id": attempt_id, "started_at": started, "expires_at": expires,
            "duration_minutes": assessment["duration_minutes"], "question_count": len(questions),
        })

    def submit_assessment(self, attempt_id: str, payload: dict) -> None:
        user = self.current_user()
        answers = payload.get("answers")
        if not isinstance(answers, dict) or len(answers) > 100:
            raise APIError(HTTPStatus.BAD_REQUEST, "Шалгалтын хариултын бүтэц буруу байна.")
        with connect_db() as db:
            attempt = db.execute(
                """SELECT a.*,s.question_ids FROM assessment_attempts a
                   JOIN assessments s ON s.id=a.assessment_id
                   WHERE a.id=? AND a.user_id=?""",
                (attempt_id, user["id"]),
            ).fetchone()
            if not attempt:
                raise APIError(HTTPStatus.NOT_FOUND, "Шалгалтын оролдлого олдсонгүй.")
            if attempt["submitted_at"]:
                raise APIError(HTTPStatus.CONFLICT, "Энэ шалгалтыг өмнө нь илгээсэн байна.", "already_submitted")
            if attempt["expires_at"] < timestamp():
                raise APIError(HTTPStatus.CONFLICT, "Шалгалтын хугацаа дууссан байна.", "assessment_expired")
            ids = json.loads(attempt["question_ids"])
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(f"SELECT id,answer_key,points FROM exercises WHERE id IN ({placeholders})", ids).fetchall()
            by_id = {row["id"]: row for row in rows}
            total = 0
            marked_answers = {}
            for question_id in ids:
                question = by_id.get(question_id)
                value = str(answers.get(question_id, ""))[:1000]
                correct = bool(question and answer_matches(value, question["answer_key"]))
                points = question["points"] if correct else 0
                total += points
                marked_answers[question_id] = {"answer": value, "correct": correct, "points": points}
            db.execute(
                """UPDATE assessment_attempts SET submitted_at=?,answers_json=?,points_earned=?
                   WHERE id=? AND submitted_at IS NULL""",
                (timestamp(), json.dumps(marked_answers, ensure_ascii=False), total, attempt_id),
            )
            for question_id, result in marked_answers.items():
                db.execute(
                    """INSERT INTO exercise_attempts
                       (id,exercise_id,user_id,answer,correct,points_earned,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), question_id, user["id"], result["answer"],
                        int(result["correct"]), result["points"], timestamp(),
                    ),
                )
        self.send_json(HTTPStatus.OK, {
            "attempt_id": attempt_id, "points_earned": total, "points_possible": attempt["max_points"],
            "percentage": round(total / max(1, attempt["max_points"]) * 100),
            "answers": marked_answers,
        })

    def submit_game(self, payload: dict) -> None:
        user = self.require_role("student")
        exercise_id = str(payload.get("exercise_id", ""))
        answer = str(payload.get("answer", ""))[:1000]
        try:
            elapsed = max(0, min(int(payload.get("elapsed_seconds", 0)), 86400))
        except (TypeError, ValueError):
            elapsed = 0
        with connect_db() as db:
            exercise = db.execute("SELECT * FROM exercises WHERE id=? AND published=1", (exercise_id,)).fetchone()
            if not exercise:
                raise APIError(HTTPStatus.NOT_FOUND, "Сорилын бодлого олдсонгүй.")
            correct = answer_matches(answer, exercise["answer_key"])
            bonus = 2 if correct and elapsed <= 10 else 1 if correct and elapsed <= 25 else 0
            points = exercise["points"] + bonus if correct else 0
            class_row = db.execute(
                "SELECT class_id FROM class_memberships WHERE student_id=? ORDER BY joined_at LIMIT 1",
                (user["id"],),
            ).fetchone()
            class_id = class_row["class_id"] if class_row else None
            db.execute(
                "INSERT INTO game_scores(id,user_id,class_id,exercise_id,points,elapsed_seconds,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), user["id"], class_id, exercise_id, points, elapsed, timestamp()),
            )
            total_points = db.execute(
                "SELECT COALESCE(SUM(points),0) FROM game_scores WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
            correct_count = db.execute(
                "SELECT COUNT(*) FROM game_scores WHERE user_id=? AND points>0", (user["id"],)
            ).fetchone()[0]
            badges = []
            if correct_count >= 1:
                badges.append("first_win")
            if correct_count >= 5:
                badges.append("five_wins")
            if total_points >= 50:
                badges.append("score_50")
            if total_points >= 100:
                badges.append("score_100")
            for badge in badges:
                db.execute(
                    "INSERT OR IGNORE INTO badges(user_id,badge_code,awarded_at) VALUES(?,?,?)",
                    (user["id"], badge, timestamp()),
                )
        self.send_json(HTTPStatus.OK, {
            "correct": correct, "points_earned": points, "bonus_points": bonus,
            "total_points": total_points, "level": total_points // 50 + 1,
        })

    def daily_quiz(self) -> dict:
        day = utc_now().astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        with connect_db() as db:
            rows = db.execute(
                """SELECT id,prompt,answer_key,explanation,points FROM exercises
                   WHERE chapter_id='absolute' AND published=1 ORDER BY id"""
            ).fetchall()
        if not rows:
            raise APIError(HTTPStatus.SERVICE_UNAVAILABLE, "Өдрийн сорилын баталгаатай бодлого олдсонгүй.")
        selection = int(hashlib.sha256(day.encode("ascii")).hexdigest(), 16) % len(rows)
        question = rows[selection]
        return {
            "quiz_id": f"daily-{day}",
            "quiz_type": "daily",
            "title": "Өдрийн математик сорил",
            "lesson_id": "absolute-modulus",
            "questions": [{
                "id": question["id"], "prompt": question["prompt"],
                "points": question["points"], "answer": question["answer_key"],
                "explanation": question["explanation"],
            }],
        }

    def record_lesson_view(self, lesson_id: str) -> None:
        student = self.require_role("student")
        lesson = next((item for item in load_lessons() if item["id"] == lesson_id), None)
        if not lesson:
            raise APIError(HTTPStatus.NOT_FOUND, "Цахим хичээл олдсонгүй.")
        now = timestamp()
        with connect_db() as db:
            db.execute(
                """INSERT INTO lesson_progress(user_id,lesson_id,viewed_at,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id,lesson_id) DO UPDATE SET
                     viewed_at=excluded.viewed_at, updated_at=excluded.updated_at""",
                (student["id"], lesson_id, now, now),
            )
        self.send_json(HTTPStatus.OK, {"lesson_id": lesson_id, "viewed_at": now})

    def submit_lesson_quiz(self, quiz_id: str, payload: dict) -> None:
        student = self.require_role("student")
        if quiz_id == "daily":
            quiz = self.daily_quiz()
        else:
            lesson = next((item for item in load_lessons() if item["id"] == quiz_id), None)
            if not lesson:
                raise APIError(HTTPStatus.NOT_FOUND, "Сорил олдсонгүй.")
            quiz = {
                "quiz_id": lesson["id"],
                "quiz_type": "mini",
                "lesson_id": lesson["id"],
                "questions": lesson["quiz"],
            }
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise APIError(HTTPStatus.BAD_REQUEST, "Сорилын хариултуудыг илгээнэ үү.")
        question_ids = {question["id"] for question in quiz["questions"]}
        if not answers.keys() <= question_ids:
            raise APIError(HTTPStatus.BAD_REQUEST, "Сорилд хамаарахгүй асуултын хариулт байна.")
        for answer in answers.values():
            if not isinstance(answer, str) or len(answer) > 1000:
                raise APIError(HTTPStatus.BAD_REQUEST, "Хариулт 1000 тэмдэгтээс хэтрэхгүй текст байна.")
        now = timestamp()
        attempt_id = str(uuid.uuid4())
        score = 0
        max_score = sum(question["points"] for question in quiz["questions"])
        results = []
        for question in quiz["questions"]:
            answer = answers.get(question["id"], "").strip()
            correct = answer_matches(answer, question["answer"])
            if correct:
                score += question["points"]
            results.append({
                "question_id": question["id"], "prompt": question["prompt"], "answer": answer,
                "expected_answer": question["answer"], "correct": correct,
                "points_earned": question["points"] if correct else 0,
                "points_possible": question["points"], "explanation": question["explanation"],
            })
        with connect_db() as db:
            if quiz["quiz_type"] == "daily":
                previous = db.execute(
                    "SELECT 1 FROM lesson_quiz_attempts WHERE user_id=? AND quiz_id=?",
                    (student["id"], quiz["quiz_id"]),
                ).fetchone()
                if previous:
                    raise APIError(HTTPStatus.CONFLICT, "Өдрийн сорилыг өнөөдөр аль хэдийн илгээсэн байна.")
            db.execute(
                """INSERT INTO lesson_quiz_attempts
                   (id,user_id,lesson_id,quiz_id,quiz_type,points_earned,max_points,submitted_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, student["id"], quiz["lesson_id"], quiz["quiz_id"],
                    quiz["quiz_type"], score, max_score, now,
                ),
            )
            db.executemany(
                """INSERT INTO lesson_quiz_answers
                   (attempt_id,question_id,prompt,answer,expected_answer,correct,explanation)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        attempt_id, item["question_id"], item["prompt"], item["answer"],
                        item["expected_answer"], int(item["correct"]), item["explanation"],
                    )
                    for item in results
                ],
            )
            if quiz["quiz_type"] == "mini":
                passed = score * 100 >= max_score * 70
                db.execute(
                    """INSERT INTO lesson_progress
                       (user_id,lesson_id,viewed_at,completed_at,last_quiz_score,last_quiz_max,quiz_attempts,updated_at)
                       VALUES(?,?,?,?,?,?,1,?)
                       ON CONFLICT(user_id,lesson_id) DO UPDATE SET
                         viewed_at=COALESCE(lesson_progress.viewed_at,excluded.viewed_at),
                         completed_at=CASE WHEN ? THEN COALESCE(lesson_progress.completed_at,excluded.completed_at)
                                           ELSE lesson_progress.completed_at END,
                         last_quiz_score=excluded.last_quiz_score,
                         last_quiz_max=excluded.last_quiz_max,
                         quiz_attempts=lesson_progress.quiz_attempts+1,
                         updated_at=excluded.updated_at""",
                    (student["id"], quiz["lesson_id"], now, now if passed else None, score, max_score, now, int(passed)),
                )
        self.send_json(HTTPStatus.CREATED, {
            "attempt_id": attempt_id, "quiz_id": quiz["quiz_id"], "quiz_type": quiz["quiz_type"],
            "points_earned": score, "points_possible": max_score,
            "passed": quiz["quiz_type"] == "mini" and score * 100 >= max_score * 70,
            "answers": results,
        })

    def student_learning(self, student_id: str) -> dict:
        lessons = load_lessons()
        with connect_db() as db:
            progress_rows = db.execute(
                "SELECT * FROM lesson_progress WHERE user_id=?",
                (student_id,),
            ).fetchall()
            attempts = db.execute(
                """SELECT id,lesson_id,quiz_id,quiz_type,points_earned,max_points,submitted_at
                   FROM lesson_quiz_attempts WHERE user_id=? ORDER BY submitted_at DESC LIMIT 100""",
                (student_id,),
            ).fetchall()
            errors = db.execute(
                """SELECT qa.lesson_id,qa.quiz_id,qa.quiz_type,q.question_id,q.prompt,q.answer,
                          q.expected_answer,q.explanation,q.attempt_id,qa.submitted_at
                   FROM lesson_quiz_answers q
                   JOIN lesson_quiz_attempts qa ON qa.id=q.attempt_id
                   WHERE qa.user_id=? AND q.correct=0
                   ORDER BY qa.submitted_at DESC LIMIT 100""",
                (student_id,),
            ).fetchall()
        by_lesson = {row["lesson_id"]: dict(row) for row in progress_rows}
        available = []
        for lesson in lessons:
            item = by_lesson.get(lesson["id"], {})
            available.append({
                "lesson_id": lesson["id"],
                "viewed_at": item.get("viewed_at"),
                "completed_at": item.get("completed_at"),
                "last_quiz_score": item.get("last_quiz_score"),
                "last_quiz_max": item.get("last_quiz_max"),
                "quiz_attempts": item.get("quiz_attempts", 0),
            })
        return {"lessons": available, "attempts": [dict(row) for row in attempts], "errors": [dict(row) for row in errors]}

    def teacher_learning(self, teacher_id: str) -> dict:
        with connect_db() as db:
            students = db.execute(
                """SELECT c.id AS class_id,c.name AS class_name,u.id AS student_id,
                          u.display_name,u.email,
                          (SELECT COUNT(*) FROM lesson_progress lp
                           WHERE lp.user_id=u.id AND lp.viewed_at IS NOT NULL) AS viewed_lessons,
                          (SELECT COUNT(*) FROM lesson_progress lp
                           WHERE lp.user_id=u.id AND lp.completed_at IS NOT NULL) AS completed_lessons,
                          (SELECT COUNT(*) FROM lesson_quiz_attempts qa WHERE qa.user_id=u.id) AS quiz_attempts,
                          (SELECT COUNT(*) FROM lesson_quiz_answers q
                           JOIN lesson_quiz_attempts qa ON qa.id=q.attempt_id
                           WHERE qa.user_id=u.id AND q.correct=0) AS quiz_errors,
                          (SELECT MAX(qa.submitted_at) FROM lesson_quiz_attempts qa
                           WHERE qa.user_id=u.id) AS last_quiz_at
                   FROM classes c JOIN class_memberships m ON m.class_id=c.id
                   JOIN users u ON u.id=m.student_id
                   WHERE c.teacher_id=? ORDER BY c.name,u.display_name""",
                (teacher_id,),
            ).fetchall()
            errors = db.execute(
                """SELECT c.name AS class_name,u.display_name,u.email,qa.lesson_id,
                          qa.quiz_id,qa.quiz_type,q.prompt,q.answer,q.expected_answer,
                          q.explanation,qa.submitted_at
                   FROM classes c JOIN class_memberships m ON m.class_id=c.id
                   JOIN users u ON u.id=m.student_id
                   JOIN lesson_quiz_attempts qa ON qa.user_id=u.id
                   JOIN lesson_quiz_answers q ON q.attempt_id=qa.id
                   WHERE c.teacher_id=? AND q.correct=0
                   ORDER BY qa.submitted_at DESC LIMIT 300""",
                (teacher_id,),
            ).fetchall()
        return {"students": [dict(row) for row in students], "errors": [dict(row) for row in errors]}

    def student_errors(self, student_id: str) -> dict:
        with connect_db() as db:
            rows = db.execute(
                """SELECT a.id AS attempt_id,a.exercise_id,a.answer,a.created_at,e.chapter_id,e.topic,
                          e.level,e.prompt,e.explanation,e.hint,e.criteria_json
                   FROM exercise_attempts a JOIN exercises e ON e.id=a.exercise_id
                   WHERE a.user_id=? AND a.correct=0
                   ORDER BY a.created_at DESC LIMIT 50""",
                (student_id,),
            ).fetchall()
        errors = []
        for row in rows:
            item = dict(row)
            item["criteria"] = json.loads(item.pop("criteria_json"))
            errors.append(item)
        return {"errors": errors}

    def student_dashboard(self, student_id: str) -> dict:
        with connect_db() as db:
            chapters = db.execute(
                """SELECT c.id,c.number,c.title,COUNT(DISTINCT e.id) AS available_exercises,
                          COUNT(DISTINCT a.exercise_id) AS attempted_exercises,
                          COUNT(a.id) AS attempts,
                          COALESCE(SUM(a.points_earned),0) AS points,
                          COALESCE((SELECT SUM(available.points) FROM exercises available
                                    WHERE available.chapter_id=c.id AND available.published=1),0) AS possible_points,
                          SUM(CASE WHEN a.id IS NOT NULL AND a.correct=1 THEN 1 ELSE 0 END) AS correct_attempts
                   FROM chapters c
                   LEFT JOIN exercises e ON e.chapter_id=c.id AND e.published=1
                   LEFT JOIN exercise_attempts a ON a.exercise_id=e.id AND a.user_id=?
                   GROUP BY c.id ORDER BY c.rowid""",
                (student_id,),
            ).fetchall()
            exercise_score = db.execute(
                "SELECT COALESCE(SUM(points_earned),0) FROM exercise_attempts WHERE user_id=?",
                (student_id,),
            ).fetchone()[0]
            game = db.execute(
                "SELECT COALESCE(SUM(points),0) AS points,COUNT(*) AS games FROM game_scores WHERE user_id=?",
                (student_id,),
            ).fetchone()
            badges = db.execute(
                "SELECT badge_code,awarded_at FROM badges WHERE user_id=? ORDER BY awarded_at",
                (student_id,),
            ).fetchall()
            assessments = db.execute(
                """SELECT s.title,aa.points_earned,aa.max_points,aa.submitted_at
                   FROM assessment_attempts aa JOIN assessments s ON s.id=aa.assessment_id
                   WHERE aa.user_id=? AND aa.submitted_at IS NOT NULL
                   ORDER BY aa.submitted_at DESC LIMIT 20""",
                (student_id,),
            ).fetchall()
        chapter_list = [dict(row) for row in chapters]
        total_attempts = sum(row["attempts"] for row in chapter_list)
        correct_attempts = sum(row["correct_attempts"] or 0 for row in chapter_list)
        return {
            "chapters": chapter_list,
            "summary": {
                "studied_chapters": sum(row["attempts"] > 0 for row in chapter_list),
                "total_chapters": len(chapter_list),
                "total_attempts": total_attempts,
                "correct_attempts": correct_attempts,
                "accuracy_percent": round(correct_attempts / max(total_attempts, 1) * 100),
                "exercise_points": exercise_score,
                "game_points": game["points"],
                "games_played": game["games"],
                "level": game["points"] // 50 + 1,
                "points_to_next_level": 50 - game["points"] % 50,
            },
            "badges": [dict(row) for row in badges],
            "assessments": [dict(row) for row in assessments],
            **self.student_errors(student_id),
        }

    def leaderboard(self, user: sqlite3.Row, class_id: str | None) -> dict:
        if user["role"] != "teacher" and class_id:
            with connect_db() as db:
                member = db.execute(
                    """SELECT 1 FROM class_memberships WHERE class_id=? AND student_id=?""",
                    (class_id, user["id"]),
                ).fetchone()
                owner = db.execute(
                    "SELECT 1 FROM classes WHERE id=? AND teacher_id=?", (class_id, user["id"])
                ).fetchone()
            if not member and not owner:
                raise APIError(HTTPStatus.FORBIDDEN, "Энэ ангийн жагсаалтыг харах эрхгүй байна.")
        with connect_db() as db:
            if class_id:
                rows = db.execute(
                    """SELECT u.id,u.display_name,COALESCE(SUM(g.points),0) AS points,
                              COUNT(g.id) AS games
                       FROM class_memberships m JOIN users u ON u.id=m.student_id
                       LEFT JOIN game_scores g ON g.user_id=u.id AND g.class_id=m.class_id
                       WHERE m.class_id=? GROUP BY u.id ORDER BY points DESC,u.display_name LIMIT 100""",
                    (class_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT u.id,u.display_name,COALESCE(SUM(g.points),0) AS points,
                              COUNT(g.id) AS games
                       FROM users u LEFT JOIN game_scores g ON g.user_id=u.id
                       WHERE u.role='student' GROUP BY u.id ORDER BY points DESC,u.display_name LIMIT 100"""
                ).fetchall()
        return {"leaderboard": [dict(row) for row in rows]}

    def teacher_dashboard(self, teacher_id: str) -> dict:
        with connect_db() as db:
            classes = db.execute(
                """SELECT c.id,c.name,c.created_at,
                          (SELECT COUNT(*) FROM class_memberships m WHERE m.class_id=c.id) AS student_count,
                          (SELECT COUNT(*) FROM assignments a WHERE a.class_id=c.id) AS assignment_count,
                          (SELECT COUNT(*) FROM assessment_assignments aa WHERE aa.class_id=c.id) AS assessment_assignment_count,
                          (SELECT ROUND(AVG(a.points_earned*100.0/e.points),1)
                           FROM class_memberships m JOIN exercise_attempts a ON a.user_id=m.student_id
                           JOIN exercises e ON e.id=a.exercise_id WHERE m.class_id=c.id) AS average_percent,
                          (SELECT COUNT(*) FROM class_memberships m JOIN exercise_attempts a ON a.user_id=m.student_id
                           WHERE m.class_id=c.id AND a.reviewed_points IS NULL AND a.correct=0) AS pending_review
                   FROM classes c WHERE c.teacher_id=? ORDER BY c.created_at DESC""",
                (teacher_id,),
            ).fetchall()
            students = db.execute(
                """SELECT c.id AS class_id,c.name AS class_name,u.id AS student_id,u.display_name,u.email,
                          COUNT(a.id) AS attempts,COALESCE(SUM(a.points_earned),0) AS points,
                          COUNT(DISTINCT CASE WHEN a.correct=0 THEN a.exercise_id END) AS error_questions,
                          COUNT(a.reviewed_points) AS teacher_reviews
                   FROM classes c JOIN class_memberships m ON m.class_id=c.id
                   JOIN users u ON u.id=m.student_id
                   LEFT JOIN exercise_attempts a ON a.user_id=u.id
                   WHERE c.teacher_id=? GROUP BY c.id,u.id ORDER BY c.name,u.display_name""",
                (teacher_id,),
            ).fetchall()
            attempts = db.execute(
                """SELECT a.id,a.user_id,u.display_name,a.exercise_id,e.topic,e.level,a.answer,a.correct,
                          a.points_earned,e.points,a.reviewed_points,a.review_comment,a.created_at
                   FROM classes c JOIN class_memberships m ON m.class_id=c.id
                   JOIN exercise_attempts a ON a.user_id=m.student_id
                   JOIN users u ON u.id=a.user_id JOIN exercises e ON e.id=a.exercise_id
                   WHERE c.teacher_id=? ORDER BY a.created_at DESC LIMIT 300""",
                (teacher_id,),
            ).fetchall()
            assessment_rows = db.execute(
                """SELECT aa.id,aa.assessment_id,aa.user_id,u.display_name,s.title,aa.points_earned,
                          aa.max_points,aa.started_at,aa.submitted_at
                   FROM assessment_attempts aa JOIN assessments s ON s.id=aa.assessment_id
                   JOIN users u ON u.id=aa.user_id
                   JOIN class_memberships m ON m.student_id=u.id
                   JOIN classes c ON c.id=m.class_id
                   WHERE c.teacher_id=? ORDER BY aa.started_at DESC LIMIT 200""",
                (teacher_id,),
            ).fetchall()
        return {
            "classes": [dict(row) for row in classes],
            "students": [dict(row) for row in students],
            "attempts": [dict(row) for row in attempts],
            "assessments": [dict(row) for row in assessment_rows],
        }

    def serve_static(self, requested: str | None = None, head_only: bool = False) -> None:
        relative = (requested or "/").lstrip("/") or "index.html"
        if relative in {"", "."}:
            relative = "index.html"
        if re.fullmatch(
            r"media/lessons/[a-f0-9]{32}\.(?:ppt|pptx|pdf|mp4|m4v|mov|webm|mkv)",
            relative,
        ):
            self.serve_lesson_media(relative, head_only)
            return
        media_file = relative.startswith("media/")
        if relative.startswith("media/"):
            media_match = re.fullmatch(r"media/profiles/(profile-[a-f0-9]{32}\.(?:png|jpg|webp))", relative)
            cover_match = re.fullmatch(r"media/cover/(cover-[a-f0-9]{32}\.(?:png|jpg|webp))", relative)
            logo_match = re.fullmatch(r"media/cover/(logo-[a-f0-9]{32}\.(?:png|jpg|webp))", relative)
            if media_match:
                candidate = PROFILE_PHOTO_DIR / media_match.group(1)
            elif cover_match:
                candidate = COVER_IMAGE_DIR / cover_match.group(1)
            elif logo_match:
                candidate = COVER_IMAGE_DIR / logo_match.group(1)
            else:
                candidate = ROOT / "__not_found__"
        else:
            candidate = (ROOT / relative).resolve()
            if relative not in STATIC_FILES:
                candidate = ROOT / "__not_found__"
        if not media_file and (INSTANCE in candidate.parents or (ROOT / "tests") in candidate.parents):
            candidate = ROOT / "__not_found__"
        if (not media_file and ROOT not in candidate.parents) or not candidate.is_file():
            body = b"Not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return
        content_types = {
            ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
            ".webp": "image/webp", ".ico": "image/x-icon",
        }
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(candidate.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def serve_lesson_media(self, relative: str, head_only: bool) -> None:
        match = re.fullmatch(
            r"media/lessons/([a-f0-9]{32})\.(ppt|pptx|pdf|mp4|m4v|mov|webm|mkv)",
            relative,
        )
        if not match:
            raise APIError(HTTPStatus.NOT_FOUND, "Материал олдсонгүй.")
        media_id, extension = match.groups()
        with connect_db() as db:
            media = db.execute(
                "SELECT id,extension FROM lesson_media WHERE id=?", (media_id,)
            ).fetchone()
        if not media or media["extension"] != extension:
            raise APIError(HTTPStatus.NOT_FOUND, "Материал олдсонгүй.")
        path = LESSON_MEDIA_DIR / f"lesson-{media_id}.{extension}"
        if not path.is_file():
            raise APIError(HTTPStatus.NOT_FOUND, "Материалын файл олдсонгүй.")

        content_types = {
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pdf": "application/pdf",
            "mp4": "video/mp4",
            "m4v": "video/mp4",
            "mov": "video/quicktime",
            "webm": "video/webm",
            "mkv": "video/x-matroska",
        }
        file_size = path.stat().st_size
        start, end = 0, file_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            range_match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not range_match or (not range_match.group(1) and not range_match.group(2)):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            first, last = range_match.groups()
            if first:
                start = int(first)
                end = min(int(last), file_size - 1) if last else file_size - 1
            else:
                suffix_length = int(last)
                start = max(file_size - suffix_length, 0)
            if start >= file_size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        disposition = "attachment" if extension in {"ppt", "pptx"} else "inline"
        self.send_response(status)
        self.send_header("Content-Type", content_types[extension])
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'{disposition}; filename="lesson-{media_id}.{extension}"')
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    schema()
    invite = os.environ.get("TEACHER_INVITE_CODE")
    if not invite:
        invite = secrets.token_hex(6).upper()
        print(f"[БАГШИЙН БҮРТГЭЛ] Анхны багшийн урилгын код: {invite}", flush=True)
        print("[ТОХИРГОО] Дахин асаахад хадгалахын тулд TEACHER_INVITE_CODE орчны хувьсагч тохируулна уу.", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.teacher_invite_code = invite
    server.dummy_password_hash = password_hash("timing-safe dummy password")
    print(f"[СЕРВЕР] http://{HOST}:{PORT}/", flush=True)
    print(f"[ӨГӨГДӨЛ] SQLite: {DB_PATH}", flush=True)
    if APP_ENV == "production" and HOST not in {"127.0.0.1", "::1", "localhost"} and not COOKIE_SECURE:
        print("[АНХААРУУЛГА] Алсын production хостод COOKIE_SECURE=1 болон HTTPS proxy тохируулна уу.", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[СЕРВЕР] Зогслоо.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
