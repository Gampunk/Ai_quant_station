#!/usr/bin/env python3
"""
One-time backfill: embed existing assistant chat messages for RAG.

Embeddings are normally generated when a new AI response is saved, so chats
created before the RAG feature existed have no embedding row. This script
fills them in (idempotent — skips chat_memory_ids that already exist).

Usage (on the server):
    cd /opt/impulse_analyst/backend
    source venv/bin/activate
    python scripts/backfill_embeddings.py            # dry run, prints plan
    python scripts/backfill_embeddings.py --apply    # actually write rows
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sql_text

from app.core.database import AsyncSessionLocal
from app.core.rag_service import generate_embedding, _clean_for_embedding


async def backfill(apply: bool, limit: int | None = None) -> None:
    async with AsyncSessionLocal() as db:
        pending = await db.execute(
            sql_text(
                """
                SELECT c.id, c.content
                FROM chat_memories c
                LEFT JOIN chat_embeddings e ON e.chat_memory_id = c.id
                WHERE c.role = 'assistant'
                  AND c.content IS NOT NULL
                  AND c.content <> ''
                  AND e.id IS NULL
                ORDER BY c.created_at ASC
                """
            )
        )
        rows = pending.fetchall()

    if limit:
        rows = rows[:limit]

    # Chats that are 100% code blocks strip to nothing — generate_embedding()
    # skips them silently (by design), so report them truthfully instead of
    # counting them as embedded while the DB stays short.
    embeddable, skipped_ids = [], []
    for r in rows:
        if _clean_for_embedding(r.content or ""):
            embeddable.append(r)
        else:
            skipped_ids.append(r.id)
    if skipped_ids:
        print(f"Skipped (code-only, no embeddable text): ids {skipped_ids}")
    rows = embeddable

    print(f"Embeddings to create: {len(rows)}")
    if not rows:
        print("Nothing to do.")
        return
    if not apply:
        print("Dry run. Re-run with --apply to write rows.")
        return

    ok = failed = 0
    for idx, row in enumerate(rows, 1):
        try:
            await generate_embedding(row.id, row.content)
            ok += 1
            print(f"  [{idx}/{len(rows)}] embedded chat_memory_id={row.id}")
        except Exception as e:
            failed += 1
            print(f"  [{idx}/{len(rows)}] FAILED chat_memory_id={row.id}: {e}")

    print(f"Done. embedded={ok} failed={failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill chat embeddings for RAG")
    parser.add_argument("--apply", action="store_true", help="Actually write rows (default is dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process")
    args = parser.parse_args()

    asyncio.run(backfill(apply=args.apply, limit=args.limit))


if __name__ == "__main__":
    main()
