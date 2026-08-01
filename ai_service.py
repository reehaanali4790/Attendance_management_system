import os
import tempfile
import base64
import datetime
import json
import re
import difflib
import hashlib
import time
from sqlalchemy import text
from database import engine, DATABASE_URL
from dotenv import load_dotenv
import openai

# Load environment variables
load_dotenv()

# OpenAI Models
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o").strip()
OPENAI_FAST_MODEL = os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini").strip()
OPENAI_WHISPER_MODEL = "whisper-1"

_employee_names_cache = {"names": [], "fetched_at": 0.0}
_EMPLOYEE_CACHE_TTL_SECONDS = 60

_tts_audio_cache = {}
_TTS_CACHE_MAX_ENTRIES = 200
_TTS_CACHE_TTL_SECONDS = 86400


def get_chat_model(fast: bool = False) -> str:
    if os.environ.get("OPENAI_FAST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return OPENAI_FAST_MODEL
    return OPENAI_FAST_MODEL if fast else OPENAI_MODEL


PHONETIC_GROUPS = [
    frozenset({"omer", "omar", "umer", "amr", "amer", "umair", "omair"}),
    frozenset({"mohammad", "muhammad", "mohd", "md", "mohammed"}),
    frozenset({"shehryar", "shahryar", "shahriyar", "shehriyar"}),
    frozenset({"rabia", "rabab", "rabbia"}),
]

URDU_TO_LATIN_MAP = {
    "محمد": "Mohammad",
    "عمر": "Omer",
    "امر": "Omer",
    "عمیر": "Omer",
    "شہریار": "Shaharyar",
    "علی": "Ali",
    "احمد": "Ahmed",
    "عثمان": "usama",
    "اسامہ": "usama",
    "فرقان": "Furqan",
    "ادریس": "IDREES",
    "فیض": "Fayez",
    "فیاض": "Fayyaz",
    "مجید": "Majeed",
    "مجیب": "Mujeeb",
    "حمزہ": "Hamza",
    "عامر": "amir",
    "بلال": "BILAL",
    "فہد": "FAHAD",
    "ولید": "Waleed",
    "طاہر": "Tahir",
    "یاسر": "Yasir",
    "نعمان": "Noman",
    "حبیب": "Habib",
    "رضا": "Raza",
    "خالد": "Khalid",
    "فرخ": "Farrukh",
    "ثاقب": "Saqib",
    "عمران": "IMRAN",
    "حسن": "HASSAN",
    "سعد": "Saeed",
    "سعید": "Saeed",
    "عاصم": "Asim",
    "عاصف": "Asif",
    "اقرا": "Iqra",
    "رباب": "Rabia",
    "دعاء": "dua",
    "دعا": "dua",
    "امام": "imamuddin",
    "ابوبکر": "abubakar",
    "منہال": "Minhal",
    "عرفان": "irfan",
    "عمامہ": "umama",
    "شجاعت": "SHUJAAT",
    "اسحاق": "Ishaq",
    "شکیل": "Shakeel",
    "امبرین": "ambreen",
    "عنبرین": "ambreen",
    "سید": "Syed",
}

def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def _tokens_match(word: str, token: str) -> bool:
    """Fuzzy + phonetic matching for employee name tokens."""
    word = word.lower()
    token = token.lower()
    if word == token or word in token or token in word:
        return True
    if difflib.SequenceMatcher(None, word, token).ratio() >= 0.68:
        return True
    for group in PHONETIC_GROUPS:
        if word in group and token in group:
            return True
    return False

NAME_FILLER_WORDS = frozenset({
    "acha", "achha", "okay", "ok", "theek", "haan", "ji", "aur", "or", "and",
    "pichle", "pichlay", "pichla", "thi", "tha", "the", "saal", "mahine", "aaj",
    "kal", "kitni", "kitne", "kaun", "kon", "kya", "ne", "ki", "ke", "ka", "ko",
    "hai", "hain", "ho", "tha", "the", "today", "yesterday", "month", "year",
    "late", "present", "absent", "leave", "leaves", "chutti", "chutiyan", "hazri",
    "who", "which", "how", "many", "employee", "employees", "log", "sabse", "zyada",
    "ilawa", "siwa", "besides", "except", "chor", "chhod", "other", "than",
})

SUPERLATIVE_RANKING_MARKERS = (
    "sabse zyada", "sab se zyada", "sabse ziada", "sab se ziada",
    "most ", "highest", "top ", "kis ne ki", "kisne ki",
    "ranking", "sab se zyada der", "sabse zyada der",
)

LIST_MARKERS = (
    "kon kon", "kaun kaun", "koun koun", "who all", "who are", "who were",
)

COUNT_MARKERS = (
    "kitne log", "kitne", "how many",
)

# Backward-compatible alias used in a few places.
RANKING_MARKERS = SUPERLATIVE_RANKING_MARKERS

EXCLUSION_MARKERS = (
    "ke ilawa", "k ilawa", "ke siwa", "k siwa", "ilawa", "siwa",
    "besides", "except", "other than", "chor kar", "chhod kar", "chhor kar",
)


def _is_superlative_ranking_question(text: str, understanding: dict | None = None) -> bool:
    q = (text or "").lower()
    if understanding and understanding.get("metric") == "ranking":
        return True
    if any(marker in q for marker in SUPERLATIVE_RANKING_MARKERS):
        return True
    if "zyada" in q and any(word in q for word in ("kaun", "kon", "kis", "who")):
        return True
    return False


def _is_list_question(text: str) -> bool:
    q = (text or "").lower()
    if any(marker in q for marker in LIST_MARKERS):
        return True
    if _is_superlative_ranking_question(q):
        return False
    if _is_count_question(q):
        return False
    if any(marker in q for marker in ("who is", "who was", "who came", "kaun", "kon")):
        return True
    return False


def _is_count_question(text: str) -> bool:
    q = (text or "").lower()
    if "kis ne" in q or "kisne" in q:
        return False
    return any(marker in q for marker in COUNT_MARKERS)


def _is_ranking_question(text: str) -> bool:
    """True only for superlative/ranking questions, not list-all questions."""
    return _is_superlative_ranking_question(text)


def _is_exclusion_question(text: str) -> bool:
    q = (text or "").lower()
    return any(marker in q for marker in EXCLUSION_MARKERS)


def _resolve_excluded_employees(text: str, db_names: list = None) -> list:
    """Names the user wants excluded (e.g. 'Hassan aur Raihan ke ilawa ...')."""
    db_names = db_names or get_db_employee_names()
    if not text or not _is_exclusion_question(text):
        return []

    q_lower = text.lower()
    excluded_fragment = text
    for marker in sorted(EXCLUSION_MARKERS, key=len, reverse=True):
        if marker in q_lower:
            excluded_fragment = text[: q_lower.index(marker)]
            break

    return resolve_employees_from_text(excluded_fragment, db_names)

def resolve_employees_from_text(text: str, db_names: list = None) -> list:
    """
    Fuzzy-match employee names mentioned in free text against DB Latin names.
    Always transliterates Urdu script to Latin before matching.
    """
    db_names = db_names or get_db_employee_names()
    if not text or not db_names:
        return []

    latin = transliterate_query_to_latin(text)
    latin = _normalize_whitespace(latin)
    words = [
        w for w in re.findall(r"\b[A-Za-z0-9]+\b", latin)
        if len(w) >= 2 and w.lower() not in NAME_FILLER_WORDS
    ]
    if not words:
        return []

    word_lowers = [w.lower() for w in words]
    matched = []

    for db_name in db_names:
        tokens = [t for t in re.findall(r"\b[A-Za-z0-9]+\b", db_name) if len(t) >= 2]
        if not tokens:
            continue

        if db_name.lower() in latin.lower():
            matched.append(db_name)
            continue

        if all(any(_tokens_match(tok, w) for w in words) for tok in tokens):
            matched.append(db_name)
            continue

        if len(tokens) == 1:
            close = difflib.get_close_matches(tokens[0].lower(), word_lowers, n=1, cutoff=0.68)
            if close:
                matched.append(db_name)
                continue

        joined = " ".join(words).lower()
        if difflib.SequenceMatcher(None, joined, db_name.lower()).ratio() >= 0.68:
            matched.append(db_name)
            continue

        close_full = difflib.get_close_matches(db_name.lower(), [joined], n=1, cutoff=0.68)
        if close_full:
            matched.append(db_name)

    return list(dict.fromkeys(matched))

def validate_employee_names(suggested_names: list, db_names: list = None) -> list:
    """
    Map LLM-suggested employee names to exact Latin names in the database.
    Safety rail: never pass hallucinated names to SQL.
    """
    db_names = db_names or get_db_employee_names()
    if not suggested_names or not db_names:
        return []

    validated = []
    db_lower_map = {n.lower(): n for n in db_names}

    for suggested in suggested_names:
        if not suggested or not isinstance(suggested, str):
            continue

        suggested_latin = transliterate_query_to_latin(suggested.strip())
        suggested_lower = suggested_latin.lower()

        if suggested_lower in db_lower_map:
            name = db_lower_map[suggested_lower]
            if name not in validated:
                validated.append(name)
            continue

        close = difflib.get_close_matches(suggested_lower, list(db_lower_map.keys()), n=1, cutoff=0.68)
        if close:
            name = db_lower_map[close[0]]
            if name not in validated:
                validated.append(name)
            continue

        words = [w for w in re.findall(r"\b[A-Za-z0-9]+\b", suggested_lower) if len(w) >= 2]
        for db_name in db_names:
            tokens = [t for t in re.findall(r"\b[A-Za-z0-9]+\b", db_name.lower()) if len(t) >= 2]
            if tokens and words and all(
                any(_tokens_match(w, t) for w in words) for t in tokens
            ):
                if db_name not in validated:
                    validated.append(db_name)
                break

    return validated

def understand_query(user_query: str, conversation_history: list = None) -> dict:
    """
    LLM-first understanding step. Resolves follow-ups, intent, employees, metric, and language
    in ONE structured call — no brittle regex rule lists.
    """
    history = trim_conversation_history(conversation_history or [])
    latin_query = transliterate_query_to_latin(user_query or "")
    db_names = get_db_employee_names()
    emp_list = "\n".join(f"- {n}" for n in db_names) if db_names else "- (none)"
    today = datetime.date.today()
    last_year = today.year - 1

    system_prompt = f"""You are an expert HR attendance assistant that understands questions in English, Urdu script, and Roman Urdu.

Today's date: {today.isoformat()}
Last year: {last_year}
Registered ACTIVE employees only (inactive/former staff excluded):
{emp_list}

Your job: analyze the user's question (and conversation history for follow-ups) and return structured JSON.

CRITICAL RULES:
1. CONVERSATIONAL FILLERS ARE NEVER EMPLOYEE NAMES:
   acha/achha/okay/ok/theek/haan/ji, aur/or/and, pichle/pichlay/pichla, thi/tha/the, saal/mahine/aaj/kal, kitni/kitne — these are NOT people.

2. FOLLOW-UP QUESTIONS: If the user continues a previous topic (e.g. "acha aur pichle saal kitni thi?" after asking about Bashir),
   resolve into a complete standalone question inheriting employee + topic from history.
   Example: history about Bashir leaves this year + "acha aur pichle saal kitni thi?" ->
   resolved_question: "Bashir ne pichle saal kitni chutiyan ki?"

3. GENERAL vs SPECIFIC:
   - general LIST: "aaj kon kon late aaya", "who is absent today" — return ALL matching employees (not just one)
   - general RANKING: "sabse zyada late kaun", "sab se zyada chutiyan kis ne ki" — return the TOP one only
   - general COUNT: "kitne log late aaye" — return a number
   - specific_person: question about one named active employee's attendance/leaves/status
   - EXCLUSION RANKING (CRITICAL): "Hassan aur Raihan ke ilawa sab se zyada chutiyan kis ne ki?" is GENERAL ranking, NOT specific_person.
     Put excluded names in `excluded_employees` (exact DB spellings) and leave `employees` as [].
     Phrases: ke ilawa, ilawa, besides, except, siwa, chor kar.

4. EMPLOYEES: List only employees the question is ABOUT (target person for specific_person questions).
   For exclusion ranking, use `excluded_employees` instead — do NOT put excluded names in `employees`.

5. METRIC values: leaves, late, present, absent, work_hours, ranking, count, check_in, other
   - Use metric=ranking ONLY when user asks sabse zyada / most / top / kis ne ki style superlative questions.
   - Use metric=late/absent/present for "kon kon" / "who is" list questions even if multiple people are expected.

6. TIME_PERIOD values: today, yesterday, this_year, last_year, this_month, last_month, other, null

7. LANGUAGE: urdu_script | roman_urdu | english (match the user's question language)

Return JSON with keys:
- resolved_question (string): complete standalone question
- intent ("general" | "specific_person")
- employees (array of exact DB employee names targeted by the question, or [])
- excluded_employees (array of exact DB names to EXCLUDE from ranking, or [])
- metric (string)
- time_period (string or null)
- is_follow_up (boolean)
- language (string)
- reasoning (string, brief — for debugging)"""

    user_content = f"User question: {latin_query}"
    if latin_query != (user_query or ""):
        user_content += f"\nOriginal script: {user_query}"
    if history:
        user_content = _format_history_block(history) + user_content

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=get_chat_model(fast=True),
            messages=[
                {"role": "system", "content": system_prompt},
                *[{"role": m["role"], "content": m["content"]} for m in history],
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = json.loads(response.choices[0].message.content.strip())
        resolved = _normalize_whitespace(raw.get("resolved_question") or latin_query)
        suggested_employees = raw.get("employees") or []
        if isinstance(suggested_employees, str):
            suggested_employees = [suggested_employees]

        validated = validate_employee_names(suggested_employees, db_names)
        excluded_suggested = raw.get("excluded_employees") or []
        if isinstance(excluded_suggested, str):
            excluded_suggested = [excluded_suggested]
        excluded = validate_employee_names(excluded_suggested, db_names)
        if not excluded and _is_exclusion_question(latin_query):
            excluded = _resolve_excluded_employees(latin_query, db_names)

        if not validated and not excluded:
            validated = resolve_employees_from_text(resolved, db_names)
        if not validated and not excluded:
            validated = resolve_employees_from_text(latin_query, db_names)

        is_ranking = _is_ranking_question(resolved) or _is_ranking_question(latin_query)
        is_exclusion_ranking = is_ranking and bool(excluded or _is_exclusion_question(latin_query))

        intent = raw.get("intent", "specific_person")
        if intent not in ("general", "specific_person"):
            intent = "general" if is_ranking else ("specific_person" if validated else "general")
        elif is_exclusion_ranking or (is_ranking and "kis ne" in (resolved or latin_query).lower()):
            intent = "general"
            if not excluded and validated:
                excluded = validated
            validated = []
        elif validated and intent == "general" and suggested_employees and not is_ranking:
            intent = "specific_person"

        understanding = {
            "resolved_question": resolved,
            "intent": intent,
            "employees": validated,
            "excluded_employees": excluded,
            "metric": raw.get("metric") or "other",
            "time_period": raw.get("time_period"),
            "is_follow_up": bool(raw.get("is_follow_up")),
            "language": raw.get("language") or detect_query_language(user_query),
            "reasoning": raw.get("reasoning", ""),
            "original_query": user_query,
            "latin_query": latin_query if latin_query != (user_query or "") else None,
        }
        print(f"[Understand] '{user_query}' -> intent={intent}, employees={validated}, excluded={excluded}, resolved='{resolved}'")
        return understanding
    except Exception as e:
        print(f"[Understand Error] {e}")
        fallback_validated = resolve_employees_from_text(latin_query, db_names)
        return {
            "resolved_question": latin_query,
            "intent": "general" if not fallback_validated else "specific_person",
            "employees": fallback_validated,
            "metric": "other",
            "time_period": None,
            "is_follow_up": bool(history),
            "language": detect_query_language(user_query),
            "reasoning": f"Fallback due to error: {e}",
            "original_query": user_query,
            "latin_query": latin_query if latin_query != (user_query or "") else None,
        }

def convert_urdu_script_to_latin(user_query: str) -> str:
    """Replace known Urdu name tokens with Latin equivalents using the dictionary."""
    result = user_query
    for urdu, latin in sorted(URDU_TO_LATIN_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        result = result.replace(urdu, latin)
    return result

def transliterate_query_to_latin(user_query: str) -> str:
    """
    If the query contains Urdu script, transliterates Urdu script into standard Roman Urdu / English Latin.
    e.g., 'پچھلے سال رابیا نے کتنی چھوٹیاں کی تھیں؟' -> 'Pichle saal Rabia ne kitni chutiyan ki thin?'
    """
    if not user_query or not any('\u0600' <= char <= '\u06FF' for char in user_query):
        return user_query or ""

    # Apply dictionary first so common name spellings (e.g. امر vs عمر) resolve before GPT
    dict_result = convert_urdu_script_to_latin(user_query)
    if not any('\u0600' <= char <= '\u06FF' for char in dict_result):
        print(f"[Urdu Dict Transliterated] '{user_query}' -> '{dict_result}'")
        return dict_result
        
    db_names = get_db_employee_names()
    emp_hints = ", ".join(db_names[:35]) if db_names else "Rabia, Mohammad Omer, Shaharyar, Ali, Ahmed"
    
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=get_chat_model(fast=True),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert Urdu to Roman Urdu / English transliterator for employee attendance software. "
                        "Convert input Urdu script into standard Roman Urdu / English text. "
                        f"Database Employee Name Hints: {emp_hints}. "
                        "MUST spell employee names matching English Latin names (e.g. Rabia, Shehryar, Mohammad Omer, Ali, Ahmed, Farrukh, Fayyaz). "
                        "Return ONLY the transliterated string without quotes, formatting, or extra text."
                    )
                },
                {"role": "user", "content": user_query}
            ],
            max_tokens=60,
            temperature=0.0
        )
        result = response.choices[0].message.content.strip()
        print(f"[Urdu Transliterated] '{user_query}' -> '{result}'")
        return result
    except Exception as e:
        print(f"[Transliteration Error] {e}")
        return dict_result

def get_openai_tts_model() -> str:
    return os.environ.get("OPENAI_TTS_MODEL", "tts-1").strip().lower()


def _tts_cache_key(speech_text: str, voice: str, model: str) -> str:
    payload = f"{model}|{voice}|{speech_text.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_cached_tts(cache_key: str) -> str | None:
    entry = _tts_audio_cache.get(cache_key)
    if not entry:
        return None
    audio_b64, fetched_at = entry
    if (time.time() - fetched_at) > _TTS_CACHE_TTL_SECONDS:
        _tts_audio_cache.pop(cache_key, None)
        return None
    return audio_b64


def _set_cached_tts(cache_key: str, audio_b64: str) -> None:
    if len(_tts_audio_cache) >= _TTS_CACHE_MAX_ENTRIES:
        oldest_key = min(_tts_audio_cache, key=lambda key: _tts_audio_cache[key][1])
        _tts_audio_cache.pop(oldest_key, None)
    _tts_audio_cache[cache_key] = (audio_b64, time.time())


def attach_audio_to_result(result: dict) -> dict:
    """Generate TTS once per query response (uses cache). Does not change answer text."""
    if not isinstance(result, dict):
        return result
    if result.get("audio"):
        return result

    answer = (result.get("answer") or "").strip()
    if not answer:
        return result

    try:
        understanding = result.get("understanding") or {}
        tts_lang = understanding.get("language")
        speech_text = prepare_speech_text(answer, tts_lang)
        result["speech_text"] = speech_text
        result["audio"] = generate_speech(answer, language=tts_lang, speech_text=speech_text)
    except Exception as exc:
        print(f"[attach_audio] {exc}")
    return result

def get_openai_client() -> openai.OpenAI:
    """
    Returns an OpenAI client instance using OPENAI_API_KEY.
    Raises ValueError if OPENAI_API_KEY is missing.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "sk-proj-your_openai_api_key_here" or api_key.startswith("your_"):
        raise ValueError("OPENAI_API_KEY is not configured in the environment or .env file.")
    return openai.OpenAI(api_key=api_key)

def get_db_employee_names() -> list:
    """
    Fetches active employee names with a short in-memory cache to avoid repeated DB hits per request.
    """
    import time

    now = time.time()
    if _employee_names_cache["names"] and (now - _employee_names_cache["fetched_at"]) < _EMPLOYEE_CACHE_TTL_SECONDS:
        return _employee_names_cache["names"]

    try:
        with engine.connect() as conn:
            if DATABASE_URL.startswith("sqlite"):
                sql = "SELECT name FROM employees WHERE is_active = 1"
            else:
                sql = "SELECT name FROM employees WHERE is_active IS TRUE"
            res = conn.execute(text(sql))
            names = [row[0].strip() for row in res.fetchall() if row[0]]
            _employee_names_cache["names"] = names
            _employee_names_cache["fetched_at"] = now
            return names
    except Exception as e:
        print(f"[DB Employee Fetch Error] {e}")
        return []

def find_candidate_employees(user_query: str, normalized_query: str = None, conversation_history: list = None) -> list:
    """Returns validated employee names via LLM understanding."""
    return understand_query(user_query, conversation_history).get("employees", [])


def get_ai_status() -> dict:
    """
    Returns active OpenAI AI engine configuration status.
    """
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_active = bool(openai_key and not openai_key.startswith("sk-proj-your") and not openai_key.startswith("your_"))
    
    voice = os.environ.get("OPENAI_TTS_VOICE", "sage").lower()
    tts_model = get_openai_tts_model()
    
    return {
        "engine": "OpenAI",
        "openai_available": openai_active,
        "tts_provider": f"OpenAI Speech ({tts_model})",
        "stt_provider": "OpenAI Whisper",
        "tts_model": tts_model,
        "voice": voice,
        "available_voices": ["sage", "nova", "onyx", "alloy", "echo", "fable", "shimmer", "coral", "ash"]
    }

def transcribe_audio(audio_bytes: bytes, original_filename: str) -> str:
    """
    Transcribes spoken audio bytes into text using OpenAI Whisper (whisper-1).
    Provides high accuracy in English, Urdu script, and Roman Urdu with dynamic employee vocabulary hints.
    """
    client = get_openai_client()
    
    ext = os.path.splitext(original_filename)[1].lower() if original_filename else ".m4a"
    if ext not in [".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".webm", ".mp4"]:
        ext = ".m4a"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as temp_file:
        temp_file.write(audio_bytes)
        temp_path = temp_file.name

    # Dynamic employee names from DB
    db_employees = get_db_employee_names()
    emp_str = ", ".join(db_employees[:30]) if db_employees else "Mohammad Omer, Shaharyar, Ali, Ahmed, Usman, Farrukh"

    try:
        with open(temp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                model=OPENAI_WHISPER_MODEL,
                file=f,
                prompt=(
                    "Transcribe the spoken audio clip accurately in Roman Urdu or English. "
                    "Do NOT translate Roman Urdu into English words. "
                    f"Target employee names in system: {emp_str}. "
                    "Common terms: chutiyan, hazri, der se, gair hazir, late, present, absent, aagaye, is saal, pichle mahine, aaj."
                ),
                temperature=0.0
            )
        text = transcription.text.strip() if transcription and transcription.text else ""
        if text and any('\u0600' <= c <= '\u06FF' for c in text):
            text = transliterate_query_to_latin(text)
        return text
    except Exception as e:
        print(f"[OpenAI Whisper Error] Failed to transcribe audio: {e}")
        raise RuntimeError(f"OpenAI Whisper transcription failed: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def _inactive_employee_predicate() -> str:
    if DATABASE_URL.startswith("sqlite"):
        return "employees.is_active = 0"
    return "employees.is_active IS FALSE"

def _sql_targets_inactive_employees(sql_query: str) -> bool:
    sql_lower = (sql_query or "").lower()
    inactive_markers = (
        r"is_active\s*=\s*0",
        r"is_active\s*=\s*false",
        r"is_active\s+is\s+false",
        r"employees\.is_active\s*=\s*0",
        r"employees\.is_active\s*=\s*false",
        r"employees\.is_active\s+is\s+false",
    )
    return any(re.search(pattern, sql_lower) for pattern in inactive_markers)

def _strip_contradictory_active_filters(sql_query: str) -> str:
    if not _sql_targets_inactive_employees(sql_query):
        return sql_query
    cleaned = sql_query
    for pattern, replacement in (
        (r"\s+AND\s+employees\.is_active\s+IS\s+TRUE\b", ""),
        (r"\s+AND\s+is_active\s+IS\s+TRUE\b", ""),
        (r"\s+AND\s+employees\.is_active\s*=\s*1\b", ""),
        (r"\s+AND\s+is_active\s*=\s*1\b", ""),
        (r"\bWHERE\s+employees\.is_active\s+IS\s+TRUE\s+AND\s+", "WHERE "),
        (r"\bWHERE\s+is_active\s+IS\s+TRUE\s+AND\s+", "WHERE "),
        (r"\bWHERE\s+employees\.is_active\s+IS\s+TRUE\s*$", ""),
        (r"\bWHERE\s+is_active\s+IS\s+TRUE\s*$", ""),
    ):
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def friendly_user_error(user_query: str, understanding: dict = None, reason: str = "unknown") -> str:
    """Return a natural, user-facing message — never expose SQL/DB internals."""
    lang = (understanding or {}).get("language") or detect_query_language(user_query or "")
    if lang == "urdu_script":
        messages = {
            "database": "معذرت، اس سوال کا جواب نکالتے وقت کوئی مسئلہ آ گیا۔ براہ کرم سوال دوسرے الفاظ میں دوبارہ پوچھیں۔",
            "understand": "معذرت، مجھے آپ کا سوال سمجھ نہیں آیا۔ براہ کرم اسے دوسرے انداز میں پوچھیں۔",
            "default": "معذرت، میں ابھی یہ نہیں سمجھ سکا۔ تھوڑا سا مختلف انداز میں دوبارہ کوشش کریں۔",
        }
    elif lang == "roman_urdu":
        messages = {
            "database": "Maazrat, is sawaal ka jawab nikalte waqt koi masla aa gaya. Barah-e-karam sawaal ko doosray lafzon mein dobara poochein.",
            "understand": "Mujhe aap ka sawaal samajh nahi aaya. Barah-e-karam isko doosray andaaz mein phir se poochein.",
            "default": "Maazrat, main abhi yeh samajh nahi saka. Thora alag andaaz mein dobara koshish karein.",
        }
    else:
        messages = {
            "database": "Sorry, I had trouble fetching that answer. Please try asking in a slightly different way.",
            "understand": "I didn't quite understand that. Please try rephrasing your question.",
            "default": "Sorry, I couldn't process that right now. Please try again in a different way.",
        }
    return messages.get(reason, messages["default"])

def process_user_query(query_text: str, conversation_history: list = None) -> dict:
    """End-to-end AI query pipeline with safe user-facing errors."""
    history = trim_conversation_history(conversation_history or [])
    understanding = {}
    sql_info = {}
    try:
        sql_info = generate_sql(query_text, conversation_history=history)
        understanding = sql_info.get("understanding") or {}
        sql_query = sql_info.get("sql")
        explanation = sql_info.get("explanation", "")
        query_intent = sql_info.get("query_intent", "specific_person")

        if sql_query:
            effective_text = sql_info.get("resolved_query") or query_text
            sql_query = correct_chutti_sql(
                sql_query, effective_text, metric=understanding.get("metric")
            )

        query_results = []
        error_msg = None
        if sql_query:
            try:
                query_results = execute_sql(sql_query)
            except Exception as exc:
                print(f"[SQL Execution Error] {exc}")
                error_msg = str(exc)
                sql_query = None

        if not sql_query and error_msg:
            answer = friendly_user_error(query_text, understanding, reason="database")
        elif not sql_query:
            answer = friendly_user_error(query_text, understanding, reason="understand")
            if explanation and len(explanation) < 120 and "sql" not in explanation.lower():
                pass  # keep friendly message only
        else:
            answer = synthesize_answer(
                query_text,
                query_results,
                sql_query,
                query_intent,
                candidate_matches=sql_info.get("candidate_matches", []),
                conversation_history=history,
                resolved_query=sql_info.get("resolved_query"),
                understanding=understanding,
            )

        return {
            "question": query_text,
            "sql": sql_query,
            "answer": answer,
            "audio": "",
            "speech_text": "",
            "explanation": explanation,
            "candidate_matches": sql_info.get("candidate_matches", []),
            "transliterated_query": sql_info.get("transliterated_query"),
            "match_hint": sql_info.get("match_hint"),
            "query_intent": query_intent,
            "resolved_query": sql_info.get("resolved_query"),
            "understanding": understanding,
            "query_results": query_results,
        }
    except Exception as exc:
        print(f"[AI Query Error] {exc}")
        return {
            "question": query_text,
            "sql": None,
            "answer": friendly_user_error(query_text, understanding, reason="default"),
            "audio": "",
            "speech_text": "",
            "explanation": "",
            "candidate_matches": sql_info.get("candidate_matches", []),
            "transliterated_query": sql_info.get("transliterated_query"),
            "match_hint": sql_info.get("match_hint"),
            "query_intent": sql_info.get("query_intent", "specific_person"),
            "resolved_query": sql_info.get("resolved_query"),
            "understanding": understanding,
            "query_results": [],
        }

def _active_employee_predicate() -> str:
    if DATABASE_URL.startswith("sqlite"):
        return "employees.is_active = 1"
    return "employees.is_active IS TRUE"

def _normalize_sql_dialect(sql_query: str) -> str:
    """Adjust SQLite-style SQL for PostgreSQL when needed."""
    if DATABASE_URL.startswith("sqlite"):
        return sql_query

    sql = sql_query

    def _convert_date_offset(match: re.Match) -> str:
        base = match.group(1)
        offset = match.group(2).strip().strip("'\"")
        offset_match = re.match(r"-(\d+)\s+(day|days|month|months|year|years)", offset, re.IGNORECASE)
        if not offset_match:
            return match.group(0)
        amount, unit = offset_match.group(1), offset_match.group(2).lower()
        if unit.startswith("day"):
            interval = f"{amount} days"
        elif unit.startswith("month"):
            interval = f"{amount} months"
        else:
            interval = f"{amount} years"
        if base.lower() == "now":
            return f"(CURRENT_DATE - INTERVAL '{interval}')"
        return f"('{base}'::date - INTERVAL '{interval}')"

    sql = re.sub(
        r"date\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)",
        _convert_date_offset,
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"date\s*\(\s*'now'\s*,\s*'-\s*(\d+)\s+days?'\s*\)",
        r"(CURRENT_DATE - INTERVAL '\1 days')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"date\s*\(\s*'now'\s*\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"date\s*\(\s*'([^']+)'\s*\)", r"'\1'::date", sql, flags=re.IGNORECASE)
    sql = re.sub(r"datetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bemployees\.is_active\s*=\s*1\b", "employees.is_active IS TRUE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bis_active\s*=\s*1\b", "is_active IS TRUE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bemployees\.is_active\s*=\s*0\b", "employees.is_active IS FALSE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bis_active\s*=\s*0\b", "is_active IS FALSE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bemployees\.is_active\s*=\s*false\b", "employees.is_active IS FALSE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bis_active\s*=\s*false\b", "is_active IS FALSE", sql, flags=re.IGNORECASE)

    # Cast bare date strings in BETWEEN comparisons when needed
    sql = re.sub(
        r"\bBETWEEN\s+'(\d{4}-\d{2}-\d{2})'\s+AND\s+'(\d{4}-\d{2}-\d{2})'",
        r"BETWEEN '\1'::date AND '\2'::date",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(daily_attendance\.date\s*[=<>]+\s*)'(\d{4}-\d{2}-\d{2})'",
        r"\1'\2'::date",
        sql,
        flags=re.IGNORECASE,
    )

    return sql

def finalize_sql(sql_query: str) -> str:
    """Apply all SQL safety normalizations before execution."""
    if not sql_query:
        return sql_query
    sql = _normalize_sql_dialect(sql_query)
    sql = _strip_contradictory_active_filters(sql)
    if _sql_targets_inactive_employees(sql):
        return sql
    sql = ensure_employees_join_for_attendance(sql)
    sql = ensure_active_employees_filter(sql)
    return sql

def ensure_employees_join_for_attendance(sql_query: str) -> str:
    """Ensure attendance queries join employees so active-only filter can apply."""
    sql_lower = sql_query.lower()
    if "daily_attendance" not in sql_lower or "employees" in sql_lower:
        return sql_query

    pattern = re.compile(
        r"(FROM\s+daily_attendance(?:\s+(?:AS\s+)?\w+)?)",
        re.IGNORECASE,
    )
    match = pattern.search(sql_query)
    if not match:
        return sql_query

    insert_pos = match.end()
    join_clause = " JOIN employees ON daily_attendance.employee_id = employees.id "
    updated = sql_query[:insert_pos] + join_clause + sql_query[insert_pos:]
    print("[SQL Active Filter] Added employees JOIN for active-only filtering")
    return updated

def ensure_active_employees_filter(sql_query: str) -> str:
    """
    Inject active-employee constraint when queries touch the employees table.
    Prevents rankings/counts from including former/inactive staff.
    """
    if not sql_query:
        return sql_query

    sql_lower = sql_query.lower()
    if "employees" not in sql_lower:
        return sql_query

    if _sql_targets_inactive_employees(sql_query):
        return _strip_contradictory_active_filters(sql_query)

    if re.search(r"\b(employees\.)?is_active\s*(=|is)", sql_lower):
        return _strip_contradictory_active_filters(sql_query)

    predicate = _active_employee_predicate()
    clause_boundary = re.compile(r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING)\b", re.IGNORECASE)

    if re.search(r"\bWHERE\b", sql_query, re.IGNORECASE):
        where_match = re.search(r"\bWHERE\b", sql_query, re.IGNORECASE)
        end_match = clause_boundary.search(sql_query, where_match.end())
        if end_match:
            insert_pos = end_match.start()
            updated = sql_query[:insert_pos].rstrip() + f" AND {predicate} " + sql_query[insert_pos:]
        else:
            updated = sql_query.rstrip().rstrip(";") + f" AND {predicate}"
    else:
        end_match = clause_boundary.search(sql_query)
        if end_match:
            insert_pos = end_match.start()
            updated = sql_query[:insert_pos].rstrip() + f" WHERE {predicate} " + sql_query[insert_pos:]
        else:
            updated = sql_query.rstrip().rstrip(";") + f" WHERE {predicate}"

    print(f"[SQL Active Filter] Applied: {predicate}")
    return updated

def _build_sql_system_prompt() -> str:
    today = datetime.date.today()
    current_year = today.year
    first_day_of_year = f"{current_year}-01-01"
    last_day_of_year = f"{current_year}-12-31"
    first_day_of_month = today.replace(day=1)
    
    last_month_end = first_day_of_month - datetime.timedelta(days=1)
    first_day_of_last_month = last_month_end.replace(day=1)
    last_year = current_year - 1
    first_day_of_last_year = f"{last_year}-01-01"
    last_day_of_last_year = f"{last_year}-12-31"
    last_week_start = (today - datetime.timedelta(days=7)).isoformat()
    
    db_employees = get_db_employee_names()
    emp_list_str = "\n".join([f"- {name}" for name in db_employees]) if db_employees else "- Mohammad Omer\n- Shaharyar\n- Ali\n- Ahmed"
    
    is_postgres = not DATABASE_URL.startswith("sqlite")
    db_label = "PostgreSQL" if is_postgres else "SQLite"
    date_fn_yesterday = (
        f"(CURRENT_DATE - INTERVAL '1 day')" if is_postgres
        else f"date('{today.isoformat()}', '-1 day')"
    )
    date_fn_last_week = (
        f"(CURRENT_DATE - INTERVAL '7 days')" if is_postgres
        else f"date('{today.isoformat()}', '-7 days')"
    )
    
    date_context = (
        f"Today's date: {today.isoformat()} ({today.strftime('%A')})\n"
        f"Current year range ('this year' / 'is saal'): {first_day_of_year} to {last_day_of_year}\n"
        f"First day of current month ('this month' / 'is mahine'): {first_day_of_month.isoformat()}\n"
        f"Last month range ('last month' / 'pichle mahine'): {first_day_of_last_month.isoformat()} to {last_month_end.isoformat()}\n"
        f"Last year range ('last year' / 'pichle saal' / 'pichlay saal'): {first_day_of_last_year} to {last_day_of_last_year}\n"
        f"Last week range ('last week' / 'pichle hafte' / 'pichlay hafte'): {last_week_start} to {today.isoformat()}\n"
    )
    
    date_syntax_rules = (
        "10. DATE SYNTAX (PostgreSQL — CRITICAL):\n"
        "   - NEVER use SQLite date('YYYY-MM-DD', '-7 days').\n"
        "   - Use: (CURRENT_DATE - INTERVAL '7 days') for relative dates.\n"
        "   - Use: 'YYYY-MM-DD'::date for fixed dates.\n"
        "   - Example last week: daily_attendance.date BETWEEN (CURRENT_DATE - INTERVAL '7 days') AND CURRENT_DATE\n"
        if is_postgres else
        "10. DATE SYNTAX (SQLite):\n"
        "   - Use date('YYYY-MM-DD', '-7 days') for relative dates.\n"
    )
    
    return f"""You are a senior {db_label} database expert for an employee attendance management system.
Your job is to translate natural language questions (asked in English, Urdu script, or Roman Urdu) into a valid {db_label} SELECT query.

Here is the {db_label} schema:

1. Table: employees
   - id (INTEGER, PRIMARY KEY)
   - user_id (VARCHAR, UNIQUE) - Biometric user ID / card number (e.g. "1", "2")
   - name (VARCHAR) - Name of employee (ALWAYS stored strictly in standard English/Latin alphabet, e.g. 'Mohammad Omer', 'Shaharyar', 'amir shaikh')
   - is_active (BOOLEAN) - 1 for active, 0 for inactive
   - department_id (INTEGER, FOREIGN KEY to departments.id)
   - shift_id (INTEGER, FOREIGN KEY to shifts.id)

2. Table: departments
   - id (INTEGER, PRIMARY KEY)
   - name (VARCHAR, UNIQUE) - e.g. "HR", "Engineering", "Marketing", "Sales", "Operations"
   - is_active (BOOLEAN)

3. Table: shifts
   - id (INTEGER, PRIMARY KEY)
   - name (VARCHAR, UNIQUE) - e.g. "Morning", "Night"
   - start_time (TIME) - e.g. '09:00:00'
   - end_time (TIME) - e.g. '17:00:00'

4. Table: daily_attendance
   - id (INTEGER, PRIMARY KEY)
   - employee_id (INTEGER, FOREIGN KEY to employees.id)
   - date (DATE) - Date ('YYYY-MM-DD')
   - check_in (DATETIME)
   - check_out (DATETIME)
   - work_hours (FLOAT)
   - status (VARCHAR) - EXACT values: 'Present', 'Late', 'Absent', 'Left Early', 'Half Day', 'On Leave'
   - late_minutes (INTEGER)
   - early_leave_minutes (INTEGER)
   - remarks (VARCHAR)

5. Table: leave_requests
   - id (INTEGER, PRIMARY KEY)
   - employee_id (INTEGER, FOREIGN KEY to employees.id)
   - leave_type_id (INTEGER, FOREIGN KEY to leave_types.id)
   - start_date (DATE)
   - end_date (DATE)
   - is_half_day (BOOLEAN)
   - half_day_period (VARCHAR)
   - status (VARCHAR) - 'Pending', 'Approved', 'Rejected'
   - reason (TEXT)

6. Table: leave_types
   - id (INTEGER, PRIMARY KEY)
   - name (VARCHAR, UNIQUE) - e.g. "Casual Leave", "Sick Leave", "Annual Leave"
   - is_paid (BOOLEAN)

LIST OF ALL ACTIVE EMPLOYEES (currently employed — inactive/former staff excluded):
{emp_list_str}

Context details:
{date_context}

RULES FOR QUERY GENERATION:
1. ONLY return a valid SQLite SELECT query. Do NOT generate modifying queries (INSERT, UPDATE, DELETE, DROP, ALTER).

2. STRICT LATIN ALPHABET RULE FOR SQL LITERALS (CRITICAL):
   - ALL employee names in the database are strictly stored in standard English/Latin alphabet.
   - NEVER EVER output Urdu script characters (like '%محمد عمر%' or '%شہریار%') inside SQL query literals!
   - ALWAYS output string literals strictly in standard English/Latin alphabet matching the candidate names listed above!

3. UNIVERSAL PHONETIC & FUZZY MATCHING FOR ALL EMPLOYEES:
   - Apply universal phonetic and fuzzy spelling matching for ANY employee name asked in English, Urdu script, or Roman Urdu.
   - Examples of Vowel & Phonetic Variations:
     - "mohammad umer" / "محمد عمر" / "محمد امر" / "omer" -> match 'Mohammad Omer'
     - "shehryar" / "شہریار" / "shahryar" -> match 'Shaharyar'
     - "aamir" / "عامر" -> match 'amir shaikh'
     - "idris" / "ادریس" -> match 'IDREES KHAN'
     - "furqan" / "فرقان" -> match 'Furqan Accounts'

4. GENERAL vs SPECIFIC-PERSON QUESTIONS (CRITICAL):
   - GENERAL questions ask about ALL employees, a list, a count, or a ranking — NO specific person is named.
     Examples:
     - "Who is absent today?" / "Aaj kaun absent hai?" / "آج کون غیر حاضر ہے؟"
     - "Who came late today?" / "Aaj kaun late aaya?" / "آج سب سے زیادہ لیٹ کون آیا؟"
     - "Aaj sabse zyada late kaun aaya?" / "Who was the most late today?"
     - "How many employees were late this month?" / "Kitne log late aaye?"
     - "Which employee has highest work hours?" / "Sabse zyada kaam kis ne kiya?"
     For GENERAL questions: ALWAYS generate SQL. Do NOT look for an employee name. Do NOT return sql: null.
     Use JOINs, date filters, ORDER BY, LIMIT, COUNT, MAX as appropriate.
     ONLY include currently ACTIVE employees (employees.is_active = 1). Exclude former/inactive staff.

   - SPECIFIC-PERSON questions name one employee and ask about their status/record.
     Examples: "Is Mohammad Omer present?", "Shehryar ne kitni chutiyan ki?"

5. NON-EXISTENT EMPLOYEE HANDLING (ONLY for SPECIFIC-PERSON questions):
   - ONLY apply this rule when the user clearly names ONE specific person (e.g. "Is David present?", "Zack aaya hai?")
     AND that name does NOT match any employee in the database list above.
   - Then set `"sql": null` and `"explanation": "No employee named '[Name]' exists in the company database."`
   - NEVER apply this rule to GENERAL questions like "who is late" or "sabse zyada late kaun aaya" — those are NOT person names!
   - NEVER generate a query assuming a non-existent person is absent!

6. SUPERLATIVE & RANKING QUERIES (ONLY when user asks sabse zyada / most / top):
   - "sabse zyada late" / "most late" / "latest arrival" / "sab se zyada der se":
     Filter today's late records, ORDER BY `daily_attendance.late_minutes DESC` or `daily_attendance.check_in DESC`, LIMIT 1.
     Example: `SELECT employees.name, daily_attendance.late_minutes, daily_attendance.check_in FROM daily_attendance JOIN employees ON daily_attendance.employee_id = employees.id WHERE daily_attendance.date = '{today.isoformat()}' AND daily_attendance.status = 'Late' ORDER BY daily_attendance.late_minutes DESC LIMIT 1`
   - LIST questions like "aaj kon kon late aaya" / "who is late today":
     Return ALL matching names — NO LIMIT 1.
     Example: `SELECT employees.name FROM daily_attendance JOIN employees ON daily_attendance.employee_id = employees.id WHERE daily_attendance.date = '{today.isoformat()}' AND daily_attendance.status = 'Late' AND employees.is_active IS TRUE`
   - "sabse zyada work hours" / "highest work hours":
     ORDER BY `daily_attendance.work_hours DESC` with appropriate date filter.
   - "kitne log late" / "how many late":
     `SELECT COUNT(*) ... WHERE status = 'Late' AND date = today`

7. MULTILINGUAL & ROMAN URDU VOCABULARY MAPPING:
   - "chutiyan" / "chutti" / "chuti" / "chhutti" / "chhuttiyan" / "chhutiyan" / "chhuti" / "off":
     ALWAYS means days off from attendance records. Count from `daily_attendance` with `status IN ('Absent', 'On Leave')`.
     NEVER use only 'Absent' or only 'On Leave' for chutti/chhuti questions — always BOTH.
     Do NOT use `leave_requests` for chutti/chutiyan counts — that table is often empty; real absence data lives in `daily_attendance`.
     Example: `SELECT COUNT(*) FROM daily_attendance JOIN employees ON daily_attendance.employee_id = employees.id WHERE employees.name = 'Mohammad Omer' AND daily_attendance.status IN ('Absent', 'On Leave') AND daily_attendance.date BETWEEN '{first_day_of_last_year}' AND '{last_day_of_last_year}'`
   - "gair hazir" / "absent" (WITHOUT chutti/chhuti words):
     Usually means not present. Query `daily_attendance.status IN ('Absent', 'On Leave')` unless user clearly means only unapproved absence.
   - "haazri" / "hazri" / "present" / "aaya" / "aaye" / "attendance":
     Refers to presence. Query `daily_attendance.status IN ('Present', 'Late')`.
   - "der" / "late" / "der se aaya" / "sabse zyada late" / "sab se zyada late":
     Refers to late arrivals. Query `daily_attendance.status = 'Late'` OR `daily_attendance.late_minutes > 0`.
     For "who was most late": filter today + status Late + ORDER BY late_minutes DESC LIMIT 1.
   - "jaldi gaya" / "left early":
     Refers to early departures. Query `daily_attendance.status = 'Left Early'` OR `daily_attendance.early_leave_minutes > 0`.
   - "is saal" / "iss saal" / "this year":
     `daily_attendance.date BETWEEN '{first_day_of_year}' AND '{last_day_of_year}'`
   - "is mahine" / "iss mahine" / "this month":
     `daily_attendance.date BETWEEN '{first_day_of_month.isoformat()}' AND '{today.isoformat()}'`.
   - "pichle mahine" / "last month":
     `daily_attendance.date BETWEEN '{first_day_of_last_month.isoformat()}' AND '{last_month_end.isoformat()}'`.
   - "pichle saal" / "pichlay saal" / "last year":
     `daily_attendance.date BETWEEN '{first_day_of_last_year}' AND '{last_day_of_last_year}'`.
   - "aaj" / "today":
     `daily_attendance.date = '{today.isoformat()}'`.
   - "kal" / "yesterday":
     `daily_attendance.date = {date_fn_yesterday}`.
   - "pichle hafte" / "pichlay hafte" / "last week":
     `daily_attendance.date BETWEEN {date_fn_last_week} AND '{today.isoformat()}'`.

{date_syntax_rules}

11. JOINING & SELECTING FIELDS:
   - Always join `daily_attendance` with `employees` on `daily_attendance.employee_id = employees.id`.
   - Always select `employees.name` alongside counts or date details so the synthesis step knows the matched employee's exact name.
   - ALWAYS add `AND employees.is_active = 1` (or `employees.is_active IS TRUE`) on every query that references the employees table.
   - Rankings like "sabse zyada chutiyan/late/work hours" must only compare ACTIVE employees, never people who left the company.
   - EXCLUSION RANKING: "Hassan aur Raihan ke ilawa sab se zyada chutiyan kis ne ki?" ->
     `SELECT employees.name, COUNT(*) AS leave_count FROM daily_attendance JOIN employees ON ... 
      WHERE daily_attendance.status IN ('Absent', 'On Leave') 
      AND LOWER(employees.name) NOT IN ('hassan raza', 'rehan ali') 
      AND employees.is_active IS TRUE 
      GROUP BY employees.name ORDER BY leave_count DESC LIMIT 1`
     Use LOWER(employees.name) for NOT IN so casing matches the database.

7. ACTIVE EMPLOYEES ONLY (CRITICAL):
   - Company-wide counts, rankings, and "who has the most" questions apply ONLY to active staff.
   - Inactive employees may have old attendance rows — those rows must NOT appear in rankings or "sabse zyada" answers.
   - Unless the user explicitly asks about inactive/former/left employees, always filter `employees.is_active = 1`.

8. INACTIVE / FORMER EMPLOYEES (when user asks explicitly):
   - Phrases like "jo ab kaam nahi karte", "inactive", "left company", "former staff", "purane employees":
     Use ONLY `employees.is_active IS FALSE` (PostgreSQL) or `employees.is_active = 0` (SQLite).
   - NEVER combine inactive filter with active filter in the same query.
   - Example: `SELECT COUNT(*) FROM employees WHERE employees.is_active IS FALSE`

9. FOLLOW-UP & CONVERSATIONAL QUESTIONS (CRITICAL):
   - When a "Resolved standalone question" is provided, ALWAYS use it for SQL generation.
   - Follow-up fillers are NEVER employee names: acha/achha/okay/ok, aur/or, pichle/pichlay/pichla, thi/tha/the, saal, mahine.
   - Example: "acha aur pichle saal kitni thi?" after asking about Bashir -> resolved to "Bashir ne pichle saal kitni chutiyan ki?" -> generate SQL for Bashir last year.
   - NEVER return sql: null for follow-ups that only change the time period or use conversational fillers.

9. JSON OUTPUT FORMAT:
   Return strictly a JSON object with keys: "sql" (string or null) and "explanation" (string).
"""

def generate_sql(user_query: str, conversation_history: list = None) -> dict:
    """
    Converts natural language to SQLite using LLM-first understanding, then SQL generation.
    """
    client = get_openai_client()
    system_prompt = _build_sql_system_prompt()
    history = trim_conversation_history(conversation_history or [])

    latin_query = transliterate_query_to_latin(user_query or "")
    transliterated_query = latin_query if latin_query != (user_query or "") else None

    # Step 1: LLM understands intent, resolves follow-ups, identifies employees (always on Latin text)
    understanding = understand_query(latin_query, history)
    resolved_query = understanding["resolved_question"]
    query_intent = understanding["intent"]
    candidate_matches = understanding["employees"]
    excluded_employees = understanding.get("excluded_employees") or []
    metric = understanding.get("metric", "other")

    if is_exclusion_ranking := (query_intent == "general" and excluded_employees):
        candidate_matches = []

    if not candidate_matches and query_intent == "specific_person" and not excluded_employees:
        candidate_matches = resolve_employees_from_text(resolved_query) or resolve_employees_from_text(latin_query)
        if candidate_matches:
            understanding["employees"] = candidate_matches
            query_intent = "specific_person"

    # Build context hint for SQL generation from understanding (not regex rules)
    if query_intent == "general" and excluded_employees:
        excluded_sql = ", ".join(repr(name.lower()) for name in excluded_employees)
        match_hint = (
            f"\nUNDERSTANDING: GENERAL ranking with EXCLUSIONS — metric={metric}, "
            f"time_period={understanding.get('time_period')}.\n"
            f"Find who has the MOST among active employees EXCLUDING: {excluded_employees}.\n"
            f"Use case-insensitive exclusion: LOWER(employees.name) NOT IN ({excluded_sql}).\n"
            "Return ONE row: employee name + count, ORDER BY count DESC LIMIT 1.\n"
            "Count chutti/leave from daily_attendance with status IN ('Absent', 'On Leave').\n"
            "ALWAYS return valid SQL.\n"
        )
    elif query_intent == "general":
        list_note = ""
        if _is_list_question(resolved_query) or _is_list_question(latin_query):
            list_note = (
                " This is a LIST question (kon kon / who all) — return ALL matching employee names. "
                "Do NOT use LIMIT 1. "
            )
        elif _is_superlative_ranking_question(resolved_query, understanding) or _is_superlative_ranking_question(latin_query, understanding):
            list_note = (
                " This is a RANKING/superlative question — return ONE top result with ORDER BY + LIMIT 1. "
            )
        elif _is_count_question(resolved_query) or _is_count_question(latin_query):
            list_note = " This is a COUNT question — use COUNT(*). "
        match_hint = (
            f"\nUNDERSTANDING: GENERAL question — metric={metric}, time_period={understanding.get('time_period')}. "
            f"{list_note}"
            "Generate SQL across ACTIVE employees only (employees.is_active = 1). "
            "Do NOT treat conversational words as employee names. "
            "Exclude inactive/former staff from rankings and counts. "
            "ALWAYS return valid SQL.\n"
        )
    elif candidate_matches:
        follow_up_note = " FOLLOW-UP." if understanding.get("is_follow_up") else ""
        match_hint = (
            f"\nUNDERSTANDING: SPECIFIC employee question.{follow_up_note} "
            f"Employees: {candidate_matches}. Metric: {metric}. Time: {understanding.get('time_period')}. "
            f"Use ONLY these exact Latin names in SQL.\n"
        )
    else:
        match_hint = (
            f"\nUNDERSTANDING: specific_person intent but no employee matched DB. "
            f"Metric: {metric}. If user named someone not in DB, return sql: null. "
            "Conversational fillers (acha, aur, pichle) are NOT names.\n"
        )

    try:
        user_content = f"User Question: {latin_query}"
        if transliterated_query and user_query:
            user_content += f"\nOriginal script: {user_query}"
        if resolved_query != latin_query:
            user_content += f"\nResolved standalone question: {resolved_query}"
        user_content += f"\nIntent: {query_intent} | Metric: {metric} | Time: {understanding.get('time_period')}"
        user_content += match_hint

        response = client.chat.completions.create(
            model=get_chat_model(),
            messages=_build_openai_messages(system_prompt, history, user_content),
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content.strip()
        result = json.loads(raw_text)
        
        sql_str = result.get("sql")
        if sql_str and re.search(r'[\u0600-\u06FF]', sql_str):
            print(f"[SQL Sanitization Warning] Replacing leaked Urdu script in SQL: {sql_str}")
            if candidate_matches:
                latin_name = candidate_matches[0]
                sql_str = re.sub(r"'[\u0600-\u06FF\s]+'", f"'{latin_name}'", sql_str)
                sql_str = re.sub(r"%[\u0600-\u06FF\s]+%", f"%{latin_name}%", sql_str)
                result["sql"] = sql_str
            else:
                result["sql"] = None
                result["explanation"] = result.get("explanation") or "Could not resolve employee name to Latin database spelling."

        if result.get("sql"):
            result["sql"] = finalize_sql(result["sql"])
            if excluded_employees:
                result["sql"] = fix_exclusion_not_in_case(result["sql"], excluded_employees)

        result["candidate_matches"] = candidate_matches
        result["excluded_employees"] = excluded_employees
        result["resolved_query"] = resolved_query if resolved_query != latin_query else None
        result["transliterated_query"] = transliterated_query
        result["query_intent"] = query_intent
        result["understanding"] = understanding
        result["match_hint"] = match_hint.strip()
        return result
    except Exception as e:
        print(f"[OpenAI SQL Error] {e}")
        return {
            "sql": None,
            "explanation": f"Failed to generate SQL query: {e}",
            "candidate_matches": candidate_matches,
            "resolved_query": resolved_query if resolved_query != latin_query else None,
            "transliterated_query": transliterated_query,
            "query_intent": query_intent,
            "understanding": understanding,
            "match_hint": match_hint.strip(),
        }

def execute_sql(sql_query: str) -> list:
    """
    Safely executes a SELECT query and returns the list of rows as dictionaries.
    """
    if not sql_query:
        return []

    sql_query = finalize_sql(sql_query)
        
    query_clean = sql_query.strip().lower()
    if not query_clean.startswith("select"):
        raise ValueError("Only SELECT queries are allowed for safety.")
        
    forbidden = ["insert", "update", "delete", "drop", "alter", "create", "replace", "truncate", "schema"]
    for keyword in forbidden:
        if re.search(r'\b' + keyword + r'\b', query_clean):
            raise ValueError(f"Unauthorized keyword '{keyword}' found in SQL query.")

    try:
        return _execute_sql_rows(sql_query)
    except Exception as first_error:
        repaired = finalize_sql(_normalize_sql_dialect(sql_query))
        if repaired != sql_query:
            print(f"[SQL Retry] {first_error}\n  Retrying: {repaired}")
            try:
                return _execute_sql_rows(repaired)
            except Exception:
                pass
        raise first_error

def _execute_sql_rows(sql_query: str) -> list:
    with engine.connect() as conn:
        result = conn.execute(text(sql_query))
        columns = result.keys()
        return [dict(zip(columns, row)) for row in result.fetchall()]

def trim_conversation_history(history: list, max_messages: int = 10) -> list:
    """Keep the most recent conversation turns for context."""
    if not history:
        return []
    cleaned = []
    for msg in history:
        role = (msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")) or ""
        content = (msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")) or ""
        role = role.strip().lower()
        content = content.strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    return cleaned[-max_messages:]

def _format_history_block(history: list) -> str:
    if not history:
        return ""
    lines = []
    for msg in history:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{prefix}: {msg['content']}")
    return "Previous conversation:\n" + "\n".join(lines) + "\n\n"

def detect_query_language(user_query: str) -> str:
    """Detect whether to reply in Urdu script, Roman Urdu, or English."""
    if user_query and any("\u0600" <= c <= "\u06FF" for c in user_query):
        return "urdu_script"
    q = (user_query or "").lower()
    roman_markers = [
        "hai", "hain", "ne", "ki", "kaun", "kon", "aaj", "chutti", "chutiyan", "chhuttiyan",
        "pichle", "pichlay", "saal", "mahine", "kya", "kitni", "kitne", "aaya", "aaye", "log",
        "hazir", "gair", "der", "zyada", "zaida", "kis", "kisne",
    ]
    if sum(1 for m in roman_markers if m in q) >= 1:
        return "roman_urdu"
    return "english"

def _language_instruction(user_query: str, language: str = None) -> str:
    lang = language or detect_query_language(user_query)
    if lang == "urdu_script":
        return (
            "Respond in natural Urdu script. Sound like a friendly HR assistant, not a database report. "
            'Example: "محمد عمر نے پچھلے سال 261 چھٹیاں لیں۔"'
        )
    if lang == "roman_urdu":
        return (
            "Respond in natural Roman Urdu. Sound like a friendly HR assistant, not a database report. "
            'Example: "Mohammad Omer ne pichle saal 261 chutiyan ki hain."'
        )
    return (
        "Respond in natural English. Sound like a friendly HR assistant, not a database report. "
        'Example: "Mohammad Omer took 261 leaves last year."'
    )

def _build_openai_messages(system_prompt: str, history: list, user_content: str) -> list:
    messages = [{"role": "system", "content": system_prompt}]
    for msg in trim_conversation_history(history):
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_content})
    return messages

def _extract_scalar_count(query_results: list):
    """Extract a single numeric COUNT from query results, if present."""
    if not query_results or len(query_results) != 1:
        return None
    row = query_results[0]
    if len(row) == 1:
        val = list(row.values())[0]
        if isinstance(val, (int, float)):
            return int(val)
    for key, val in row.items():
        if "count" in key.lower() and isinstance(val, (int, float)):
            return int(val)
    return None

def _is_leave_count_question(user_query: str) -> bool:
    q = (user_query or "").lower()
    markers = [
        "chutti", "chutiyan", "chhutti", "chhuttiyan", "chhutiyan", "chhuti", "chhutti",
        "chuti", "chuttiyan", "leave", "leaves",
        "چھٹی", "چھٹیاں", "چھوٹیاں", "چھٹیوں", "چھٹي",
    ]
    return any(m in q for m in markers)

def correct_chutti_sql(sql_query: str, user_query: str, metric: str = None) -> str:
    """
    Rewrites leave/chutti SQL to use daily_attendance with correct status filters.
    """
    is_leave = metric == "leaves" or _is_leave_count_question(user_query)
    if not sql_query:
        return sql_query

    fixed = sql_query
    if is_leave and "leave_requests" in fixed.lower():
        fixed = re.sub(r"\bFROM\s+leave_requests\b", "FROM daily_attendance", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"\bleave_requests\.", "daily_attendance.", fixed, flags=re.IGNORECASE)
        fixed = re.sub(
            r"daily_attendance\.status\s*=\s*['\"]Approved['\"]",
            "daily_attendance.status IN ('Absent', 'On Leave')",
            fixed,
            flags=re.IGNORECASE,
        )
        fixed = re.sub(r"daily_attendance\.start_date", "daily_attendance.date", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"daily_attendance\.end_date", "daily_attendance.date", fixed, flags=re.IGNORECASE)

    if is_leave:
        fixed = re.sub(
            r"daily_attendance\.status\s*=\s*'Absent'",
            "daily_attendance.status IN ('Absent', 'On Leave')",
            fixed,
            flags=re.IGNORECASE,
        )
        fixed = re.sub(
            r"daily_attendance\.status\s*=\s*'On Leave'",
            "daily_attendance.status IN ('Absent', 'On Leave')",
            fixed,
            flags=re.IGNORECASE,
        )
        if "daily_attendance.status" not in fixed.lower() and "count(" in fixed.lower():
            fixed = re.sub(
                r"(WHERE\s+)",
                r"\1daily_attendance.status IN ('Absent', 'On Leave') AND ",
                fixed,
                count=1,
                flags=re.IGNORECASE,
            )

    fixed = finalize_sql(fixed)
    if fixed != sql_query:
        print(f"[SQL Chutti Correction]\n  Before: {sql_query}\n  After:  {fixed}")
    return fixed


def fix_exclusion_not_in_case(sql_query: str, excluded_names: list) -> str:
    """PostgreSQL/SQLite name comparisons are case-sensitive — normalize NOT IN exclusions."""
    if not sql_query or not excluded_names:
        return sql_query

    lowered = [name.lower().replace("'", "''") for name in excluded_names]
    in_list = ", ".join(f"'{name}'" for name in lowered)

    sql = re.sub(
        r"LOWER\s*\(\s*employees\.name\s*\)\s+NOT\s+IN\s*\([^)]+\)",
        f"LOWER(employees.name) NOT IN ({in_list})",
        sql_query,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"employees\.name\s+NOT\s+IN\s*\([^)]+\)",
        f"LOWER(employees.name) NOT IN ({in_list})",
        sql,
        flags=re.IGNORECASE,
    )
    if sql != sql_query:
        print(f"[SQL Exclusion Fix] NOT IN -> ({in_list})")
    return sql


def _extract_name_list(query_results: list) -> list[str]:
    names = []
    for row in query_results or []:
        for key, value in row.items():
            key_lower = key.lower()
            if value is not None and ("name" in key_lower or key_lower == "employee"):
                name = str(value).strip()
                if name and name not in names:
                    names.append(name)
                break
    return names


def _join_names_natural(names: list[str], lang: str) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if lang == "urdu_script":
        return "، ".join(names[:-1]) + f" اور {names[-1]}"
    if lang == "roman_urdu":
        return ", ".join(names[:-1]) + f" aur {names[-1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _format_list_answer(
    query_results: list,
    understanding: dict,
    user_query: str,
) -> str | None:
    names = _extract_name_list(query_results)
    lang = understanding.get("language") or detect_query_language(user_query)
    metric = understanding.get("metric") or "other"

    if not names:
        if lang == "urdu_script":
            if metric == "late":
                return "آج کوئی بھی لیٹ نہیں آیا۔"
            if metric == "absent":
                return "آج کوئی بھی غیر حاضر نہیں ہے۔"
            if metric == "present":
                return "آج کوئی بھی حاضر نہیں ہے۔"
        if lang == "roman_urdu":
            if metric == "late":
                return "Aaj koi bhi late nahi aaya."
            if metric == "absent":
                return "Aaj koi bhi absent nahi hai."
            if metric == "present":
                return "Aaj koi bhi present nahi hai."
        return "No matching employees found."

    joined = _join_names_natural(names, lang)
    if lang == "urdu_script":
        if metric == "late":
            return f"آج لیٹ آنے والے: {joined}۔"
        if metric == "absent":
            return f"آج غیر حاضر: {joined}۔"
        if metric == "present":
            return f"آج حاضر: {joined}۔"
        return f"نتائج: {joined}۔"
    if lang == "roman_urdu":
        if metric == "late":
            return f"Aaj late aane wale: {joined}."
        if metric == "absent":
            return f"Aaj absent hain: {joined}."
        if metric == "present":
            return f"Aaj present hain: {joined}."
        return f"Results: {joined}."
    if metric == "late":
        return f"Late today: {joined}."
    if metric == "absent":
        return f"Absent today: {joined}."
    if metric == "present":
        return f"Present today: {joined}."
    return f"Results: {joined}."


def _format_count_answer(
    query_results: list,
    understanding: dict,
    user_query: str,
) -> str | None:
    count = _extract_scalar_count(query_results)
    if count is None:
        count = len(_extract_name_list(query_results))
    lang = understanding.get("language") or detect_query_language(user_query)
    metric = understanding.get("metric") or "other"

    if lang == "urdu_script":
        if metric == "late":
            return f"آج {count} افراد لیٹ آئے۔"
        if metric == "absent":
            return f"آج {count} افراد غیر حاضر ہیں۔"
        if metric in ("leaves", "other"):
            return f"کل {count}۔"
        return f"کل {count}۔"
    if lang == "roman_urdu":
        if metric == "late":
            return f"Aaj {count} log late aaye."
        if metric == "absent":
            return f"Aaj {count} log absent hain."
        if metric in ("leaves", "other"):
            return f"Kul {count}."
        return f"Kul {count}."
    if metric == "late":
        return f"{count} employee(s) were late today."
    if metric == "absent":
        return f"{count} employee(s) are absent today."
    return f"Total: {count}."


def _extract_ranking_row(query_results: list) -> tuple[str | None, int | None]:
    if not query_results:
        return None, None
    row = query_results[0]
    name = None
    count = None
    for key, value in row.items():
        key_lower = key.lower()
        if name is None and ("name" in key_lower or key_lower == "employee"):
            name = value
        if count is None and isinstance(value, (int, float)) and (
            "count" in key_lower or "leave" in key_lower or key_lower.endswith("_count")
        ):
            count = int(value)
    if count is None:
        for key, value in row.items():
            if isinstance(value, (int, float)) and key.lower() not in ("id", "employee_id"):
                count = int(value)
                break
    return (str(name) if name else None), count


def _format_ranking_answer(
    query_results: list,
    understanding: dict,
    user_query: str,
    metric: str = "leaves",
) -> str | None:
    name, count = _extract_ranking_row(query_results)
    if not name:
        return None

    excluded = understanding.get("excluded_employees") or []
    lang = understanding.get("language") or detect_query_language(user_query)
    excluded_clause = ""
    if excluded:
        names = " aur ".join(excluded) if lang == "roman_urdu" else ", ".join(excluded)
        if lang == "urdu_script":
            excluded_clause = f"{names} کے علاوہ، "
        elif lang == "roman_urdu":
            excluded_clause = f"{names} ke ilawa, "
        else:
            excluded_clause = f"Besides {names}, "

    if metric in ("leaves", "other") and count is not None:
        if lang == "urdu_script":
            return f"{excluded_clause}سب سے زیادہ چھٹیاں {name} نے لی ہیں — کل {count}۔"
        if lang == "roman_urdu":
            return f"{excluded_clause}sab se zyada chutiyan {name} ne ki hain — kul {count}."
        return f"{excluded_clause}{name} has the most leave days ({count})."

    if lang == "roman_urdu":
        if metric == "late":
            return f"{excluded_clause}aaj sab se zyada late {name} aaya."
        return f"{excluded_clause}sab se zyada {name}."
    return f"{excluded_clause}top result: {name}."

def synthesize_answer(
    user_query: str,
    query_results: list,
    sql_query: str,
    query_intent: str = "specific_person",
    candidate_matches: list = None,
    conversation_history: list = None,
    resolved_query: str = None,
    understanding: dict = None,
) -> str:
    """
    Synthesizes SQLite query results into a conversational response.
    Differentiates between non-existent employees and absent employees.
    """
    understanding = understanding or {}
    candidate_matches = candidate_matches or understanding.get("employees") or []
    history = trim_conversation_history(conversation_history or [])
    employee_confirmed = bool(candidate_matches) and bool(sql_query) and query_intent != "general"
    count_val = _extract_scalar_count(query_results)
    language_rule = _language_instruction(user_query, understanding.get("language"))
    answer_source = understanding.get("resolved_question") or resolved_query or user_query

    if query_intent == "general" and query_results:
        if _is_count_question(answer_source) or _is_count_question(user_query):
            deterministic = _format_count_answer(query_results, understanding, user_query)
            if deterministic:
                return deterministic
        elif (
            _is_superlative_ranking_question(answer_source, understanding)
            or _is_superlative_ranking_question(user_query, understanding)
            or understanding.get("excluded_employees")
        ):
            deterministic = _format_ranking_answer(
                query_results,
                understanding,
                user_query,
                metric=understanding.get("metric", "other"),
            )
            if deterministic:
                return deterministic
        else:
            deterministic = _format_list_answer(query_results, understanding, user_query)
            if deterministic:
                return deterministic
    elif query_intent == "general" and not query_results:
        empty_list = _format_list_answer([], understanding, user_query)
        if empty_list:
            return empty_list

    client = get_openai_client()
    today = datetime.date.today().isoformat()
    
    system_prompt = f"""You are an intelligent, helpful voice-enabled HR assistant for the CEO of the company.
The CEO asked a question in English, Urdu script, or Roman Urdu. We queried the SQLite database and got results.

Today's date: {today}

CRITICAL RULES FOR CONVERSATIONAL SYNTHESIS:
1. NATURAL LANGUAGE (MOST IMPORTANT):
   - {language_rule}
   - NEVER use robotic phrases like "records found", "record(s) found for your query", "query returned", or "COUNT(*)".
   - Speak naturally as if telling a colleague the answer verbally.

2. GENERAL vs SPECIFIC QUESTIONS:
   - For GENERAL LIST questions (kon kon late, who is absent today): list ALL names from SQL results.
   - For GENERAL RANKING questions (sabse zyada late kaun): answer with the single top result only.
   - For GENERAL COUNT questions (kitne log late): give the number.
   - Results only include currently ACTIVE employees — never mention inactive/former staff for rankings.
   - Name the employee(s) from the results. NEVER say "no employee named [phrase] exists" for general questions.
   - For SPECIFIC-PERSON questions only: apply the non-existent employee rule below.

3. NON-EXISTENT EMPLOYEE VS ZERO RESULTS (CRITICAL):
   - If `employee_confirmed` is True (candidate_matches provided AND sql_query executed), the employee DEFINITELY EXISTS in the database.
   - A COUNT of 0 or empty rows means ZERO matching records (e.g. 0 leaves), NOT that the employee doesn't exist!
   - NEVER say "no employee record found" or "employee doesn't exist" when employee_confirmed is True.
   - Only say employee doesn't exist when sql_query is null and no employee was matched.

4. NON-EXISTENT EMPLOYEE VS ABSENT EMPLOYEE (only when employee_confirmed is False):
   - If the user asked about a specific person who DOES NOT EXIST in the registered employee list, OR if `sql_query` is null because no employee matching that name exists:
     You MUST state clearly in the response language:
     "Database mein [Name] ke naam se koi employee record nahi mila." OR "No employee named [Name] exists in the system."
     DO NOT say "He/She is absent today" for a person who isn't a registered employee!

   - Saying "he/she is absent" is ONLY allowed if the employee actually exists in the database system but was marked Absent or didn't check in today.

5. ACCURATE NUMBERS & EMPLOYEES:
   - Synthesize numbers, dates, and employee names naturally.
   - Use ONLY names and counts present in Query Results JSON — NEVER invent or substitute a different employee name.
   - If Query Results show name=hassan raza, you MUST say hassan raza — never say Farman or any other name.
   - If the employee exists in the database but has no record for today's date, state clearly: "[Employee Name] has not checked in today." / "[Employee Name] ne aaj check-in nahi kiya."

6. CONVERSATION CONTEXT:
   - Use previous conversation messages to resolve follow-up questions (e.g. "what about last month?" refers to the same employee/topic).
   - If a resolved_query is provided, the user asked a follow-up — answer about the same employee/topic in the new time period.
   - Keep answers to 1-3 concise sentences suitable for Text-to-Speech playback.
"""

    prompt_content = (
        f"CEO's Question: {user_query}\n"
        f"Resolved Question (if follow-up): {resolved_query or 'N/A'}\n"
        f"Query Intent: {query_intent}\n"
        f"Employee Confirmed in Database: {employee_confirmed}\n"
        f"Matched Employee Names: {candidate_matches}\n"
        f"SQL Query executed: {sql_query}\n"
        f"Query Results (JSON): {query_results}\n"
        f"Extracted Count (if any): {count_val}"
    )

    try:
        response = client.chat.completions.create(
            model=get_chat_model(fast=True),
            messages=_build_openai_messages(system_prompt, history, prompt_content)
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[OpenAI Synthesize Error] {e}")
        if employee_confirmed and count_val is not None and candidate_matches:
            name = candidate_matches[0]
            if detect_query_language(user_query) == "urdu_script":
                return f"{name} کے لیے درخواست کردہ مدت میں {count_val} چھٹیاں ہیں۔"
            if detect_query_language(user_query) == "roman_urdu":
                return f"{name} ki {count_val} chutiyan hain is muddat mein."
            return f"{name} has {count_val} matching record(s) for that period."
        return "I couldn't process the query results."

def prepare_speech_text(text: str, language: str = None) -> str:
    """
    Convert answer text into a form OpenAI TTS can speak clearly.
    Urdu script sounds garbled on tts-1-hd (English-optimized voices) — romanize for speech only.
    Display text in the app stays in Urdu; only the audio uses Roman Urdu.
    """
    if not text or not text.strip():
        return text or ""

    has_urdu_script = any("\u0600" <= c <= "\u06FF" for c in text)

    # Roman Urdu / English answers are already speakable — skip an extra LLM call.
    if not has_urdu_script:
        return text.strip()

    db_names = get_db_employee_names()
    emp_hints = ", ".join(db_names[:30]) if db_names else "Bashir, Mohammad Omer, Shaharyar"

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=get_chat_model(fast=True),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You prepare HR assistant answers for text-to-speech (OpenAI TTS, English-optimized voice).\n"
                        "Convert the answer into natural spoken Roman Urdu using ONLY Latin/English letters and digits.\n\n"
                        "RULES:\n"
                        "- If input is Urdu script, transliterate fully to Roman Urdu.\n"
                        "- If already Roman Urdu or English, return clean spoken Roman Urdu.\n"
                        f"- Employee names must use exact Latin spellings: {emp_hints}\n"
                        "- Use natural Pakistani Roman Urdu: 'ne', 'ki', 'hain', 'chutiyan', 'saal', 'aaj', 'pichle'.\n"
                        "- Keep numbers as digits (38, 261).\n"
                        "- No Urdu/Arabic script, no special symbols, no markdown.\n"
                        "- Short conversational tone suitable for voice playback.\n"
                        "Return ONLY the speakable text, nothing else."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=250,
        )
        result = _normalize_whitespace(response.choices[0].message.content.strip().strip('"'))
        if result:
            print(f"[TTS Speech Text] '{text[:60]}...' -> '{result}'")
            return result
    except Exception as e:
        print(f"[TTS Speech Text Error] {e}")

    if has_urdu_script:
        fallback = convert_urdu_script_to_latin(text)
        fallback = re.sub(r"[\u0600-\u06FF]+", "", fallback)
        return _normalize_whitespace(fallback) or text
    return text.strip()

def generate_speech(text: str, voice_override: str = None, language: str = None, speech_text: str = None) -> str:
    """
    Converts text to speech audio using OpenAI Speech API (tts-1-hd by default).
    Urdu script answers are romanized for speech; chat display text is unchanged.
    Returns the audio bytes as a base64-encoded MP3 string.
    """
    if not text or not text.strip():
        return ""

    speech_text = speech_text or prepare_speech_text(text, language)
        
    client = get_openai_client()
    voice = voice_override or os.environ.get("OPENAI_TTS_VOICE", "sage").lower()
    valid_voices = ["sage", "nova", "onyx", "alloy", "echo", "fable", "shimmer", "coral", "ash"]
    if voice not in valid_voices:
        voice = "sage"
        
    tts_model = get_openai_tts_model()
    cache_key = _tts_cache_key(speech_text, voice, tts_model)
    cached_audio = _get_cached_tts(cache_key)
    if cached_audio:
        print(f"[TTS Cache] hit ({len(speech_text)} chars)")
        return cached_audio
    
    try:
        response = client.audio.speech.create(
            model=tts_model,
            voice=voice,
            input=speech_text,
            speed=0.95,
        )
        audio_bytes = response.content
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        _set_cached_tts(cache_key, audio_b64)
        return audio_b64
    except Exception as e:
        print(f"[OpenAI TTS Error] {e}")
        raise RuntimeError(f"OpenAI TTS audio generation failed: {e}")
