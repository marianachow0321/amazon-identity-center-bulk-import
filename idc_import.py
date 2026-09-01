#!/usr/bin/env python3
"""Import users from a CSV into an IAM Identity Center group.

    ./idc_import.py --identity-store-id d-1234567890 \
                    --region us-east-1 \
                    --group quick-author-pro \
                    --csv users.csv

For each row: create the user if they don't exist, then add them to the group.
Both steps are idempotent, so rerunning the same CSV is safe and does nothing the
second time -- that is also how you retry failures.

The tool first looks up every row and prints how many users it would create,
then asks to confirm. Pass --yes to skip the prompt in scripts.

CSV format (header required, extra columns ignored):

    email,firstName,lastName
    ada@example.com,Ada,Lovelace
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - dependency check
    sys.exit("boto3 is required: pip install boto3")


# Deliberately permissive: real corporate addresses defeat strict RFC validation
# and the identity store is the final authority. This only catches CSV damage.
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

REQUIRED_COLUMNS = ("email", "firstname", "lastname")
CSV_HEADER = "email,firstName,lastName\n"


@dataclass(frozen=True)
class Row:
    line: int
    email: str
    first_name: str
    last_name: str

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def load_csv(path: str) -> tuple[list[Row], list[str]]:
    """Parse and validate the whole file up front.

    Returns (rows, errors). Validation is a separate pass so a malformed file is
    rejected before any write, rather than half-importing and leaving the group
    in an unknown state.
    """
    rows: list[Row] = []
    errors: list[str] = []

    try:
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"cannot read {path}: {exc}"]

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return [], [f"{path} is empty"]

        columns = {(n or "").strip().lower(): (n or "") for n in reader.fieldnames}
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            return [], [
                f"{path} is missing required column(s): {', '.join(missing)} "
                f"(found: {', '.join(reader.fieldnames)})"
            ]

        seen: dict[str, int] = {}
        for record in reader:
            line = reader.line_num
            cell = lambda col: (record.get(columns[col]) or "").strip()  # noqa: E731
            email, first, last = cell("email"), cell("firstname"), cell("lastname")

            if not any((email, first, last)):
                continue  # blank padding line

            problems = []
            if not email:
                problems.append("email is empty")
            elif not EMAIL_RE.match(email):
                problems.append(f"malformed email {email!r}")
            if not first:
                problems.append("firstName is empty")
            if not last:
                problems.append("lastName is empty")
            if email.lower() in seen:
                problems.append(f"duplicate email, first seen on line {seen[email.lower()]}")

            if problems:
                errors.extend(f"line {line}: {p}" for p in problems)
                continue

            seen[email.lower()] = line
            rows.append(Row(line, email, first, last))

    if not rows and not errors:
        errors.append(f"{path} contains a header but no data rows")
    return rows, errors


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


class IdentityStore:
    """The four identitystore calls this tool needs."""

    def __init__(self, client, identity_store_id: str):
        self.client = client
        self.ids = identity_store_id

    def group_id(self, display_name: str) -> str | None:
        try:
            return self.client.get_group_id(
                IdentityStoreId=self.ids,
                AlternateIdentifier={
                    "UniqueAttribute": {
                        "AttributePath": "displayName",
                        "AttributeValue": display_name,
                    }
                },
            )["GroupId"]
        except ClientError as exc:
            if _code(exc) == "ResourceNotFoundException":
                return None
            raise

    def user_id(self, email: str) -> str | None:
        try:
            return self.client.get_user_id(
                IdentityStoreId=self.ids,
                AlternateIdentifier={
                    "UniqueAttribute": {
                        "AttributePath": "userName",
                        "AttributeValue": email,
                    }
                },
            )["UserId"]
        except ClientError as exc:
            if _code(exc) == "ResourceNotFoundException":
                return None
            raise

    def create_user(self, row: Row) -> str:
        return self.client.create_user(
            IdentityStoreId=self.ids,
            UserName=row.email,
            DisplayName=row.display_name,
            Name={"GivenName": row.first_name, "FamilyName": row.last_name},
            Emails=[{"Value": row.email, "Type": "work", "Primary": True}],
        )["UserId"]

    def add_member(self, group_id: str, user_id: str) -> str:
        """Returns 'added' or 'present'. Idempotent."""
        try:
            self.client.create_group_membership(
                IdentityStoreId=self.ids,
                GroupId=group_id,
                MemberId={"UserId": user_id},
            )
            return "added"
        except ClientError as exc:
            if _code(exc) == "ConflictException":
                return "present"
            raise


class FailureFile:
    """Collects failed rows in the input CSV format, so it can be fed back in."""

    def __init__(self, path: str | None):
        self.path = path
        self.rows: list[Row] = []

    def add(self, row: Row) -> None:
        self.rows.append(row)

    def write(self) -> str | None:
        if not self.path or not self.rows:
            return None
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            fh.write(CSV_HEADER)
            writer = csv.writer(fh)
            for row in self.rows:
                writer.writerow([row.email, row.first_name, row.last_name])
        return self.path


def build_client(region: str, profile: str | None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client(
        "identitystore",
        region_name=region,
        # These APIs throttle aggressively; let botocore back off.
        config=Config(retries={"mode": "adaptive", "max_attempts": 10}),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--identity-store-id", required=True, help="e.g. d-1234567890")
    p.add_argument("--region", required=True, help="region of the IdC instance")
    p.add_argument("--group", required=True, help="displayName of the target group")
    p.add_argument("--csv", required=True, help="path to users CSV")
    p.add_argument("--profile", help="AWS profile name")
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt (required when not on a terminal)",
    )
    p.add_argument(
        "--failures",
        default="failed.csv",
        help="write failed rows here in input format for retry (default: failed.csv)",
    )
    p.add_argument(
        "--validate-only", action="store_true", help="check the CSV and exit; no AWS calls"
    )
    p.add_argument(
        "--sleep", type=float, default=0.2, help="pause between users (default: 0.2)"
    )
    return p.parse_args(argv)


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print(
            "not running on a terminal; pass --yes to proceed without confirmation",
            file=sys.stderr,
        )
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def run(args: argparse.Namespace, store: IdentityStore | None = None) -> int:
    rows, errors = load_csv(args.csv)
    if errors:
        print(f"CSV validation failed ({len(errors)} problem(s)):", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 2
    print(f"CSV OK: {len(rows)} user(s) in {args.csv}")

    if args.validate_only:
        return 0

    if store is None:
        store = IdentityStore(
            build_client(args.region, args.profile), args.identity_store_id
        )

    group_id = store.group_id(args.group)
    if group_id is None:
        print(
            f"group {args.group!r} not found in identity store {args.identity_store_id}",
            file=sys.stderr,
        )
        return 3
    print(f"target group: {args.group} ({group_id})")

    failures = FailureFile(args.failures)
    counts = {"created": 0, "existing": 0, "added": 0, "present": 0, "failed": 0}

    # Preflight: resolve every row first, so we can report how many users this
    # will create before creating any. Each new member of a role-mapped group is
    # a paid subscription, so the count is worth seeing up front. The lookups are
    # reused by the write loop below, so this costs no extra API calls.
    plan: list[tuple[Row, str | None]] = []
    for row in rows:
        try:
            plan.append((row, store.user_id(row.email)))
        except ClientError as exc:
            counts["failed"] += 1
            failures.add(row)
            print(f"FAILED        {row.email}: {_code(exc)} {exc}", file=sys.stderr)

    to_create = [(r, u) for r, u in plan if u is None]
    existing = [(r, u) for r, u in plan if u is not None]
    print(
        f"plan: create {len(to_create)} new user(s), "
        f"{len(existing)} already exist; all {len(plan)} added to {args.group}"
    )
    for row, _ in to_create:
        print(f"  new: {row.email} ({row.display_name})")

    if not plan:
        print("nothing to do")
        retry_path = failures.write()
        if retry_path:
            print(f"failed rows written to {retry_path}")
        return 1 if counts["failed"] else 0

    if not args.yes and not confirm("proceed?"):
        print("aborted; nothing was changed")
        return 4

    print()
    for row, user_id in plan:
        try:
            if user_id is None:
                user_id = store.create_user(row)
                counts["created"] += 1
                print(f"CREATED       {row.email} -> {user_id}")
            else:
                counts["existing"] += 1
                print(f"EXISTS        {row.email} -> {user_id}")

            result = store.add_member(group_id, user_id)
            counts[result] += 1
            print(
                "              added to group"
                if result == "added"
                else "              already in group"
            )

        except ClientError as exc:
            counts["failed"] += 1
            failures.add(row)
            print(f"FAILED        {row.email}: {_code(exc)} {exc}", file=sys.stderr)

        if args.sleep:
            time.sleep(args.sleep)

    print(
        f"\nsummary: {counts['created']} created, {counts['existing']} existing, "
        f"{counts['added']} added to group, {counts['present']} already member, "
        f"{counts['failed']} failed"
    )

    retry_path = failures.write()
    if retry_path:
        print(f"failed rows written to {retry_path} -- rerun with --csv {retry_path}")

    return 1 if counts["failed"] else 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
