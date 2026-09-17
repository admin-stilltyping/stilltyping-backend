"""Operator-only account provisioning; passwords never appear in arguments or output."""

import argparse
import asyncio
import getpass
import sys
import warnings

from context_agent.config import Settings
from context_agent.db import Database

from .businesses.service import reset_business_password
from .service import create_account, normalize_username, reset_password


async def run(command: str, username: str, password: str) -> None:
    db = Database(Settings().database_url)
    try:
        if command == "create":
            await create_account(db, username, password)
        elif command == "reset-business-password":
            await reset_business_password(db, username, password)
        else:
            await reset_password(db, username, password)
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Manage super-admin accounts")
    parser.add_argument("command", choices=["create", "reset-password", "reset-business-password"])
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    try:
        username = normalize_username(args.username)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password (15–128 characters): ")
            confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match.")
        asyncio.run(run(args.command, username, password))
    except (ValueError, getpass.GetPassWarning, EOFError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        parser.exit(1, "Cancelled.\n")
    kind = "Business-admin" if args.command == "reset-business-password" else "Super-admin"
    print(f"{kind} {username}: {args.command} completed.", file=sys.stdout)


if __name__ == "__main__":
    main()
