"""Tests for the live Spotify lookup.

**Nothing here touches the network or an LLM.** Two seams are stubbed,
and only those two:

- `servicers.spotify._request_json` — the single choke point every
  outbound HTTP call in that module goes through. Stubbing it (rather
  than `httpx`) means the tests exercise the real URL building, the real
  Spotify JSON parsing, the real error classification, and the real
  workflow control flow; only the socket is replaced. `_FakeSpotify`
  below routes on `(method, url)` so a test asserts against the exact
  endpoints the servicer chose.
- `servicers.spotify.infer_guest_names` — the Reboot `Agent` call. The
  durable `Agent` needs a real OpenRouter key even to *construct*, and
  its whole point is to hit a live model; the servicer keeps the call
  behind one module-level function precisely so tests can substitute it.

The catalog's `podcasts_test.py` has no external calls and so sets no
precedent here; this is the convention for the Spotify side.

Everything else is real: the real servicers, the real authorizers, the
real workflows, driven through `Service.ref(id).method(context, ...)`
with a genuinely verified identity — the same shape `podcasts_test.py`
uses.
"""

import unittest
from typing import Any, Optional
from unittest import mock

from reboot.aio.applications import Application
from reboot.aio.contexts import WorkflowContext
from reboot.aio.external import InitializeContext
from reboot.aio.tests import Reboot

import servicers.spotify as spotify_servicers
from searchpod.v1.spotify import (
    MAX_SEARCH_LIMIT,
    MAX_SHOW_EPISODES_SCANNED,
    SPOTIFY_ATTRIBUTION,
    SPOTIFY_LOOKUP_ID,
    SPOTIFY_TOKEN_ID,
    SpotifyUnavailable,
)
from searchpod.v1.spotify_rbt import SpotifyLookup, SpotifyToken
from servicers.spotify import SPOTIFY_SERVICERS

TOKEN_URL = spotify_servicers.TOKEN_URL
API = spotify_servicers.API_BASE_URL


async def initialize(context: InitializeContext) -> None:
    """The Spotify half of the bootstrap `main.py` runs."""
    await SpotifyLookup.create(context, SPOTIFY_LOOKUP_ID)
    await SpotifyToken.create(context, SPOTIFY_TOKEN_ID)


# ---------------------------------------------------------------------------
# Canned Spotify payloads. Field names and nesting are exactly what
# Spotify's OpenAPI spec declares, so a test failing here means our
# parsing is wrong, not that the fixture drifted.
# ---------------------------------------------------------------------------


def token_body(expires_in: int = 3600) -> dict[str, Any]:
    return {
        "access_token": "test-access-token",
        "token_type": "Bearer",
        "expires_in": expires_in,
    }


def simplified_episode(
    episode_id: str, name: str, description: str
) -> dict[str, Any]:
    """A `SimplifiedEpisodeObject` — note: no `show` key. That's real."""
    return {
        "id": episode_id,
        "name": name,
        "description": description,
        "html_description": f"<p>{description}</p>",
        "duration_ms": 1686230,
        "release_date": "2024-03-05",
        "release_date_precision": "day",
        "explicit": False,
        "is_playable": True,
        "languages": ["en"],
        "type": "episode",
        "uri": f"spotify:episode:{episode_id}",
        "href": f"{API}/episodes/{episode_id}",
        "images": [],
        "is_externally_hosted": False,
        "audio_preview_url": None,
        "external_urls": {"spotify": f"https://open.spotify.com/episode/{episode_id}"},
    }


def full_episode(
    episode_id: str,
    name: str,
    description: str,
    show_name: str,
    publisher: str,
) -> dict[str, Any]:
    """An `EpisodeObject` — `SimplifiedEpisodeObject` plus `show`."""
    episode = simplified_episode(episode_id, name, description)
    episode["show"] = show_object("show-1", show_name, publisher)
    return episode


def show_object(show_id: str, name: str, publisher: str) -> dict[str, Any]:
    return {
        "id": show_id,
        "name": name,
        "publisher": publisher,
        "description": "A weekly show about computing history.",
        "html_description": "<p>A weekly show about computing history.</p>",
        "total_episodes": 42,
        "media_type": "audio",
        "explicit": False,
        "languages": ["en"],
        "type": "show",
        "uri": f"spotify:show:{show_id}",
        "href": f"{API}/shows/{show_id}",
        "images": [],
        "is_externally_hosted": False,
        "copyrights": [],
        "available_markets": ["US"],
        "external_urls": {"spotify": f"https://open.spotify.com/show/{show_id}"},
    }


def paging(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "href": "",
        "items": items,
        "limit": len(items),
        "next": None,
        "offset": 0,
        "previous": None,
        "total": len(items),
    }


class _FakeSpotify:
    """A stand-in for `_request_json`, routing on `(method, url)`.

    Records every call so tests can assert on the exact endpoints and
    query parameters the servicer chose — which is where "don't guess
    endpoints or field names" actually gets enforced.

    **Never raises**, exactly like the real `_request_json`. An
    unstubbed URL comes back as a failure envelope rather than an
    `AssertionError`: a raise here would happen inside an
    `at_least_once` callable, and the runtime's response to that is to
    replay the workflow forever — so a mis-stubbed test would hang the
    suite instead of failing it. Unexpected URLs are recorded in
    `unexpected`, which `asyncTearDown` asserts is empty.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # url -> envelope, or url -> list of envelopes consumed in order.
        self.responses: dict[str, Any] = {}
        self.default: Optional[dict[str, Any]] = None
        self.unexpected: list[str] = []

    def ok(self, url: str, body: dict[str, Any]) -> None:
        self.responses[url] = {"ok": True, "status": 200, "error": "", "body": body}

    def sequence(self, url: str, bodies: list[dict[str, Any]]) -> None:
        self.responses[url] = [
            {"ok": True, "status": 200, "error": "", "body": body}
            for body in bodies
        ]

    def fail(self, url: str, status: int, error: str) -> None:
        self.responses[url] = {
            "ok": False, "status": status, "error": error, "body": {}
        }

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "params": params or {},
                "data": data or {},
            }
        )
        canned = self.responses.get(url, self.default)
        if canned is None:
            self.unexpected.append(f"{method} {url}")
            return {
                "ok": False,
                "status": 599,
                "error": f"test fixture has no response for {method} {url}",
                "body": {},
            }
        if isinstance(canned, list):
            if not canned:
                self.unexpected.append(f"{method} {url} (responses exhausted)")
                return {
                    "ok": False,
                    "status": 599,
                    "error": f"test fixture ran out of responses for {url}",
                    "body": {},
                }
            return canned.pop(0)
        return canned

    def params_for(self, url: str) -> dict[str, str]:
        for call in self.calls:
            if call["url"] == url:
                return call["params"]
        raise AssertionError(f"no call recorded for {url}")

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def count(self, url: str) -> int:
        """How many times `url` has been hit.

        Compare *deltas*, not absolute values: the test harness re-runs
        workflow steps to validate their effects ("Re-running block with
        idempotency alias ... to validate effects"), so every stubbed
        call is observed more than once. The multiplier is an artifact
        of the harness; a delta of zero still means "no new request".
        """
        return len([call for call in self.calls if call["url"] == url])


class SpotifyTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.rbt = Reboot()
        await self.rbt.start()
        await self.rbt.up(
            Application(
                # The REAL servicers, with their REAL authorizers.
                servicers=SPOTIFY_SERVICERS,
                initialize=initialize,
            ),
        )
        self.user_id = "test-user"
        self.context = await self.rbt.create_external_context_as(
            name=f"test-{self.id()}",
            user_id=self.user_id,
        )
        self.lookup = SpotifyLookup.ref(SPOTIFY_LOOKUP_ID)

        self.spotify = _FakeSpotify()
        self.spotify.ok(TOKEN_URL, token_body())
        patcher = mock.patch.object(
            spotify_servicers, "_request_json", self.spotify
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.rbt.stop()
        # A mis-stubbed URL degrades to a 599 rather than hanging the
        # suite; surface it here so it still fails the test.
        self.assertEqual(self.spotify.unexpected, [])

    def stub_guests(self, names_by_episode: dict[str, list[str]]) -> None:
        """Replace the LLM call with a lookup table."""

        async def fake(
            context: WorkflowContext,
            episode_id: str,
            episode_name: str,
            description: str,
        ) -> list[str]:
            return names_by_episode.get(episode_id, [])

        patcher = mock.patch.object(
            spotify_servicers, "infer_guest_names", fake
        )
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# The token actor.
# ---------------------------------------------------------------------------


class TestSpotifyToken(SpotifyTestCase):

    async def test_token_is_fetched_once_and_then_reused(self) -> None:
        """The documented request shape, and the cache.

        POST to `accounts.spotify.com/api/token`, credentials in an
        `Authorization: Basic` header (**not** the body), form-encoded
        `grant_type=client_credentials`. A second lookup reuses the
        cached token: the token is a credential, not Spotify content, so
        caching it until expiry is the intended behaviour.
        """
        self.spotify.ok(
            f"{API}/search", {"episodes": paging([simplified_episode("e1", "A", "d")])}
        )
        self.spotify.ok(
            f"{API}/episodes/e1", full_episode("e1", "A", "d", "Show", "Pub")
        )
        with mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        ):
            await self.lookup.search_episodes(self.context, topic="babbage")
            after_first = self.spotify.count(TOKEN_URL)
            await self.lookup.search_episodes(self.context, topic="lovelace")
            after_second = self.spotify.count(TOKEN_URL)

        self.assertGreater(after_first, 0)
        # The cached token was still fresh: the second lookup issued no
        # new token request at all.
        self.assertEqual(after_second, after_first, self.spotify.urls())

        call = next(c for c in self.spotify.calls if c["url"] == TOKEN_URL)
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["data"], {"grant_type": "client_credentials"})
        self.assertTrue(call["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(
            call["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )
        # Credentials go in the header, never the body.
        self.assertNotIn("client_id", call["data"])
        self.assertNotIn("client_secret", call["data"])

    async def test_an_expired_token_is_refetched(self) -> None:
        # `expires_in=0` means the token is already stale when stored.
        self.spotify.ok(TOKEN_URL, token_body(expires_in=0))
        self.spotify.ok(f"{API}/search", {"episodes": paging([])})
        with mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        ):
            await self.lookup.search_episodes(self.context, topic="a")
            after_first = self.spotify.count(TOKEN_URL)
            await self.lookup.search_episodes(self.context, topic="b")
            after_second = self.spotify.count(TOKEN_URL)

        # Unlike the fresh-token case, the second lookup *did* go back
        # to the token endpoint.
        self.assertGreater(after_second, after_first)

    async def test_the_token_actor_is_not_reachable_by_a_signed_in_user(
        self,
    ) -> None:
        """The app's own bearer credential must not be extractable.

        `SpotifyToken`'s authorizer is `all=[is_app_internal]`, unlike
        the lookup's `any=[is_app_internal, has_verified_token]`. A
        verified end user calling it directly is denied.
        """
        with self.assertRaises(SpotifyToken.AccessTokenAborted) as caught:
            await SpotifyToken.ref(SPOTIFY_TOKEN_ID).access_token(self.context)
        self.assertIn("PermissionDenied", str(caught.exception))


# ---------------------------------------------------------------------------
# Searching all of Spotify.
# ---------------------------------------------------------------------------


class TestSearchAcrossSpotify(SpotifyTestCase):

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_search_hits_the_documented_endpoint_with_a_market(
        self,
    ) -> None:
        """`market` is not optional for us.

        Spotify's spec: "If neither market or user country are provided,
        the content is considered unavailable for the client." A Client
        Credentials token has no user country, so omitting `market`
        silently returns nothing.
        """
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "Babbage", "About it")])},
        )
        self.spotify.ok(
            f"{API}/episodes/e1",
            full_episode("e1", "Babbage", "About it", "Signals & Noise", "Acme"),
        )

        await self.lookup.search_episodes(self.context, topic="babbage")

        params = self.spotify.params_for(f"{API}/search")
        self.assertEqual(params["q"], "babbage")
        self.assertEqual(params["type"], "episode")
        self.assertEqual(params["market"], "US")

    async def test_limit_is_clamped_to_spotifys_maximum(self) -> None:
        """Spotify's `/search` caps `limit` at 10; asking for 50 is a 400."""
        self.spotify.ok(f"{API}/search", {"episodes": paging([])})

        await self.lookup.search_episodes(self.context, topic="x", limit=50)

        params = self.spotify.params_for(f"{API}/search")
        self.assertEqual(params["limit"], str(MAX_SEARCH_LIMIT))

    async def test_the_show_is_hydrated_because_search_results_lack_one(
        self,
    ) -> None:
        """Search returns `SimplifiedEpisodeObject`s, which have no `show`.

        The only way to learn the podcast an episode belongs to is
        `GET /episodes/{id}`.
        """
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "Babbage", "d")])},
        )
        self.spotify.ok(
            f"{API}/episodes/e1",
            full_episode("e1", "Babbage", "d", "Signals & Noise", "Acme Audio"),
        )

        response = await self.lookup.search_episodes(self.context, topic="babbage")

        self.assertIn(f"{API}/episodes/e1", self.spotify.urls())
        self.assertEqual(len(response.matches), 1)
        match = response.matches[0]
        self.assertEqual(match.episode_name, "Babbage")
        self.assertEqual(match.show_name, "Signals & Noise")
        self.assertEqual(match.show_publisher, "Acme Audio")
        self.assertEqual(match.release_date, "2024-03-05")
        self.assertEqual(match.release_date_precision, "day")
        self.assertEqual(match.duration_ms, 1686230)

    async def test_every_result_carries_attribution_and_a_spotify_link(
        self,
    ) -> None:
        """A Developer Terms requirement, not a nicety."""
        self.spotify.ok(
            f"{API}/search",
            {
                "episodes": paging(
                    [
                        simplified_episode("e1", "One", "d"),
                        simplified_episode("e2", "Two", "d"),
                    ]
                )
            },
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "One", "d", "S", "P"))
        self.spotify.ok(f"{API}/episodes/e2", full_episode("e2", "Two", "d", "S", "P"))

        response = await self.lookup.search_episodes(self.context, topic="x")

        self.assertEqual(response.attribution, SPOTIFY_ATTRIBUTION)
        self.assertEqual(len(response.matches), 2)
        for match in response.matches:
            self.assertTrue(
                match.spotify_url.startswith("https://open.spotify.com/episode/"),
                match.spotify_url,
            )

    async def test_a_missing_external_url_still_yields_a_link(self) -> None:
        """The link back to Spotify is required, so it is derived if absent."""
        episode = simplified_episode("e9", "No URL", "d")
        del episode["external_urls"]
        self.spotify.ok(f"{API}/search", {"episodes": paging([episode])})
        self.spotify.fail(f"{API}/episodes/e9", 404, "not found")

        response = await self.lookup.search_episodes(self.context, topic="x")

        self.assertEqual(
            response.matches[0].spotify_url,
            "https://open.spotify.com/episode/e9",
        )

    async def test_a_failed_hydration_degrades_rather_than_failing(self) -> None:
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "Babbage", "d")])},
        )
        self.spotify.fail(f"{API}/episodes/e1", 500, "boom")

        response = await self.lookup.search_episodes(self.context, topic="x")

        self.assertEqual(len(response.matches), 1)
        self.assertEqual(response.matches[0].episode_name, "Babbage")
        self.assertEqual(response.matches[0].show_name, "")
        self.assertIn("show resolved", response.message)

    async def test_no_results_is_an_empty_response_not_an_error(self) -> None:
        self.spotify.ok(f"{API}/search", {"episodes": paging([])})

        response = await self.lookup.search_episodes(self.context, topic="nothing")

        self.assertEqual(response.matches, [])
        self.assertIn("no episodes", response.message)

    async def test_a_missing_episodes_key_is_treated_as_no_results(self) -> None:
        """Spotify omits a type's key entirely when it had no hits."""
        self.spotify.ok(f"{API}/search", {})

        response = await self.lookup.search_episodes(self.context, topic="nothing")

        self.assertEqual(response.matches, [])

    async def test_search_never_infers_guests(self) -> None:
        """`search_episodes` must leave `inferred_guest_names` empty."""
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "A", "With Ada Lovelace")])},
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "A", "d", "S", "P"))

        response = await self.lookup.search_episodes(self.context, topic="x")

        self.assertEqual(response.matches[0].inferred_guest_names, [])
        self.assertIsNone(response.inference_caveat)


# ---------------------------------------------------------------------------
# Searching within a named show.
# ---------------------------------------------------------------------------


class TestSearchWithinShow(SpotifyTestCase):

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _show_with_episodes(
        self, episodes: list[dict[str, Any]], name: str = "Signals & Noise"
    ) -> None:
        self.spotify.ok(
            f"{API}/search", {"shows": paging([show_object("show-1", name, "Acme")])}
        )
        self.spotify.ok(f"{API}/shows/show-1/episodes", paging(episodes))

    async def test_a_named_show_is_resolved_then_its_episodes_are_scanned(
        self,
    ) -> None:
        """Spotify has no "search within a show" — this is why.

        `/search`'s field filters (album, artist, track, year, upc, tag,
        isrc, genre) don't apply to episodes, and a search result
        carries no show to check against, so the only honest way to
        honour `show_name` is to resolve the show and list its episodes.
        """
        self._show_with_episodes(
            [
                simplified_episode("e1", "The Analytical Engine", "Babbage's designs"),
                simplified_episode("e2", "Punch Cards", "Jacquard looms"),
            ]
        )

        response = await self.lookup.search_episodes(
            self.context, topic="babbage", show_name="Signals & Noise"
        )

        self.assertEqual(
            self.spotify.params_for(f"{API}/search")["type"], "show"
        )
        self.assertIn(f"{API}/shows/show-1/episodes", self.spotify.urls())
        self.assertEqual(len(response.matches), 1)
        self.assertEqual(response.matches[0].episode_name, "The Analytical Engine")
        # The show is already known here — no per-episode hydration needed.
        self.assertEqual(response.matches[0].show_name, "Signals & Noise")
        self.assertNotIn(f"{API}/episodes/e1", self.spotify.urls())

    async def test_the_topic_matches_the_description_too(self) -> None:
        self._show_with_episodes(
            [simplified_episode("e1", "Episode 12", "All about Babbage")]
        )

        response = await self.lookup.search_episodes(
            self.context, topic="BABBAGE", show_name="Signals & Noise"
        )

        self.assertEqual(len(response.matches), 1)

    async def test_an_unknown_show_says_so_instead_of_searching_everything(
        self,
    ) -> None:
        self.spotify.ok(f"{API}/search", {"shows": paging([])})

        response = await self.lookup.search_episodes(
            self.context, topic="babbage", show_name="Does Not Exist"
        )

        self.assertEqual(response.matches, [])
        self.assertIn("no show matching", response.message)

    async def test_an_inexact_show_match_is_disclosed(self) -> None:
        self._show_with_episodes(
            [simplified_episode("e1", "Ep", "Babbage")], name="Signals and Noise"
        )

        response = await self.lookup.search_episodes(
            self.context, topic="babbage", show_name="Signals & Noise"
        )

        self.assertIn("closest match", response.message)
        self.assertIn("Signals and Noise", response.message)

    async def test_the_episode_scan_is_capped(self) -> None:
        """A daily show must not turn one tool call into 40 round trips."""
        page = [
            simplified_episode(f"e{i}", f"Episode {i}", "Babbage")
            for i in range(50)
        ]
        self.spotify.ok(
            f"{API}/search",
            {"shows": paging([show_object("show-1", "Signals & Noise", "Acme")])},
        )
        self.spotify.default = {
            "ok": True, "status": 200, "error": "", "body": paging(page)
        }
        self.spotify.responses.pop(f"{API}/shows/show-1/episodes", None)

        response = await self.lookup.search_episodes(
            self.context, topic="babbage", show_name="Signals & Noise", limit=10
        )

        # Distinct offsets, not raw call count: the harness re-runs each
        # workflow step to validate effects, so calls are observed more
        # than once (see `_FakeSpotify.count`).
        offsets = sorted(
            {
                int(call["params"]["offset"])
                for call in self.spotify.calls
                if call["url"].endswith("/episodes")
            }
        )
        self.assertEqual(offsets, [0, 50, 100, 150])
        self.assertEqual(len(offsets), MAX_SHOW_EPISODES_SCANNED // 50)
        self.assertEqual(len(response.matches), 10)
        self.assertIn("Only the first", response.message)

    async def test_show_episode_paging_uses_the_larger_limit(self) -> None:
        """`/shows/{id}/episodes` caps at 50, not the 10 `/search` caps at."""
        self._show_with_episodes([simplified_episode("e1", "Ep", "Babbage")])

        await self.lookup.search_episodes(
            self.context, topic="babbage", show_name="Signals & Noise"
        )

        params = self.spotify.params_for(f"{API}/shows/show-1/episodes")
        self.assertEqual(params["limit"], "50")
        self.assertEqual(params["market"], "US")


# ---------------------------------------------------------------------------
# Inferred guests.
# ---------------------------------------------------------------------------


class TestInferredGuests(SpotifyTestCase):

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_guests_are_labelled_inferred_and_carry_a_caveat(self) -> None:
        """The field is `inferred_guest_names`, never `guest_names`.

        Spotify's episode schema has no guest or host field at all, so
        these names are a model's reading of prose and must never be
        presented as confirmed metadata.
        """
        self.spotify.ok(
            f"{API}/search",
            {
                "episodes": paging(
                    [simplified_episode("e1", "A", "Our guest is Ada Lovelace.")]
                )
            },
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "A", "d", "S", "P"))
        self.stub_guests({"e1": ["Ada Lovelace"]})

        response = await self.lookup.search_episodes_with_guests(
            self.context, topic="babbage"
        )

        self.assertEqual(
            response.matches[0].inferred_guest_names, ["Ada Lovelace"]
        )
        assert response.inference_caveat is not None
        self.assertIn("inferred", response.inference_caveat.lower())
        self.assertIn("not confirmed", response.inference_caveat.lower())
        # The response model has no `guest_names` field, inferred or not.
        self.assertFalse(hasattr(response.matches[0], "guest_names"))

    async def test_an_episode_with_no_identifiable_guest_gets_an_empty_list(
        self,
    ) -> None:
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "Solo", "Just the host.")])},
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "Solo", "d", "S", "P"))
        self.stub_guests({})

        response = await self.lookup.search_episodes_with_guests(
            self.context, topic="x"
        )

        self.assertEqual(response.matches[0].inferred_guest_names, [])

    async def test_inference_runs_per_episode(self) -> None:
        seen: list[str] = []

        async def fake(
            context: WorkflowContext,
            episode_id: str,
            episode_name: str,
            description: str,
        ) -> list[str]:
            seen.append(episode_id)
            return [f"guest-of-{episode_id}"]

        patcher = mock.patch.object(spotify_servicers, "infer_guest_names", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.spotify.ok(
            f"{API}/search",
            {
                "episodes": paging(
                    [
                        simplified_episode("e1", "One", "d"),
                        simplified_episode("e2", "Two", "d"),
                    ]
                )
            },
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "One", "d", "S", "P"))
        self.spotify.ok(f"{API}/episodes/e2", full_episode("e2", "Two", "d", "S", "P"))

        response = await self.lookup.search_episodes_with_guests(
            self.context, topic="x"
        )

        # Deduplicated, order-preserving: the harness re-runs the
        # workflow to validate effects, so `seen` records each episode
        # more than once (see `_FakeSpotify.count`). What matters is
        # *which* episodes were asked about, and in what order.
        distinct = list(dict.fromkeys(seen))
        self.assertEqual(distinct, ["e1", "e2"])
        self.assertEqual(
            [match.inferred_guest_names for match in response.matches],
            [["guest-of-e1"], ["guest-of-e2"]],
        )


# ---------------------------------------------------------------------------
# Failure handling.
# ---------------------------------------------------------------------------


class TestFailures(SpotifyTestCase):

    def setUp(self) -> None:
        self.env = mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_bad_credentials_abort_rather_than_retry_forever(self) -> None:
        """A declared abort stops the workflow.

        These lookups are on a chat tool's synchronous request path; an
        undeclared exception would replay the workflow indefinitely
        while the caller's RPC hangs with no explanation.
        """
        self.spotify.fail(TOKEN_URL, 400, "Spotify returned HTTP 400: invalid_client")

        with self.assertRaises(SpotifyLookup.SearchEpisodesAborted) as caught:
            await self.lookup.search_episodes(self.context, topic="x")

        error = caught.exception.error
        assert isinstance(error, SpotifyUnavailable)
        self.assertEqual(error.status, 400)
        self.assertFalse(error.retryable)
        self.assertIn("invalid_client", error.reason)

    async def test_a_rate_limit_is_reported_as_retryable(self) -> None:
        self.spotify.fail(f"{API}/search", 429, "Spotify returned HTTP 429")

        with self.assertRaises(SpotifyLookup.SearchEpisodesAborted) as caught:
            await self.lookup.search_episodes(self.context, topic="x")

        error = caught.exception.error
        assert isinstance(error, SpotifyUnavailable)
        self.assertEqual(error.status, 429)
        self.assertTrue(error.retryable)

    async def test_a_network_failure_is_retryable_with_status_zero(self) -> None:
        self.spotify.fail(f"{API}/search", 0, "network error contacting Spotify")

        with self.assertRaises(SpotifyLookup.SearchEpisodesAborted) as caught:
            await self.lookup.search_episodes(self.context, topic="x")

        error = caught.exception.error
        assert isinstance(error, SpotifyUnavailable)
        self.assertTrue(error.retryable)
        self.assertEqual(error.status, 0)

    async def test_a_token_response_without_an_access_token_aborts(self) -> None:
        self.spotify.ok(TOKEN_URL, {"token_type": "Bearer", "expires_in": 3600})

        with self.assertRaises(SpotifyLookup.SearchEpisodesAborted) as caught:
            await self.lookup.search_episodes(self.context, topic="x")

        error = caught.exception.error
        assert isinstance(error, SpotifyUnavailable)
        self.assertIn("no access_token", error.reason)


# ---------------------------------------------------------------------------
# Nothing is persisted.
# ---------------------------------------------------------------------------


class TestNothingIsStored(SpotifyTestCase):

    async def test_lookup_state_stays_empty_across_searches(self) -> None:
        """The product/legal constraint, asserted.

        Spotify's Developer Terms forbid building a persistent database
        of their content, so `SpotifyLookup` must accumulate nothing.
        """
        self.spotify.ok(
            f"{API}/search",
            {"episodes": paging([simplified_episode("e1", "Babbage", "d")])},
        )
        self.spotify.ok(f"{API}/episodes/e1", full_episode("e1", "A", "d", "S", "P"))

        with mock.patch.dict(
            "os.environ",
            {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"},
        ):
            await self.lookup.search_episodes(self.context, topic="babbage")
            await self.lookup.search_episodes(self.context, topic="lovelace")

        # `SpotifyLookupState` declares no fields at all — there is
        # nowhere for Spotify content to land, by construction.
        from searchpod.v1.spotify import SpotifyLookupState

        self.assertEqual(list(SpotifyLookupState.model_fields.keys()), [])
