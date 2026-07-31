"""Servicers for the searchpod catalog.

Search is plain case-insensitive substring matching — no LLM anywhere —
so every method here is a Reader, Writer, or Transaction.

Two conventions run through the whole file:

- **Chapter end times are always derived, never stored.** `chapter_views`
  is the single source of truth: a chapter ends where the next one
  begins, and the last chapter of an episode has no end time. Everything
  that reports a timeframe goes through `Episode.get`, which calls it.
- **Index ids stay private to the actor that owns them.** `Directory`
  never reaches into a `Podcast`'s episode indexes or a `Person`'s
  appearance index; it calls an internal (`mcp=None`) method on the
  owner instead.
"""

from typing import Any, Optional, Sequence
from uuid import uuid4

from reboot.aio.auth.authorizers import (
    AuthorizerRule,
    allow_if,
    has_verified_token,
    is_app_internal,
)
from reboot.aio.contexts import (
    ReaderContext,
    TransactionContext,
    WriterContext,
)
from reboot.std.collections.ordered_map.v1.ordered_map import OrderedMap
from uuid7 import create as uuid7

from searchpod.v1.podcasts import (
    PAGE_SIZE,
    AppearanceSummary,
    Chapter,
    ChapterView,
    EpisodeMatch,
    EpisodeSummary,
    PersonMatch,
    PodcastMatch,
    PodcastSummary,
)
from searchpod.v1.podcasts_rbt import (
    Directory,
    Episode,
    Person,
    Podcast,
    User,
)

# The most index entries `find_person` / `find_podcast` will scan before
# giving up. These are name lookups over a personal catalog, not a search
# engine; the cap keeps a pathological catalog from turning one tool call
# into an unbounded scan.
MAX_NAME_SCAN = 1024


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def normalize(name: str) -> str:
    """The dedup/lookup key for a person or podcast name."""
    return name.strip().lower()


def date_key(publish_date: str) -> str:
    """A chronological, globally unique OrderedMap key for an episode.

    ISO dates are fixed width, so lexicographic order is date order; the
    UUIDv7 suffix makes same-day episodes unique and stably ordered.
    """
    return f"{publish_date}#{uuid7()}"


def next_cursor(entries: Sequence[Any], limit: int) -> str:
    """The cursor to resume an `OrderedMap.range` scan after `entries`.

    `range`'s `start_key` is *inclusive*, so the cursor is the last key
    seen plus the lowest possible code point — the smallest string
    strictly greater than that key. A short page means the scan reached
    the end, which is reported as an empty cursor.
    """
    if len(entries) < limit:
        return ""
    return entries[-1].key + "\x00"


def chapter_views(chapters: list[Chapter]) -> list[ChapterView]:
    """Attach derived end times to an episode's chapters.

    A chapter runs until the next one starts; the last chapter has no end
    time because it runs to the end of the episode. This is the only place
    end times are computed — readers that report a timeframe get theirs
    from `Episode.get`, which calls this.
    """
    ordered = sorted(chapters, key=lambda chapter: chapter.start_time_seconds)
    views: list[ChapterView] = []
    for index, chapter in enumerate(ordered):
        end: Optional[int] = None
        if index + 1 < len(ordered):
            end = ordered[index + 1].start_time_seconds
        views.append(
            ChapterView(
                title=chapter.title,
                start_time_seconds=chapter.start_time_seconds,
                end_time_seconds=end,
            )
        )
    return views


def catalog_authorizer() -> AuthorizerRule:
    """The rule every catalog type shares.

    `has_verified_token` lets any signed-in user read the catalog and
    hand-enter test data. `is_app_internal` is what lets the servicers
    call each other — `Directory.add_episode` reaching into `Podcast` and
    `Person` — and leaves room for a future in-app ingestion job that runs
    with no user token at all.
    """
    return allow_if(any=[is_app_internal, has_verified_token])


async def scan_index(
    context: ReaderContext,
    index: OrderedMap.WeakReference,
    max_entries: int,
) -> list[tuple[str, str]]:
    """Read up to `max_entries` `(key, value)` pairs out of an index."""
    pairs: list[tuple[str, str]] = []
    cursor = ""
    while len(pairs) < max_entries:
        page = await index.range(context, start_key=cursor, limit=PAGE_SIZE)
        for entry in page.entries:
            pairs.append((entry.key, entry.bytes.decode()))
        cursor = next_cursor(page.entries, PAGE_SIZE)
        if not cursor:
            break
    return pairs


# ---------------------------------------------------------------------------
# User — the MCP identity front door. No methods and no custom authorizer:
# the framework's default (`state_id_is_user_id` + `is_app_internal`) is
# already the right rule.
# ---------------------------------------------------------------------------


class UserServicer(User.Servicer):
    pass


# ---------------------------------------------------------------------------
# Directory.
# ---------------------------------------------------------------------------


class DirectoryServicer(Directory.Servicer):

    def authorizer(self) -> AuthorizerRule:
        return catalog_authorizer()

    async def create(self, context: WriterContext) -> None:
        if context.constructor:
            # Just allocate the ids; each OrderedMap is constructed
            # implicitly on its first insert.
            self.state.podcasts_by_feed_url_index_id = str(uuid4())
            self.state.people_by_name_index_id = str(uuid4())
            self.state.episodes_by_date_index_id = str(uuid4())

    # -- Writes. --

    async def add_podcast(
        self,
        context: TransactionContext,
        request: Directory.AddPodcastRequest,
    ) -> Directory.AddPodcastResponse:
        index = OrderedMap.ref(self.state.podcasts_by_feed_url_index_id)
        existing = await index.search(context, key=request.feed_url)
        if existing.found:
            # Already in the catalog under this feed URL; hand back the id
            # we already have rather than creating a duplicate show.
            return Directory.AddPodcastResponse(
                podcast_id=existing.bytes.decode(),
                created=False,
            )

        podcast, _ = await Podcast.create(
            context,
            name=request.name,
            feed_url=request.feed_url,
            description=request.description,
        )
        await index.insert(
            context,
            key=request.feed_url,
            bytes=podcast.state_id.encode(),
        )
        return Directory.AddPodcastResponse(
            podcast_id=podcast.state_id,
            created=True,
        )

    async def add_episode(
        self,
        context: TransactionContext,
        request: Directory.AddEpisodeRequest,
    ) -> Directory.AddEpisodeResponse:
        # Resolve the guests first: the Episode stores their ids, so they
        # have to exist before it does. Resolution is itself deduplicating,
        # so this is safe to redo for an episode that turns out to be one
        # we already have.
        guest_person_ids = await self._resolve_people(
            context,
            request.guest_names,
        )

        # The podcast owns the source-id index that decides whether this is
        # a new episode, so the upsert happens there.
        result = await Podcast.ref(request.podcast_id).add_episode(
            context,
            source_id=request.source_id,
            title=request.title,
            publish_date=request.publish_date,
            description=request.description,
            chapters=list(request.chapters),
            guest_person_ids=guest_person_ids,
        )
        if not result.created:
            return Directory.AddEpisodeResponse(
                episode_id=result.episode_id,
                created=False,
            )

        # New episode: index it globally, and on each guest.
        await OrderedMap.ref(self.state.episodes_by_date_index_id).insert(
            context,
            key=date_key(request.publish_date),
            bytes=result.episode_id.encode(),
        )
        for person_id in guest_person_ids:
            await Person.ref(person_id).record_appearance(
                context,
                episode_id=result.episode_id,
                publish_date=request.publish_date,
            )
        return Directory.AddEpisodeResponse(
            episode_id=result.episode_id,
            created=True,
        )

    async def _resolve_people(
        self,
        context: TransactionContext,
        names: list[str],
    ) -> list[str]:
        """Map guest names onto Person ids, creating people as needed.

        Matching is case-insensitive on the normalized name, which is also
        the index key — so "Ada Lovelace" and "ada  lovelace" resolve to
        the same Person.
        """
        index = OrderedMap.ref(self.state.people_by_name_index_id)
        person_ids: list[str] = []
        for name in names:
            key = normalize(name)
            if not key:
                continue
            existing = await index.search(context, key=key)
            if existing.found:
                person_id = existing.bytes.decode()
            else:
                person, _ = await Person.create(
                    context,
                    name=name.strip(),
                    bio="",
                )
                await index.insert(
                    context,
                    key=key,
                    bytes=person.state_id.encode(),
                )
                person_id = person.state_id
            if person_id not in person_ids:
                person_ids.append(person_id)
        return person_ids

    # -- Reads. --

    async def search_mentions(
        self,
        context: ReaderContext,
        request: Directory.SearchMentionsRequest,
    ) -> Directory.SearchMentionsResponse:
        topic = normalize(request.topic)
        if not topic:
            return Directory.SearchMentionsResponse()

        if request.podcast_name:
            podcasts = await self._find_podcasts(context, request.podcast_name)
            if not podcasts:
                return Directory.SearchMentionsResponse()
            # Search the best match. `find_podcast` is the tool for
            # disambiguating when there is more than one.
            page = await Podcast.ref(podcasts[0].podcast_id).episode_ids(
                context,
                cursor=request.cursor,
            )
            episode_ids = list(page.episode_ids)
            cursor = page.next_cursor
        else:
            index = OrderedMap.ref(self.state.episodes_by_date_index_id)
            entries = await index.range(
                context,
                start_key=request.cursor,
                limit=PAGE_SIZE,
            )
            episode_ids = [entry.bytes.decode() for entry in entries.entries]
            # The cursor tracks every episode *examined*, not every one
            # that matched, so a page can come back empty and still have
            # more to scan.
            cursor = next_cursor(entries.entries, PAGE_SIZE)

        matches: list[EpisodeMatch] = []
        for episode_id in episode_ids:
            matches.extend(await self._match_episode(context, episode_id, topic))
        return Directory.SearchMentionsResponse(
            matches=matches,
            next_cursor=cursor,
        )

    async def _match_episode(
        self,
        context: ReaderContext,
        episode_id: str,
        topic: str,
    ) -> list[EpisodeMatch]:
        """Match one episode against a topic.

        Chapter titles win: each matching chapter is reported with the
        timeframe it covers. Only if no chapter matches do we fall back to
        the description, which yields a match with no timeframe claimed —
        we know the episode discussed the topic, not when.
        """
        detail = await Episode.ref(episode_id).get(context)

        chapter_hits = [
            chapter for chapter in detail.chapters
            if topic in chapter.title.lower()
        ]
        if chapter_hits:
            return [
                EpisodeMatch(
                    episode_id=episode_id,
                    episode_title=detail.title,
                    podcast_name=detail.podcast_name,
                    publish_date=detail.publish_date,
                    guest_names=list(detail.guest_names),
                    matched_chapter_title=chapter.title,
                    start_time_seconds=chapter.start_time_seconds,
                    end_time_seconds=chapter.end_time_seconds,
                )
                for chapter in chapter_hits
            ]

        if topic in detail.description.lower():
            return [
                EpisodeMatch(
                    episode_id=episode_id,
                    episode_title=detail.title,
                    podcast_name=detail.podcast_name,
                    publish_date=detail.publish_date,
                    guest_names=list(detail.guest_names),
                )
            ]

        return []

    async def find_person(
        self,
        context: ReaderContext,
        request: Directory.FindPersonRequest,
    ) -> Directory.FindPersonResponse:
        needle = normalize(request.name)
        index = OrderedMap.ref(self.state.people_by_name_index_id)

        # The index is keyed by normalized name, so an exact match is a
        # single lookup.
        if needle:
            exact = await index.search(context, key=needle)
            if exact.found:
                person_id = exact.bytes.decode()
                info = await Person.ref(person_id).get(context)
                return Directory.FindPersonResponse(
                    matches=[
                        PersonMatch(person_id=person_id, name=info.name),
                    ],
                )

        # Otherwise fall back to a bounded substring scan — "lovelace"
        # should still find "Ada Lovelace".
        matches: list[PersonMatch] = []
        for key, person_id in await scan_index(context, index, MAX_NAME_SCAN):
            if needle and needle not in key:
                continue
            info = await Person.ref(person_id).get(context)
            matches.append(PersonMatch(person_id=person_id, name=info.name))
        return Directory.FindPersonResponse(matches=matches)

    async def find_podcast(
        self,
        context: ReaderContext,
        request: Directory.FindPodcastRequest,
    ) -> Directory.FindPodcastResponse:
        return Directory.FindPodcastResponse(
            matches=await self._find_podcasts(context, request.name),
        )

    async def _find_podcasts(
        self,
        context: ReaderContext,
        name: str,
    ) -> list[PodcastMatch]:
        """Name lookup over the podcast index.

        The index is keyed by feed URL rather than name, so this reads each
        podcast to compare names. A personal catalog holds tens of shows,
        not millions, and `MAX_NAME_SCAN` bounds the pathological case.
        """
        needle = normalize(name)
        index = OrderedMap.ref(self.state.podcasts_by_feed_url_index_id)
        matches: list[PodcastMatch] = []
        for _, podcast_id in await scan_index(context, index, MAX_NAME_SCAN):
            info = await Podcast.ref(podcast_id).get(context)
            if needle and needle not in normalize(info.name):
                continue
            matches.append(PodcastMatch(podcast_id=podcast_id, name=info.name))
        return matches

    async def list_podcasts(
        self,
        context: ReaderContext,
        request: Directory.ListPodcastsRequest,
    ) -> Directory.ListPodcastsResponse:
        index = OrderedMap.ref(self.state.podcasts_by_feed_url_index_id)
        page = await index.range(
            context,
            start_key=request.cursor,
            limit=PAGE_SIZE,
        )
        podcasts: list[PodcastSummary] = []
        for entry in page.entries:
            podcast_id = entry.bytes.decode()
            info = await Podcast.ref(podcast_id).get(context)
            podcasts.append(
                PodcastSummary(
                    podcast_id=podcast_id,
                    name=info.name,
                    feed_url=info.feed_url,
                    description=info.description,
                )
            )
        return Directory.ListPodcastsResponse(
            podcasts=podcasts,
            next_cursor=next_cursor(page.entries, PAGE_SIZE),
        )


# ---------------------------------------------------------------------------
# Podcast.
# ---------------------------------------------------------------------------


class PodcastServicer(Podcast.Servicer):

    def authorizer(self) -> AuthorizerRule:
        return catalog_authorizer()

    async def create(
        self,
        context: WriterContext,
        request: Podcast.CreateRequest,
    ) -> None:
        if context.constructor:
            self.state.name = request.name
            self.state.feed_url = request.feed_url
            self.state.description = request.description
            self.state.episodes_index_id = str(uuid4())
            self.state.episodes_by_source_id_index_id = str(uuid4())

    async def add_episode(
        self,
        context: TransactionContext,
        request: Podcast.AddEpisodeRequest,
    ) -> Podcast.AddEpisodeResponse:
        """Deduplicating episode upsert, keyed on `source_id`.

        This lives on `Podcast` rather than on `Directory` so that the
        source-id index and the chronological episode index — both of which
        only this show ever writes — stay private to it.
        """
        by_source_id = OrderedMap.ref(self.state.episodes_by_source_id_index_id)
        existing = await by_source_id.search(context, key=request.source_id)
        if existing.found:
            return Podcast.AddEpisodeResponse(
                episode_id=existing.bytes.decode(),
                created=False,
            )

        episode, _ = await Episode.create(
            context,
            podcast_id=self.ref().state_id,
            source_id=request.source_id,
            title=request.title,
            publish_date=request.publish_date,
            description=request.description,
            guest_person_ids=list(request.guest_person_ids),
            chapters=list(request.chapters),
        )
        await by_source_id.insert(
            context,
            key=request.source_id,
            bytes=episode.state_id.encode(),
        )
        await OrderedMap.ref(self.state.episodes_index_id).insert(
            context,
            key=date_key(request.publish_date),
            bytes=episode.state_id.encode(),
        )
        return Podcast.AddEpisodeResponse(
            episode_id=episode.state_id,
            created=True,
        )

    async def get(self, context: ReaderContext) -> Podcast.GetResponse:
        return Podcast.GetResponse(
            podcast_id=self.ref().state_id,
            name=self.state.name,
            feed_url=self.state.feed_url,
            description=self.state.description,
        )

    async def episode_ids(
        self,
        context: ReaderContext,
        request: Podcast.EpisodeIdsRequest,
    ) -> Podcast.EpisodeIdsResponse:
        page = await OrderedMap.ref(self.state.episodes_index_id).range(
            context,
            start_key=request.cursor,
            limit=PAGE_SIZE,
        )
        return Podcast.EpisodeIdsResponse(
            episode_ids=[entry.bytes.decode() for entry in page.entries],
            next_cursor=next_cursor(page.entries, PAGE_SIZE),
        )

    async def list_episodes(
        self,
        context: ReaderContext,
        request: Podcast.ListEpisodesRequest,
    ) -> Podcast.ListEpisodesResponse:
        page = await OrderedMap.ref(self.state.episodes_index_id).range(
            context,
            start_key=request.cursor,
            limit=PAGE_SIZE,
        )
        episodes: list[EpisodeSummary] = []
        for entry in page.entries:
            episode_id = entry.bytes.decode()
            detail = await Episode.ref(episode_id).get(context)
            episodes.append(
                EpisodeSummary(
                    episode_id=episode_id,
                    title=detail.title,
                    publish_date=detail.publish_date,
                    description=detail.description,
                    chapter_count=len(detail.chapters),
                )
            )
        return Podcast.ListEpisodesResponse(
            episodes=episodes,
            next_cursor=next_cursor(page.entries, PAGE_SIZE),
        )


# ---------------------------------------------------------------------------
# Episode.
# ---------------------------------------------------------------------------


class EpisodeServicer(Episode.Servicer):

    def authorizer(self) -> AuthorizerRule:
        return catalog_authorizer()

    async def create(
        self,
        context: WriterContext,
        request: Episode.CreateRequest,
    ) -> None:
        if context.constructor:
            self.state.podcast_id = request.podcast_id
            self.state.source_id = request.source_id
            self.state.title = request.title
            self.state.publish_date = request.publish_date
            self.state.description = request.description
            self.state.guest_person_ids = list(request.guest_person_ids)
            self.state.chapters = list(request.chapters)

    async def get(self, context: ReaderContext) -> Episode.GetResponse:
        """The composing read every other timeframe-reporting path goes
        through: hydrates the podcast's name and the guests' names, and
        derives each chapter's end time."""
        podcast_name = ""
        if self.state.podcast_id:
            podcast = await Podcast.ref(self.state.podcast_id).get(context)
            podcast_name = podcast.name

        guest_names: list[str] = []
        for person_id in self.state.guest_person_ids:
            person = await Person.ref(person_id).get(context)
            guest_names.append(person.name)

        return Episode.GetResponse(
            episode_id=self.ref().state_id,
            podcast_id=self.state.podcast_id,
            podcast_name=podcast_name,
            title=self.state.title,
            publish_date=self.state.publish_date,
            description=self.state.description,
            guest_person_ids=list(self.state.guest_person_ids),
            guest_names=guest_names,
            chapters=chapter_views(list(self.state.chapters)),
        )


# ---------------------------------------------------------------------------
# Person.
# ---------------------------------------------------------------------------


class PersonServicer(Person.Servicer):

    def authorizer(self) -> AuthorizerRule:
        return catalog_authorizer()

    async def create(
        self,
        context: WriterContext,
        request: Person.CreateRequest,
    ) -> None:
        if context.constructor:
            self.state.name = request.name
            self.state.bio = request.bio
            self.state.appearances_index_id = str(uuid4())

    async def record_appearance(
        self,
        context: TransactionContext,
        request: Person.RecordAppearanceRequest,
    ) -> None:
        await OrderedMap.ref(self.state.appearances_index_id).insert(
            context,
            key=date_key(request.publish_date),
            bytes=request.episode_id.encode(),
        )

    async def get(self, context: ReaderContext) -> Person.GetResponse:
        return Person.GetResponse(
            person_id=self.ref().state_id,
            name=self.state.name,
            bio=self.state.bio,
        )

    async def appearances(
        self,
        context: ReaderContext,
        request: Person.AppearancesRequest,
    ) -> Person.AppearancesResponse:
        page = await OrderedMap.ref(self.state.appearances_index_id).range(
            context,
            start_key=request.cursor,
            limit=PAGE_SIZE,
        )
        appearances: list[AppearanceSummary] = []
        for entry in page.entries:
            episode_id = entry.bytes.decode()
            detail = await Episode.ref(episode_id).get(context)
            appearances.append(
                AppearanceSummary(
                    episode_id=episode_id,
                    episode_title=detail.title,
                    podcast_id=detail.podcast_id,
                    podcast_name=detail.podcast_name,
                    publish_date=detail.publish_date,
                    description=detail.description,
                    chapters=list(detail.chapters),
                )
            )
        return Person.AppearancesResponse(
            appearances=appearances,
            next_cursor=next_cursor(page.entries, PAGE_SIZE),
        )


APPLICATION_SERVICERS = [
    UserServicer,
    DirectoryServicer,
    PodcastServicer,
    EpisodeServicer,
    PersonServicer,
]
