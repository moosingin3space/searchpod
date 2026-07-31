// One guest: their bio and what they have talked about.
//
// `Person.appearances` already carries each episode's podcast,
// description, and chapters, so the whole page is one paginated read —
// no per-episode fan-out on the client.
import { type FC } from "react";
import { usePerson } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate, formatSpan } from "../../../shared/format";
import { useCursorPager } from "../../../shared/pagination";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

export const PersonPage: FC<{ personId: string }> = ({ personId }) => {
  const person = usePerson({ id: personId });
  const pager = useCursorPager();

  const { response: info, aborted } = person.useGet();
  const { response: page, isLoading: appearancesLoading } = person.useAppearances({
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
        <p className="notice">loading person…</p>
      </div>
    );
  }

  const appearances = page?.appearances ?? [];
  const nextCursor = page?.nextCursor ?? "";

  return (
    <div className="page">
      <a className="backLink" href={hrefFor({ name: "people" })}>
        ← all people
      </a>

      <header className="detailHeader">
        <h1 className="pageTitle">{info.name || "Unnamed person"}</h1>
        {info.bio && <p className="lede">{info.bio}</p>}
      </header>

      <h2 className="sectionTitle">Appearances</h2>

      {page === undefined && appearancesLoading && (
        <p className="notice">loading appearances…</p>
      )}

      {page !== undefined && appearances.length === 0 && (
        <p className="notice">
          {pager.hasPrevious
            ? "No more appearances."
            : "No recorded appearances yet."}
        </p>
      )}

      <ul className="cardList">
        {appearances.map((appearance) => (
          <li className="card" key={appearance.episodeId}>
            <a
              className="cardKicker kickerLink"
              href={hrefFor({ name: "podcast", id: appearance.podcastId })}
            >
              {appearance.podcastName}
            </a>
            <a
              className="cardTitle"
              href={hrefFor({ name: "episode", id: appearance.episodeId })}
            >
              {appearance.episodeTitle || "Untitled episode"}
            </a>
            <div className="cardMeta">{formatDate(appearance.publishDate)}</div>
            {appearance.description && (
              <p className="cardBody">{appearance.description}</p>
            )}
            {appearance.chapters.length > 0 && (
              <ul className="chapterList inset">
                {appearance.chapters.map((chapter, index) => (
                  <li className="chapterRow" key={index}>
                    <span className="span">
                      {formatSpan(
                        chapter.startTimeSeconds,
                        chapter.endTimeSeconds
                      )}
                    </span>
                    <span>{chapter.title || "Untitled chapter"}</span>
                  </li>
                ))}
              </ul>
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
