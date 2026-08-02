import getpass
import os
import subprocess
from pathlib import Path


def connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: ./scripts/setup_python.sh") from exc

    database_url = os.getenv("OUTBOX_DATABASE_URL")
    if database_url:
        return psycopg.connect(database_url)

    required = ["OUTBOX_DB_HOST", "OUTBOX_DB_NAME", "OUTBOX_DB_USER"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing Outbox database configuration: " + ", ".join(missing))

    password = os.getenv("OUTBOX_DB_PASSWORD")
    keychain_service = os.getenv("OUTBOX_KEYCHAIN_SERVICE")
    if password is None and keychain_service:
        result = subprocess.run(
            [
                "security", "find-generic-password", "-w",
                "-s", keychain_service,
                "-a", os.environ["OUTBOX_DB_USER"],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            password = result.stdout.rstrip("\n")
    if password is None and os.isatty(0):
        password = getpass.getpass("Outbox database password (not stored): ")
    if password is None:
        raise RuntimeError("OUTBOX_DB_PASSWORD is required in non-interactive mode")

    return psycopg.connect(
        host=os.environ["OUTBOX_DB_HOST"],
        port=int(os.getenv("OUTBOX_DB_PORT", "5432")),
        dbname=os.environ["OUTBOX_DB_NAME"],
        user=os.environ["OUTBOX_DB_USER"],
        password=password,
        sslmode=os.getenv("OUTBOX_DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("OUTBOX_DB_CONNECT_TIMEOUT", "5")),
    )


def apply_migrations(project_dir: Path):
    migration_dir = project_dir / "migrations"
    paths = sorted(migration_dir.glob("*.sql"))
    if not paths:
        raise RuntimeError(f"No migrations found in {migration_dir}")
    with connect() as conn:
        with conn.cursor() as cursor:
            for path in paths:
                cursor.execute(path.read_text(encoding="utf-8"))
    return paths
