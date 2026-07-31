"""One test per user story from the searchpod design.

Every test drives the app the way a signed-in user would: through
`Service.ref(id).method(context, ...)` with a context carrying a real
verified identity (`create_external_context_as`). The servicers under
test are the exact ones `main.py` registers, authorizers included.
"""

import unittest
from typing import Optional

from reboot.aio.aborted import Aborted
from reboot.aio.applications import Application
from reboot.aio.external import InitializeContext
from reboot.aio.tests import Reboot
from reboot.std.collections.ordered_map.v1.ordered_map import (
    ordered_map_library,
)

from searchpod.v1.podcasts import DIRECTORY_ID, Chapter
from searchpod.v1.podcasts_rbt import Directory, Episode, Person, Podcast
from servicers.podcasts import APPLICATION_SERVICERS

SIGNALS_FEED = "https://example.com/signals.xml"
ARCHIVE_FEED = "https://example.com/archive.xml"


async def initialize(context: InitializeContext) -> None:
    """The same singleton bootstrap `main.py` runs."""
    await Directory.create(context, DIRECTORY_ID)


class TestSearchpod(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.rbt = Reboot()
        await self.rbt.start()
        await self.rbt.up(
            Application(
                # The REAL servicers, with their REAL authorizers.
                servicers=APPLICATION_SERVICERS,
                libraries=[ordered_map_library()],
                initialize=initialize,
            ),
        )
        self.user_id = "test-user"
        self.context = await self.rbt.create_external_context_as(
            name=f"test-{self.id()}",
            user_id=self.user_id,
        )
        self.directory = Directory.ref(DIRECTORY_ID)

    async def asyncTearDown(self) -> None:
        await self.rbt.stop()

    # -- Fixtures. ----------------------------------------------------------

    async def _add_signals_podcast(self) -> str:
        response = await self.directory.add_podcast(
            self.context,
            name="Signals & Noise",
            feed_url=SIGNALS_FEED,
            description="A weekly show about computing history.",
        )
        return response.podcast_id

    async def _add_analytical_engine_episode(self, podcast_id: str) -> str:
        """The workhorse fixture: chapters plus a guest.

        Chapter starts are deliberately out of order in the request so the
        derived end times prove they are computed from sorted starts, not
        from request order.
        """
        response = await self.directory.add_episode(
            self.context,
            podcast_id=podcast_id,
            source_id="signals-ep-12",
            title="The Analytical Engine",
            publish_date="2024-03-05",
            description=(
                "We dig into Babbage's designs and how Victorian "
                "engineering culture received them."
            ),
            chapters=[
                Chapter(title="Punch cards", start_time_seconds=1180),
                Chapter(title="Cold open", start_time_seconds=0),
                Chapter(title="Babbage's designs", start_time_seconds=240),
            ],
            guest_names=["Ada Lovelace"],
        )
        return response.episode_id

    # -- Story 1. -----------------------------------------------------------

    async def test_adding_an_episode_with_a_guest_creates_a_findable_person(
        self,
    ) -> None:
        podcast_id = await self._add_signals_podcast()
        episode_id = await self._add_analytical_engine_episode(podcast_id)

        # The episode reads back with its guest hydrated by name.
        episode = await Episode.ref(episode_id).get(self.context)
        self.assertEqual(episode.title, "The Analytical Engine")
        self.assertEqual(episode.podcast_name, "Signals & Noise")
        self.assertEqual(episode.guest_names, ["Ada Lovelace"])
        self.assertEqual(len(episode.chapters), 3)

        # And the guest is now a Person the user can look up by name.
        found = await self.directory.find_person(self.context, name="ada lovelace")
        self.assertEqual(len(found.matches), 1)
        self.assertEqual(found.matches[0].name, "Ada Lovelace")

        person = await Person.ref(found.matches[0].person_id).get(self.context)
        self.assertEqual(person.name, "Ada Lovelace")

    # -- Story 2. -----------------------------------------------------------

    async def test_re_adding_the_same_feed_url_does_not_duplicate_the_podcast(
        self,
    ) -> None:
        first = await self.directory.add_podcast(
            self.context,
            name="Signals & Noise",
            feed_url=SIGNALS_FEED,
            description="A weekly show about computing history.",
        )
        self.assertTrue(first.created)

        second = await self.directory.add_podcast(
            self.context,
            name="Signals and Noise",  # Different name, same feed.
            feed_url=SIGNALS_FEED,
            description="Re-submitted by the ingestion job.",
        )
        self.assertFalse(second.created)
        self.assertEqual(second.podcast_id, first.podcast_id)

        # The catalog still holds exactly one show.
        listed = await self.directory.list_podcasts(self.context, cursor="")
        self.assertEqual(len(listed.podcasts), 1)
        self.assertEqual(listed.podcasts[0].podcast_id, first.podcast_id)

    # -- Story 3. -----------------------------------------------------------

    async def test_re_adding_the_same_source_id_does_not_duplicate_the_episode(
        self,
    ) -> None:
        podcast_id = await self._add_signals_podcast()
        episode_id = await self._add_analytical_engine_episode(podcast_id)

        again = await self.directory.add_episode(
            self.context,
            podcast_id=podcast_id,
            source_id="signals-ep-12",  # Same source id.
            title="The Analytical Engine (re-run)",
            publish_date="2024-03-05",
            description="Re-scraped from the feed.",
            chapters=[],
            guest_names=["Ada Lovelace"],
        )
        self.assertFalse(again.created)
        self.assertEqual(again.episode_id, episode_id)

        # One episode on the show, not two.
        episodes = await Podcast.ref(podcast_id).list_episodes(
            self.context, cursor="",
        )
        self.assertEqual(len(episodes.episodes), 1)

        # ...and one appearance for the guest, not two.
        found = await self.directory.find_person(self.context, name="Ada Lovelace")
        appearances = await Person.ref(
            found.matches[0].person_id,
        ).appearances(self.context, cursor="")
        self.assertEqual(len(appearances.appearances), 1)

    # -- Story 4. -----------------------------------------------------------

    async def test_chapter_match_reports_a_start_and_a_derived_end_time(
        self,
    ) -> None:
        podcast_id = await self._add_signals_podcast()
        episode_id = await self._add_analytical_engine_episode(podcast_id)

        result = await self.directory.search_mentions(
            self.context, topic="babbage's designs", podcast_name="", cursor="",
        )
        self.assertEqual(len(result.matches), 1)

        match = result.matches[0]
        self.assertEqual(match.episode_id, episode_id)
        self.assertEqual(match.matched_chapter_title, "Babbage's designs")
        self.assertEqual(match.start_time_seconds, 240)
        # Derived: the *next* chapter ("Punch cards") starts at 1180.
        self.assertEqual(match.end_time_seconds, 1180)

        # The last chapter has no end time — it runs to the end of the
        # episode.
        last = await self.directory.search_mentions(
            self.context, topic="punch cards", podcast_name="", cursor="",
        )
        self.assertEqual(len(last.matches), 1)
        self.assertEqual(last.matches[0].start_time_seconds, 1180)
        self.assertIsNone(last.matches[0].end_time_seconds)

    # -- Story 5. -----------------------------------------------------------

    async def test_description_only_match_claims_no_timeframe(self) -> None:
        podcast_id = await self._add_signals_podcast()
        episode_id = await self._add_analytical_engine_episode(podcast_id)

        # "Victorian" appears in the description but in no chapter title.
        result = await self.directory.search_mentions(
            self.context, topic="Victorian", podcast_name="", cursor="",
        )
        self.assertEqual(len(result.matches), 1)

        match = result.matches[0]
        self.assertEqual(match.episode_id, episode_id)
        self.assertEqual(match.episode_title, "The Analytical Engine")
        self.assertIsNone(match.matched_chapter_title)
        self.assertIsNone(match.start_time_seconds)
        self.assertIsNone(match.end_time_seconds)

    # -- Story 6. -----------------------------------------------------------

    async def test_search_scoped_to_a_podcast_name_excludes_other_shows(
        self,
    ) -> None:
        signals_id = await self._add_signals_podcast()
        await self._add_analytical_engine_episode(signals_id)

        archive = await self.directory.add_podcast(
            self.context,
            name="The Archive Hour",
            feed_url=ARCHIVE_FEED,
            description="Long-form interviews.",
        )
        await self.directory.add_episode(
            self.context,
            podcast_id=archive.podcast_id,
            source_id="archive-ep-3",
            title="Looms and Logic",
            publish_date="2024-04-01",
            description="A different show that also covers punch cards.",
            chapters=[Chapter(title="Punch cards", start_time_seconds=90)],
            guest_names=["Grace Hopper"],
        )

        # Unscoped, the topic hits both shows.
        everywhere = await self.directory.search_mentions(
            self.context, topic="punch cards", podcast_name="", cursor="",
        )
        self.assertEqual(
            {match.podcast_name for match in everywhere.matches},
            {"Signals & Noise", "The Archive Hour"},
        )

        # Scoped, only the named show comes back — with its guest on the
        # match, which is what answers "who was the guest when they
        # discussed this".
        scoped = await self.directory.search_mentions(
            self.context,
            topic="punch cards",
            podcast_name="Archive Hour",
            cursor="",
        )
        self.assertEqual(len(scoped.matches), 1)
        self.assertEqual(scoped.matches[0].podcast_name, "The Archive Hour")
        self.assertEqual(scoped.matches[0].episode_title, "Looms and Logic")
        self.assertEqual(scoped.matches[0].guest_names, ["Grace Hopper"])

    # -- Story 7. -----------------------------------------------------------

    async def test_a_guest_appearances_span_every_podcast_they_were_on(
        self,
    ) -> None:
        signals_id = await self._add_signals_podcast()
        await self._add_analytical_engine_episode(signals_id)

        archive = await self.directory.add_podcast(
            self.context,
            name="The Archive Hour",
            feed_url=ARCHIVE_FEED,
            description="Long-form interviews.",
        )
        await self.directory.add_episode(
            self.context,
            podcast_id=archive.podcast_id,
            source_id="archive-ep-7",
            title="On Notation",
            publish_date="2024-05-20",
            description="Notation, and why it decides what you can think.",
            chapters=[Chapter(title="Notation", start_time_seconds=30)],
            # Different capitalization and spacing: still the same person.
            guest_names=["  ada lovelace  "],
        )

        # The name resolves to exactly one Person despite the two spellings.
        found = await self.directory.find_person(self.context, name="Lovelace")
        self.assertEqual(len(found.matches), 1)

        appearances = await Person.ref(
            found.matches[0].person_id,
        ).appearances(self.context, cursor="")
        self.assertEqual(len(appearances.appearances), 2)

        # Both shows are represented, in chronological order.
        self.assertEqual(
            [a.podcast_name for a in appearances.appearances],
            ["Signals & Noise", "The Archive Hour"],
        )
        self.assertEqual(
            [a.episode_title for a in appearances.appearances],
            ["The Analytical Engine", "On Notation"],
        )
        # Each appearance carries what the person talked about.
        self.assertEqual(
            [c.title for c in appearances.appearances[1].chapters],
            ["Notation"],
        )

    # -- Story 8. -----------------------------------------------------------

    async def test_writes_without_an_auth_token_are_rejected(self) -> None:
        # A context with no identity at all — the authorizer's
        # `has_verified_token` arm must fail, and `is_app_internal` must
        # not save it, because this call comes from outside the app.
        anonymous = self.rbt.create_external_context(
            name=f"anonymous-{self.id()}",
        )

        with self.assertRaises(Aborted):
            await Directory.ref(DIRECTORY_ID).add_podcast(
                anonymous,
                name="Signals & Noise",
                feed_url=SIGNALS_FEED,
                description="Should never be created.",
            )

        # A second anonymous context: the first one is now in an uncertain
        # state after a failed mutation, so it can't be reused.
        anonymous_again = self.rbt.create_external_context(
            name=f"anonymous-again-{self.id()}",
        )
        podcast_id = await self._add_signals_podcast()
        with self.assertRaises(Aborted):
            await Directory.ref(DIRECTORY_ID).add_episode(
                anonymous_again,
                podcast_id=podcast_id,
                source_id="signals-ep-99",
                title="Should never be created",
                publish_date="2024-06-01",
                description="",
                chapters=[],
                guest_names=[],
            )

        # Nothing the anonymous caller attempted landed: the authenticated
        # user sees only the podcast they added themselves, with no
        # episodes.
        listed = await self.directory.list_podcasts(self.context, cursor="")
        self.assertEqual(len(listed.podcasts), 1)
        self.assertEqual(listed.podcasts[0].name, "Signals & Noise")

        episodes = await Podcast.ref(podcast_id).list_episodes(
            self.context, cursor="",
        )
        self.assertEqual(episodes.episodes, [])


    # -- Not a user story, but the cursor mechanism every paginated
    # -- reader shares, which the stories above never fill a page of.

    async def test_paging_walks_every_episode_exactly_once(self) -> None:
        podcast_id = await self._add_signals_podcast()

        # More than one page (PAGE_SIZE is 32).
        expected = []
        for index in range(40):
            response = await self.directory.add_episode(
                self.context,
                podcast_id=podcast_id,
                source_id=f"signals-ep-{index}",
                title=f"Episode {index}",
                # Spread across days so the chronological keys differ.
                publish_date=f"2024-03-{(index % 28) + 1:02d}",
                description="Every episode mentions punch cards.",
                chapters=[],
                guest_names=[],
            )
            expected.append(response.episode_id)

        # Walk `list_episodes` page by page.
        seen: list[str] = []
        cursor: Optional[str] = ""
        while cursor is not None:
            page = await Podcast.ref(podcast_id).list_episodes(
                self.context, cursor=cursor,
            )
            seen.extend(episode.episode_id for episode in page.episodes)
            cursor = page.next_cursor or None
        self.assertEqual(len(seen), 40)
        self.assertEqual(len(set(seen)), 40, "an episode was paged twice")
        self.assertEqual(set(seen), set(expected))

        # And `search_mentions`, whose cursor tracks episodes *examined*
        # rather than episodes matched.
        found: list[str] = []
        cursor = ""
        while cursor is not None:
            result = await self.directory.search_mentions(
                self.context,
                topic="punch cards",
                podcast_name="",
                cursor=cursor,
            )
            found.extend(match.episode_id for match in result.matches)
            cursor = result.next_cursor or None
        self.assertEqual(len(found), 40)
        self.assertEqual(set(found), set(expected))


if __name__ == "__main__":
    unittest.main()
