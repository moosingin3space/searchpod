"""Servicers for the live Spotify lookup.

Read `api/searchpod/v1/spotify.py` first — it explains why this is
separate from the catalog and why nothing here is persisted.

Three conventions run through this file:

- **Every external call goes through `_request_json`, which never
  raises.** Failures come back as a plain-dict envelope so the
  `at_least_once` step wrapping the call always *completes*, and the
  workflow body — not the callable — decides what to do. A `raise`
  inside an `at_least_once` callable would make the runtime replay the
  workflow forever, which is the wrong behaviour for a synchronous chat
  tool: the caller's RPC would just hang.
- **Nothing Spotify returns is stored.** Every value read out of a
  Spotify response is turned into a response `Model` and dropped when
  the request ends. The only Spotify-issued value written to durable
  state is the access token, which is a credential, not content.
- **Endpoint paths, query parameters, and JSON field names were taken
  from Spotify's own OpenAPI spec** (`open-api-schema.yaml`) and their
  Client Credentials tutorial, not from memory. Where the spec
  contradicts a reasonable assumption, there's a comment saying so.
"""

import asyncio
import base64
import os
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel
from reboot.agents.pydantic_ai import Agent
from reboot.aio.auth.authorizers import (
    AuthorizerRule,
    allow_if,
    has_verified_token,
    is_app_internal,
)
from reboot.aio.contexts import WorkflowContext, WriterContext
from reboot.aio.workflows import at_least_once

from searchpod.v1.spotify import (
    DEFAULT_MARKET,
    MAX_SEARCH_LIMIT,
    MAX_SHOW_EPISODES_PAGE,
    MAX_SHOW_EPISODES_SCANNED,
    SPOTIFY_ATTRIBUTION,
    SPOTIFY_TOKEN_ID,
    SpotifyEpisodeMatch,
    SpotifyUnavailable,
)
from searchpod.v1.spotify_rbt import SpotifyLookup, SpotifyToken

# ---------------------------------------------------------------------------
# Spotify endpoints. Verbatim from Spotify's OpenAPI spec / tutorial.
# ---------------------------------------------------------------------------

# `components.securitySchemes.oauth_2_0.flows.authorizationCode.tokenUrl`,
# and the Client Credentials tutorial. Note this is on `accounts.` —
# a *different* host from the API base below.
TOKEN_URL = "https://accounts.spotify.com/api/token"

# `servers[0].url`.
API_BASE_URL = "https://api.spotify.com/v1"

# How many seconds before a token's stated expiry we treat it as stale,
# so a token can't expire in flight between the check and the call.
TOKEN_EXPIRY_SKEW_SECONDS = 60

HTTP_TIMEOUT_SECONDS = 15.0

# Attempts made *within a single* `_request_json` call for a retryable
# failure (429, 5xx, network). Deliberately small and in-callable: a
# retry loop inside the callable genuinely counts, whereas relying on
# workflow replay would retry forever while the caller waits.
MAX_HTTP_ATTEMPTS = 3
HTTP_RETRY_BASE_DELAY_SECONDS = 0.5

# Default number of matches when the request doesn't say.
DEFAULT_SEARCH_LIMIT = 5

# The OpenRouter model used to guess guest names. Structured-output
# capable, which `output_type=` requires.
GUEST_MODEL = "openrouter:openai/gpt-5.6-luna"

# Shown next to any `inferred_guest_names`.
INFERENCE_CAVEAT = (
    "Guest names were inferred by a language model from each episode's "
    "description text. Spotify's API has no guest or host field, so these "
    "are guesses, not confirmed metadata: they may be wrong, incomplete, "
    "or may name someone discussed rather than present."
)


# ---------------------------------------------------------------------------
# HTTP. Every external call in this file goes through here.
# ---------------------------------------------------------------------------

# The envelope `_request_json` returns:
#   {"ok": bool, "status": int, "error": str, "body": dict}
# A plain dict rather than a Model because these values are memoized by
# `at_least_once`, and dicts round-trip through memoization cleanly
# (verified) without adding wire types to the API for what is purely an
# implementation detail.
HttpResult = dict[str, Any]


def _ok(body: dict[str, Any]) -> HttpResult:
    return {"ok": True, "status": 200, "error": "", "body": body}


def _err(status: int, error: str) -> HttpResult:
    return {"ok": False, "status": status, "error": error, "body": {}}


def _retryable(status: int) -> bool:
    """429 and 5xx are worth another attempt; 4xx generally is not."""
    return status == 0 or status == 429 or 500 <= status < 600


async def _request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, str]] = None,
    data: Optional[dict[str, str]] = None,
) -> HttpResult:
    """One HTTP request to Spotify. **Never raises.**

    Retries retryable failures a bounded number of times *within this
    call*, then reports the outcome as data. The caller decides what a
    failure means; see the module docstring for why this must not raise.
    """
    last: HttpResult = _err(0, "no attempt made")
    for attempt in range(MAX_HTTP_ATTEMPTS):
        if attempt:
            await asyncio.sleep(HTTP_RETRY_BASE_DELAY_SECONDS * (2**(attempt - 1)))
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, data=data
                )
        except httpx.HTTPError as error:
            last = _err(0, f"network error contacting Spotify: {error}")
            continue

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                # A 200 that isn't JSON is not worth retrying.
                return _err(200, "Spotify returned a non-JSON 200 response")
            if not isinstance(body, dict):
                return _err(200, "Spotify returned a non-object JSON body")
            return _ok(body)

        last = _err(response.status_code, _describe_error(response))
        if not _retryable(response.status_code):
            return last
    return last


def _describe_error(response: httpx.Response) -> str:
    """A safe, human-readable summary of a non-200 Spotify response.

    Spotify's error bodies are `{"error": {"status": .., "message": ..}}`
    for the Web API and `{"error": .., "error_description": ..}` for the
    token endpoint (they are different shapes — the token endpoint is an
    OAuth server, not the Web API). Handle both, and fall back to the
    status line rather than echoing an arbitrary body back to a user.
    """
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message", ""))
        elif isinstance(error, str):
            detail = str(body.get("error_description", "") or error)

    summary = f"Spotify returned HTTP {response.status_code}"
    if detail:
        summary = f"{summary}: {detail}"
    if response.status_code == 429:
        # Spotify's OpenAPI spec documents NO headers on its 429
        # response, so `Retry-After` is not guaranteed to be there.
        # Report it opportunistically rather than depending on it.
        retry_after = response.headers.get("Retry-After", "")
        if retry_after:
            summary = f"{summary} (Retry-After: {retry_after}s)"
    return summary


async def _fetch_access_token() -> HttpResult:
    """Spotify's Client Credentials grant.

    Shape verified against Spotify's Client Credentials tutorial:
    `POST https://accounts.spotify.com/api/token`, credentials in an
    **`Authorization: Basic base64(client_id:client_secret)` header**
    (not in the body), `Content-Type: application/x-www-form-urlencoded`,
    body `grant_type=client_credentials`. The 200 response is
    `{"access_token": str, "token_type": "Bearer", "expires_in": int}`.
    """
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return await _request_json(
        "POST",
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
    )


async def _api_get(
    path: str, access_token: str, params: dict[str, str]
) -> HttpResult:
    """A GET against the Spotify Web API. Naturally idempotent."""
    return await _request_json(
        "GET",
        f"{API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
    )


# ---------------------------------------------------------------------------
# Guest-name inference. Isolated behind one module-level function so
# tests can substitute it without stubbing the durable `Agent` itself.
# ---------------------------------------------------------------------------


class InferredGuestNames(BaseModel):
    """Structured output for the extractor.

    Module scope, as `output_type=` values must be picklable.
    """

    names: list[str] = []


_GUEST_SYSTEM_PROMPT = (
    "You extract the names of people who appear as guests on a podcast "
    "episode, given only the episode's title and description. Return "
    "only names of people the text indicates were guests on, or "
    "interviewed for, the episode. Do NOT return the host, the show, "
    "the publisher, a company, or a person merely mentioned as a topic. "
    "If the text does not clearly identify any guest, return an empty "
    "list. Never guess a plausible-sounding name that is not in the text."
)

# Built lazily and cached: constructing a pydantic-ai OpenRouter agent
# raises `UserError` when `OPENROUTER_API_KEY` is unset, and this module
# is imported by tests (and by `main.py`) in environments that may not
# have it. Still one instance per process with a stable `name=`, which is
# what the memoization keys depend on.
_guest_agent: Optional[Agent[None, InferredGuestNames]] = None


def guest_agent() -> Agent[None, InferredGuestNames]:
    global _guest_agent
    if _guest_agent is None:
        _guest_agent = Agent(
            GUEST_MODEL,
            name="searchpod-spotify-guest-extractor",
            output_type=InferredGuestNames,
            system_prompt=_GUEST_SYSTEM_PROMPT,
        )
    return _guest_agent


async def infer_guest_names(
    context: WorkflowContext,
    episode_id: str,
    episode_name: str,
    description: str,
) -> list[str]:
    """Ask the model who the guests probably were. Best-effort.

    A Reboot `Agent` call, so the model call is already memoized across
    workflow replays — it needs no `at_least_once` of its own. `variant=`
    is the episode id so two episodes with identical descriptions in one
    workflow don't collide as "duplicate agent runs".

    Returns `[]` rather than raising when the model can't be reached: an
    inference failure should degrade the answer, not fail the lookup.
    The `except Exception` is deliberately broad and covers Reboot
    `Aborted`s as well as provider errors — losing the guest guesses is
    always preferable to losing the (real, attributed) Spotify results
    the caller actually asked for.
    """
    if not description.strip():
        return []
    prompt = (
        f"Podcast episode title: {episode_name}\n\n"
        f"Episode description:\n{description}"
    )
    try:
        result = await guest_agent().run(context, prompt, variant=episode_id)
    except Exception:
        return []
    names: list[str] = []
    for name in result.output.names:
        cleaned = name.strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names


# ---------------------------------------------------------------------------
# Parsing Spotify JSON into our own types. Field names per the spec.
# ---------------------------------------------------------------------------


def _spotify_url(item: dict[str, Any], kind: str, item_id: str) -> str:
    """`external_urls.spotify`, with a derived fallback.

    The link back to Spotify is a Developer Terms requirement, so this
    never returns empty for an item that has an id — if the field is
    missing, the canonical open.spotify.com form is constructed instead.
    """
    external_urls = item.get("external_urls")
    if isinstance(external_urls, dict):
        url = external_urls.get("spotify")
        if isinstance(url, str) and url:
            return url
    if item_id:
        return f"https://open.spotify.com/{kind}/{item_id}"
    return ""


def _episode_match(
    episode: dict[str, Any],
    show_name: str = "",
    show_publisher: str = "",
) -> SpotifyEpisodeMatch:
    """One `EpisodeBase`-shaped object → one match.

    Works for both `SimplifiedEpisodeObject` (what `/search` and
    `/shows/{id}/episodes` return, which has no `show`) and the full
    `EpisodeObject` from `/episodes/{id}` (which does). When the object
    carries a `show`, it wins over the passed-in values.
    """
    episode_id = str(episode.get("id", "") or "")
    name = show_name
    publisher = show_publisher
    show = episode.get("show")
    if isinstance(show, dict):
        name = str(show.get("name", "") or "") or name
        publisher = str(show.get("publisher", "") or "") or publisher
    return SpotifyEpisodeMatch(
        episode_id=episode_id,
        episode_name=str(episode.get("name", "") or ""),
        show_name=name,
        show_publisher=publisher,
        release_date=str(episode.get("release_date", "") or ""),
        release_date_precision=str(
            episode.get("release_date_precision", "") or ""
        ),
        description=str(episode.get("description", "") or ""),
        duration_ms=int(episode.get("duration_ms", 0) or 0),
        spotify_url=_spotify_url(episode, "episode", episode_id),
    )


def _items(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """`body[key].items`, defensively.

    `/search` returns `{"episodes": <PagingObject>, ...}` where a key is
    absent entirely if that type had no hits, so a missing key is normal
    and means "no results", not "malformed".
    """
    paging = body.get(key) if key else body
    if not isinstance(paging, dict):
        return []
    items = paging.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _matches_topic(episode: dict[str, Any], topic: str) -> bool:
    """Case-insensitive substring match, the same rule the catalog uses.

    Only used on the show-scoped path: Spotify has no way to search
    within one show, so that path lists the show's episodes and filters
    here.
    """
    needle = topic.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        [
            str(episode.get("name", "") or ""),
            str(episode.get("description", "") or ""),
        ]
    ).lower()
    return needle in haystack


def _clamp_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_SEARCH_LIMIT
    return min(limit, MAX_SEARCH_LIMIT)


def _market(market: str) -> str:
    return market.strip() or DEFAULT_MARKET


def _unavailable(result: HttpResult) -> SpotifyUnavailable:
    status = int(result["status"])
    return SpotifyUnavailable(
        reason=str(result["error"]),
        status=status,
        retryable=_retryable(status),
    )


def _token_failure(aborted: Exception) -> SpotifyUnavailable:
    """Translate a `SpotifyToken.AccessTokenAborted` into our error type.

    Usually the abort already carries a declared `SpotifyUnavailable`, in
    which case it's passed straight through. It might instead carry a
    framework error (e.g. `PermissionDenied` if the authorizer ever
    changed), so fall back to a generic, non-retryable description
    rather than assuming the declared shape.
    """
    error = getattr(aborted, "error", None)
    if isinstance(error, SpotifyUnavailable):
        return error
    return SpotifyUnavailable(
        reason=f"Could not obtain a Spotify access token: {aborted}",
        status=0,
        retryable=False,
    )


class LookupFailed(Exception):
    """Internal: a lookup could not be completed.

    The shared `_lookup` helper serves two workflow methods, and codegen
    gives each its **own** abort class (`SearchEpisodesAborted` vs.
    `SearchEpisodesWithGuestsAborted`). Raising one of those from the
    shared helper would make it *undeclared* in the other method — and
    an undeclared exception doesn't stop a workflow, it makes the
    runtime replay it from its last checkpoint, forever, while the
    caller's RPC hangs. So the helper raises this instead and each
    method converts it to its own declared abort at the boundary.
    """

    def __init__(self, error: SpotifyUnavailable) -> None:
        super().__init__(error.reason)
        self.error = error


# ---------------------------------------------------------------------------
# SpotifyToken.
# ---------------------------------------------------------------------------


class SpotifyTokenServicer(SpotifyToken.Servicer):

    def authorizer(self) -> AuthorizerRule:
        """App-internal **only** — deliberately narrower than the lookup.

        `access_token` hands back the application's own Spotify bearer
        credential. The catalog's `any=[is_app_internal,
        has_verified_token]` rule would let any signed-in user pull that
        credential out over RPC and use the app's Spotify quota (and
        identity) directly. The only legitimate callers are
        `SpotifyLookup`'s workflows and the `initialize` hook, both of
        which are app-internal.
        """
        return allow_if(all=[is_app_internal])

    async def create(self, context: WriterContext) -> None:
        # Nothing to seed: an empty token with expiry 0 already reads as
        # "stale, fetch before use".
        pass

    @classmethod
    async def access_token(
        cls, context: WorkflowContext
    ) -> SpotifyToken.AccessTokenResponse:
        state = await SpotifyToken.ref().always().read(context)

        async def _now() -> int:
            return int(time.time())

        # Wall-clock reads must be memoized, or a replay sees a different
        # "now" and can make a different staleness decision.
        now = await at_least_once("Read clock", context, _now)

        if (
            state.access_token
            and state.expires_at_epoch_seconds - TOKEN_EXPIRY_SKEW_SECONDS > now
        ):
            return SpotifyToken.AccessTokenResponse(
                access_token=state.access_token, refreshed=False
            )

        result = await at_least_once(
            "Fetch Spotify access token", context, _fetch_access_token
        )
        if not result["ok"]:
            raise SpotifyToken.AccessTokenAborted(_unavailable(result))

        body = result["body"]
        access_token = str(body.get("access_token", "") or "")
        expires_in = int(body.get("expires_in", 0) or 0)
        if not access_token:
            raise SpotifyToken.AccessTokenAborted(
                SpotifyUnavailable(
                    reason="Spotify's token response contained no access_token",
                    status=200,
                    retryable=False,
                )
            )
        expires_at = now + expires_in

        async def store(state: Any) -> None:
            state.access_token = access_token
            state.expires_at_epoch_seconds = expires_at

        await SpotifyToken.ref().per_workflow("Store access token").write(
            context, store
        )
        return SpotifyToken.AccessTokenResponse(
            access_token=access_token, refreshed=True
        )


# ---------------------------------------------------------------------------
# SpotifyLookup.
# ---------------------------------------------------------------------------


class SpotifyLookupServicer(SpotifyLookup.Servicer):

    def authorizer(self) -> AuthorizerRule:
        """The same rule the catalog types use.

        `has_verified_token` lets any signed-in user run a lookup;
        `is_app_internal` keeps the door open for in-app callers. Note
        the token actor is *not* covered by this — see its authorizer.
        """
        return allow_if(any=[is_app_internal, has_verified_token])

    async def create(self, context: WriterContext) -> None:
        # `SpotifyLookupState` is empty by design; nothing to seed.
        pass

    @classmethod
    async def search_episodes(
        cls,
        context: WorkflowContext,
        request: SpotifyLookup.SearchEpisodesRequest,
    ) -> SpotifyLookup.SearchEpisodesResponse:
        try:
            matches, message = await _lookup(context, request)
        except LookupFailed as failed:
            raise SpotifyLookup.SearchEpisodesAborted(failed.error)
        return SpotifyLookup.SearchEpisodesResponse(
            matches=matches,
            attribution=SPOTIFY_ATTRIBUTION,
            message=message,
        )

    @classmethod
    async def search_episodes_with_guests(
        cls,
        context: WorkflowContext,
        request: SpotifyLookup.SearchEpisodesRequest,
    ) -> SpotifyLookup.SearchEpisodesResponse:
        try:
            matches, message = await _lookup(context, request)
        except LookupFailed as failed:
            raise SpotifyLookup.SearchEpisodesWithGuestsAborted(failed.error)
        for match in matches:
            match.inferred_guest_names = await infer_guest_names(
                context,
                match.episode_id,
                match.episode_name,
                match.description,
            )
        return SpotifyLookup.SearchEpisodesResponse(
            matches=matches,
            attribution=SPOTIFY_ATTRIBUTION,
            message=message,
            inference_caveat=INFERENCE_CAVEAT,
        )


# ---------------------------------------------------------------------------
# The lookup itself, shared by both workflow methods.
# ---------------------------------------------------------------------------


async def _lookup(
    context: WorkflowContext,
    request: SpotifyLookup.SearchEpisodesRequest,
) -> tuple[list[SpotifyEpisodeMatch], str]:
    """One live Spotify lookup. Returns `(matches, message)`.

    Raises `LookupFailed` when Spotify can't be reached or refuses; the
    calling workflow method converts that to its own declared abort.
    """
    try:
        token = await SpotifyToken.ref(SPOTIFY_TOKEN_ID).per_workflow(
            "Get Spotify access token"
        ).access_token(context)
    except SpotifyToken.AccessTokenAborted as aborted:
        # `AccessTokenAborted` is declared on `SpotifyToken`, not on
        # `SpotifyLookup` — letting it escape would be an *undeclared*
        # exception here and would replay this workflow forever.
        raise LookupFailed(_token_failure(aborted))
    access_token = token.access_token
    market = _market(request.market)
    limit = _clamp_limit(request.limit)

    if request.show_name.strip():
        return await _lookup_within_show(
            context, request, access_token, market, limit
        )
    return await _lookup_across_spotify(
        context, request, access_token, market, limit
    )


async def _lookup_across_spotify(
    context: WorkflowContext,
    request: SpotifyLookup.SearchEpisodesRequest,
    access_token: str,
    market: str,
    limit: int,
) -> tuple[list[SpotifyEpisodeMatch], str]:
    """`GET /search?type=episode`, then fill in each episode's show.

    Two calls' worth of work because Spotify's search returns
    `SimplifiedEpisodeObject`s, which carry **no `show` field** — the
    only way to learn which podcast an episode belongs to is
    `GET /episodes/{id}`, whose `EpisodeObject` has one.
    """

    async def search() -> HttpResult:
        return await _api_get(
            "/search",
            access_token,
            {
                "q": request.topic,
                "type": "episode",
                "market": market,
                "limit": str(limit),
            },
        )

    result = await at_least_once("Search Spotify episodes", context, search)
    if not result["ok"]:
        raise LookupFailed(_unavailable(result))

    episodes = _items(result["body"], "episodes")
    if not episodes:
        return [], (
            f"Spotify returned no episodes for '{request.topic}' in market "
            f"{market}."
        )

    episode_ids = [
        str(episode.get("id", "") or "")
        for episode in episodes
        if episode.get("id")
    ]

    async def hydrate() -> list[dict[str, Any]]:
        """Fetch the full `EpisodeObject` for each hit, concurrently.

        One memoized step for the whole batch rather than one per
        episode: the ids come from the already-memoized search result,
        so the batch is stable across replays.

        Note `GET /episodes?ids=...` (Get Several Episodes) is marked
        **deprecated** in Spotify's spec, so this fans out over the
        single-episode endpoint instead of batching.
        """
        results = await asyncio.gather(
            *[
                _api_get(f"/episodes/{episode_id}", access_token, {"market": market})
                for episode_id in episode_ids
            ]
        )
        return [one["body"] for one in results if one["ok"]]

    hydrated = await at_least_once(
        "Hydrate Spotify episode shows", context, hydrate
    )
    by_id = {
        str(episode.get("id", "") or ""): episode
        for episode in hydrated
        if episode.get("id")
    }

    matches: list[SpotifyEpisodeMatch] = []
    for episode in episodes:
        episode_id = str(episode.get("id", "") or "")
        # Prefer the hydrated object (it has the show); fall back to the
        # search hit so a failed hydration loses the show name, not the
        # whole result.
        matches.append(_episode_match(by_id.get(episode_id, episode)))

    missing = [match for match in matches if not match.show_name]
    message = ""
    if missing:
        message = (
            f"{len(missing)} of {len(matches)} results could not have their "
            f"show resolved; those entries have an empty show_name."
        )
    return matches, message


async def _lookup_within_show(
    context: WorkflowContext,
    request: SpotifyLookup.SearchEpisodesRequest,
    access_token: str,
    market: str,
    limit: int,
) -> tuple[list[SpotifyEpisodeMatch], str]:
    """Resolve the show, then match the topic against its episodes.

    Spotify has **no "search within a show" capability**: `/search`'s
    field filters are `album`, `artist`, `track`, `year`, `upc`,
    `tag:hipster`, `tag:new`, `isrc` and `genre`, none of which apply to
    episodes. Merging the show name into the free-text query would just
    return episodes from *other* shows that happen to mention it, and a
    `SimplifiedEpisodeObject` carries no show to check against. So the
    only way to honour `show_name` honestly is to find the show and list
    its episodes.
    """
    show_name = request.show_name.strip()

    async def find_show() -> HttpResult:
        return await _api_get(
            "/search",
            access_token,
            {
                "q": show_name,
                "type": "show",
                "market": market,
                "limit": str(MAX_SEARCH_LIMIT),
            },
        )

    result = await at_least_once("Search Spotify shows", context, find_show)
    if not result["ok"]:
        raise LookupFailed(_unavailable(result))

    shows = _items(result["body"], "shows")
    if not shows:
        return [], f"Spotify has no show matching '{show_name}' in market {market}."

    # Prefer an exact (case-insensitive) name match; otherwise Spotify's
    # own top-ranked hit. Never silently search a different show than
    # asked for without saying so.
    wanted = show_name.lower()
    show = next(
        (
            candidate
            for candidate in shows
            if str(candidate.get("name", "") or "").strip().lower() == wanted
        ),
        shows[0],
    )
    resolved_name = str(show.get("name", "") or "")
    publisher = str(show.get("publisher", "") or "")
    show_id = str(show.get("id", "") or "")
    if not show_id:
        return [], f"Spotify returned a show for '{show_name}' with no id."

    async def list_episodes() -> list[dict[str, Any]]:
        """Page through the show's episodes up to the scan cap.

        `limit` here is the shared `QueryLimit` parameter, whose maximum
        is 50 — not the 10 that `/search` caps at. Returns whatever it
        managed to read; a mid-scan failure degrades the scan rather
        than failing the lookup, and the shortfall is reported in the
        message.
        """
        collected: list[dict[str, Any]] = []
        offset = 0
        while len(collected) < MAX_SHOW_EPISODES_SCANNED:
            page = await _api_get(
                f"/shows/{show_id}/episodes",
                access_token,
                {
                    "market": market,
                    "limit": str(MAX_SHOW_EPISODES_PAGE),
                    "offset": str(offset),
                },
            )
            if not page["ok"]:
                break
            items = _items(page["body"], "")
            if not items:
                break
            collected.extend(items)
            offset += len(items)
            if len(items) < MAX_SHOW_EPISODES_PAGE:
                break
        return collected[:MAX_SHOW_EPISODES_SCANNED]

    episodes = await at_least_once(
        "List Spotify show episodes", context, list_episodes
    )

    matches = [
        _episode_match(episode, resolved_name, publisher)
        for episode in episodes
        if _matches_topic(episode, request.topic)
    ][:limit]

    notes: list[str] = []
    if resolved_name.strip().lower() != wanted:
        notes.append(
            f"No show is named exactly '{show_name}'; searched Spotify's "
            f"closest match, '{resolved_name}', instead."
        )
    if not matches:
        notes.append(
            f"No episode of '{resolved_name}' mentions '{request.topic}' in "
            f"the {len(episodes)} most recent episodes scanned."
        )
    if len(episodes) >= MAX_SHOW_EPISODES_SCANNED:
        notes.append(
            f"Only the first {MAX_SHOW_EPISODES_SCANNED} episodes of "
            f"'{resolved_name}' were scanned; there may be older matches."
        )
    return matches, " ".join(notes)


SPOTIFY_SERVICERS = [
    SpotifyTokenServicer,
    SpotifyLookupServicer,
]
