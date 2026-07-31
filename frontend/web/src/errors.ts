// One place that turns a typed backend abort into something a person
// can read.
//
// The API declares no custom errors, so everything reaching here is a
// framework error. `PermissionDenied` is the one worth spelling out:
// every catalog method is gated on a verified token, so it means the
// session lapsed rather than that anything is broken.
export interface AbortedLike {
  error: { type: string };
  message: string;
}

export function friendlyError(aborted: AbortedLike): string {
  switch (aborted.error.type) {
    case "PermissionDenied":
      return "Your session isn't authorized for this. Try signing in again.";
    case "Unauthenticated":
      return "You're signed out. Sign in to browse the catalog.";
    case "NotFound":
    case "StateNotConstructed":
      return "That item isn't in the catalog.";
    default:
      return aborted.message || "Something went wrong.";
  }
}
