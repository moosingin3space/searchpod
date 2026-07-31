// The `Person.show` MCP UI: a guest's bio and what they have talked
// about, episode by episode.
//
// `Person.appearances` is the composing reader that answers "what has
// this person talked about" — each entry already carries the episode's
// podcast, description, and chapters, so the chapter list renders from
// the same single subscription.
import { type FC } from "react";
import { usePerson } from "@api/searchpod/v1/podcasts_rbt_react";
import { formatDate, formatSpan } from "../../shared/format";
import { useCursorPager } from "../../shared/pagination";
import css from "./App.module.css";

export const PersonApp: FC = () => {
  const { person, isLoading } = usePerson();

  if (person === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>
          {isLoading ? "loading person…" : "No person selected."}
        </div>
      </div>
    );
  }

  return <PersonView personId={person.state_id} />;
};

const PersonView: FC<{ personId: string }> = ({ personId }) => {
  const person = usePerson({ id: personId });
  const pager = useCursorPager();

  const { response: info, aborted } = person.useGet();
  const { response: page, isLoading: appearancesLoading } =
    person.useAppearances({ cursor: pager.cursor });

  if (aborted !== undefined) {
    return (
      <div className={css.container}>
        <div className={css.error}>
          Could not load this person: {aborted.message}
        </div>
      </div>
    );
  }

  if (info === undefined) {
    return (
      <div className={css.container}>
        <div className={css.notice}>loading person…</div>
      </div>
    );
  }

  const appearances = page?.appearances ?? [];
  const nextCursor = page?.nextCursor ?? "";

  return (
    <div className={css.container}>
      <header className={css.header}>
        <h1 className={css.title}>{info.name || "Unnamed person"}</h1>
        {info.bio && <p className={css.bio}>{info.bio}</p>}
      </header>

      <section>
        <h2 className={css.sectionTitle}>
          Appearances{pager.page > 1 ? ` · page ${pager.page}` : ""}
        </h2>

        {page === undefined && appearancesLoading && (
          <div className={css.notice}>loading appearances…</div>
        )}

        {page !== undefined && appearances.length === 0 && (
          <div className={css.notice}>
            {pager.hasPrevious
              ? "No more appearances on this page."
              : "No recorded appearances yet."}
          </div>
        )}

        <ul className={css.list}>
          {appearances.map((appearance) => (
            <li key={appearance.episodeId} className={css.appearance}>
              <div className={css.podcastName}>{appearance.podcastName}</div>
              <div className={css.episodeTitle}>
                {appearance.episodeTitle || "Untitled episode"}
              </div>
              <div className={css.date}>
                {formatDate(appearance.publishDate)}
              </div>
              {appearance.description && (
                <p className={css.description}>{appearance.description}</p>
              )}
              {appearance.chapters.length > 0 && (
                <ul className={css.chapters}>
                  {appearance.chapters.map((chapter, index) => (
                    <li key={index} className={css.chapter}>
                      <span className={css.span}>
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
