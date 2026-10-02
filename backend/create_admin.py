"""
Create a user account, or reset an existing account's password.

The password is typed at a prompt and never stored in this file or shown.

    python create_admin.py                                              # the admin account
    python create_admin.py --username meet_rao --name "Meet Rao" --role trader
    python create_admin.py --username guest --name "Guest" --role viewer
    python create_admin.py --username admin --reset                     # new password
"""
import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select  # noqa: E402

from app.core.config import admin_password_problem  # noqa: E402
from app.core.database import AsyncSessionLocal, init_db  # noqa: E402
from app.core.security import get_password_hash, set_password  # noqa: E402
from app.models.user import User  # noqa: E402

ROLES = ("admin", "trader", "viewer")


def ask_password() -> str:
    while True:
        first = getpass.getpass("Password: ")
        problem = admin_password_problem(first)
        if problem:
            print(f"That password {problem}. Try again.")
            continue
        if getpass.getpass("Repeat password: ") != first:
            print("The two entries did not match. Try again.")
            continue
        return first


async def main(args) -> int:
    await init_db()
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == args.username))).scalar_one_or_none()

        if user and not args.reset:
            print(f"'{args.username}' already exists. Use --reset to set a new password.")
            return 1
        if not user and args.reset:
            print(f"'{args.username}' does not exist, so there is nothing to reset.")
            return 1

        password = ask_password()
        if user:
            set_password(user, password)
            await session.commit()
            print(f"Password for '{args.username}' updated. Everyone logged in as '{args.username}' must log in again.")
        else:
            session.add(User(username=args.username, name=args.name or args.username,
                             role=args.role, hashed_password=get_password_hash(password)))
            await session.commit()
            print(f"Created '{args.username}' with role {args.role}.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--name", default=None, help="display name, defaults to the username")
    parser.add_argument("--role", choices=ROLES, default=None,
                        help="defaults to admin for the admin account, otherwise trader")
    parser.add_argument("--reset", action="store_true", help="set a new password on an existing account")
    a = parser.parse_args()
    if a.role is None:
        a.role = "admin" if a.username == "admin" else "trader"
    sys.exit(asyncio.run(main(a)))
