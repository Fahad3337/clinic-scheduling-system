#!/usr/bin/env python
"""Bootstrap a staff login. There is no self-registration endpoint on purpose
-- an open POST /auth/register would let anyone create a staff account, which
defeats the point of authentication. Accounts are created by whoever has
database access, via this script.

Usage:
    python -m scripts.create_staff_account --email rao@clinic.example.com --role doctor --doctor-id <uuid>
    python -m scripts.create_staff_account --email desk@clinic.example.com --role front_desk
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.doctor import Doctor
from app.models.enums import StaffRole
from app.models.staff_account import StaffAccount
from app.services.auth_service import hash_password


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", required=True, choices=[r.value for r in StaffRole])
    parser.add_argument("--doctor-id", help="required when --role doctor")
    args = parser.parse_args()

    role = StaffRole(args.role)
    if role is StaffRole.DOCTOR and not args.doctor_id:
        parser.error("--doctor-id is required for --role doctor")
    if role is StaffRole.FRONT_DESK and args.doctor_id:
        parser.error("--doctor-id must be omitted for --role front_desk")

    # Prompted, never a CLI flag: a password on the command line lands in
    # shell history and `ps` output for every other process on the machine.
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1

    engine = create_async_engine(str(get_settings().database_url))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        if await session.scalar(select(StaffAccount).where(StaffAccount.email == args.email.lower())):
            print(f"An account for {args.email} already exists.", file=sys.stderr)
            return 1

        doctor_id: UUID | None = None
        if args.doctor_id:
            doctor_id = UUID(args.doctor_id)
            if await session.get(Doctor, doctor_id) is None:
                print(f"No doctor with id {doctor_id}.", file=sys.stderr)
                return 1

        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        account = StaffAccount(
            email=args.email.lower(), password_hash=password_hash, role=role, doctor_id=doctor_id
        )
        session.add(account)
        await session.commit()
        print(f"Created {role.value} account for {args.email} (id={account.id}).")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
