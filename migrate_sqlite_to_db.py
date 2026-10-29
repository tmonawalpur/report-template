#!/usr/bin/env python3
"""Copy data from a SQLite deck.db into the target database (e.g. the
PostgreSQL database the deployed app uses).

The target is chosen exactly like the app: the DATABASE_URL environment
variable (or --database-url). The source is a SQLite file (--source,
default ./deck.db). Users, decks, slides and images are copied preserving
their primary keys and relationships; on PostgreSQL the id sequences are
bumped afterwards so later inserts don't collide.

Examples
--------
# Dry run: show what would be copied, change nothing
python migrate_sqlite_to_db.py --source deck.db --dry-run \
    --database-url "postgresql://user:pass@host:5432/db"

# Copy only the real demo deck (id 1), clearing the target first
DATABASE_URL="postgresql://user:pass@host:5432/db" \
    python migrate_sqlite_to_db.py --decks 1 --wipe

Run it with the target DATABASE_URL in your environment (locally, or via
`railway run python migrate_sqlite_to_db.py ...` so Railway injects the
Postgres URL). Use --wipe to replace the target's contents; without it the
script refuses to run against a non-empty target to avoid key collisions.
"""
import argparse
import os
import re
import sqlite3
import sys

IMG_REF = re.compile(r"/api/images/(\d+)")


def read_source(path, only_decks=None):
    """Read rows from the source SQLite DB, optionally limited to some decks."""
    if not os.path.exists(path):
        sys.exit(f"source SQLite file not found: {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    decks = [dict(r) for r in con.execute("SELECT * FROM decks")]
    if only_decks is not None:
        decks = [d for d in decks if d["id"] in only_decks]
    deck_ids = {d["id"] for d in decks}

    all_slides = [dict(r) for r in con.execute("SELECT * FROM slides")]
    slides = [s for s in all_slides if s["deck_id"] in deck_ids]

    # Images actually referenced by the slides we're copying.
    ref_img = set()
    for s in slides:
        for m in IMG_REF.finditer(s.get("content") or ""):
            ref_img.add(int(m.group(1)))
    all_images = [dict(r) for r in con.execute("SELECT * FROM images")]
    images = all_images if only_decks is None else [
        im for im in all_images if im["id"] in ref_img]

    # Users we must keep: owners of the decks and images we're copying.
    all_users = [dict(r) for r in con.execute("SELECT * FROM users")]
    if only_decks is None:
        users = all_users
    else:
        needed = {d["owner_id"] for d in decks if d["owner_id"] is not None}
        needed |= {im["owner_id"] for im in images if im["owner_id"] is not None}
        users = [u for u in all_users if u["id"] in needed]

    con.close()
    return users, decks, slides, images


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="deck.db", help="source SQLite file")
    ap.add_argument("--database-url", default=None,
                    help="target DB URL (else uses DATABASE_URL env)")
    ap.add_argument("--decks", default=None,
                    help="comma-separated deck ids to copy (default: all)")
    ap.add_argument("--wipe", action="store_true",
                    help="delete all rows in the target before copying")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be copied; make no changes")
    args = ap.parse_args()

    target_url = args.database_url or os.environ.get("DATABASE_URL", "").strip()
    if not target_url:
        sys.exit("no target DB: pass --database-url or set DATABASE_URL")

    only = None
    if args.decks:
        only = {int(x) for x in args.decks.split(",") if x.strip()}

    users, decks, slides, images = read_source(args.source, only)

    # Drop owner references that point at users we aren't bringing along.
    keep_uids = {u["id"] for u in users}
    for d in decks:
        if d["owner_id"] is not None and d["owner_id"] not in keep_uids:
            d["owner_id"] = None
    for im in images:
        if im["owner_id"] is not None and im["owner_id"] not in keep_uids:
            im["owner_id"] = None

    print(f"source {args.source}: "
          f"{len(users)} users, {len(decks)} decks, "
          f"{len(slides)} slides, {len(images)} images")
    print(f"target: {target_url.split('@')[-1] if '@' in target_url else target_url}")

    if args.dry_run:
        for d in decks:
            print(f"  deck {d['id']}: {d['title']!r} "
                  f"(owner={d['owner_id']}, public={d['is_public']})")
        print("dry run — nothing written")
        return

    # Point the app at the target and import its models WITHOUT seeding.
    os.environ["DATABASE_URL"] = target_url
    os.environ["AUTO_INIT_DB"] = "0"
    import app as A
    from sqlalchemy import text

    with A.app.app_context():
        A.db.create_all()
        is_pg = A.db.engine.dialect.name == "postgresql"

        existing = A.Deck.query.count() + A.User.query.count()
        if existing and not args.wipe:
            sys.exit("target is not empty; pass --wipe to replace its contents")

        if args.wipe:
            # Child tables first to respect foreign keys.
            for model in (A.Slide, A.Image, A.Deck, A.User):
                A.db.session.query(model).delete()
            A.db.session.commit()

        for u in users:
            A.db.session.add(A.User(id=u["id"], username=u["username"],
                                    email=u["email"],
                                    password_hash=u["password_hash"]))
        for d in decks:
            deck = A.Deck(id=d["id"], owner_id=d["owner_id"],
                          title=d.get("title") or "",
                          is_public=bool(d["is_public"]))
            for f in A.META_FIELDS:
                if f in d and d[f] is not None:
                    setattr(deck, f, d[f])
            A.db.session.add(deck)
        for s in slides:
            A.db.session.add(A.Slide(id=s["id"], deck_id=s["deck_id"],
                                     position=s["position"], type=s["type"],
                                     title=s.get("title") or "",
                                     content=s.get("content") or "{}"))
        for im in images:
            A.db.session.add(A.Image(id=im["id"], owner_id=im["owner_id"],
                                     filename=im.get("filename") or "",
                                     mimetype=im.get("mimetype") or "image/png",
                                     data=im["data"]))
        A.db.session.commit()

        # On PostgreSQL the id sequences don't know about the explicit ids we
        # just inserted; bump each to max(id) so new rows get fresh ids.
        if is_pg:
            for tbl in ("users", "decks", "slides", "images"):
                A.db.session.execute(text(
                    "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
                    "COALESCE((SELECT MAX(id) FROM " + tbl + "), 1), true)"),
                    {"t": tbl})
            A.db.session.commit()

    print(f"imported {len(users)} users, {len(decks)} decks, "
          f"{len(slides)} slides, {len(images)} images")


if __name__ == "__main__":
    main()
