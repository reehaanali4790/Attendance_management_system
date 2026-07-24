import os
import tempfile
import base64
import datetime
import json
import re
import difflib
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
})

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
   - general: who is late/absent, sabse zyada late kaun, sabse zyada chutiyan, kitne log, ranking across active employees
   - specific_person: question about one named active employee's attendance/leaves/status

4. EMPLOYEES: List only employees from the registered list above. Use exact Latin spellings from the list.
   NEVER output Urdu script in the employees array — only Latin/English names from the list.
   For Urdu/Roman variants (محمد امر, mohammad umer, shehryar) map to the correct DB name (Mohammad Omer, Shaharyar).
   Leave employees [] for general questions.

5. METRIC values: leaves, late, present, absent, work_hours, ranking, count, check_in, other

6. TIME_PERIOD values: today, yesterday, this_year, last_year, this_month, last_month, other, null

7. LANGUAGE: urdu_script | roman_urdu | english (match the user's question language)

Return JSON with keys:
- resolved_question (string): complete standalone question
- intent ("general" | "specific_person")
- employees (array of exact DB employee names, or [])
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
        if not validated:
            validated = resolve_employees_from_text(resolved, db_names)
        if not validated:
            validated = resolve_employees_from_text(latin_query, db_names)

        intent = raw.get("intent", "specific_person")
        if intent not in ("general", "specific_person"):
            intent = "general" if not validated else "specific_person"
        elif validated and intent == "general" and suggested_employees:
            intent = "specific_person"

        understanding = {
            "resolved_question": resolved,
            "intent": intent,
            "employees": validated,
            "metric": raw.get("metric") or "other",
            "time_period": raw.get("time_period"),
            "is_follow_up": bool(raw.get("is_follow_up")),
            "language": raw.get("language") or detect_query_language(user_query),
            "reasoning": raw.get("reasoning", ""),
            "original_query": user_query,
            "latin_query": latin_query if latin_query != (user_query or "") else None,
        }
        print(f"[Understand] '{user_query}' -> intent={intent}, employees={validated}, resolved='{resolved}'")
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

def _active_employee_predicate() -> str:
    if DATABASE_URL.startswith("sqlite"):
        return "employees.is_active = 1"
    return "employees.is_active IS TRUE"

def _normalize_sql_dialect(sql_query: str) -> str:
    """Adjust SQLite-style literals for PostgreSQL when needed."""
    if DATABASE_URL.startswith("sqlite"):
        return sql_query
    sql = re.sub(r"\bemployees\.is_active\s*=\s*1\b", "employees.is_active IS TRUE", sql_query, flags=re.IGNORECASE)
    sql = re.sub(r"\bis_active\s*=\s*1\b", "is_active IS TRUE", sql, flags=re.IGNORECASE)
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

    if re.search(r"employees\.is_active\s*(=|is)", sql_lower):
        return sql_query

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
    
    db_employees = get_db_employee_names()
    emp_list_str = "\n".join([f"- {name}" for name in db_employees]) if db_employees else "- Mohammad Omer\n- Shaharyar\n- Ali\n- Ahmed"
    
    date_context = (
        f"Today's date: {today.isoformat()} ({today.strftime('%A')})\n"
        f"Current year range ('this year' / 'is saal'): {first_day_of_year} to {last_day_of_year}\n"
        f"First day of current month ('this month' / 'is mahine'): {first_day_of_month.isoformat()}\n"
        f"Last month range ('last month' / 'pichle mahine'): {first_day_of_last_month.isoformat()} to {last_month_end.isoformat()}\n"
        f"Last year range ('last year' / 'pichle saal' / 'pichlay saal'): {first_day_of_last_year} to {last_day_of_last_year}\n"
    )
    
    return f"""You are a senior SQLite database expert for an employee attendance management system.
Your job is to translate natural language questions (asked in English, Urdu script, or Roman Urdu) into a valid SQLite SELECT query.

Here is the SQLite schema:

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

6. SUPERLATIVE & RANKING QUERIES:
   - "sabse zyada late" / "most late" / "latest arrival" / "sab se zyada der se":
     Filter today's late records, ORDER BY `daily_attendance.late_minutes DESC` or `daily_attendance.check_in DESC`, LIMIT 1.
     Example: `SELECT employees.name, daily_attendance.late_minutes, daily_attendance.check_in FROM daily_attendance JOIN employees ON daily_attendance.employee_id = employees.id WHERE daily_attendance.date = '{today.isoformat()}' AND daily_attendance.status = 'Late' ORDER BY daily_attendance.late_minutes DESC LIMIT 1`
   - "sabse zyada work hours" / "highest work hours":
     ORDER BY `daily_attendance.work_hours DESC` with appropriate date filter.
   - "kitne log late" / "how many late":
     `SELECT COUNT(*) ... WHERE status = 'Late' AND date = today`

7. MULTILINGUAL & ROMAN URDU VOCABULARY MAPPING:
   - "chutiyan" / "chutti" / "chuti" / "chhuttiyan" / "gair hazir" / "absent" / "leaves" / "off":
     ALWAYS count from `daily_attendance` with `status IN ('Absent', 'On Leave')`.
     Do NOT use `leave_requests` for chutti/chutiyan counts — that table is often empty; real absence data lives in `daily_attendance`.
     Example: `SELECT COUNT(*) FROM daily_attendance JOIN employees ON daily_attendance.employee_id = employees.id WHERE employees.name = 'Mohammad Omer' AND daily_attendance.status IN ('Absent', 'On Leave') AND daily_attendance.date BETWEEN '{first_day_of_last_year}' AND '{last_day_of_last_year}'`
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
     `daily_attendance.date = date('{today.isoformat()}', '-1 day')`.

6. JOINING & SELECTING FIELDS:
   - Always join `daily_attendance` with `employees` on `daily_attendance.employee_id = employees.id`.
   - Always select `employees.name` alongside counts or date details so the synthesis step knows the matched employee's exact name.
   - ALWAYS add `AND employees.is_active = 1` (or `employees.is_active IS TRUE`) on every query that references the employees table.
   - Rankings like "sabse zyada chutiyan/late/work hours" must only compare ACTIVE employees, never people who left the company.

7. ACTIVE EMPLOYEES ONLY (CRITICAL):
   - Company-wide counts, rankings, and "who has the most" questions apply ONLY to active staff.
   - Inactive employees may have old attendance rows — those rows must NOT appear in rankings or "sabse zyada" answers.
   - Unless the user explicitly asks about inactive/former/left employees, always filter `employees.is_active = 1`.

8. FOLLOW-UP & CONVERSATIONAL QUESTIONS (CRITICAL):
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
    metric = understanding.get("metric", "other")

    if not candidate_matches and query_intent == "specific_person":
        candidate_matches = resolve_employees_from_text(resolved_query) or resolve_employees_from_text(latin_query)
        if candidate_matches:
            understanding["employees"] = candidate_matches
            query_intent = "specific_person"

    # Build context hint for SQL generation from understanding (not regex rules)
    if query_intent == "general":
        match_hint = (
            f"\nUNDERSTANDING: GENERAL question — metric={metric}, time_period={understanding.get('time_period')}. "
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
            normalized = _normalize_sql_dialect(result["sql"])
            normalized = ensure_employees_join_for_attendance(normalized)
            result["sql"] = ensure_active_employees_filter(normalized)

        result["candidate_matches"] = candidate_matches
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

    sql_query = ensure_employees_join_for_attendance(_normalize_sql_dialect(sql_query))
    sql_query = ensure_active_employees_filter(sql_query)
        
    query_clean = sql_query.strip().lower()
    if not query_clean.startswith("select"):
        raise ValueError("Only SELECT queries are allowed for safety.")
        
    forbidden = ["insert", "update", "delete", "drop", "alter", "create", "replace", "truncate", "schema"]
    for keyword in forbidden:
        if re.search(r'\b' + keyword + r'\b', query_clean):
            raise ValueError(f"Unauthorized keyword '{keyword}' found in SQL query.")
            
    with engine.connect() as conn:
        result = conn.execute(text(sql_query))
        columns = result.keys()
        rows = [dict(zip(columns, row)) for row in result.fetchall()]
        
    return rows

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
        "chutti", "chutiyan", "chhutti", "chhuttiyan", "chhutiyan", "leave", "leaves", "gair hazir",
        "چھٹی", "چھٹیاں", "چھوٹیاں", "چھٹیوں", "چھٹي", "چھٹیاں",
    ]
    return any(m in q for m in markers)

def correct_chutti_sql(sql_query: str, user_query: str, metric: str = None) -> str:
    """
    Rewrites leave_requests-based chutti counts to daily_attendance,
    where actual absence/leave records are stored.
    """
    is_leave = metric == "leaves" or _is_leave_count_question(user_query)
    if not sql_query or not is_leave:
        return sql_query
    if "leave_requests" not in sql_query.lower():
        return sql_query

    fixed = sql_query
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
    fixed = ensure_employees_join_for_attendance(_normalize_sql_dialect(fixed))
    fixed = ensure_active_employees_filter(fixed)
    if fixed != sql_query:
        print(f"[SQL Chutti Correction] leave_requests -> daily_attendance\n  Before: {sql_query}\n  After:  {fixed}")
    return fixed

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
    employee_confirmed = bool(candidate_matches) and bool(sql_query)
    count_val = _extract_scalar_count(query_results)
    language_rule = _language_instruction(user_query, understanding.get("language"))

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
   - For GENERAL questions (who is late, who was most late, sabse zyada chutiyan, how many absent): answer with the SQL results directly.
     Results only include currently ACTIVE employees — never mention inactive/former staff for rankings.
     Name the employee(s) from the results. NEVER say "no employee named [phrase] exists" for general questions.
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
    lang = language or detect_query_language(text)

    if not has_urdu_script and lang == "english":
        return text.strip()

    db_names = get_db_employee_names()
    emp_hints = ", ".join(db_names[:30]) if db_names else "Bashir, Mohammad Omer, Shaharyar"

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=get_chat_model(),
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
    
    try:
        response = client.audio.speech.create(
            model=tts_model,
            voice=voice,
            input=speech_text,
            speed=0.95,
        )
        audio_bytes = response.content
        return base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        print(f"[OpenAI TTS Error] {e}")
        raise RuntimeError(f"OpenAI TTS audio generation failed: {e}")
