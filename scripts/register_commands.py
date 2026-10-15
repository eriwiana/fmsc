"""Register /fmsc invite and /fmsc rotate with Discord. Run by hand, once per change.

Commands are registered against the application, not the bot process, so nothing in the
service does this at startup — and a startup that called Discord would make a deploy fail
over somebody else's rate limit.

    DISCORD_APPLICATION_ID=... DISCORD_BOT_TOKEN=... uv run python scripts/register_commands.py

Global by default, which Discord can take up to an hour to roll out. Pass a guild id while
testing: guild commands appear at once.

    ... uv run python scripts/register_commands.py --guild 123456789012345678
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from app.discord import API

# Subcommands of one command, so the guild sees a single /fmsc rather than two verbs
# competing with every other bot's.
COMMANDS = [
    {
        "name": "fmsc",
        "description": "Invite codes for this guild's community",
        "options": [
            {
                "type": 1,  # SUB_COMMAND
                "name": "invite",
                "description": "Post this community's invite code, if it has none yet",
            },
            {
                "type": 1,
                "name": "rotate",
                "description": "Replace this community's invite code and post the new one",
            },
        ],
    }
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild", help="register to this guild instead of globally")
    args = parser.parse_args()

    application_id = os.environ.get("DISCORD_APPLICATION_ID", "")
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not (application_id and token):
        print("DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN must both be set", file=sys.stderr)
        return 2

    path = f"/applications/{application_id}/commands"
    if args.guild:
        path = f"/applications/{application_id}/guilds/{args.guild}/commands"

    async with httpx.AsyncClient(base_url=API, timeout=10.0) as client:
        # PUT, not POST: it replaces the whole set, so a command removed from COMMANDS
        # disappears instead of lingering in every guild that ever saw it.
        response = await client.put(path, headers={"Authorization": f"Bot {token}"}, json=COMMANDS)
    print(response.status_code, response.text[:500])
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
