from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

DB_PATH = PROJECT_ROOT / "feedback_system.db"
CHROMA_DIR = PROJECT_ROOT / "chromadb"
DOCUMENTS_DIR = PROJECT_ROOT / "documents"
SCHEMA_PATH = PACKAGE_ROOT / "setup" / "schema.sql"
