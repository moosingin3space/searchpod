// One show: its details plus its episodes, in chronological order.
import { type FC } from "react";
import { usePodcast } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate } from "../../../shared/format";
import { useCursorPager } from "../../../shared/pagination";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

export const PodcastPage: FC<{ podcastId: string }> = ({ podcastId }) => {
  const podcast = usePodcast({ id: podcastId });
  const pager = useCursorPager();

  const { response: info, aborted } = podcast.useGet();
  const { response: page, isLoading: episodesLoading } = podcast.useListEpisodes({
    cursor: pager.cursor,
  });

  if (aborted !== undefined) {
    return (
      <div className="page">
        <div className="error">{friendlyError(aborted)}</div>
      </div>
    );
  }

  if (info === undefined) {
    return (
      <div className="page">
        <p className="notice">loading podcast…</p>
      </div>
    );
  }

  const episodes = page?.episodes ?? [];
  const nextCursor = page?.nextCursor ?? "";

  return (
    <div className="page">
      <a className="backLink" href={hrefFor({ name: "podcasts" })}>
        ← all podcasts
      </a>

      <header className="detailHeader">
        <h1 className="pageTitle">{info.name || "Untitled podcast"}</h1>
        {info.description && <p className="lede">{info.description}</p>}
        {info.feedUrl && <div className="cardMeta">{info.feedUrl}</div>}
      </header>

      <h2 className="sectionTitle">Episodes</h2>

      {page === undefined && episodesLoading && (
        <p className="notice">loading episodes…</p>
      )}

      {page !== undefined && episodes.length === 0 && (
        <p className="notice">
          {pager.hasPrevious
            ? "No more episodes."
            : "This podcast has no episodes yet."}
        </p>
      )}

      <ul className="cardList">
        {episodes.map((episode) => (
          <li className="card" key={episode.episodeId}>
            <a
              className="cardTitle"
              href={hrefFor({ name: "episode", id: episode.episodeId })}
            >
              {episode.title || "Untitled episode"}
            </a>
            <div className="cardMeta">
              {formatDate(episode.publishDate)}
              {episode.chapterCount > 0 &&
                ` · ${episode.chapterCount} chapter${
                  episode.chapterCount === 1 ? "" : "s"
                }`}
            </div>
            {episode.description && (
              <p className="cardBody">{episode.description}</p>
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
            next →
          </button>
        </div>
      )}
    </div>
  );
};
