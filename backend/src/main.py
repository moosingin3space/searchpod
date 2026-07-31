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
from searchpod.v1.spotify import SPOTIFY_LOOKUP_ID, SPOTIFY_TOKEN_ID
from searchpod.v1.spotify_rbt import SpotifyLookup, SpotifyToken
from servicers.podcasts import APPLICATION_SERVICERS
from servicers.spotify import SPOTIFY_SERVICERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def initialize(context: InitializeContext) -> None:
    """Bring up the one global catalog root.

    Unlike a typical chat app, searchpod's catalog is shared rather than
    per-user, so it needs a singleton that no `User` method creates.
    `create` is idempotent, so this is safe on every restart.

    The two Spotify singletons are brought up the same way. They are
    separate from the catalog on purpose: Spotify results are looked up
    live and never stored (see `api/searchpod/v1/spotify.py`), so
    `SpotifyLookup` holds no state at all, and `SpotifyToken` holds only
    the app's own OAuth access token.
    """
    await Directory.create(context, DIRECTORY_ID)
    await SpotifyLookup.create(context, SPOTIFY_LOOKUP_ID)
    await SpotifyToken.create(context, SPOTIFY_TOKEN_ID)


async def main() -> None:
    application = Application(
        title="searchpod",
        description=(
            "Search a podcast catalog by topic: find which episode "
            "discussed something, when in the episode it came up, and who "
            "the guest was."
        ),
        servicers=APPLICATION_SERVICERS + SPOTIFY_SERVICERS,
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
