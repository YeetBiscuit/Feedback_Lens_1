import sqlite3
import unittest

from feedback_lens.db.connection import ensure_schema_updates, seed_local_demo_accounts
from feedback_lens.paths import SCHEMA_PATH


def _connect_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


class ProductFoundationTests(unittest.TestCase):
    def test_frontend_role_columns_are_in_the_schema(self) -> None:
        with _connect_schema() as conn:
            unit_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(units)")
            }
            user_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            }

        self.assertTrue(
            {"faculty", "academic_level", "is_archived"} <= unit_columns
        )
        self.assertIn("student_identifier", user_columns)

    def test_schema_updates_migrate_existing_frontend_databases(self) -> None:
        with _connect_schema() as conn:
            conn.execute("ALTER TABLE units DROP COLUMN faculty")
            conn.execute("ALTER TABLE units DROP COLUMN academic_level")
            conn.execute("ALTER TABLE units DROP COLUMN is_archived")
            conn.execute("ALTER TABLE users DROP COLUMN student_identifier")

            ensure_schema_updates(conn)

            unit_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(units)")
            }
            user_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            }

        self.assertTrue(
            {"faculty", "academic_level", "is_archived"} <= unit_columns
        )
        self.assertIn("student_identifier", user_columns)

    def test_demo_accounts_cover_every_frontend_role(self) -> None:
        with _connect_schema() as conn:
            seed_local_demo_accounts(conn)
            accounts = {
                row["role"]: dict(row)
                for row in conn.execute(
                    """
                    SELECT email, role, student_identifier
                    FROM users
                    ORDER BY role
                    """
                )
            }

        self.assertEqual(
            {"admin", "lead_lecturer", "educator", "student"},
            set(accounts),
        )
        self.assertEqual(
            "DEMO-STUDENT-001",
            accounts["student"]["student_identifier"],
        )


if __name__ == "__main__":
    unittest.main()
