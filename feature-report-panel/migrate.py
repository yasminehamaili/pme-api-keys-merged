"""Apply migrations/*.sql in order.  Usage: python migrate.py"""
import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


async def apply() -> None:
    load_dotenv()
    dsn = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    try:
        for f in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            await conn.execute(f.read_text())
            print(f"applied {f.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(apply())
