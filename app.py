import json
import os
import re
import time
import threading
import uuid
import secrets
import hashlib
import hmac
import html
from urllib.parse import quote_plus
from datetime import datetime, timezone

import requests
import yaml
from flask import (
    Flask, request, jsonify, Response, stream_with_context,
    render_template, send_from_directory, g,
)
from cryptography.fernet import Fernet

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


# ==================================================================
# CONFIG
# ==================================================================

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_DIR        = os.path.join(BASE_DIR, "db")
DB_PATH       = os.path.join(DB_DIR, "users.json")
KEY_PATH      = os.path.join(DB_DIR, "secret.key")
LOCK_PATH     = os.path.join(DB_DIR, "db.lock")
ASSETS_DIR    = os.path.join(BASE_DIR, "assets")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
AI_DIR        = os.path.join(BASE_DIR, "ai")
PROMPT_PATH   = os.path.join(AI_DIR, "prompt.md")
STILLS_DIR    = os.path.join(AI_DIR, "stills")

ANON_COOKIE    = "cobalt1_anon"
ANON_MAX_AGE   = 60 * 60 * 24 * 365 * 5
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = os.environ.get("MODEL_NAME", "llama3.2:1b")

CALLS_PER_MINUTE   = 10
DAILY_TOKEN_LIMIT  = 1_000_000
WEEKLY_TOKEN_LIMIT = 5_000_000
THRESHOLDS = (50, 75, 90, 99)

PBKDF2_ITERATIONS = 200_000
_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")

app = Flask(__name__, template_folder=TEMPLATES_DIR)


# ==================================================================
# PROMPT + SKILLS (loaded at startup)
# ==================================================================

def _load_prompt() -> str:
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except Exception as e:
        print(f"[warn] failed to load {PROMPT_PATH}: {e}")
    return "You are Cobalt1, a helpful AI assistant."


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError as e:
        print(f"[warn] bad YAML frontmatter: {e}")
        meta = {}
    return meta, parts[2].strip()


def _load_skills() -> dict:
    skills = {}
    if not os.path.isdir(STILLS_DIR):
        return skills
    for fname in sorted(os.listdir(STILLS_DIR)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(STILLS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta, body = _parse_frontmatter(f.read())
            name = meta.get("name") or os.path.splitext(fname)[0]
            if not body:
                print(f"[warn] skill {fname} has empty body — skipped")
                continue
            skills[name] = {
                "name": name,
                "strong": list(meta.get("strong") or []),
                "weak":   list(meta.get("weak")   or []),
                "prompt": body,
            }
        except Exception as e:
            print(f"[warn] failed to load skill {fname}: {e}")
    return skills


BASE_SYSTEM_PROMPT = _load_prompt()
SKILLS = _load_skills()


def _compile_skill_pattern(words: list[str]):
    if not words:
        return None
    escaped = [re.escape(w).replace(" ", r"\s+") for w in words]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


_SKILL_COMPILED = {
    name: {
        "strong": _compile_skill_pattern(s["strong"]),
        "weak":   _compile_skill_pattern(s["weak"]),
    }
    for name, s in SKILLS.items()
}


def _detect_skills(text: str) -> list[str]:
    if not text:
        return []
    scored = []
    for name, pat in _SKILL_COMPILED.items():
        strong = len(pat["strong"].findall(text)) if pat["strong"] else 0
        weak   = len(pat["weak"].findall(text))   if pat["weak"]   else 0
        if strong >= 1 or weak >= 2:
            scored.append((name, strong * 2 + weak))
    scored.sort(key=lambda x: -x[1])
    return [n for n, _ in scored[:2]]


def _detect_skills_with_context(text: str, history: list[dict]) -> list[str]:
    skills = _detect_skills(text)
    if skills:
        return skills
    prior = [m["content"] for m in history[:-1] if m["role"] == "user"]
    for msg in reversed(prior[-2:]):
        skills = _detect_skills(msg)
        if skills:
            return skills
    return []


def _build_skill_prompt(names: list[str]) -> str:
    if not names:
        return ""
    out = ["\n\n## Active Skills\nApply the following domain guidance to this request:\n"]
    for n in names:
        if n in SKILLS:
            out.append(f"\n### {n.title()}\n{SKILLS[n]['prompt']}\n")
    return "".join(out)


# ==================================================================
# EFFORT
# ==================================================================

EFFORT_PROMPTS = {
    "min":  "\n\n## Effort: MIN\nRespond with maximum brevity. 1-2 sentences. No elaboration, no examples, no filler.",
    "low":  "\n\n## Effort: LOW\nRespond concisely. Essential information only. Skip optional detail.",
    "medium": "",
    "high": "\n\n## Effort: HIGH\nRespond thoroughly with examples and detail where useful.",
    "max":  "\n\n## Effort: MAX\nRespond exhaustively. Cover edge cases, provide multiple examples, explain reasoning.",
    "ultracore": "\n\n## Effort: ULTRACORE\nRespond with maximum thoroughness. Multiple examples, edge cases, comprehensive analysis. Long, detailed responses are expected and welcome.",
}
EFFORT_TOKENS = {
    "min": 128, "low": 256, "medium": 512,
    "high": 1024, "max": 2048, "ultracore": 4096,
}


# ==================================================================
# CROSS-PROCESS DB LOCK
# ==================================================================

class DatabaseLock:
    def __init__(self, path: str):
        self.path = path
        self._file = None
        self._thread_lock = threading.Lock()

    def __enter__(self):
        self._thread_lock.acquire()
        if HAS_FCNTL:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self._file = open(self.path, "a")
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
            except Exception:
                if self._file:
                    try: self._file.close()
                    except Exception: pass
                self._file = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file is not None:
            try: fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception: pass
            try: self._file.close()
            except Exception: pass
            self._file = None
        self._thread_lock.release()


DB_LOCK = DatabaseLock(LOCK_PATH)


# ==================================================================
# FERNET / DB
# ==================================================================

_FERNET = None

def _fernet():
    global _FERNET
    if _FERNET is None:
        os.makedirs(DB_DIR, exist_ok=True)
        if os.path.exists(KEY_PATH):
            with open(KEY_PATH, "rb") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            with open(KEY_PATH, "wb") as f:
                f.write(key)
            try: os.chmod(KEY_PATH, 0o600)
            except OSError: pass
        _FERNET = Fernet(key)
    return _FERNET


def _empty_db() -> dict:
    return {"users": {}, "sessions": {}}


def _load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return _empty_db()
    try:
        with open(DB_PATH, "rb") as f:
            data = f.read()
        if not data:
            return _empty_db()
        try:
            db = json.loads(_fernet().decrypt(data))
        except Exception:
            db = json.loads(data.decode("utf-8"))
        db.setdefault("users", {})
        db.setdefault("sessions", {})
        return db
    except Exception:
        return _empty_db()


def _save_db(db: dict) -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    encrypted = _fernet().encrypt(json.dumps(db, ensure_ascii=False).encode("utf-8"))
    tmp = DB_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(encrypted)
    os.replace(tmp, DB_PATH)


# ==================================================================
# PASSWORDS / USERNAMES
# ==================================================================

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk, bytes.fromhex(hash_hex))
    except Exception:
        return False


def _valid_username(u: str) -> bool:
    return bool(_USERNAME_RE.match(u))


# ==================================================================
# MODELS
# ==================================================================

def _new_user(username: str) -> dict:
    return {
        "username": username,
        "password_hash": "",
        "accepted_terms_at": 0,
        "created_at": time.time(),
        "skills": "",
        "usage": {
            "day": "", "day_tokens": 0,
            "week": "", "week_tokens": 0,
            "calls": [],
            "thresholds_day": ["", []],
            "thresholds_week": ["", []],
            "pending_thresholds": [],
        },
        "conversations": [],
        "active_id": None,
    }


def _new_conversation() -> dict:
    return {"id": "c_" + uuid.uuid4().hex[:16], "title": "New Chat",
            "messages": [], "created_at": time.time()}


# ==================================================================
# USAGE / LIMITS
# ==================================================================

def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _refresh_periods(usage: dict) -> None:
    now = datetime.now(timezone.utc)
    day  = now.strftime("%Y-%m-%d")
    week = _week_key(now)
    if usage["day"] != day:
        usage["day"] = day
        usage["day_tokens"] = 0
    if usage["week"] != week:
        usage["week"] = week
        usage["week_tokens"] = 0
    if usage["thresholds_day"][0] != day:
        usage["thresholds_day"] = [day, []]
    if usage["thresholds_week"][0] != week:
        usage["thresholds_week"] = [week, []]


def _check_rate_limit(user: dict, now: float) -> tuple[bool, str | None]:
    _refresh_periods(user["usage"])
    u = user["usage"]
    u["calls"] = [t for t in u["calls"] if now - t < 60]
    if len(u["calls"]) >= CALLS_PER_MINUTE:
        return False, f"Rate limit reached: max {CALLS_PER_MINUTE} messages per minute. Please wait a few seconds and try again."
    if u["day_tokens"] >= DAILY_TOKEN_LIMIT:
        return False, f"Daily token limit reached ({DAILY_TOKEN_LIMIT:,} tokens). Resets at midnight UTC."
    if u["week_tokens"] >= WEEKLY_TOKEN_LIMIT:
        return False, f"Weekly token limit reached ({WEEKLY_TOKEN_LIMIT:,} tokens). Resets Monday UTC."
    return True, None


def _record_call(user: dict, now: float) -> None:
    user["usage"]["calls"].append(now)


def _check_thresholds(usage: dict, kind: str, used: int, limit: int, key: str) -> None:
    pct = (used / limit) * 100
    seen = usage[key][1]
    for t in THRESHOLDS:
        if pct >= t and t not in seen:
            seen.append(t)
            usage["pending_thresholds"].append({"type": kind, "pct": t})


def _record_tokens(user: dict, tokens: int) -> None:
    if tokens <= 0:
        return
    u = user["usage"]
    u["day_tokens"]  += tokens
    u["week_tokens"] += tokens
    _check_thresholds(u, "day",  u["day_tokens"],  DAILY_TOKEN_LIMIT,  "thresholds_day")
    _check_thresholds(u, "week", u["week_tokens"], WEEKLY_TOKEN_LIMIT, "thresholds_week")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _usage_snapshot(user: dict, drain: bool = False) -> dict:
    u = user["usage"]
    now = time.time()
    snap = {
        "day_tokens":  u["day_tokens"],  "day_limit":  DAILY_TOKEN_LIMIT,
        "week_tokens": u["week_tokens"], "week_limit": WEEKLY_TOKEN_LIMIT,
        "calls_last_minute": len([t for t in u["calls"] if now - t < 60]),
        "minute_limit": CALLS_PER_MINUTE,
        "pending_thresholds": list(u.get("pending_thresholds", [])),
    }
    if drain:
        u["pending_thresholds"] = []
    return snap


# ==================================================================
# SERIALIZATION
# ==================================================================

def _public_conversation(conv: dict) -> dict:
    return {
        "id": conv["id"],
        "title": conv.get("title", "New Chat"),
        "messages": [{"role": m["role"], "content": m["content"]}
                     for m in conv.get("messages", [])],
    }


def _public_state(user: dict, is_auth: bool) -> dict:
    return {
        "conversations": [_public_conversation(c) for c in user["conversations"]],
        "active_id": user["active_id"],
        "usage": _usage_snapshot(user, drain=False),
        "authenticated": is_auth,
        "username": user["username"] if is_auth else None,
        "skills": user.get("skills", ""),
    }


# ==================================================================
# AUTH: session tokens + anon cookies
# ==================================================================

@app.after_request
def _apply_cookie_changes(response):
    if getattr(g, "anon_token_to_clear", False):
        response.delete_cookie(ANON_COOKIE)
    elif hasattr(g, "anon_token_to_set"):
        response.set_cookie(
            ANON_COOKIE, g.anon_token_to_set,
            max_age=ANON_MAX_AGE, httponly=True,
            samesite="Lax", secure=SECURE_COOKIES,
        )
    return response


def _resolve_user(db: dict) -> tuple[dict, bool]:
    """Session token, else anonymous-cookie identity."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else None
    if token:
        sess = db.get("sessions", {}).get(token)
        if sess:
            user = db.get("users", {}).get(sess["username"])
            if user:
                return user, True

    anon_token = getattr(g, "anon_token_to_set", None)
    if not anon_token:
        anon_token = request.cookies.get(ANON_COOKIE)
        if not anon_token:
            anon_token = secrets.token_hex(16)
            g.anon_token_to_set = anon_token

    anon_id = "anon_" + anon_token
    if anon_id not in db["users"]:
        user = _new_user(anon_id)
        conv = _new_conversation()
        user["conversations"].append(conv)
        user["active_id"] = conv["id"]
        db["users"][anon_id] = user
        _save_db(db)
    return db["users"][anon_id], False


def _issue_token(db: dict, username: str) -> str:
    token = secrets.token_hex(32)
    db.setdefault("sessions", {})[token] = {"username": username, "created_at": time.time()}
    return token


def _merge_anon_to_user(db: dict, username: str) -> None:
    anon_token = getattr(g, "anon_token_to_set", None) or request.cookies.get(ANON_COOKIE)
    if not anon_token:
        return
    anon_id = "anon_" + anon_token
    if anon_id == username:
        return
    anon = db["users"].pop(anon_id, None)
    if not anon:
        return
    user = db["users"][username]
    merged = anon.get("conversations", []) + user.get("conversations", [])
    user["conversations"] = merged
    if not any(c["id"] == user.get("active_id") for c in merged):
        user["active_id"] = anon.get("active_id") or (merged[0]["id"] if merged else None)
    au, uu = anon.get("usage", {}), user.setdefault("usage", {})
    for key in ("day_tokens", "week_tokens"):
        uu[key] = uu.get(key, 0) + au.get(key, 0)
    uu["calls"] = uu.get("calls", []) + au.get("calls", [])
    g.anon_token_to_clear = True
    if hasattr(g, "anon_token_to_set"):
        del g.anon_token_to_set


# ==================================================================
# WEB SEARCH
# ==================================================================

def _perform_search(query: str) -> str:
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36")
        }
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', r.text, re.DOTALL)
        if not snippets:
            snippets = re.findall(r'<div class="result__snippet[^>]*>(.*?)</div>', r.text, re.DOTALL)
        clean = [html.unescape(re.sub(r"<[^>]+>", "", s)).strip() for s in snippets[:4]]
        clean = [s for s in clean if s]
        if clean:
            return "\n".join(f"- {s}" for s in clean)
        return "No relevant search results found."
    except Exception as e:
        return f"Search failed: {str(e)}"


# ==================================================================
# OLLAMA STREAMING (with ReAct search loop)
# ==================================================================

def _stream_ollama(payload_messages, effort, on_complete=None, max_turns=3):
    total_tokens = 0
    full_output_text = ""
    error = None
    num_predict = EFFORT_TOKENS.get(effort, EFFORT_TOKENS["medium"])
    current_messages = list(payload_messages)

    for turn in range(max_turns):
        turn_text = ""
        try:
            with requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": MODEL_NAME,
                    "messages": current_messages,
                    "stream": True,
                    "options": {"num_predict": num_predict},
                },
                stream=True, timeout=300,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        turn_text += token
                        yield "data: " + json.dumps({"token": token}) + "\n\n"
                    if chunk.get("done"):
                        total_tokens += int(chunk.get("prompt_eval_count", 0)) + int(chunk.get("eval_count", 0))
                        break
        except requests.exceptions.ConnectionError:
            error = "Cobalt1 is offline. Start Ollama with 'ollama serve'."
            yield "data: " + json.dumps({"error": error}) + "\n\n"
            break
        except Exception as e:
            error = str(e)
            yield "data: " + json.dumps({"error": error}) + "\n\n"
            break

        full_output_text += turn_text

        search_match = re.search(r"<search>(.*?)</search>", turn_text, re.DOTALL | re.IGNORECASE)
        if search_match and turn < max_turns - 1:
            query = search_match.group(1).strip()
            yield "data: " + json.dumps({"token": f"\n\n**Searching the web for:** `{query}`\n\n"}) + "\n\n"
            results = _perform_search(query)
            current_messages.append({"role": "assistant", "content": turn_text})
            current_messages.append({
                "role": "user",
                "content": f"Search Results for '{query}':\n{results}\n\nPlease continue answering based on these results.",
            })
            yield "data: " + json.dumps({"token": f"**Search Results:**\n{results}\n\n"}) + "\n\n"
            full_output_text += f"\n\n**Search Results:**\n{results}\n\n"
        else:
            break

    yield "data: [DONE]\n\n"
    if on_complete:
        on_complete(total_tokens, full_output_text, error)


def _generate_title(first_user_text: str) -> str | None:
    if not first_user_text:
        return None
    prompt = (
        "You are a conversation title generator. Generate a short, descriptive title "
        "(2 to 5 words) for a conversation that begins with the user message below. "
        "Reply with ONLY the title. No quotes, no trailing punctuation, no explanation.\n\n"
        f"User message: {first_user_text[:600]}"
    )
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.4, "num_predict": 24}},
            timeout=45,
        )
        r.raise_for_status()
        title = (r.json().get("response") or "").strip().split("\n")[0].strip().strip("\"'`").strip()
        if not title or len(title) < 2:
            return None
        if len(title) > 40:
            title = title[:40].rstrip() + "\u2026"
        return title
    except Exception:
        return None


# ==================================================================
# ROUTES: pages
# ==================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth")
def auth_page():
    return render_template("auth.html")


@app.route("/assets/<path:filename>")
def assets(filename):
    try:
        return send_from_directory(ASSETS_DIR, filename)
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": time.time(), "skills": list(SKILLS.keys())})


# ==================================================================
# ROUTES: auth
# ==================================================================

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not data.get("accept_terms"):
        return jsonify({"error": "You must accept the Terms and Conditions."}), 400
    if not _valid_username(username):
        return jsonify({"error": "Username must be 3-32 chars: lowercase letters, digits, underscore."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    with DB_LOCK:
        db = _load_db()
        if username in db["users"]:
            return jsonify({"error": "Username already taken."}), 409

        user = _new_user(username)
        user["password_hash"] = _hash_password(password)
        user["accepted_terms_at"] = time.time()
        conv = _new_conversation()
        user["conversations"].append(conv)
        user["active_id"] = conv["id"]
        db["users"][username] = user

        token = _issue_token(db, username)
        _merge_anon_to_user(db, username)
        _save_db(db)

        return jsonify({
            "token": token, "username": username,
            "state": _public_state(db["users"][username], True),
        })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    with DB_LOCK:
        db = _load_db()
        user = db.get("users", {}).get(username)
        if not user or not _verify_password(password, user.get("password_hash", "")):
            return jsonify({"error": "Invalid username or password."}), 401

        if not user["conversations"]:
            conv = _new_conversation()
            user["conversations"].append(conv)
            user["active_id"] = conv["id"]

        token = _issue_token(db, username)
        _merge_anon_to_user(db, username)
        _save_db(db)

        return jsonify({
            "token": token, "username": username,
            "state": _public_state(db["users"][username], True),
        })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else None
    if token:
        with DB_LOCK:
            db = _load_db()
            if token in db.get("sessions", {}):
                del db["sessions"][token]
                _save_db(db)
    return jsonify({"ok": True})


# ==================================================================
# ROUTES: state
# ==================================================================

@app.route("/api/state")
def get_state():
    with DB_LOCK:
        db = _load_db()
        user, is_auth = _resolve_user(db)
        _refresh_periods(user["usage"])
        if not user["conversations"]:
            conv = _new_conversation()
            user["conversations"].append(conv)
            user["active_id"] = conv["id"]
        elif user["active_id"] is None or not any(
            c["id"] == user["active_id"] for c in user["conversations"]
        ):
            user["active_id"] = user["conversations"][0]["id"]
        _save_db(db)
        return jsonify(_public_state(user, is_auth))


@app.route("/api/skills", methods=["POST"])
def save_skills():
    data = request.get_json(silent=True) or {}
    skills = data.get("skills", "")
    with DB_LOCK:
        db = _load_db()
        user, _ = _resolve_user(db)
        user["skills"] = skills
        _save_db(db)
    return jsonify({"ok": True})


# ==================================================================
# ROUTES: conversations
# ==================================================================

@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    with DB_LOCK:
        db = _load_db()
        user, is_auth = _resolve_user(db)
        conv = _new_conversation()
        user["conversations"].insert(0, conv)
        user["active_id"] = conv["id"]
        _save_db(db)
        return jsonify(_public_state(user, is_auth))


@app.route("/api/conversations", methods=["DELETE"])
def delete_all_conversations():
    with DB_LOCK:
        db = _load_db()
        user, is_auth = _resolve_user(db)
        conv = _new_conversation()
        user["conversations"] = [conv]
        user["active_id"] = conv["id"]
        _save_db(db)
        return jsonify(_public_state(user, is_auth))


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    with DB_LOCK:
        db = _load_db()
        user, is_auth = _resolve_user(db)
        before = len(user["conversations"])
        user["conversations"] = [c for c in user["conversations"] if c["id"] != conv_id]
        if len(user["conversations"]) == before:
            _save_db(db)
            return jsonify({"error": "Conversation not found"}), 404
        if user["active_id"] == conv_id:
            if user["conversations"]:
                user["active_id"] = user["conversations"][0]["id"]
            else:
                conv = _new_conversation()
                user["conversations"].append(conv)
                user["active_id"] = conv["id"]
        _save_db(db)
        return jsonify(_public_state(user, is_auth))


@app.route("/api/conversations/<conv_id>/active", methods=["POST"])
def activate_conversation(conv_id):
    with DB_LOCK:
        db = _load_db()
        user, _ = _resolve_user(db)
        conv = next((c for c in user["conversations"] if c["id"] == conv_id), None)
        if not conv:
            _save_db(db)
            return jsonify({"error": "Conversation not found"}), 404
        user["active_id"] = conv_id
        _save_db(db)
        return jsonify({"active_id": conv_id})


# ==================================================================
# ROUTES: usage
# ==================================================================

@app.route("/usage")
def usage():
    with DB_LOCK:
        db = _load_db()
        user, _ = _resolve_user(db)
        _refresh_periods(user["usage"])
        snap = _usage_snapshot(user, drain=True)
        _save_db(db)
    return jsonify(snap)


# ==================================================================
# ROUTES: chat
# ==================================================================

@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    data = request.get_json(silent=True) or {}
    effort = data.get("effort", "medium")
    if effort not in EFFORT_TOKENS:
        effort = "medium"

    with DB_LOCK:
        db = _load_db()
        user, _ = _resolve_user(db)
        user_skills = user.get("skills", "")

        conv_id = data.get("conversation_id")
        content = (data.get("content") or "").strip()
        if not conv_id or not content:
            _save_db(db)
            return jsonify({"error": "conversation_id and content are required"}), 400

        now = time.time()
        conv = next((c for c in user["conversations"] if c["id"] == conv_id), None)
        if not conv:
            _save_db(db)
            return jsonify({"error": "Conversation not found"}), 404

        allowed, err = _check_rate_limit(user, now)
        if not allowed:
            _save_db(db)
            return jsonify({"error": err}), 429

        _record_call(user, now)

        if data.get("retry"):
            cut_index = None
            for i in range(len(conv["messages"]) - 1, -1, -1):
                m = conv["messages"][i]
                if m["role"] == "user" and m["content"] == content:
                    cut_index = i
                    break
            if cut_index is not None:
                conv["messages"] = conv["messages"][:cut_index + 1]
            else:
                if conv["messages"] and conv["messages"][-1]["role"] == "assistant":
                    conv["messages"].pop()
                if conv["messages"] and conv["messages"][-1]["role"] == "user":
                    conv["messages"].pop()
                conv["messages"].append({"role": "user", "content": content, "ts": now})
        else:
            conv["messages"].append({"role": "user", "content": content, "ts": now})

        _save_db(db)
        history = [{"role": m["role"], "content": m["content"]} for m in conv["messages"]]
        original_username = user["username"]

    skill_names = _detect_skills_with_context(content, history)
    skill_prompt = _build_skill_prompt(skill_names)

    system_prompt = (BASE_SYSTEM_PROMPT + skill_prompt
                     + EFFORT_PROMPTS.get(effort, ""))
    if user_skills.strip():
        system_prompt += f"\n\n## Custom Skills/Instructions\n{user_skills.strip()}"

    payload = [{"role": "system", "content": system_prompt}] + history
    input_estimate = _estimate_tokens("".join(m["content"] for m in payload))

    def on_complete(total_tokens, output_text, error):
        tokens = total_tokens if total_tokens > 0 else (input_estimate + _estimate_tokens(output_text))
        with DB_LOCK:
            db2 = _load_db()
            user2 = db2["users"].get(original_username)
            if user2 is None:
                return
            conv2 = next((c for c in user2["conversations"] if c["id"] == conv_id), None)
            if conv2 is not None and not error and output_text.strip():
                conv2["messages"].append({
                    "role": "assistant", "content": output_text, "ts": time.time(),
                })
            _record_tokens(user2, tokens)
            _save_db(db2)

    return Response(
        stream_with_context(_stream_ollama(payload, effort, on_complete)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/chat/title", methods=["POST"])
def chat_title():
    data = request.get_json(silent=True) or {}
    conv_id = data.get("conversation_id")
    if not conv_id:
        return jsonify({"title": "New Chat"})

    with DB_LOCK:
        db = _load_db()
        user, _ = _resolve_user(db)
        conv = next((c for c in user["conversations"] if c["id"] == conv_id), None)
        if not conv:
            _save_db(db)
            return jsonify({"title": "New Chat"})
        first_user = next((m["content"] for m in conv["messages"] if m["role"] == "user"), "")
        original_username = user["username"]
        _save_db(db)

    title = _generate_title(first_user) if first_user else None

    with DB_LOCK:
        db = _load_db()
        user = db["users"].get(original_username)
        if user is not None:
            conv = next((c for c in user["conversations"] if c["id"] == conv_id), None)
            if conv is not None and title:
                conv["title"] = title
            _record_tokens(user, _estimate_tokens(first_user + (title or "")))
            _save_db(db)

    return jsonify({"title": title or "New Chat"})


# ==================================================================
# ENTRY POINT
# ==================================================================

if __name__ == "__main__":
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)

    print(f"[init] DB: {DB_PATH}")
    print(f"[init] Prompt: {PROMPT_PATH}")
    print(f"[init] Skills ({len(SKILLS)}): {', '.join(SKILLS.keys()) or '—'}")

    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=debug)