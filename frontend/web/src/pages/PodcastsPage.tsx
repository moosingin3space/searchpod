// Browse the whole catalog, one page at a time.
import { type FC } from "react";
import { useDirectory } from "@api/searchpod/v1/podcasts_rbt_react";
import { useCursorPager } from "../../../shared/pagination";
import { DIRECTORY_ID } from "../directory";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

export const PodcastsPage: FC = () => {
  const directory = useDirectory({ id: DIRECTORY_ID });
  const pager = useCursorPager();
  const { response, isLoading, aborted } = directory.useListPodcasts({
    cursor: pager.cursor,
  });

  if (aborted !== undefined) {
    return (
      <div className="page">
        <div className="error">{friendlyError(aborted)}</div>
      </div>
    );
  }

  if (response === undefined) {
    return (
      <div className="page">
        <p className="notice">{isLoading ? "loading catalog…" : "No catalog."}</p>
      </div>
    );
  }

  const podcasts = response.podcasts;
  const nextCursor = response.nextCursor;

  return (
    <div className="page">
      <h1 className="pageTitle">Podcasts</h1>

      {podcasts.length === 0 && (
        <p className="notice">
          {pager.hasPrevious
            ? "No more podcasts."
            : "The catalog is empty. Add a podcast through the chat app to get started."}
        </p>
      )}

      <ul className="cardList">
        {podcasts.map((podcast) => (
          <li className="card" key={podcast.podcastId}>
            <a
              className="cardTitle"
              href={hrefFor({ name: "podcast", id: podcast.podcastId })}
            >
              {podcast.name || "Untitled podcast"}
            </a>
            {podcast.description && (
              <p className="cardBody">{podcast.description}</p>
            )}
            {podcast.feedUrl && <div className="cardMeta">{podcast.feedUrl}</div>}
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
