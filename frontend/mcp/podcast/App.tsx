// The `Podcast.show` MCP UI: one show's details plus its episode list.
//
// `Podcast.show` is declared `UI(request=None)`, so the actor is the
// tool call's target and `usePodcast()` resolves it with no arguments.
// The no-arg overload hands back `{ podcast, isLoading }` where the
// handle is `undefined` until resolution settles — and a handle's
// reader hooks can't be called conditionally — so the resolution lives
// in a parent that renders nothing but a placeholder until it has a
// real id, and the view below always gets one.
import { type FC } from "react";
import { usePodcast } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate } from "../../shared/format";
import { useCursorPager } from "../../shared/pagination";
import css from "./App.module.css";

export const PodcastApp: FC = () => {
  const { podcast, isLoading } = usePodcast();

  if (podcast === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>
          {isLoading ? "loading podcast…" : "No podcast selected."}
        </div>
      </div>
    );
  }

  return <PodcastView podcastId={podcast.state_id} />;
};

const PodcastView: FC<{ podcastId: string }> = ({ podcastId }) => {
  const podcast = usePodcast({ id: podcastId });
  const pager = useCursorPager();

  const { response: info, aborted: infoAborted } = podcast.useGet();
  const { response: page, isLoading: episodesLoading } =
    podcast.useListEpisodes({ cursor: pager.cursor });

  if (infoAborted !== undefined) {
    return (
      <div className={css.container}>
        <div className={css.error}>
          Could not load this podcast: {infoAborted.message}
        </div>
      </div>
    );
  }

  if (info === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>loading podcast…</div>
      </div>
    );
  }

  const episodes = page?.episodes ?? [];
  const nextCursor = page?.nextCursor ?? "";

  return (
    <div className={css.container}>
      <header className={css.header}>
        <h1 className={css.title}>{info.name || "Untitled podcast"}</h1>
        {info.description && (
          <p className={css.description}>{info.description}</p>
        )}
        {info.feedUrl && <div className={css.feedUrl}>{info.feedUrl}</div>}
      </header>

      <section>
        <h2 className={css.sectionTitle}>
          Episodes{pager.page > 1 ? ` · page ${pager.page}` : ""}
        </h2>

        {page === undefined && episodesLoading && (
          <div className={css.notice}>loading episodes…</div>
        )}

        {page !== undefined && episodes.length === 0 && (
          <div className={css.notice}>
            {pager.hasPrevious
              ? "No more episodes on this page."
              : "This podcast has no episodes yet."}
          </div>
        )}

        <ul className={css.list}>
          {episodes.map((episode) => (
            <li key={episode.episodeId} className={css.episode}>
              <div className={css.episodeTitle}>
                {episode.title || "Untitled episode"}
              </div>
              <div className={css.episodeMeta}>
                {formatDate(episode.publishDate)}
                {episode.chapterCount > 0 && (
                  <>
                    {" · "}
                    <span className={css.chapterCount}>
                      {episode.chapterCount} chapter
                      {episode.chapterCount === 1 ? "" : "s"}
                    </span>
                  </>
                )}
              </div>
              {episode.description && (
                <p className={css.episodeDescription}>{episode.description}</p>
              )}
            </li>
          ))}
        </ul>

        {(pager.hasPrevious || nextCursor) && (
          <div className={css.pager}>
            <button
              className={css.pagerButton}
              onClick={pager.goPrevious}
              disabled={!pager.hasPrevious}
            >
              ← previous
            </button>
            <button
              className={css.pagerButton}
              onClick={() => pager.goNext(nextCursor)}
              disabled={!nextCursor}
            >
              next →
            </button>
          </div>
        )}
      </section>
    </div>
  );
};
