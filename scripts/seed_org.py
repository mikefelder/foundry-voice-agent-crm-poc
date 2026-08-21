"""Seed a demo account with a controlled pipeline.

Re-runnable: records are matched by name and updated in place rather than
duplicated, so running this twice leaves the org in the same state.

The point of seeding rather than reusing the stock sample data is that the
acceptance script asserts exact counts. That only works against records whose
stage, close date and creation date we chose deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from crm_companion.config import get_settings
from crm_companion.crm.salesforce_auth import build_token_provider
from crm_companion.crm.salesforce_client import SalesforceClient
from crm_companion.crm.soql import record_id, soql_literal

# Days relative to today. Negative close dates are past due; the mix matters,
# because seed data that is entirely overdue cannot demonstrate a count.
PAST_DUE = 6
TOTAL_OPEN = 14


@dataclass(frozen=True)
class OppSpec:
    name: str
    stage: str
    amount: int
    close_offset_days: int
    created_offset_days: int

    @property
    def is_past_due(self) -> bool:
        return self.close_offset_days < 0


# 14 open opportunities, 6 deliberately past due, spread across stages and ages.
SPECS: tuple[OppSpec, ...] = (
    # --- past due -----------------------------------------------------------
    OppSpec("Northgate Commons Phase 2", "Bidding", 42_000, -113, -527),
    OppSpec("Ashwood Commons", "Negotiation/Review", 18_000, -67, -401),
    OppSpec("Cedar Park Townhomes", "Proposal/Price Quote", 95_500, -41, -298),
    OppSpec("Harborview Retail Block", "Bidding", 128_000, -22, -240),
    OppSpec("Lakeside Medical Annex", "Value Proposition", 61_250, -9, -186),
    OppSpec("Fairmont Civic Center", "Needs Analysis", 210_000, -3, -152),
    # --- current ------------------------------------------------------------
    OppSpec("Riverbend Apartments", "Bidding", 74_000, 12, -121),
    OppSpec("Stonegate Business Park", "Proposal/Price Quote", 156_800, 26, -98),
    OppSpec("Elmwood School Expansion", "Qualification", 88_400, 39, -77),
    OppSpec("Summit Ridge Estates", "Value Proposition", 47_900, 54, -63),
    OppSpec("Beacon Hill Mixed Use", "Negotiation/Review", 302_500, 71, -44),
    OppSpec("Willow Creek Phase 1", "Prospecting", 33_750, 88, -31),
    OppSpec("Maple Grove Distribution", "Needs Analysis", 119_000, 104, -18),
    OppSpec("Kingsford Logistics Hub", "Bidding", 265_300, 133, -6),
)


async def seed(*, dry_run: bool = False) -> int:
    settings = get_settings()
    provider = build_token_provider(settings)

    async with SalesforceClient(
        provider,
        api_version=settings.sf_api_version,
        timeout=settings.request_timeout_seconds,
    ) as sf:
        audit_writable = await _audit_fields_writable(sf)
        _report_audit_mode(audit_writable)

        account_id = await _ensure_account(sf, settings.demo_account_name, dry_run=dry_run)
        if account_id is None and not dry_run:
            return 1

        created, updated = await _ensure_opportunities(
            sf, account_id, audit_writable=audit_writable, dry_run=dry_run
        )
        if not dry_run:
            print(f"\nopportunities: {created} created, {updated} updated")

        await _suggest_mention_targets(sf, settings.demo_mention_name)

        if not dry_run and account_id is not None:
            await _verify(sf, account_id, audit_writable=audit_writable)

    return 0


async def _audit_fields_writable(sf: SalesforceClient) -> bool:
    """CreatedDate is only settable when 'Set Audit Fields upon Record Creation' is on."""
    described = await sf.describe("Opportunity")
    for field in described.get("fields", []):
        if field.get("name") == "CreatedDate":
            return bool(field.get("createable"))
    return False


def _report_audit_mode(writable: bool) -> None:
    if writable:
        print("audit fields  : writable - seeding realistic creation dates")
        return
    print(
        "audit fields  : NOT writable - every record will be created today.\n"
        "                'Oldest entry date' will therefore report today, and the\n"
        "                pipeline-age half of the demo will not be meaningful.\n"
        '                To fix: Setup > User Interface > enable "Set Audit Fields\n'
        '                upon Record Creation", then grant that permission to this\n'
        "                user and re-run."
    )


async def _ensure_account(sf: SalesforceClient, name: str, *, dry_run: bool) -> str | None:
    existing = await sf.query_one(
        # Name is escaped by soql_literal; nothing else is interpolated.
        f"SELECT Id, Name FROM Account WHERE Name = {soql_literal(name)} LIMIT 1"  # noqa: S608
    )
    if existing:
        print(f"account       : reusing {existing['Name']} ({existing['Id']})")
        return existing["Id"]

    if dry_run:
        print(f"account       : would create {name}")
        return None

    account_id = await sf.create(
        "Account",
        # No address fields: orgs with State/Country picklists reject a state
        # without a country, and they add nothing to the scenario.
        {
            "Name": name,
            "Industry": "Construction",
            "Description": "Demo account for the voice CRM companion POC.",
        },
    )
    print(f"account       : created {name} ({account_id})")
    return account_id


async def _ensure_opportunities(
    sf: SalesforceClient,
    account_id: str | None,
    *,
    audit_writable: bool,
    dry_run: bool,
) -> tuple[int, int]:
    by_name: dict[str, str] = {}
    if account_id is not None:
        acct = record_id(account_id, field="account_id")
        existing_rows = await sf.query(
            f"SELECT Id, Name FROM Opportunity WHERE AccountId = '{acct}'"  # noqa: S608
        )
        by_name = {row["Name"]: row["Id"] for row in existing_rows}

    today = date.today()
    created = updated = 0

    for spec in SPECS:
        close_on = today + timedelta(days=spec.close_offset_days)
        payload = {
            "Name": spec.name,
            "AccountId": account_id,
            "StageName": spec.stage,
            "Amount": spec.amount,
            "CloseDate": close_on.isoformat(),
        }

        existing_id = by_name.get(spec.name)
        marker = "past due" if spec.is_past_due else "current "
        if dry_run:
            verb = "update" if existing_id else "create"
            print(
                f"  [{marker}] {verb:6} {spec.name:<28} "
                f"{spec.stage:<20} ${spec.amount:>9,}  closes {close_on}"
            )
            continue

        if existing_id:
            await sf.update("Opportunity", existing_id, payload)
            updated += 1
        else:
            if audit_writable:
                stamp = datetime.now(UTC) + timedelta(days=spec.created_offset_days)
                payload["CreatedDate"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            await sf.create("Opportunity", payload)
            created += 1
        print(f"  [{marker}] {spec.name}")

    return created, updated


async def _suggest_mention_targets(sf: SalesforceClient, configured: str | None) -> None:
    """Identity-licence users resolve by name but can never receive a Chatter notification."""
    rows = await sf.query(
        "SELECT Name, UserType FROM User "
        "WHERE IsActive = true AND UserType IN ('Standard', 'CsnOnly') "
        "ORDER BY Name"
    )
    names = [row["Name"] for row in rows]

    if configured:
        status = "found" if configured in names else "NOT FOUND among Chatter-capable users"
        print(f"\nmention target: {configured} - {status}")
        if configured not in names:
            print("                candidates: " + ", ".join(names))
        return

    print("\nmention target: DEMO_MENTION_NAME not set in .env")
    print("                Chatter-capable candidates: " + ", ".join(names))


async def _verify(sf: SalesforceClient, account_id: str, *, audit_writable: bool) -> None:
    # Validated above, so interpolation cannot smuggle anything into the query.
    acct = record_id(account_id, field="account_id")
    open_count = await sf.count(
        f"SELECT COUNT(Id) FROM Opportunity WHERE AccountId = '{acct}' AND IsClosed = false"  # noqa: S608
    )
    past_due = await sf.count(
        f"SELECT COUNT(Id) FROM Opportunity WHERE AccountId = '{acct}' "  # noqa: S608
        "AND IsClosed = false AND CloseDate < TODAY"
    )
    oldest_row = await sf.query_one(
        f"SELECT MIN(CreatedDate) oldest FROM Opportunity WHERE AccountId = '{acct}' "  # noqa: S608
        "AND IsClosed = false"
    )
    oldest = (oldest_row or {}).get("oldest")

    print("\n--- what the agent will report ---")
    print(f"  open opportunities : {open_count}   (expected {TOTAL_OPEN})")
    print(f"  past due           : {past_due}   (expected {PAST_DUE})")
    print(f"  oldest open entry  : {oldest}")

    problems = []
    if open_count != TOTAL_OPEN:
        problems.append(f"open count {open_count} != {TOTAL_OPEN}")
    if past_due != PAST_DUE:
        problems.append(f"past due {past_due} != {PAST_DUE}")
    if audit_writable and oldest and oldest.startswith(str(date.today().year)):
        if oldest[:10] == date.today().isoformat():
            problems.append("oldest entry is today despite audit fields being writable")

    if problems:
        print("\n  MISMATCH: " + "; ".join(problems))
    else:
        print("\n  counts match the acceptance script")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change without writing"
    )
    args = parser.parse_args()
    return asyncio.run(seed(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
