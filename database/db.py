import json
import logging
from typing import Optional, List, Dict, Any
import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

async def init_db():
    """Initializes the database schema."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("PRAGMA journal_mode = WAL;")
        
        # Table: users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                full_name TEXT,
                class_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Table: tests
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                test_type TEXT NOT NULL DEFAULT 'regular',
                is_active INTEGER DEFAULT 1,
                time_limit_minutes INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Table: questions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                order_num INTEGER NOT NULL DEFAULT 1,
                question_text TEXT NOT NULL,
                context_text TEXT,
                options_json TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                explanation TEXT
            );
        """)

        # Table: submissions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                score INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                answers_json TEXT,
                time_spent_seconds INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP
            );
        """)

        # Migration: ensure status and confirmed_at exist on existing installations
        async with db.execute("PRAGMA table_info(submissions)") as cursor:
            existing_cols = [row[1] for row in await cursor.fetchall()]
            if "status" not in existing_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';")
            if "confirmed_at" not in existing_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN confirmed_at TIMESTAMP;")
        
        await db.commit()
        logger.info("Database initialized successfully at %s", DB_PATH)


# --- User CRUD ---
async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def register_student(telegram_id: int, full_name: str, class_name: str) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT INTO users (telegram_id, role, full_name, class_name)
            VALUES (?, 'student', ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                class_name = excluded.class_name
        """, (telegram_id, full_name, class_name))
        await db.commit()
        return await get_user_by_telegram_id(telegram_id)


# --- Test CRUD ---
async def create_test_with_questions(
    title: str,
    test_type: str,
    created_by: int,
    questions: List[Dict[str, Any]],
    description: str = "",
    time_limit_minutes: int = 0
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("""
            INSERT INTO tests (title, description, test_type, is_active, time_limit_minutes, created_by)
            VALUES (?, ?, ?, 1, ?, ?)
        """, (title, description, test_type, time_limit_minutes, created_by))
        test_id = cursor.lastrowid

        for idx, q in enumerate(questions, start=1):
            await db.execute("""
                INSERT INTO questions (test_id, order_num, question_text, context_text, options_json, correct_answer, explanation)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                test_id,
                idx,
                q.get("question_text", ""),
                q.get("context_text"),
                json.dumps(q.get("options", []), ensure_ascii=False),
                str(q.get("correct_answer", "")).strip(),
                q.get("explanation")
            ))

        await db.commit()
        return test_id

async def get_tests(test_type: Optional[str] = None, only_active: bool = True) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM tests WHERE 1=1"
        params = []
        if test_type:
            query += " AND test_type = ?"
            params.append(test_type)
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY id DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def get_tests_with_stats() -> List[Dict[str, Any]]:
    """Returns all tests with submission statistics (total, pending, confirmed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT 
                t.id, t.title, t.description, t.test_type, t.is_active, t.created_at,
                COUNT(s.id) as total_submissions,
                COALESCE(SUM(CASE WHEN s.status = 'pending' THEN 1 ELSE 0 END), 0) as pending_count,
                COALESCE(SUM(CASE WHEN s.status = 'confirmed' THEN 1 ELSE 0 END), 0) as confirmed_count
            FROM tests t
            LEFT JOIN submissions s ON t.id = s.test_id
            GROUP BY t.id
            ORDER BY t.id DESC
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def get_test_by_id(test_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tests WHERE id = ?", (test_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def toggle_test_active(test_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE tests SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?
        """, (test_id,))
        await db.commit()
        async with db.execute("SELECT is_active FROM tests WHERE id = ?", (test_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def delete_test(test_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM tests WHERE id = ?", (test_id,))
        await db.commit()

async def get_test_questions(test_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM questions WHERE test_id = ? ORDER BY order_num ASC", (test_id,)) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                item = dict(r)
                item["options"] = json.loads(item["options_json"]) if item["options_json"] else []
                result.append(item)
            return result


# --- Submissions CRUD ---
async def save_submission(
    student_id: int,
    test_id: int,
    score: int,
    total_questions: int,
    answers: List[Dict[str, Any]],
    time_spent_seconds: int = 0
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO submissions (student_id, test_id, score, total_questions, answers_json, time_spent_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            student_id,
            test_id,
            score,
            total_questions,
            json.dumps(answers, ensure_ascii=False),
            time_spent_seconds
        ))
        await db.commit()
        return cursor.lastrowid

async def get_student_submission(student_id: int, test_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM submissions WHERE student_id = ? AND test_id = ? ORDER BY id DESC LIMIT 1
        """, (student_id, test_id)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_student_submissions(student_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, t.title as test_title, t.test_type
            FROM submissions s
            JOIN tests t ON s.test_id = t.id
            WHERE s.student_id = ?
            ORDER BY s.id DESC
        """, (student_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def get_test_submissions(test_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, u.full_name, u.class_name, u.telegram_id
            FROM submissions s
            JOIN users u ON s.student_id = u.id
            WHERE s.test_id = ?
            ORDER BY (CASE WHEN s.status = 'pending' THEN 0 ELSE 1 END), s.completed_at DESC
        """, (test_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def confirm_submission(submission_id: int) -> Optional[Dict[str, Any]]:
    """Marks a single submission as confirmed and returns updated submission info with student and test details."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            UPDATE submissions 
            SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (submission_id,))
        await db.commit()
        
        async with db.execute("""
            SELECT s.*, u.full_name, u.class_name, u.telegram_id, t.title as test_title
            FROM submissions s
            JOIN users u ON s.student_id = u.id
            JOIN tests t ON s.test_id = t.id
            WHERE s.id = ?
        """, (submission_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def confirm_all_test_submissions(test_id: int) -> List[Dict[str, Any]]:
    """Marks all pending submissions of a test as confirmed and returns list of updated submissions."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.id, s.score, s.total_questions, u.telegram_id, u.full_name, t.title as test_title
            FROM submissions s
            JOIN users u ON s.student_id = u.id
            JOIN tests t ON s.test_id = t.id
            WHERE s.test_id = ? AND s.status = 'pending'
        """, (test_id,)) as cursor:
            pending_rows = [dict(r) for r in await cursor.fetchall()]

        await db.execute("""
            UPDATE submissions 
            SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP
            WHERE test_id = ? AND status = 'pending'
        """, (test_id,))
        await db.commit()
        return pending_rows
