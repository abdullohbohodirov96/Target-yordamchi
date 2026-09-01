"""
create_manager.py — birinchi admin hisobni (yoki keyingi menejerlarni)
buyruq qatoridan yaratish uchun. Render'da "Shell" bo'limidan bir marta
ishga tushiriladi:

    python scripts/create_manager.py --username admin --password xxxxx \
        --full-name "Abdulloh" --role admin

Keyingi menejerlarni esa admin o'zi web panel (/managers) orqali qo'sha oladi.
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module  # noqa: E402
from db import init_db, get_session, Manager  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--full-name", default="")
    parser.add_argument("--role", default="admin", choices=["admin", "manager"])
    args = parser.parse_args()

    init_db()
    session = get_session()
    try:
        with db_module.unscoped():
            existing = session.query(Manager).filter_by(username=args.username).first()
        if existing:
            print(f"XATO: '{args.username}' allaqachon mavjud.")
            return
        # 2026-09 multi-tenant 2-bosqich: bu skript birinchi (bootstrap)
        # admin hisobini yaratadi -- `init_db()` yuqorida standart
        # kompaniyani allaqachon yaratib qo'ygan, shu kompaniyaga bog'laymiz.
        m = Manager(
            username=args.username, full_name=args.full_name, role=args.role,
            company_id=db_module.get_default_company_id(),
        )
        m.set_password(args.password)
        session.add(m)
        session.commit()
        print(f"OK: '{args.username}' ({args.role}) yaratildi.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
