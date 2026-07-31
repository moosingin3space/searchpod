import asyncio
import logging

from example_prompts import example_prompts
from reboot.aio.applications import Application
from reboot.aio.auth.oauth_providers import (
    Development,
    OAuthProviderByEnvironment,
)
from reboot.aio.external import InitializeContext
from reboot.std.collections.ordered_map.v1.ordered_map import (
    ordered_map_library,
)

from searchpod.v1.podcasts import DIRECTORY_ID
from searchpod.v1.podcasts_rbt import Directory
from servicers.podcasts import APPLICATION_SERVICERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def initialize(context: InitializeContext) -> None:
    """Bring up the one global catalog root.

    Unlike a typical chat app, searchpod's catalog is shared rather than
    per-user, so it needs a singleton that no `User` method creates.
    `create` is idempotent, so this is safe on every restart.
    """
    await Directory.create(context, DIRECTORY_ID)


async def main() -> None:
    application = Application(
        title="searchpod",
        description=(
            "Search a podcast catalog by topic: find which episode "
            "discussed something, when in the episode it came up, and who "
            "the guest was."
        ),
        servicers=APPLICATION_SERVICERS,
        libraries=[ordered_map_library()],
        initialize=initialize,
        example_prompts=example_prompts,
        oauth=OAuthProviderByEnvironment(
            dev=Development(),
            # Deliberately unset: choosing the production identity
            # provider is a separate decision, and a selected `None` arm
            # makes the app fail to start rather than ship without auth.
            prod=None,
        ),
    )
    await application.run()


if __name__ == "__main__":
    asyncio.run(main())
