// One episode: description, guests, and the chapter list with derived
// end times. `Episode.get` composes all of it in a single read.
import { type FC } from "react";
import { useEpisode } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate, formatSpan } from "../../../shared/format";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

export const EpisodePage: FC<{ episodeId: string }> = ({ episodeId }) => {
  const episode = useEpisode({ id: episodeId });
  const { response: detail, aborted } = episode.useGet();

  if (aborted !== undefined) {
    return (
      <div className="page">
        <div className="error">{friendlyError(aborted)}</div>
      </div>
    );
  }

  if (detail === undefined) {
    return (
      <div className="page">
        <p className="notice">loading episode…</p>
      </div>
    );
  }

  const guestIds = detail.guestPersonIds;

  return (
    <div className="page">
      {detail.podcastId && (
        <a
          className="backLink"
          href={hrefFor({ name: "podcast", id: detail.podcastId })}
        >
          ← {detail.podcastName || "podcast"}
        </a>
      )}

      <header className="detailHeader">
        <h1 className="pageTitle">{detail.title || "Untitled episode"}</h1>
        <div className="cardMeta">{formatDate(detail.publishDate)}</div>
      </header>

      {detail.guestNames.length > 0 && (
        <section>
          <h2 className="sectionTitle">
            Guest{detail.guestNames.length === 1 ? "" : "s"}
          </h2>
          <div className="guests">
            {detail.guestNames.map((name, index) => {
              // `guestNames` and `guestPersonIds` are parallel lists, so
              // each name links to that person's page when the id is
              // there.
              const personId = guestIds[index];
              return personId ? (
                <a
                  className="guest guestLink"
                  key={personId}
                  href={hrefFor({ name: "person", id: personId })}
                >
                  {name}
                </a>
              ) : (
                <span className="guest" key={`${name}-${index}`}>
                  {name}
                </span>
              );
            })}
          </div>
        </section>
      )}

      {detail.description && (
        <section>
          <h2 className="sectionTitle">Description</h2>
          <p className="lede">{detail.description}</p>
        </section>
      )}

      <section>
        <h2 className="sectionTitle">Chapters</h2>
        {detail.chapters.length === 0 ? (
          <p className="notice">This episode has no chapter markers.</p>
        ) : (
          <ul className="chapterList">
            {detail.chapters.map((chapter, index) => (
              <li className="chapterRow" key={index}>
                <span className="span">
                  {formatSpan(chapter.startTimeSeconds, chapter.endTimeSeconds)}
                </span>
                <span>{chapter.title || "Untitled chapter"}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
};
