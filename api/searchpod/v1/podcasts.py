"""searchpod — podcast catalog + topic search.

Four state types plus the MCP `User` front door:

- `Directory` — a single global catalog root (state id `"global"`) that
  owns the three top-level indexes: podcasts keyed by feed URL, people
  keyed by normalized name, and every episode keyed by publish date.
- `Podcast` — one show; owns its own episode index and a source-id
  index used to deduplicate episodes.
- `Episode` — one episode; chapters live inline on it.
- `Person` — one guest; owns an index of the episodes they appear on.

There is no LLM anywhere in this backend: search is plain
case-insensitive substring matching over chapter titles and episode
descriptions, so every method is a Reader, Writer, or Transaction.
"""

from typing import Optional

from reboot.api import (
    API,
    UI,
    Field,
    Methods,
    Model,
    Reader,
    Tool,
    Transaction,
    Type,
    Writer,
)

# The well-known state id of the one and only `Directory` actor. Created
# by the `initialize` hook in `backend/src/main.py`.
DIRECTORY_ID = "global"

# How many index entries a single page of a paginated reader scans.
PAGE_SIZE = 32


# ---------------------------------------------------------------------------
# Shared sub-records.
# ---------------------------------------------------------------------------


class Chapter(Model):
    """A timestamped section header within an episode.

    Chapters have no lifecycle or identity apart from the episode they
    belong to, so they live inline on `Episode` rather than as their own
    state type. Only the *start* time is stored — the end of a chapter is
    always the start of the next one, and is derived at read time.
    """

    title: str = Field(tag=1, default="")
    start_time_seconds: int = Field(tag=2, default=0)


class ChapterView(Model):
    """A chapter as returned by a reader, with its end time derived.

    `end_time_seconds` is the next chapter's start time, or `None` for the
    last chapter of an episode ("runs to the end of the episode").
    """

    title: str = Field(tag=1, default="")
    start_time_seconds: int = Field(tag=2, default=0)
    end_time_seconds: Optional[int] = Field(tag=3, default=None)


# ---------------------------------------------------------------------------
# User — the MCP identity front door.
# ---------------------------------------------------------------------------


class UserState(Model):
    """Empty: the catalog is shared across all signed-in users.

    `User` exists because the MCP chat-app front door requires an identity
    type; it holds no per-user catalog state.
    """


# ---------------------------------------------------------------------------
# Directory — the global catalog root.
# ---------------------------------------------------------------------------


class DirectoryState(Model):
    # OrderedMap: key = feed URL, value = podcast id. The dedup key for
    # podcasts.
    podcasts_by_feed_url_index_id: str = Field(tag=1, default="")
    # OrderedMap: key = normalized (lowercased, stripped) person name,
    # value = person id. The dedup key for people.
    people_by_name_index_id: str = Field(tag=2, default="")
    # OrderedMap: key = "<publish_date_iso>#<uuid7>", value = episode id.
    # Chronological and globally unique; backs unscoped search/browse.
    episodes_by_date_index_id: str = Field(tag=3, default="")


class AddPodcastRequest(Model):
    name: str = Field(tag=1, default="")
    feed_url: str = Field(tag=2, default="")
    description: str = Field(tag=3, default="")


class AddPodcastResponse(Model):
    podcast_id: str = Field(tag=1, default="")
    # False when a podcast with this feed URL already existed.
    created: bool = Field(tag=2, default=False)


class AddEpisodeRequest(Model):
    podcast_id: str = Field(tag=1, default="")
    # The scraper's stable id for this episode (GUID or canonical URL).
    # Together with `podcast_id` this is the dedup key.
    source_id: str = Field(tag=2, default="")
    title: str = Field(tag=3, default="")
    publish_date: str = Field(tag=4, default="")
    description: str = Field(tag=5, default="")
    chapters: list[Chapter] = Field(tag=6, default_factory=list)
    guest_names: list[str] = Field(tag=7, default_factory=list)


class AddEpisodeResponse(Model):
    episode_id: str = Field(tag=1, default="")
    # False when this podcast already had an episode with this source id.
    created: bool = Field(tag=2, default=False)


class SearchMentionsRequest(Model):
    topic: str = Field(tag=1, default="")
    # Empty string means "search every podcast".
    podcast_name: str = Field(tag=2, default="")
    # Empty string means "start from the beginning".
    cursor: str = Field(tag=3, default="")


class EpisodeMatch(Model):
    """One place a topic was mentioned.

    A chapter-level hit sets `matched_chapter_title`, `start_time_seconds`
    and `end_time_seconds` (the latter is `None` for an episode's last
    chapter). A description-level hit leaves all three unset: the topic was
    discussed somewhere in the episode, but no timeframe can be claimed.
    """

    episode_id: str = Field(tag=1, default="")
    episode_title: str = Field(tag=2, default="")
    podcast_name: str = Field(tag=3, default="")
    publish_date: str = Field(tag=4, default="")
    guest_names: list[str] = Field(tag=5, default_factory=list)
    matched_chapter_title: Optional[str] = Field(tag=6, default=None)
    start_time_seconds: Optional[int] = Field(tag=7, default=None)
    end_time_seconds: Optional[int] = Field(tag=8, default=None)


class SearchMentionsResponse(Model):
    matches: list[EpisodeMatch] = Field(tag=1, default_factory=list)
    # Empty string means "no more episodes to scan". A page can legitimately
    # come back with zero matches and a non-empty cursor.
    next_cursor: str = Field(tag=2, default="")


class FindPersonRequest(Model):
    name: str = Field(tag=1, default="")


class PersonMatch(Model):
    person_id: str = Field(tag=1, default="")
    name: str = Field(tag=2, default="")


class FindPersonResponse(Model):
    matches: list[PersonMatch] = Field(tag=1, default_factory=list)


class FindPodcastRequest(Model):
    name: str = Field(tag=1, default="")


class PodcastMatch(Model):
    podcast_id: str = Field(tag=1, default="")
    name: str = Field(tag=2, default="")


class FindPodcastResponse(Model):
    matches: list[PodcastMatch] = Field(tag=1, default_factory=list)


class ListPodcastsRequest(Model):
    cursor: str = Field(tag=1, default="")


class PodcastSummary(Model):
    podcast_id: str = Field(tag=1, default="")
    name: str = Field(tag=2, default="")
    feed_url: str = Field(tag=3, default="")
    description: str = Field(tag=4, default="")


class ListPodcastsResponse(Model):
    podcasts: list[PodcastSummary] = Field(tag=1, default_factory=list)
    next_cursor: str = Field(tag=2, default="")


# ---------------------------------------------------------------------------
# Podcast.
# ---------------------------------------------------------------------------


class PodcastState(Model):
    name: str = Field(tag=1, default="")
    feed_url: str = Field(tag=2, default="")
    description: str = Field(tag=3, default="")
    # OrderedMap: key = "<publish_date_iso>#<uuid7>", value = episode id.
    # This show's own episodes, in chronological order.
    episodes_index_id: str = Field(tag=4, default="")
    # OrderedMap: key = source id, value = episode id. The dedup key for
    # episodes within this show.
    episodes_by_source_id_index_id: str = Field(tag=5, default="")


class CreatePodcastRequest(Model):
    name: str = Field(tag=1, default="")
    feed_url: str = Field(tag=2, default="")
    description: str = Field(tag=3, default="")


class PodcastInfo(Model):
    podcast_id: str = Field(tag=1, default="")
    name: str = Field(tag=2, default="")
    feed_url: str = Field(tag=3, default="")
    description: str = Field(tag=4, default="")


class ListEpisodesRequest(Model):
    cursor: str = Field(tag=1, default="")


class EpisodeSummary(Model):
    episode_id: str = Field(tag=1, default="")
    title: str = Field(tag=2, default="")
    publish_date: str = Field(tag=3, default="")
    description: str = Field(tag=4, default="")
    chapter_count: int = Field(tag=5, default=0)


class ListEpisodesResponse(Model):
    episodes: list[EpisodeSummary] = Field(tag=1, default_factory=list)
    next_cursor: str = Field(tag=2, default="")


class EpisodeIdsRequest(Model):
    cursor: str = Field(tag=1, default="")


class EpisodeIdsResponse(Model):
    episode_ids: list[str] = Field(tag=1, default_factory=list)
    next_cursor: str = Field(tag=2, default="")


class AddEpisodeToPodcastRequest(Model):
    source_id: str = Field(tag=1, default="")
    title: str = Field(tag=2, default="")
    publish_date: str = Field(tag=3, default="")
    description: str = Field(tag=4, default="")
    chapters: list[Chapter] = Field(tag=5, default_factory=list)
    guest_person_ids: list[str] = Field(tag=6, default_factory=list)


class AddEpisodeToPodcastResponse(Model):
    episode_id: str = Field(tag=1, default="")
    created: bool = Field(tag=2, default=False)


# ---------------------------------------------------------------------------
# Episode.
# ---------------------------------------------------------------------------


class EpisodeState(Model):
    podcast_id: str = Field(tag=1, default="")
    source_id: str = Field(tag=2, default="")
    title: str = Field(tag=3, default="")
    publish_date: str = Field(tag=4, default="")
    description: str = Field(tag=5, default="")
    # A handful of guests per episode, so a plain list of Person ids is the
    # right shape — no index actor needed.
    guest_person_ids: list[str] = Field(tag=6, default_factory=list)
    chapters: list[Chapter] = Field(tag=7, default_factory=list)


class CreateEpisodeRequest(Model):
    podcast_id: str = Field(tag=1, default="")
    source_id: str = Field(tag=2, default="")
    title: str = Field(tag=3, default="")
    publish_date: str = Field(tag=4, default="")
    description: str = Field(tag=5, default="")
    guest_person_ids: list[str] = Field(tag=6, default_factory=list)
    chapters: list[Chapter] = Field(tag=7, default_factory=list)


class EpisodeDetail(Model):
    episode_id: str = Field(tag=1, default="")
    podcast_id: str = Field(tag=2, default="")
    podcast_name: str = Field(tag=3, default="")
    title: str = Field(tag=4, default="")
    publish_date: str = Field(tag=5, default="")
    description: str = Field(tag=6, default="")
    guest_person_ids: list[str] = Field(tag=7, default_factory=list)
    guest_names: list[str] = Field(tag=8, default_factory=list)
    chapters: list[ChapterView] = Field(tag=9, default_factory=list)


# ---------------------------------------------------------------------------
# Person.
# ---------------------------------------------------------------------------


class PersonState(Model):
    name: str = Field(tag=1, default="")
    bio: str = Field(tag=2, default="")
    # OrderedMap: key = "<publish_date_iso>#<uuid7>", value = episode id.
    # Unbounded — a prolific guest accumulates appearances without limit.
    appearances_index_id: str = Field(tag=3, default="")


class CreatePersonRequest(Model):
    name: str = Field(tag=1, default="")
    bio: str = Field(tag=2, default="")


class PersonInfo(Model):
    person_id: str = Field(tag=1, default="")
    name: str = Field(tag=2, default="")
    bio: str = Field(tag=3, default="")


class RecordAppearanceRequest(Model):
    episode_id: str = Field(tag=1, default="")
    publish_date: str = Field(tag=2, default="")


class AppearancesRequest(Model):
    cursor: str = Field(tag=1, default="")


class AppearanceSummary(Model):
    episode_id: str = Field(tag=1, default="")
    episode_title: str = Field(tag=2, default="")
    podcast_id: str = Field(tag=3, default="")
    podcast_name: str = Field(tag=4, default="")
    publish_date: str = Field(tag=5, default="")
    description: str = Field(tag=6, default="")
    chapters: list[ChapterView] = Field(tag=7, default_factory=list)


class AppearancesResponse(Model):
    appearances: list[AppearanceSummary] = Field(tag=1, default_factory=list)
    next_cursor: str = Field(tag=2, default="")


# ---------------------------------------------------------------------------
# The API.
# ---------------------------------------------------------------------------

_DIRECTORY_ID_HINT = (
    f" The catalog is a single shared directory whose id is always "
    f"'{DIRECTORY_ID}' — pass that as the directory id."
)


api = API(
    User=Type(
        state=UserState,
        methods=Methods(),
    ),
    Directory=Type(
        state=DirectoryState,
        methods=Methods(
            # A Transaction rather than a Writer because it explicitly
            # constructs the three index actors it owns, and constructing
            # another actor is a cross-actor mutation.
            create=Transaction(
                request=None,
                response=None,
                factory=True,
                mcp=None,
            ),
            add_podcast=Transaction(
                request=AddPodcastRequest,
                response=AddPodcastResponse,
                description=(
                    "Add a podcast to the catalog. Deduplicated on "
                    "`feed_url`: if a podcast with that feed URL already "
                    "exists, its existing id is returned and nothing is "
                    "created." + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
            add_episode=Transaction(
                request=AddEpisodeRequest,
                response=AddEpisodeResponse,
                description=(
                    "Add an episode to a podcast already in the catalog. "
                    "`source_id` is the episode's stable id from its feed "
                    "(GUID or canonical URL) and deduplicates within the "
                    "podcast. `chapters` is optional — many episodes have "
                    "none — and each chapter carries only a start time; "
                    "end times are derived when the episode is read. Each "
                    "name in `guest_names` is matched case-insensitively "
                    "against existing people and a new person is created "
                    "if there is no match." + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
            search_mentions=Reader(
                request=SearchMentionsRequest,
                response=SearchMentionsResponse,
                description=(
                    "Find where a topic was discussed. Matches `topic` "
                    "case-insensitively against chapter titles first: each "
                    "matching chapter becomes one result with a start and "
                    "end time. If no chapter matches, the episode "
                    "description is checked and a match there returns the "
                    "episode with no timeframe. Set `podcast_name` to "
                    "restrict the search to one show; leave it empty to "
                    "search everything. Every result carries the episode's "
                    "guest names, so this also answers 'who was the guest "
                    "when they talked about X'. Pass back `next_cursor` to "
                    "scan further; a page may return no matches and still "
                    "have a cursor." + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
            find_person=Reader(
                request=FindPersonRequest,
                response=FindPersonResponse,
                description=(
                    "Look up people by name, case-insensitively. Returns "
                    "every plausible match, since a name can be ambiguous; "
                    "use `Person.get`/`Person.appearances` (or the Person "
                    "UI) on the one you want." + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
            find_podcast=Reader(
                request=FindPodcastRequest,
                response=FindPodcastResponse,
                description=(
                    "Look up podcasts by name, case-insensitively. Returns "
                    "every plausible match." + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
            list_podcasts=Reader(
                request=ListPodcastsRequest,
                response=ListPodcastsResponse,
                description=(
                    "Browse every podcast in the catalog, one page at a "
                    "time. Pass back `next_cursor` for the next page."
                    + _DIRECTORY_ID_HINT
                ),
                mcp=Tool(),
            ),
        ),
    ),
    Podcast=Type(
        state=PodcastState,
        methods=Methods(
            show=UI(
                request=None,
                path="frontend/mcp/podcast",
                title="Podcast",
                description=(
                    "Open the visual UI for this podcast: its details and "
                    "its episode list."
                ),
            ),
            # A Transaction rather than a Writer because it explicitly
            # constructs this show's two index actors.
            create=Transaction(
                request=CreatePodcastRequest,
                response=None,
                factory=True,
                mcp=None,
            ),
            get=Reader(
                request=None,
                response=PodcastInfo,
                description="Get this podcast's name, feed URL, and description.",
                mcp=Tool(),
            ),
            list_episodes=Reader(
                request=ListEpisodesRequest,
                response=ListEpisodesResponse,
                description=(
                    "List this podcast's episodes in chronological order, "
                    "one page at a time. Pass back `next_cursor` for the "
                    "next page."
                ),
                mcp=Tool(),
            ),
            # Internal: pages this podcast's episode index without
            # hydrating each episode. Used by `Directory.search_mentions`
            # when the search is scoped to one show.
            episode_ids=Reader(
                request=EpisodeIdsRequest,
                response=EpisodeIdsResponse,
                mcp=None,
            ),
            # Internal: the deduplicating episode upsert. Lives here rather
            # than on `Directory` so that the source-id index and the
            # episode index stay private to the podcast that owns them.
            add_episode=Transaction(
                request=AddEpisodeToPodcastRequest,
                response=AddEpisodeToPodcastResponse,
                mcp=None,
            ),
        ),
    ),
    Episode=Type(
        state=EpisodeState,
        methods=Methods(
            show=UI(
                request=None,
                path="frontend/mcp/episode",
                title="Episode",
                description=(
                    "Open the visual UI for this episode: its description, "
                    "chapter list with start and end times, and guests."
                ),
            ),
            create=Writer(
                request=CreateEpisodeRequest,
                response=None,
                factory=True,
                mcp=None,
            ),
            get=Reader(
                request=None,
                response=EpisodeDetail,
                description=(
                    "Get everything about this episode: its podcast's name, "
                    "publish date, description, guest names, and its "
                    "chapters with start and end times. A chapter's end "
                    "time is the next chapter's start; the last chapter has "
                    "no end time because it runs to the end of the episode."
                ),
                mcp=Tool(),
            ),
        ),
    ),
    Person=Type(
        state=PersonState,
        methods=Methods(
            show=UI(
                request=None,
                path="frontend/mcp/person",
                title="Person",
                description=(
                    "Open the visual UI for this person: their bio and the "
                    "episodes they have appeared on."
                ),
            ),
            # A Transaction rather than a Writer because it explicitly
            # constructs this person's appearance index actor.
            create=Transaction(
                request=CreatePersonRequest,
                response=None,
                factory=True,
                mcp=None,
            ),
            get=Reader(
                request=None,
                response=PersonInfo,
                description="Get this person's name and bio.",
                mcp=Tool(),
            ),
            appearances=Reader(
                request=AppearancesRequest,
                response=AppearancesResponse,
                description=(
                    "List the episodes this person has appeared on, newest "
                    "last, one page at a time. Each entry carries the "
                    "episode's podcast, description, and chapters — this is "
                    "what answers 'what has this person talked about'. Pass "
                    "back `next_cursor` for the next page."
                ),
                mcp=Tool(),
            ),
            # Internal: append one episode to this person's appearance
            # index. Called by `Directory.add_episode`.
            record_appearance=Transaction(
                request=RecordAppearanceRequest,
                response=None,
                mcp=None,
            ),
        ),
    ),
)
