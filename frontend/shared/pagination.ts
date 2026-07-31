// Cursor pagination shared by every paginated view in this app.
//
// Every paginated reader in the API (`Directory.listPodcasts`,
// `Directory.searchMentions`, `Podcast.listEpisodes`,
// `Person.appearances`) takes an opaque `cursor` and returns a
// `nextCursor`. The cursors are forward-only and opaque — there is no
// "previous cursor" to compute — so paging backwards means remembering
// the cursors already visited. This keeps them on a stack: the top of
// the stack is the page being shown, popping it goes back a page.
import { useCallback, useState } from "react";

export interface CursorPager {
  /** The cursor for the page currently being shown. `""` is page one. */
  cursor: string;
  /** 1-based page number, for display. */
  page: number;
  hasPrevious: boolean;
  /** Advance to the page `nextCursor` points at. */
  goNext: (nextCursor: string) => void;
  goPrevious: () => void;
  /**
   * Return to page one. Callers MUST do this whenever the *query*
   * changes rather than the page — a cursor is only meaningful for the
   * scan that produced it. `Directory.searchMentions` in particular
   * pages over a different index depending on whether `podcastName` is
   * set, so carrying a cursor across a filter change would feed a key
   * from one index into another.
   */
  reset: () => void;
}

export function useCursorPager(): CursorPager {
  const [stack, setStack] = useState<string[]>([""]);

  const goNext = useCallback((nextCursor: string) => {
    if (!nextCursor) return;
    setStack((previous) => [...previous, nextCursor]);
  }, []);

  const goPrevious = useCallback(() => {
    setStack((previous) =>
      previous.length > 1 ? previous.slice(0, -1) : previous
    );
  }, []);

  const reset = useCallback(() => setStack([""]), []);

  return {
    cursor: stack[stack.length - 1],
    page: stack.length,
    hasPrevious: stack.length > 1,
    goNext,
    goPrevious,
    reset,
  };
}
