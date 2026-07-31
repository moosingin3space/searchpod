// The `Episode.show` MCP UI: one episode's description, guests, and its
// chapter list with start and end times.
//
// `Episode.get` is the API's single composing read — it hydrates the
// podcast's name and the guests' names and derives every chapter's end
// time — so this UI needs exactly one reader subscription and no
// fan-out of its own.
import { type FC } from "react";
import { useEpisode } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate, formatSpan } from "../../shared/format";
import css from "./App.module.css";

export const EpisodeApp: FC = () => {
  const { episode, isLoading } = useEpisode();

  if (episode === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>
          {isLoading ? "loading episode…" : "No episode selected."}
        </div>
      </div>
    );
  }

  return <EpisodeView episodeId={episode.state_id} />;
};

const EpisodeView: FC<{ episodeId: string }> = ({ episodeId }) => {
  const episode = useEpisode({ id: episodeId });
  const { response: detail, aborted } = episode.useGet();

  if (aborted !== undefined) {
    return (
      <div className={css.container}>
        <div className={css.error}>
          Could not load this episode: {aborted.message}
        </div>
      </div>
    );
  }

  if (detail === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>loading episode…</div>
      </div>
    );
  }

  const chapters = detail.chapters ?? [];
  const guests = detail.guestNames ?? [];

  return (
    <div className={css.container}>
      <header className={css.header}>
        {detail.podcastName && (
          <div className={css.podcastName}>{detail.podcastName}</div>
        )}
        <h1 className={css.title}>{detail.title || "Untitled episode"}</h1>
        {detail.publishDate && (
          <div className={css.date}>{formatDate(detail.publishDate)}</div>
        )}
      </header>

      {guests.length > 0 && (
        <section>
          <h2 className={css.sectionTitle}>
            Guest{guests.length === 1 ? "" : "s"}
          </h2>
          <div className={css.guests}>
            {guests.map((name, index) => (
              <span key={`${name}-${index}`} className={css.guest}>
                {name}
              </span>
            ))}
          </div>
        </section>
      )}

      {detail.description && (
        <section>
          <h2 className={css.sectionTitle}>Description</h2>
          <p className={css.description}>{detail.description}</p>
        </section>
      )}

      <section>
        <h2 className={css.sectionTitle}>Chapters</h2>
        {chapters.length === 0 ? (
          // Plenty of episodes ship without chapters; that is normal, not
          // a failure to load.
          <div className={css.notice}>
            This episode has no chapter markers.
          </div>
        ) : (
          <ul className={css.list}>
            {chapters.map((chapter, index) => (
              <li key={index} className={css.chapter}>
                <span className={css.span}>
                  {formatSpan(
                    chapter.startTimeSeconds,
                    chapter.endTimeSeconds
                  )}
                </span>
                <span className={css.chapterTitle}>
                  {chapter.title || "Untitled chapter"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
};
