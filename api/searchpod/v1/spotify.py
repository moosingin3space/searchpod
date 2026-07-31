"""searchpod — live Spotify episode lookup.

Deliberately **separate from the durable catalog** in `podcasts.py`, and
deliberately **not persisted**. Spotify's Developer Terms forbid building
a persistent database of their content, so nothing Spotify returns is
ever written to disk: every method here hits Spotify fresh at query
time and the results live only for the duration of the request. That is
why there are no `OrderedMap` indexes, no cursors, and no
`Podcast`/`Episode` actors on this side of the app — there is nothing to
index because there is nothing stored.

Two state types, for two different concerns:

- `SpotifyLookup` — a single well-known actor (state id
  `"global"`) that owns the lookup methods. Its state is
  **empty on purpose**: it is a place to hang `Workflow` methods (the
  only method kind allowed to make external calls), not a cache.
- `SpotifyToken` — a single well-known actor (state id `"spotify"`)
  holding the Client Credentials access token and its expiry. An OAuth
  access token is not Spotify *content*, so caching it until it expires
  is ordinary API hygiene rather than a durable catalog. It is a
  separate type from `SpotifyLookup` because it has a completely
  different write cadence (once an hour, app-internal) from the lookup
  methods (every call, user-facing).

Every match carries `spotify_url` — the item's `external_urls.spotify`
link — plus the `attribution` string on the response. Both are
Developer Terms requirements, not decoration.

`inferred_guest_names` is named the way it is on purpose. Spotify's
episode schema has **no guest or host field at all** (verified against
their OpenAPI spec: `EpisodeBase` carries `description`,
`html_description`, `name`, `release_date`, ... and nothing about
people). Those names are an LLM's guess read out of prose, so they are
never called `guest_names` and never presented as confirmed metadata.
"""

from typing import Optional

from reboot.api import API, Field, Methods, Model, Tool, Type, Workflow, Writer

# The well-known state id of the one and only `SpotifyLookup` actor.
# Created by the `initialize` hook in `backend/src/main.py`.
SPOTIFY_LOOKUP_ID = "global"

# The well-known state id of the one and only `SpotifyToken` actor.
SPOTIFY_TOKEN_ID = "spotify"

# Spotify's `/search` endpoint caps `limit` at 10 (spec:
# `paths./search.get.parameters[limit].schema.maximum: 10`). Asking for
# more is a 400, so this is a hard ceiling, not a policy choice.
MAX_SEARCH_LIMIT = 10

# `GET /shows/{id}/episodes` uses the shared `QueryLimit` parameter,
# which caps at 50 (spec: `components.parameters.QueryLimit`).
MAX_SHOW_EPISODES_PAGE = 50

# The most episodes the show-scoped path will page through before giving
# up. Spotify has no "search within a show" endpoint — the `/search`
# field filters are album/artist/track/year/upc/tag/isrc/genre, none of
# which apply to episodes — so scoping to one show means listing that
# show's episodes and matching locally. This cap keeps a 2,000-episode
# daily show from turning one tool call into 40 HTTP round trips.
MAX_SHOW_EPISODES_SCANNED = 200

# Spotify's `market` parameter is effectively **required** for us. The
# spec says: "If neither market or user country are provided, the
# content is considered unavailable for the client." A Client
# Credentials token has no user, and therefore no country — so without
# an explicit market every result comes back unplayable/empty.
DEFAULT_MARKET = "US"

# Displayed with every response. Spotify's Developer Terms require both
# attribution and a link back to the content on Spotify; the link is the
# per-match `spotify_url`.
SPOTIFY_ATTRIBUTION = (
    "Content metadata from Spotify. Open each episode on Spotify via its "
    "`spotify_url`."
)


# ---------------------------------------------------------------------------
# Shared errors.
# ---------------------------------------------------------------------------


class SpotifyUnavailable(Model):
    """Spotify could not be reached, or refused the request.

    Raised as a declared abort rather than left to retry. These lookups
    sit on the *synchronous* request path of a chat tool: an undeclared
    exception would make the runtime replay the workflow indefinitely
    while the caller's RPC hangs with no explanation. A declared abort
    stops the workflow and hands the caller a reason it can act on.
    """

    # Human-readable; safe to show a user. Never contains credentials.
    reason: str = Field(tag=1, default="")
    # The HTTP status Spotify returned, or 0 for a network-level failure.
    status: int = Field(tag=2, default=0)
    # True when trying again later is likely to work (429, 5xx, network).
    retryable: bool = Field(tag=3, default=False)


# ---------------------------------------------------------------------------
# SpotifyToken — the Client Credentials access token cache.
# ---------------------------------------------------------------------------


class SpotifyTokenState(Model):
    """The app's current Spotify access token.

    Not Spotify *content* — an OAuth bearer credential the app minted
    for itself. Caching it until `expires_at_epoch_seconds` is ordinary
    practice and is explicitly not the persistent-catalog the Developer
    Terms prohibit.
    """

    access_token: str = Field(tag=1, default="")
    # Epoch seconds. 0 means "never fetched"; a value in the past means
    # "stale, refresh before use".
    expires_at_epoch_seconds: int = Field(tag=2, default=0)


class AccessTokenResponse(Model):
    access_token: str = Field(tag=1, default="")
    # False when the cached token was still fresh and no call to
    # Spotify's token endpoint was made. Exposed for tests and logs.
    refreshed: bool = Field(tag=2, default=False)


# ---------------------------------------------------------------------------
# SpotifyLookup — the live lookup surface.
# ---------------------------------------------------------------------------


class SpotifyLookupState(Model):
    """Empty on purpose.

    Nothing Spotify returns is stored. This type exists so the lookup
    methods have an actor to live on — a `Workflow` is the only method
    kind permitted to make external calls, and a `Workflow` needs a
    `Type`.
    """


class SpotifyEpisodeMatch(Model):
    """One Spotify episode, as returned live. Never stored.

    Field names track Spotify's own schema so nothing is invented:
    `episode_name` is `name`, `release_date` is `release_date`,
    `duration_ms` is `duration_ms`, `spotify_url` is
    `external_urls.spotify`.
    """

    episode_id: str = Field(tag=1, default="")
    episode_name: str = Field(tag=2, default="")
    # Spotify's *search* results are `SimplifiedEpisodeObject`s, which
    # carry no `show` — the show is filled in from `GET /episodes/{id}`
    # (whose `EpisodeObject` does have one), or is already known when the
    # lookup was scoped to a named show. Empty if it could not be
    # resolved; never guessed.
    show_name: str = Field(tag=3, default="")
    show_publisher: str = Field(tag=4, default="")
    release_date: str = Field(tag=5, default="")
    # Spotify's `release_date_precision`: "year", "month", or "day".
    # `release_date` is only as precise as this says.
    release_date_precision: str = Field(tag=6, default="")
    description: str = Field(tag=7, default="")
    duration_ms: int = Field(tag=8, default=0)
    # `external_urls.spotify` — the link back to this item on Spotify.
    # Required by the Developer Terms; always populated.
    spotify_url: str = Field(tag=9, default="")
    # LLM-inferred, never confirmed. Only ever populated by
    # `search_episodes_with_guests`; always empty from `search_episodes`.
    # Spotify's API has no guest or host field, so these names were read
    # out of `description` prose by a model and may be wrong, incomplete,
    # or may name someone merely mentioned rather than present.
    inferred_guest_names: list[str] = Field(tag=10, default_factory=list)


class SearchEpisodesRequest(Model):
    topic: str = Field(tag=1, default="")
    # Empty means "search all of Spotify". When set, the lookup resolves
    # the show first and matches only within that show's episodes.
    show_name: str = Field(tag=2, default="")
    # ISO 3166-1 alpha-2. Empty falls back to `DEFAULT_MARKET`; see the
    # note on that constant for why this can't just be omitted.
    market: str = Field(tag=3, default="")
    # 0 means "use the default". Clamped to `MAX_SEARCH_LIMIT`.
    limit: int = Field(tag=4, default=0)


class SearchEpisodesResponse(Model):
    matches: list[SpotifyEpisodeMatch] = Field(tag=1, default_factory=list)
    # Always `SPOTIFY_ATTRIBUTION`. Present on the wire so every consumer
    # of this response has the attribution in hand.
    attribution: str = Field(tag=2, default="")
    # Human-readable note when the result needs explaining — zero
    # matches, a named show that couldn't be found, a scan that hit
    # `MAX_SHOW_EPISODES_SCANNED`. Empty on a plain successful lookup.
    message: str = Field(tag=3, default="")
    # Only set by `search_episodes_with_guests`: the caveat that belongs
    # next to any `inferred_guest_names` shown to a user.
    inference_caveat: Optional[str] = Field(tag=4, default=None)


# ---------------------------------------------------------------------------
# The API.
# ---------------------------------------------------------------------------

_LOOKUP_ID_HINT = (
    f" Spotify lookup is a single shared service whose id is always "
    f"'{SPOTIFY_LOOKUP_ID}' — pass that as the id."
)

_NOT_STORED_HINT = (
    " Results are fetched from Spotify live on every call and are not "
    "stored anywhere; there is no cursor and no page 2. Each match "
    "includes `spotify_url`, the link to the episode on Spotify, which "
    "must be shown alongside the result."
)


api = API(
    SpotifyToken=Type(
        state=SpotifyTokenState,
        methods=Methods(
            create=Writer(
                request=None,
                response=None,
                factory=True,
                mcp=None,
            ),
            # A Workflow because refreshing the token is an external HTTP
            # call, and external calls may only happen in a Workflow.
            # Internal-only (`mcp=None`, and an app-internal-only
            # authorizer): this returns the application's own bearer
            # credential and must never be reachable by an end user.
            access_token=Workflow(
                request=None,
                response=AccessTokenResponse,
                errors=[SpotifyUnavailable],
                mcp=None,
            ),
        ),
    ),
    SpotifyLookup=Type(
        state=SpotifyLookupState,
        methods=Methods(
            create=Writer(
                request=None,
                response=None,
                factory=True,
                mcp=None,
            ),
            search_episodes=Workflow(
                request=SearchEpisodesRequest,
                response=SearchEpisodesResponse,
                errors=[SpotifyUnavailable],
                description=(
                    "Search Spotify for podcast episodes about a topic, "
                    "live. Set `show_name` to search within one show; "
                    "leave it empty to search all of Spotify. `market` is "
                    "an ISO 3166-1 alpha-2 country code and defaults to "
                    f"'{DEFAULT_MARKET}' — Spotify treats content as "
                    "unavailable when no market is given. `limit` is "
                    f"capped at {MAX_SEARCH_LIMIT} by Spotify. This does "
                    "NOT return guests: Spotify's API has no guest or "
                    "host field. Use `search_episodes_with_guests` if "
                    "guest names matter."
                    + _NOT_STORED_HINT
                    + _LOOKUP_ID_HINT
                ),
                mcp=Tool(),
            ),
            search_episodes_with_guests=Workflow(
                request=SearchEpisodesRequest,
                response=SearchEpisodesResponse,
                errors=[SpotifyUnavailable],
                description=(
                    "Same live Spotify episode search as "
                    "`search_episodes`, and then additionally asks a "
                    "language model to read each episode's description "
                    "and guess who the guests were, returned as "
                    "`inferred_guest_names`. Spotify's API has no guest "
                    "or host field, so these names are a model's "
                    "inference from prose — they are NOT confirmed "
                    "metadata, may be wrong or incomplete, and may name "
                    "someone merely discussed rather than present. "
                    "Always present them as inferred, alongside the "
                    "`inference_caveat` on the response. Slower and more "
                    "expensive than `search_episodes`; prefer that one "
                    "when guests don't matter."
                    + _NOT_STORED_HINT
                    + _LOOKUP_ID_HINT
                ),
                mcp=Tool(),
            ),
        ),
    ),
)
