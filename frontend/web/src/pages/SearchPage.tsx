// Topic search — the app's front page.
//
// Two things about `Directory.searchMentions` shape this page:
//
//  1. It paginates over *episodes scanned*, not results found, so a
//     page can legitimately come back with zero matches and a non-empty
//     cursor. "No matches on this page, keep scanning" and "nothing in
//     the catalog matches" are different states and are rendered
//     differently.
//  2. The `podcastName` filter is a *substring* match resolved
//     server-side, and the search silently uses the first podcast that
//     matches. A browser can do better than the AI can here, so the
//     filter shows which show the backend will actually search and
//     lists the other candidates.
import { useState, type FC } from "react";
import { useDirectory } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate, formatSpan } from "../../../shared/format";
import { useCursorPager } from "../../../shared/pagination";
import { DIRECTORY_ID } from "../directory";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

interface Query {
  topic: string;
  podcastName: string;
}

export const SearchPage: FC = () => {
  const [topicInput, setTopicInput] = useState("");
  const [podcastInput, setPodcastInput] = useState("");
  const [query, setQuery] = useState<Query>({ topic: "", podcastName: "" });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setQuery({
      topic: topicInput.trim(),
      podcastName: podcastInput.trim(),
    });
  };

  const searched = query.topic !== "";

  return (
    <div className="page">
      <h1 className="pageTitle">Find where a topic came up</h1>

      <form className="searchForm" onSubmit={submit}>
        <label className="field">
          <span className="fieldLabel">Topic</span>
          <input
            className="input"
            value={topicInput}
            onChange={(e) => setTopicInput(e.target.value)}
            placeholder="e.g. transformers, sourdough, interest rates"
          />
        </label>
        <label className="field">
          <span className="fieldLabel">
            Podcast <span className="optional">(optional)</span>
          </span>
          <input
            className="input"
            value={podcastInput}
            onChange={(e) => setPodcastInput(e.target.value)}
            placeholder="leave blank to search every show"
          />
        </label>
        <button className="primaryButton" type="submit" disabled={!topicInput.trim()}>
          Search
        </button>
      </form>

      {query.podcastName !== "" && (
        <PodcastFilterNotice
          key={query.podcastName}
          name={query.podcastName}
        />
      )}

      {!searched && (
        <p className="notice">
          Enter a topic to search chapter titles and episode descriptions.
        </p>
      )}

      {/*
        Keyed on the query, so a new search *remounts* rather than
        reusing the subscription. A reader hook keeps serving the
        previous query's response until the new one arrives, and that
        stale page would otherwise be rendered as an authoritative
        answer for the new query — a search whose first page is empty
        would flash "Nothing in the catalog mentions that" before the
        real, still-scanning result landed. Remounting also resets the
        cursor stack, which a query change requires anyway: the two
        filter modes page over different indexes.
      */}
      {searched && (
        <SearchResults
          // JSON rather than a joined string: any single separator
          // character can also appear inside a topic or a podcast
          // name, and two different queries that shared a key would
          // silently skip the remount this key exists to force.
          key={JSON.stringify([query.topic, query.podcastName])}
          topic={query.topic}
          podcastName={query.podcastName}
        />
      )}
    </div>
  );
};

const SearchResults: FC<{ topic: string; podcastName: string }> = ({
  topic,
  podcastName,
}) => {
  const directory = useDirectory({ id: DIRECTORY_ID });
  const pager = useCursorPager();
  const { response, isLoading, aborted } = directory.useSearchMentions({
    topic,
    podcastName,
    cursor: pager.cursor,
  });

  if (aborted !== undefined) {
    return <div className="error">{friendlyError(aborted)}</div>;
  }

  if (response === undefined) {
    return <p className="notice">{isLoading ? "searching…" : "No results."}</p>;
  }

  const matches = response.matches;
  const nextCursor = response.nextCursor;

  return (
    <>
      {/*
        Zero matches is two different states. With a cursor still live
        the scan simply hasn't reached a match yet — the API pages over
        episodes *examined*, not results found — and saying "nothing
        found" there would be wrong.
      */}
      <div className="resultsHeader">
        {matches.length > 0
          ? `${matches.length} match${
              matches.length === 1 ? "" : "es"
            } on page ${pager.page}`
          : nextCursor
            ? `No matches on page ${pager.page} — there are more episodes to scan.`
            : pager.hasPrevious
              ? "No further matches; the whole catalog has been scanned."
              : "Nothing in the catalog mentions that."}
      </div>

      <ul className="cardList">
        {matches.map((match, index) => (
          <li
            className="card"
            key={`${match.episodeId}-${match.matchedChapterTitle ?? ""}-${index}`}
          >
            <div className="cardKicker">{match.podcastName}</div>
            <a
              className="cardTitle"
              href={hrefFor({ name: "episode", id: match.episodeId })}
            >
              {match.episodeTitle || "Untitled episode"}
            </a>
            <div className="cardMeta">{formatDate(match.publishDate)}</div>

            {match.matchedChapterTitle !== undefined &&
            match.matchedChapterTitle !== null ? (
              <div className="chapterHit">
                <span className="span">
                  {formatSpan(match.startTimeSeconds, match.endTimeSeconds)}
                </span>
                <span>{match.matchedChapterTitle}</span>
              </div>
            ) : (
              // A description-level hit: the topic came up somewhere in
              // the episode, but no timeframe can be claimed.
              <div className="descriptionHit">
                Mentioned in the episode description — no timestamp.
              </div>
            )}

            {match.guestNames.length > 0 && (
              <div className="guests">
                {match.guestNames.map((name, i) => (
                  <span className="guest" key={`${name}-${i}`}>
                    {name}
                  </span>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>

      {(pager.hasPrevious || nextCursor) && (
        <div className="pager">
          <button
            className="pagerButton"
            onClick={pager.goPrevious}
            disabled={!pager.hasPrevious}
          >
            ← previous
          </button>
          <span className="pagerLabel">page {pager.page}</span>
          <button
            className="pagerButton"
            onClick={() => pager.goNext(nextCursor)}
            disabled={!nextCursor}
          >
            scan more →
          </button>
        </div>
      )}
    </>
  );
};

/**
 * Shows which podcast the server-side filter actually resolved to.
 *
 * `searchMentions` resolves `podcastName` with the same case-insensitive
 * substring match `findPodcast` uses and then searches only the *first*
 * candidate. That is invisible from the search response itself, so this
 * runs the same lookup and reports the resolution. It is a separate
 * component so the lookup is only subscribed to when a filter is set —
 * an empty name matches every podcast in the catalog.
 */
const PodcastFilterNotice: FC<{ name: string }> = ({ name }) => {
  const directory = useDirectory({ id: DIRECTORY_ID });
  const { response } = directory.useFindPodcast({ name });

  if (response === undefined) return null;

  const matches = response.matches;
  if (matches.length === 0) {
    return (
      <div className="filterNotice warn">
        No podcast matches “{name}”, so this search has nothing to scan.
      </div>
    );
  }

  return (
    <div className="filterNotice">
      Searching <strong>{matches[0].name}</strong> only.
      {matches.length > 1 && (
        <>
          {" "}
          “{name}” also matches{" "}
          {matches
            .slice(1)
            .map((m) => m.name)
            .join(", ")}
          ; narrow the podcast name to search one of those instead.
        </>
      )}
    </div>
  );
};
