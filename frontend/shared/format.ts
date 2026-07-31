// Formatting helpers shared by the MCP UIs and the standalone web SPA.
//
// Chapter end times are *derived*, never stored: the backend reports a
// chapter's `endTimeSeconds` as the next chapter's start, and leaves it
// `null`/`undefined` for the last chapter of an episode, which runs to
// the end. Every place that renders a timeframe has to handle that
// missing end, so the handling lives here rather than in each UI.

/** `seconds` as `M:SS`, or `H:MM:SS` once it passes an hour. */
export function formatTime(seconds: number | undefined | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) {
    return "--:--";
  }
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${minutes}:${pad(secs)}`;
}

/**
 * A chapter's span. A missing end time is not an error — it means the
 * chapter is the episode's last and runs to the end.
 */
export function formatSpan(
  startTimeSeconds: number | undefined | null,
  endTimeSeconds: number | undefined | null
): string {
  const start = formatTime(startTimeSeconds);
  if (endTimeSeconds === undefined || endTimeSeconds === null) {
    return `${start} – end`;
  }
  return `${start} – ${formatTime(endTimeSeconds)}`;
}

/** An ISO publish date rendered for humans, falling back to the raw string. */
export function formatDate(publishDate: string | undefined): string {
  if (!publishDate) return "";
  const parsed = new Date(publishDate);
  if (Number.isNaN(parsed.getTime())) return publishDate;
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
