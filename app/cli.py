"""Command-line admin tools.

    python -m app.cli create-user jdoe --name "Jane Doe" --faculty
    python -m app.cli import-folder /path/to/docs [--category "Protocols & SOPs"] [--view-only]
    python -m app.cli reindex
    python -m app.cli list-users
    python -m app.cli reset-password jdoe
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import auth, db, documents
from .ingest import SUPPORTED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cu = sub.add_parser("create-user", help="Create a user account")
    cu.add_argument("username")
    cu.add_argument("--name", default="")
    cu.add_argument("--faculty", action="store_true", help="Give faculty (admin) access")
    cu.add_argument("--password", help="Set a password (otherwise you are prompted; leave blank for a temporary one)")

    imp = sub.add_parser("import-folder", help="Add every PDF/Word/PowerPoint/text file in a folder")
    imp.add_argument("folder")
    imp.add_argument("--category", default="", help="Force one category (default: guessed from filename)")
    imp.add_argument("--view-only", action="store_true", help="Mark imported documents as not downloadable")
    imp.add_argument("--actor", default="import")

    sub.add_parser("reindex", help="Re-extract and re-index all current files")
    sub.add_parser("list-users")
    rp = sub.add_parser("reset-password")
    rp.add_argument("username")

    args = parser.parse_args(argv)
    db.init_db()

    if args.cmd == "create-user":
        pw = args.password
        if pw is None and sys.stdin.isatty():
            pw = getpass.getpass("Password (blank = generate a temporary one): ") or None
        if pw and (problem := auth.password_problem(pw)):
            print(problem)
            return 1
        user, used = auth.create_user(args.username, args.name or args.username,
                                      "faculty" if args.faculty else "student", pw, must_change=pw is None)
        print(f"Created {user['role']} '{user['username']}'.")
        if not pw:
            print(f"Temporary password: {used}  (they will be asked to change it)")
        return 0

    if args.cmd == "import-folder":
        folder = Path(args.folder)
        files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                       and not p.name.startswith(("~$", ".")))
        if not files:
            print("No supported files found.")
            return 1
        ok = 0
        for p in files:
            data = p.read_bytes()
            dup = documents.find_duplicate(data)
            if dup:
                print(f"  skip  {p.name}  (already in library as '{dup['title']}')")
                continue
            try:
                res = documents.add_document(p.name, data, actor=args.actor, category=args.category or None,
                                             downloadable=not args.view_only)
                ok += 1
                warn = f"  ⚠ {' '.join(res['warnings'])}" if res["warnings"] else ""
                print(f"  added {p.name}  → {res['passages']} passages{warn}")
            except Exception as exc:  # keep going
                print(f"  FAIL  {p.name}: {exc}")
        print(f"Imported {ok} of {len(files)} files.")
        return 0

    if args.cmd == "reindex":
        print(f"Re-indexed {documents.reindex_all()} passages.")
        return 0

    if args.cmd == "list-users":
        for u in db.rows("SELECT username, display_name, role, active, last_login FROM users ORDER BY role, username"):
            print(f"{u['role']:8} {'active' if u['active'] else 'disabled':8} {u['username']:24} {u['display_name']}")
        return 0

    if args.cmd == "reset-password":
        u = db.row("SELECT * FROM users WHERE username=?", (args.username.lower(),))
        if not u:
            print("No such user.")
            return 1
        pw = auth.temp_password()
        db.get_conn().execute("UPDATE users SET pw_hash=?, must_change_pw=1, session_version=session_version+1 WHERE id=?",
                              (auth.hash_password(pw), u["id"]))
        print(f"Temporary password for {u['username']}: {pw}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
