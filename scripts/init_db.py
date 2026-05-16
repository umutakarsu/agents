"""Apply schema.sql. Idempotent (CREATE ... IF NOT EXISTS)."""

from pathlib import Path

from memlayer.db import connect

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def main() -> None:
    sql = SCHEMA.read_text()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    print("schema applied")


if __name__ == "__main__":
    main()
