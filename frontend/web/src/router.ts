// A hash router.
//
// Hash routing rather than the History API on purpose: this SPA is
// served under a path prefix (`/__/frontend/web/`) by both the Vite dev
// server and the backend's dist-mode static server, and neither is
// configured to rewrite deep paths back to `index.html`. With the route
// in the fragment, every URL is a real request for the same
// `index.html`, so deep links and reloads work on both without any
// server-side SPA fallback.
import { useEffect, useState } from "react";

export type Route =
  | { name: "search" }
  | { name: "podcasts" }
  | { name: "podcast"; id: string }
  | { name: "episode"; id: string }
  | { name: "people" }
  | { name: "person"; id: string };

export function hrefFor(route: Route): string {
  switch (route.name) {
    case "search":
      return "#/";
    case "podcasts":
      return "#/podcasts";
    case "podcast":
      return `#/podcast/${encodeURIComponent(route.id)}`;
    case "episode":
      return `#/episode/${encodeURIComponent(route.id)}`;
    case "people":
      return "#/people";
    case "person":
      return `#/person/${encodeURIComponent(route.id)}`;
  }
}

function parse(hash: string): Route {
  const path = hash.replace(/^#\/?/, "");
  const segments = path.split("/");
  const head = segments[0] ?? "";
  // An entity id is opaque and may contain characters that need
  // escaping, so it is encoded on the way in and decoded here.
  const id = decodeURIComponent(segments.slice(1).join("/"));

  switch (head) {
    case "podcasts":
      return { name: "podcasts" };
    // A detail route with no id can't render anything, so fall back to
    // the corresponding list rather than mounting a hook with an empty
    // id — an empty id throws inside the generated client.
    case "podcast":
      return id ? { name: "podcast", id } : { name: "podcasts" };
    case "episode":
      return id ? { name: "episode", id } : { name: "search" };
    case "people":
      return { name: "people" };
    case "person":
      return id ? { name: "person", id } : { name: "people" };
    default:
      return { name: "search" };
  }
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));

  useEffect(() => {
    const onHashChange = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return route;
}
