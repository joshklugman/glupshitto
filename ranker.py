"""
Star Wars Databank Beli-Style Ranker

Run the Streamlit app:
    python -m pip install streamlit requests pandas
    python -m streamlit run ranker.py

Run built-in logic tests without Streamlit installed:
    python ranker.py --test

Notes:
- This version uses the Star Wars Databank API instead of SWAPI because Databank
  includes images and longer descriptions.
- Databank categories available from the public docs are:
  characters, creatures, droids, locations, organizations, species, vehicles.
- The API does not currently document separate films or starships endpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import secrets
import sqlite3
import random
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

API_BASE = "https://starwars-databank-server.onrender.com/api/v1"
PAGE_LIMIT = 100
DB_PATH = Path(__file__).with_name("ranker_backend.sqlite3")
FORMULA_STATE_KEYS = ("rating", "wins", "losses", "battles")
SUPABASE_USERS_TABLE = "ranker_users"
SUPABASE_PROGRESS_TABLE = "ranker_progress"

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "all": {
        "label": "All Together",
        "emoji": "*",
        "endpoint": None,
    },
    "characters": {
        "label": "Characters",
        "emoji": "🧑",
        "endpoint": "characters",
    },
    "creatures": {
        "label": "Creatures",
        "emoji": "🐉",
        "endpoint": "creatures",
    },
    "droids": {
        "label": "Droids",
        "emoji": "🤖",
        "endpoint": "droids",
    },
    "locations": {
        "label": "Locations / Planets",
        "emoji": "🪐",
        "endpoint": "locations",
    },
    "organizations": {
        "label": "Organizations",
        "emoji": "🏛️",
        "endpoint": "organizations",
    },
    "species": {
        "label": "Species",
        "emoji": "👽",
        "endpoint": "species",
    },
    "vehicles": {
        "label": "Vehicles / Ships",
        "emoji": "🚀",
        "endpoint": "vehicles",
    },
}
DATABANK_CATEGORY_KEYS = [key for key, config in CATEGORIES.items() if config["endpoint"]]

SYSTEM_KEYS = {"_id", "id", "__v", "url"}
PRIMARY_KEYS = {"name", "description", "image"}
APPEARANCE_FIELD_LABELS = {
    "appearances": "Appearances",
    "appearance": "Appearances",
    "films": "Movies",
    "film": "Movies",
    "movies": "Movies",
    "movie": "Movies",
    "tvshows": "TV Shows",
    "tvshow": "TV Shows",
    "tvseries": "TV Shows",
    "shows": "TV Shows",
    "show": "TV Shows",
    "series": "TV Shows",
    "books": "Books",
    "book": "Books",
    "novels": "Books",
    "novel": "Books",
    "comics": "Comics",
    "comic": "Comics",
    "games": "Games",
    "game": "Games",
    "videogames": "Games",
    "videogame": "Games",
}


def normalize_text(value: Any) -> str:
    if value is None or value == "":
        return "Unknown"
    if isinstance(value, list):
        return ", ".join(normalize_text(v) for v in value) if value else "None listed"
    if isinstance(value, dict):
        return ", ".join(f"{pretty_key(k)}: {normalize_text(v)}" for k, v in value.items())
    return str(value)


def pretty_key(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").title()


def compact_key(key: str) -> str:
    return "".join(character.lower() for character in key if character.isalnum())


def normalize_appearance_values(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        values: List[str] = []
        for item in value:
            values.extend(normalize_appearance_values(item))
        return values
    if isinstance(value, dict):
        title = value.get("title") or value.get("name")
        return [normalize_text(title)] if title else [normalize_text(value)]
    return [normalize_text(value)]


def extract_appearances(raw_item: dict) -> Dict[str, List[str]]:
    appearances: Dict[str, List[str]] = {}

    for key, value in raw_item.items():
        label = APPEARANCE_FIELD_LABELS.get(compact_key(key))
        if not label:
            continue

        values = normalize_appearance_values(value)
        if values:
            existing = appearances.setdefault(label, [])
            for appearance in values:
                if appearance not in existing:
                    existing.append(appearance)

    return appearances


def summarize_appearances(appearances: Dict[str, List[str]]) -> str:
    return "; ".join(
        f"{label}: {', '.join(values)}"
        for label, values in appearances.items()
        if values
    )


def extract_data(payload: Any) -> Tuple[List[dict], Optional[str]]:
    """Return data list and next-page URL/path from a Databank response."""
    if isinstance(payload, list):
        return payload, None

    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected API response type: {type(payload).__name__}")

    data = payload.get("data")
    info = payload.get("info", {})
    next_path = info.get("next") if isinstance(info, dict) else None

    if not isinstance(data, list):
        raise ValueError("API response did not contain a list in the 'data' field.")

    return data, next_path


def make_page_url(endpoint: str, page: int, limit: int = PAGE_LIMIT) -> str:
    return f"{API_BASE}/{endpoint}?page={page}&limit={limit}"


def fetch_databank_category(endpoint: str, max_pages: Optional[int] = None) -> List[dict]:
    """Fetch one full Databank collection, following pagination."""
    items: List[dict] = []
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            break

        response = requests.get(make_page_url(endpoint, page), timeout=25)
        response.raise_for_status()
        data, next_path = extract_data(response.json())
        items.extend(data)

        if not next_path or not data:
            break

        page += 1

    return items


def normalize_items(category: str, raw_items: Sequence[dict]) -> List[dict]:
    normalized: List[dict] = []

    for index, item in enumerate(raw_items):
        name = normalize_text(item.get("name") or item.get("title") or f"Unknown {index + 1}")
        image = item.get("image")
        description = normalize_text(item.get("description"))
        item_id = normalize_text(item.get("_id") or item.get("id") or f"{category}-{index}")

        extra_fields = {
            key: value
            for key, value in item.items()
            if key not in SYSTEM_KEYS
            and key not in PRIMARY_KEYS
            and compact_key(key) not in APPEARANCE_FIELD_LABELS
        }

        normalized.append(
            {
                "id": item_id,
                "category": category,
                "name": name,
                "description": description,
                "image": image,
                "appearances": extract_appearances(item),
                "extra_fields": extra_fields,
                "raw": dict(item),
                "rating": 1500,
                "wins": 0,
                "losses": 0,
                "battles": 0,
            }
        )

    return normalized


def combine_category_items(category_items: Dict[str, Sequence[dict]]) -> List[dict]:
    combined: List[dict] = []

    for category, raw_items in category_items.items():
        for item in normalize_items(category, raw_items):
            item["id"] = f"{category}:{item['id']}"
            combined.append(item)

    return combined


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000)
    return salt, password_hash.hex()


def get_secret_value(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value

    try:
        import streamlit as st
    except ModuleNotFoundError:
        return None

    try:
        value = st.secrets.get(name)
    except Exception:
        return None

    return str(value) if value else None


def get_supabase_rest_url() -> Optional[str]:
    url = get_secret_value("SUPABASE_URL")
    if not url:
        return None

    url = url.rstrip("/")
    if url.endswith("/rest/v1"):
        return url
    return f"{url}/rest/v1"


def get_supabase_key() -> Optional[str]:
    return get_secret_value("SUPABASE_SERVICE_ROLE_KEY")


def supabase_enabled() -> bool:
    return bool(get_supabase_rest_url() and get_supabase_key())


def supabase_headers(prefer: Optional[str] = None) -> Dict[str, str]:
    key = get_supabase_key()
    headers = {
        "apikey": key or "",
        "Authorization": f"Bearer {key or ''}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_request(
    method: str,
    table: str,
    *,
    params: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    prefer: Optional[str] = None,
) -> Any:
    rest_url = get_supabase_rest_url()
    if not rest_url:
        raise RuntimeError("Supabase URL is not configured.")

    response = requests.request(
        method,
        f"{rest_url}/{table}",
        headers=supabase_headers(prefer),
        params=params,
        json=payload,
        timeout=25,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def parse_json_field(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def init_backend(db_path: Path = DB_PATH) -> None:
    if supabase_enabled():
        return

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                username TEXT NOT NULL,
                ranking_key TEXT NOT NULL,
                items_json TEXT NOT NULL,
                current_pair_json TEXT,
                last_pick TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (username, ranking_key),
                FOREIGN KEY (username) REFERENCES users(username)
            )
            """
        )
        conn.commit()


def create_user(username: str, password: str, db_path: Path = DB_PATH) -> bool:
    username = username.strip()
    if not username or not password:
        return False

    salt, password_hash = hash_password(password)

    if supabase_enabled():
        existing = supabase_request(
            "GET",
            SUPABASE_USERS_TABLE,
            params={"username": f"eq.{username}", "select": "username", "limit": "1"},
        )
        if existing:
            return False

        supabase_request(
            "POST",
            SUPABASE_USERS_TABLE,
            payload={
                "username": username,
                "salt": salt,
                "password_hash": password_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )
        return True

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO users (username, salt, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, salt, password_hash, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False

    return True


def authenticate_user(username: str, password: str, db_path: Path = DB_PATH) -> bool:
    username = username.strip()

    if supabase_enabled():
        rows = supabase_request(
            "GET",
            SUPABASE_USERS_TABLE,
            params={"username": f"eq.{username}", "select": "salt,password_hash", "limit": "1"},
        )
        if not rows:
            return False

        row = rows[0]
        _, actual_hash = hash_password(password, row["salt"])
        return secrets.compare_digest(actual_hash, row["password_hash"])

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT salt, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not row:
        return False

    salt, expected_hash = row
    _, actual_hash = hash_password(password, salt)
    return secrets.compare_digest(actual_hash, expected_hash)


def load_user_progress(username: str, ranking_key: str, db_path: Path = DB_PATH) -> Optional[dict]:
    if supabase_enabled():
        rows = supabase_request(
            "GET",
            SUPABASE_PROGRESS_TABLE,
            params={
                "username": f"eq.{username}",
                "ranking_key": f"eq.{ranking_key}",
                "select": "items_json,current_pair_json,last_pick",
                "limit": "1",
            },
        )
        if not rows:
            return None

        row = rows[0]
        current_pair = parse_json_field(row.get("current_pair_json"))
        return {
            "items": parse_json_field(row.get("items_json")) or [],
            "current_pair": tuple(current_pair) if current_pair else None,
            "last_pick": row.get("last_pick"),
        }

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT items_json, current_pair_json, last_pick
            FROM progress
            WHERE username = ? AND ranking_key = ?
            """,
            (username, ranking_key),
        ).fetchone()

    if not row:
        return None

    items_json, current_pair_json, last_pick = row
    return {
        "items": json.loads(items_json),
        "current_pair": tuple(json.loads(current_pair_json)) if current_pair_json else None,
        "last_pick": last_pick,
    }


def load_user_progress_with_legacy_keys(
    username: str,
    ranking_key: str,
    db_path: Path = DB_PATH,
) -> Optional[dict]:
    progress = load_user_progress(username, ranking_key, db_path)
    if progress:
        return progress

    legacy_keys = (
        f"{ranking_key}:All available",
        f"{ranking_key}:First 300",
        f"{ranking_key}:First 100",
    )
    for legacy_key in legacy_keys:
        progress = load_user_progress(username, legacy_key, db_path)
        if progress:
            return progress

    return None


def apply_saved_progress(items: List[dict], progress: Optional[dict]) -> None:
    if not progress:
        return

    saved_by_id = {item["id"]: item for item in progress.get("items", [])}
    for item in items:
        saved_item = saved_by_id.get(item["id"])
        if saved_item:
            for key in FORMULA_STATE_KEYS:
                item[key] = int(saved_item.get(key, item[key]))


def save_user_progress(
    username: str,
    ranking_key: str,
    items: Sequence[dict],
    current_pair: Optional[Sequence[str]],
    last_pick: Optional[str],
    db_path: Path = DB_PATH,
) -> None:
    items_state = [
        {"id": item["id"], **{key: item[key] for key in FORMULA_STATE_KEYS}}
        for item in items
    ]
    current_pair_state = list(current_pair) if current_pair else None

    if supabase_enabled():
        supabase_request(
            "POST",
            SUPABASE_PROGRESS_TABLE,
            params={"on_conflict": "username,ranking_key"},
            payload={
                "username": username,
                "ranking_key": ranking_key,
                "items_json": items_state,
                "current_pair_json": current_pair_state,
                "last_pick": last_pick,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return

    current_pair_json = json.dumps(current_pair_state) if current_pair_state else None

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO progress (username, ranking_key, items_json, current_pair_json, last_pick, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, ranking_key) DO UPDATE SET
                items_json = excluded.items_json,
                current_pair_json = excluded.current_pair_json,
                last_pick = excluded.last_pick,
                updated_at = excluded.updated_at
            """,
            (
                username,
                ranking_key,
                json.dumps(items_state),
                current_pair_json,
                last_pick,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def delete_user_progress_for_category(username: str, category: str, db_path: Path = DB_PATH) -> None:
    if supabase_enabled():
        supabase_request(
            "DELETE",
            SUPABASE_PROGRESS_TABLE,
            params={"username": f"eq.{username}", "ranking_key": f"eq.{category}"},
            prefer="return=minimal",
        )
        supabase_request(
            "DELETE",
            SUPABASE_PROGRESS_TABLE,
            params={"username": f"eq.{username}", "ranking_key": f"like.{category}:%"},
            prefer="return=minimal",
        )
        return

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "DELETE FROM progress WHERE username = ? AND ranking_key = ?",
            (username, category),
        )
        conn.execute(
            "DELETE FROM progress WHERE username = ? AND ranking_key LIKE ?",
            (username, f"{category}:%"),
        )
        conn.commit()


def expected_score(rating_a: int, rating_b: int) -> float:
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def update_elo(winner_rating: int, loser_rating: int, k: int = 32) -> Tuple[int, int]:
    winner_expected = expected_score(winner_rating, loser_rating)
    loser_expected = expected_score(loser_rating, winner_rating)
    return (
        round(winner_rating + k * (1 - winner_expected)),
        round(loser_rating + k * (0 - loser_expected)),
    )


def pick_pair(items: Sequence[dict]) -> Tuple[dict, dict]:
    if len(items) < 2:
        raise ValueError("Need at least two items to rank.")

    least_seen_pool = sorted(items, key=lambda item: item.get("battles", 0))[: min(20, len(items))]
    first = random.choice(least_seen_pool)

    opponents = [item for item in items if item["id"] != first["id"]]
    similar_pool = sorted(opponents, key=lambda item: abs(item["rating"] - first["rating"]))[: min(12, len(opponents))]
    second = random.choice(similar_pool)

    pair = [first, second]
    random.shuffle(pair)
    return pair[0], pair[1]


def display_rating(item: dict) -> str:
    return str(item["rating"]) if item.get("battles", 0) > 0 else "-"


def category_item_id(item: dict) -> str:
    category = item.get("category")
    item_id = item["id"]
    prefix = f"{category}:"
    return item_id[len(prefix) :] if category and item_id.startswith(prefix) else item_id


def rank_items(items: Sequence[dict]) -> List[dict]:
    return sorted(
        items,
        key=lambda item: (
            item.get("battles", 0) == 0,
            -item["rating"],
            -item["wins"],
            item["name"],
        ),
    )


def apply_vote(items: List[dict], winner_id: str, loser_id: str) -> str:
    winner = next(item for item in items if item["id"] == winner_id)
    loser = next(item for item in items if item["id"] == loser_id)

    new_winner_rating, new_loser_rating = update_elo(winner["rating"], loser["rating"])

    winner["rating"] = new_winner_rating
    winner["wins"] += 1
    winner["battles"] += 1

    loser["rating"] = new_loser_rating
    loser["losses"] += 1
    loser["battles"] += 1

    return winner["name"]


def apply_same_category_vote(category_items: List[dict], winner_item: dict, loser_item: dict) -> bool:
    if winner_item.get("category") != loser_item.get("category"):
        return False

    winner_id = category_item_id(winner_item)
    loser_id = category_item_id(loser_item)
    category_item_ids = {item["id"] for item in category_items}

    if winner_id not in category_item_ids or loser_id not in category_item_ids:
        return False

    apply_vote(category_items, winner_id, loser_id)
    return True


def run_tests() -> None:
    sample_payload = {
        "info": {"total": 2, "page": 1, "limit": 10, "next": None, "prev": None},
        "data": [
            {
                "_id": "1",
                "name": "Luke Skywalker",
                "description": "A Jedi Knight.",
                "image": "https://example.com/luke.jpg",
                "homeworld": "Tatooine",
                "films": ["A New Hope", "The Empire Strikes Back"],
                "tvShows": [{"title": "The Mandalorian"}],
            },
            {
                "_id": "2",
                "name": "Darth Vader",
                "description": "A Sith Lord.",
                "image": "https://example.com/vader.jpg",
                "weapon": "Lightsaber",
                "books": ["Lords of the Sith"],
            },
        ],
    }

    data, next_path = extract_data(sample_payload)
    assert len(data) == 2
    assert next_path is None

    items = normalize_items("characters", data)
    assert items[0]["name"] == "Luke Skywalker"
    assert items[0]["image"] == "https://example.com/luke.jpg"
    assert items[0]["extra_fields"]["homeworld"] == "Tatooine"
    assert items[0]["appearances"]["Movies"] == ["A New Hope", "The Empire Strikes Back"]
    assert items[0]["appearances"]["TV Shows"] == ["The Mandalorian"]
    assert items[1]["appearances"]["Books"] == ["Lords of the Sith"]
    assert items[0]["rating"] == 1500

    combined = combine_category_items({"characters": data, "vehicles": data})
    assert len(combined) == 4
    assert combined[0]["id"] == "characters:1"
    assert combined[2]["id"] == "vehicles:1"
    assert combined[2]["category"] == "vehicles"
    assert category_item_id(combined[0]) == "1"

    category_items = normalize_items("characters", data)
    assert apply_same_category_vote(category_items, combined[0], combined[1])
    assert category_items[0]["wins"] == 1
    assert category_items[1]["losses"] == 1
    assert not apply_same_category_vote(category_items, combined[0], combined[2])

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db = Path(tmp_dir) / "ranker_test.sqlite3"
        init_backend(test_db)
        assert create_user("luke", "jedi", test_db)
        assert not create_user("luke", "jedi", test_db)
        assert authenticate_user("luke", "jedi", test_db)
        assert not authenticate_user("luke", "sith", test_db)

        saved_items = normalize_items("characters", data)
        saved_items[0]["rating"] = 1600
        saved_items[0]["wins"] = 3
        save_user_progress("luke", "characters", saved_items, ("1", "2"), "Luke Skywalker", test_db)

        loaded_progress = load_user_progress("luke", "characters", test_db)
        fresh_items = normalize_items("characters", data)
        apply_saved_progress(fresh_items, loaded_progress)
        assert fresh_items[0]["rating"] == 1600
        assert fresh_items[0]["wins"] == 3
        assert loaded_progress["current_pair"] == ("1", "2")
        assert loaded_progress["last_pick"] == "Luke Skywalker"

        save_user_progress("luke", "vehicles:First 300", saved_items, ("1", "2"), "Luke Skywalker", test_db)
        assert load_user_progress_with_legacy_keys("luke", "vehicles", test_db)["last_pick"] == "Luke Skywalker"

    winner_rating, loser_rating = update_elo(1500, 1500)
    assert winner_rating == 1516
    assert loser_rating == 1484

    winner_name = apply_vote(items, "1", "2")
    assert winner_name == "Luke Skywalker"
    assert items[0]["wins"] == 1
    assert items[1]["losses"] == 1
    assert items[0]["rating"] == 1516
    assert items[1]["rating"] == 1484

    ranked = rank_items(items)
    assert ranked[0]["name"] == "Luke Skywalker"

    untouched = normalize_items("characters", data)
    untouched[1]["rating"] = 1800
    untouched[1]["wins"] = 5
    ranked_with_neutral = rank_items(items + [untouched[1]])
    assert ranked_with_neutral[-1]["name"] == "Darth Vader"
    assert display_rating(untouched[1]) == "-"
    assert display_rating(items[0]) == "1516"

    a, b = pick_pair(items)
    assert a["id"] != b["id"]

    print("All tests passed.")


def run_streamlit_app() -> None:
    try:
        import pandas as pd
        import streamlit as st
    except ModuleNotFoundError as exc:
        missing = exc.name or "a required package"
        print(f"Missing package: {missing}")
        print("Install dependencies with:")
        print("    python -m pip install streamlit requests pandas")
        raise SystemExit(1) from exc

    st.set_page_config(
        page_title="Glup Shitto Ranker",
        page_icon="⭐",
        layout="wide",
    )

    st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;700&display=swap');

            :root {
                --space: #05070d;
                --panel: rgba(11, 17, 31, 0.88);
                --panel-strong: rgba(7, 11, 20, 0.96);
                --rebel-gold: #ffe81f;
                --gold-soft: #fff4a8;
                --hyperspace-blue: #69d7ff;
                --saber-red: #ff4b4b;
                --text-soft: #f7fbff;
                --text-muted: #d8e5f5;
                --border-glow: rgba(255, 232, 31, 0.34);
            }

            .stApp {
                background:
                    radial-gradient(circle at 16% 18%, rgba(105, 215, 255, 0.12) 0 1px, transparent 2px),
                    radial-gradient(circle at 78% 8%, rgba(255, 232, 31, 0.18) 0 1px, transparent 2px),
                    radial-gradient(circle at 52% 42%, rgba(255, 255, 255, 0.10) 0 1px, transparent 2px),
                    linear-gradient(135deg, rgba(5, 7, 13, 0.94), rgba(7, 10, 22, 0.98)),
                    repeating-radial-gradient(circle at center, #0a1020 0 1px, #05070d 1px 4px);
                color: var(--text-soft);
            }

            .block-container {
                padding-top: 2rem;
                padding-bottom: 3rem;
            }

            h1, h2, h3, [data-testid="stMetricLabel"], .st-emotion-cache-10trblm {
                font-family: 'Orbitron', sans-serif;
                letter-spacing: 0;
            }

            h1:not(.starwars-title) {
                display: none;
            }

            p, label, span, div, button, input, textarea {
                font-family: 'Rajdhani', sans-serif;
            }

            p, label, span, [data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"] {
                color: var(--text-soft);
            }

            [data-testid="stCaptionContainer"] {
                color: var(--gold-soft) !important;
                font-weight: 700;
                text-shadow: 0 0 8px rgba(255, 232, 31, 0.18);
            }

            .starwars-hero {
                border: 1px solid var(--border-glow);
                border-radius: 8px;
                background:
                    linear-gradient(90deg, rgba(255, 232, 31, 0.12), rgba(105, 215, 255, 0.05)),
                    rgba(5, 8, 16, 0.78);
                box-shadow: 0 0 32px rgba(255, 232, 31, 0.12), inset 0 0 28px rgba(105, 215, 255, 0.06);
                padding: 1.25rem 1.35rem;
                margin-bottom: 1.15rem;
                position: relative;
                overflow: hidden;
            }

            .starwars-hero:before {
                content: "";
                position: absolute;
                inset: 0;
                background: repeating-linear-gradient(180deg, rgba(255, 232, 31, 0.06) 0 1px, transparent 1px 7px);
                pointer-events: none;
            }

            .starwars-title {
                color: var(--rebel-gold);
                font-family: 'Orbitron', sans-serif;
                font-size: clamp(2rem, 5vw, 4.4rem);
                font-weight: 900;
                line-height: 0.95;
                margin: 0;
                text-shadow: 0 0 18px rgba(255, 232, 31, 0.38);
            }

            .starwars-kicker {
                color: var(--hyperspace-blue);
                font-family: 'Orbitron', sans-serif;
                font-size: 0.82rem;
                font-weight: 700;
                margin-bottom: 0.45rem;
                text-transform: uppercase;
            }

            .starwars-copy {
                color: var(--text-soft);
                max-width: 850px;
                margin: 0.75rem 0 0;
                font-size: 1.08rem;
            }

            [data-testid="stSidebar"] {
                background: linear-gradient(180deg, var(--panel-strong), rgba(13, 20, 36, 0.96));
                border-right: 1px solid rgba(255, 232, 31, 0.22);
            }

            [data-testid="stSidebar"] h2,
            [data-testid="stSidebar"] h3 {
                color: var(--rebel-gold);
            }

            [data-testid="stMetric"] {
                background: linear-gradient(180deg, rgba(13, 21, 38, 0.95), rgba(6, 10, 20, 0.95));
                border: 1px solid rgba(105, 215, 255, 0.28);
                border-radius: 8px;
                padding: 0.85rem 1rem;
                box-shadow: inset 0 0 18px rgba(105, 215, 255, 0.05);
            }

            [data-testid="stMetricValue"] {
                color: var(--rebel-gold);
                font-family: 'Orbitron', sans-serif;
            }

            div[data-testid="stVerticalBlockBorderWrapper"] {
                border-color: rgba(255, 232, 31, 0.36) !important;
                background: linear-gradient(180deg, rgba(12, 18, 33, 0.92), rgba(5, 8, 16, 0.96));
                box-shadow: 0 0 24px rgba(105, 215, 255, 0.08), inset 0 0 22px rgba(255, 232, 31, 0.04);
            }

            div[data-testid="stVerticalBlockBorderWrapper"] img {
                border-radius: 6px;
                border: 1px solid rgba(105, 215, 255, 0.25);
            }

            .stButton > button,
            .stDownloadButton > button {
                background: linear-gradient(180deg, rgba(255, 232, 31, 0.98), rgba(201, 149, 18, 0.98));
                border: 1px solid rgba(255, 232, 31, 0.85);
                border-radius: 6px;
                color: #08111f;
                font-family: 'Orbitron', sans-serif;
                font-weight: 800;
                text-transform: uppercase;
                box-shadow: 0 0 18px rgba(255, 232, 31, 0.18);
            }

            .stButton > button:hover,
            .stDownloadButton > button:hover {
                border-color: var(--hyperspace-blue);
                box-shadow: 0 0 22px rgba(105, 215, 255, 0.28);
                color: #02060d;
            }

            input, textarea, [data-baseweb="select"] > div {
                background-color: rgba(5, 8, 16, 0.9) !important;
                border-color: rgba(105, 215, 255, 0.35) !important;
                color: var(--text-soft) !important;
            }

            input::placeholder {
                color: var(--text-muted) !important;
                opacity: 1;
            }

            hr {
                border-color: rgba(255, 232, 31, 0.18);
            }

            .starwars-vs {
                color: var(--saber-red);
                font-family: 'Orbitron', sans-serif;
                font-size: clamp(1.8rem, 3vw, 3rem);
                font-weight: 900;
                text-align: center;
                text-shadow: 0 0 18px rgba(255, 75, 75, 0.55);
                margin: 1.2rem 0;
            }

            .starwars-section {
                color: var(--rebel-gold);
                font-family: 'Orbitron', sans-serif;
                text-transform: uppercase;
                text-shadow: 0 0 12px rgba(255, 232, 31, 0.24);
            }

            .appearance-panel {
                border: 1px solid rgba(255, 232, 31, 0.28);
                border-radius: 8px;
                background: rgba(255, 232, 31, 0.06);
                padding: 0.75rem 0.85rem;
                margin: 0.85rem 0;
            }

            .appearance-title {
                color: var(--rebel-gold);
                font-family: 'Orbitron', sans-serif;
                font-size: 0.78rem;
                font-weight: 800;
                margin-bottom: 0.4rem;
                text-transform: uppercase;
            }

            .appearance-row {
                color: var(--text-soft);
                line-height: 1.35;
                margin-top: 0.25rem;
            }

            .appearance-label {
                color: var(--hyperspace-blue);
                font-weight: 800;
            }

            [data-testid="stDataFrame"] {
                border: 1px solid rgba(105, 215, 255, 0.28);
                border-radius: 8px;
                overflow: hidden;
            }

            .stAlert {
                border-radius: 8px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    init_backend()

    if "username" not in st.session_state:
        st.session_state.username = None

    if not st.session_state.username:
        st.markdown(
            """
            <div class="starwars-hero">
                <div class="starwars-kicker">Secure databank access</div>
                <h1 class="starwars-title">GLUP SHITTO RANKER</h1>
                <p class="starwars-copy">Log in to save your personal ranking progress across every mode.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        login_tab, register_tab = st.tabs(["Log in", "Create account"])

        with login_tab:
            with st.form("login_form"):
                username = st.text_input("Username", key="login_username")
                password = st.text_input("Password", type="password", key="login_password")
                submitted = st.form_submit_button("Log in", use_container_width=True)

            if submitted:
                if authenticate_user(username, password):
                    st.session_state.username = username.strip()
                    st.rerun()
                else:
                    st.error("Username or password was not recognized.")

        with register_tab:
            with st.form("register_form"):
                new_username = st.text_input("Username", key="register_username")
                new_password = st.text_input("Password", type="password", key="register_password")
                confirm_password = st.text_input("Confirm password", type="password", key="confirm_password")
                submitted = st.form_submit_button("Create account", use_container_width=True)

            if submitted:
                if not new_username.strip() or not new_password:
                    st.error("Enter a username and password.")
                elif new_password != confirm_password:
                    st.error("Passwords do not match.")
                elif create_user(new_username, new_password):
                    st.session_state.username = new_username.strip()
                    st.rerun()
                else:
                    st.error("That username is already taken.")

        return

    @st.cache_data(show_spinner=False)
    def cached_fetch(endpoint: str, max_pages: Optional[int]) -> List[dict]:
        return fetch_databank_category(endpoint, max_pages=max_pages)

    if "rankings" not in st.session_state:
        st.session_state.rankings = {}
    if "current_pair" not in st.session_state:
        st.session_state.current_pair = {}
    if "last_pick" not in st.session_state:
        st.session_state.last_pick = None
    if "active_category" not in st.session_state:
        st.session_state.active_category = "characters"

    st.title("⭐ Glup Shitto Ranker")
    st.markdown(
        """
        <div class="starwars-hero">
            <div class="starwars-kicker">Databank ranking console</div>
            <h1 class="starwars-title">GLUP SHITTO RANKER</h1>
            <p class="starwars-copy">
                Pick your favorite in each matchup. This version uses the Star Wars Databank API,
                so items include images and longer descriptions.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("Controls")
        st.caption(f"Logged in as {st.session_state.username}")
        if st.button("Log out", use_container_width=True):
            st.session_state.username = None
            st.session_state.rankings = {}
            st.session_state.current_pair = {}
            st.session_state.last_pick = None
            st.rerun()

        st.divider()
        category = st.radio(
            "Choose a category",
            options=list(CATEGORIES.keys()),
            format_func=lambda key: f"{CATEGORIES[key]['emoji']} {CATEGORIES[key]['label']}",
            key="active_category",
        )

        st.caption("Databank API categories do not currently include separate films or starships endpoints.")

        st.divider()
        if st.button("Reset this category", use_container_width=True):
            for key in list(st.session_state.rankings.keys()):
                if key.startswith(f"{category}:"):
                    st.session_state.rankings.pop(key, None)
            for key in list(st.session_state.current_pair.keys()):
                if key.startswith(f"{category}:"):
                    st.session_state.current_pair.pop(key, None)
            delete_user_progress_for_category(st.session_state.username, category)
            st.session_state.last_pick = None
            st.rerun()

        if st.button("Clear API cache", use_container_width=True):
            cached_fetch.clear()
            st.success("API cache cleared.")

    config = CATEGORIES[category]
    ranking_key = category
    max_pages = None

    try:
        if ranking_key not in st.session_state.rankings:
            with st.spinner(f"Loading {config['label']} from Databank..."):
                if category == "all":
                    category_items = {
                        key: cached_fetch(CATEGORIES[key]["endpoint"], max_pages)
                        for key in DATABANK_CATEGORY_KEYS
                    }
                    items_to_rank = combine_category_items(category_items)
                else:
                    raw_items = cached_fetch(config["endpoint"], max_pages)
                    items_to_rank = normalize_items(category, raw_items)

                saved_progress = load_user_progress_with_legacy_keys(st.session_state.username, ranking_key)
                apply_saved_progress(items_to_rank, saved_progress)
                if saved_progress:
                    current_pair = saved_progress.get("current_pair")
                    if current_pair:
                        st.session_state.current_pair[ranking_key] = current_pair
                    st.session_state.last_pick = saved_progress.get("last_pick")

                st.session_state.rankings[ranking_key] = items_to_rank

        items = st.session_state.rankings[ranking_key]
    except Exception as exc:
        st.error(f"Could not load {config['label']}: {exc}")
        return

    if len(items) < 2:
        st.warning("This category does not have enough items to rank.")
        return

    def get_pair() -> Tuple[dict, dict]:
        current = st.session_state.current_pair.get(ranking_key)
        by_id = {item["id"]: item for item in items}

        if current and len(current) == 2:
            id_a, id_b = current
            if id_a in by_id and id_b in by_id:
                return by_id[id_a], by_id[id_b]

        new_pair = pick_pair(items)
        st.session_state.current_pair[ranking_key] = (new_pair[0]["id"], new_pair[1]["id"])
        save_user_progress(
            st.session_state.username,
            ranking_key,
            items,
            st.session_state.current_pair[ranking_key],
            st.session_state.last_pick,
        )
        return new_pair

    def load_ranking_items_for_category(sync_category: str) -> List[dict]:
        if sync_category not in st.session_state.rankings:
            raw_items = cached_fetch(CATEGORIES[sync_category]["endpoint"], max_pages)
            category_items = normalize_items(sync_category, raw_items)
            saved_progress = load_user_progress_with_legacy_keys(st.session_state.username, sync_category)
            apply_saved_progress(category_items, saved_progress)
            if saved_progress:
                current_pair = saved_progress.get("current_pair")
                if current_pair:
                    st.session_state.current_pair[sync_category] = current_pair
            st.session_state.rankings[sync_category] = category_items

        return st.session_state.rankings[sync_category]

    def sync_all_vote_to_individual_category(winner_item: dict, loser_item: dict) -> None:
        sync_category = winner_item.get("category")
        if category != "all" or sync_category != loser_item.get("category") or sync_category not in DATABANK_CATEGORY_KEYS:
            return

        category_items = load_ranking_items_for_category(sync_category)
        if apply_same_category_vote(category_items, winner_item, loser_item):
            save_user_progress(
                st.session_state.username,
                sync_category,
                category_items,
                st.session_state.current_pair.get(sync_category),
                winner_item["name"],
            )

    def vote(winner_id: str, loser_id: str) -> None:
        winner_item = next(item for item in items if item["id"] == winner_id)
        loser_item = next(item for item in items if item["id"] == loser_id)
        winner_name = apply_vote(items, winner_id, loser_id)
        sync_all_vote_to_individual_category(winner_item, loser_item)
        st.session_state.last_pick = winner_name
        next_pair = pick_pair(items)
        st.session_state.current_pair[ranking_key] = (next_pair[0]["id"], next_pair[1]["id"])
        save_user_progress(
            st.session_state.username,
            ranking_key,
            items,
            st.session_state.current_pair[ranking_key],
            st.session_state.last_pick,
        )

    def show_item_card(item: dict, label: str, opponent: dict, top_pick_key: str) -> None:
        name_col, pick_col = st.columns([0.68, 0.32], vertical_alignment="center")
        with name_col:
            st.markdown(f"### {label}: {item['name']}")
        with pick_col:
            if st.button("Pick", key=top_pick_key, use_container_width=True):
                vote(item["id"], opponent["id"])
                st.rerun()

        item_category = CATEGORIES.get(item.get("category", ""), {}).get("label", item.get("category", "Unknown"))
        st.caption(item_category)

        image_url = item.get("image")
        if image_url:
            st.image(image_url, use_container_width=True)
        else:
            st.info("No image available")

        st.metric("Rating", display_rating(item))
        st.caption(f"Record: {item['wins']}W / {item['losses']}L · {item['battles']} battles")

        description = item.get("description") or "No description available."
        st.write(description)

        appearances = item.get("appearances", {})
        if appearances:
            appearance_rows = "".join(
                (
                    '<div class="appearance-row">'
                    f'<span class="appearance-label">{html.escape(label)}:</span> '
                    f'{html.escape(", ".join(values))}'
                    "</div>"
                )
                for label, values in appearances.items()
            )
            st.markdown(
                f"""
                <div class="appearance-panel">
                    <div class="appearance-title">Appearances</div>
                    {appearance_rows}
                </div>
                """,
                unsafe_allow_html=True,
            )

        extra_fields = item.get("extra_fields", {})
        if extra_fields:
            with st.expander("More details"):
                for key, value in extra_fields.items():
                    st.write(f"**{pretty_key(key)}:** {normalize_text(value)}")

    item_a, item_b = get_pair()
    total_battles = sum(item["battles"] for item in items) // 2

    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("Category", config["label"])
    metric_2.metric("Items Loaded", len(items))
    metric_3.metric("Total Matchups", total_battles)

    if st.session_state.last_pick:
        st.success(f"Last pick: {st.session_state.last_pick}")

    st.markdown('<h2 class="starwars-section">Choose your winner</h2>', unsafe_allow_html=True)
    left, middle, right = st.columns([1, 0.16, 1])

    with left:
        with st.container(border=True):
            show_item_card(item_a, "A", item_b, "pick_a_top")
            if st.button(f"Pick {item_a['name']}", key="pick_a", use_container_width=True):
                vote(item_a["id"], item_b["id"])
                st.rerun()

    with middle:
        st.markdown('<div class="starwars-vs">VS</div>', unsafe_allow_html=True)
        if st.button("Skip", use_container_width=True):
            next_pair = pick_pair(items)
            st.session_state.current_pair[ranking_key] = (next_pair[0]["id"], next_pair[1]["id"])
            st.session_state.last_pick = None
            save_user_progress(
                st.session_state.username,
                ranking_key,
                items,
                st.session_state.current_pair[ranking_key],
                st.session_state.last_pick,
            )
            st.rerun()

    with right:
        with st.container(border=True):
            show_item_card(item_b, "B", item_a, "pick_b_top")
            if st.button(f"Pick {item_b['name']}", key="pick_b", use_container_width=True):
                vote(item_b["id"], item_a["id"])
                st.rerun()

    st.divider()
    st.markdown(f'<h2 class="starwars-section">{config["emoji"]} Rankings</h2>', unsafe_allow_html=True)

    ranked = rank_items(items)
    rows = [
        {
            "Rank": index,
            "Name": item["name"],
            "Category": CATEGORIES.get(item.get("category", ""), {}).get("label", item.get("category", "")),
            "Rating": display_rating(item),
            "Wins": item["wins"],
            "Losses": item["losses"],
            "Battles": item["battles"],
            "Has Image": bool(item.get("image")),
            "Appearances": summarize_appearances(item.get("appearances", {})),
            "Description": item.get("description", ""),
        }
        for index, item in enumerate(ranked, start=1)
    ]
    rankings_df = pd.DataFrame(rows)

    search = st.text_input("Search rankings", placeholder="Anakin, Ahch-To, X-wing, Jedi...")
    if search.strip():
        mask = rankings_df["Name"].str.contains(search.strip(), case=False, na=False)
        rankings_df = rankings_df[mask]

    st.dataframe(rankings_df, use_container_width=True, hide_index=True)

    csv = rankings_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download visible rankings as CSV",
        data=csv,
        file_name=f"databank_{category}_rankings.csv",
        mime="text/csv",
        use_container_width=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Star Wars Databank Beli-style Streamlit ranker")
    parser.add_argument("--test", action="store_true", help="Run built-in logic tests and exit")
    args = parser.parse_args()

    if args.test:
        run_tests()
    else:
        run_streamlit_app()


if __name__ == "__main__":
    main()
