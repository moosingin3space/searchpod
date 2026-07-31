// The standalone web SPA's shell: sign-in gate, nav, and route switch.
//
// Every catalog method is authorized with
// `allow_if(any=[is_app_internal, has_verified_token])`, so there is no
// anonymous read path — not even browsing the podcast list. The whole
// app therefore sits behind the sign-in gate rather than showing a
// public shell that aborts on its first read.
import { type FC } from "react";
import { useSignIn, useSignOut } from "@reboot-dev/reboot-react";
import { useUser } from "@api/searchpod/v1/podcasts_rbt_react";
import { hrefFor, useRoute } from "./router";
import { SearchPage } from "./pages/SearchPage";
import { PodcastsPage } from "./pages/PodcastsPage";
import { PodcastPage } from "./pages/PodcastPage";
import { EpisodePage } from "./pages/EpisodePage";
import { PeoplePage } from "./pages/PeoplePage";
import { PersonPage } from "./pages/PersonPage";

export const App: FC = () => {
  // `isLoading` covers the `/__/oauth/whoami` session probe.
  const { user, isLoading } = useUser();
  const signIn = useSignIn();
  const signOut = useSignOut();

  if (isLoading) {
    return (
      <div className="shell">
        <div className="notice">checking your session…</div>
      </div>
    );
  }

  if (user === undefined) {
    return (
      <div className="shell signedOut">
        <h1 className="brand">searchpod</h1>
        <p className="tagline">
          Search a podcast catalog by topic — which episode discussed it,
          when it came up, and who the guest was.
        </p>
        <button className="primaryButton" onClick={() => signIn()}>
          Sign in
        </button>
      </div>
    );
  }

  return (
    <div className="shell">
      <Nav onSignOut={() => signOut()} />
      <main className="main">
        <Routed />
      </main>
    </div>
  );
};

const Nav: FC<{ onSignOut: () => void }> = ({ onSignOut }) => {
  const route = useRoute();
  const isActive = (names: string[]) => (names.includes(route.name) ? "active" : "");

  return (
    <nav className="nav">
      <a className="brandLink" href={hrefFor({ name: "search" })}>
        searchpod
      </a>
      <div className="navLinks">
        <a className={isActive(["search", "episode"])} href={hrefFor({ name: "search" })}>
          Search
        </a>
        <a
          className={isActive(["podcasts", "podcast"])}
          href={hrefFor({ name: "podcasts" })}
        >
          Podcasts
        </a>
        <a
          className={isActive(["people", "person"])}
          href={hrefFor({ name: "people" })}
        >
          People
        </a>
      </div>
      <button className="linkButton" onClick={onSignOut}>
        Sign out
      </button>
    </nav>
  );
};

const Routed: FC = () => {
  const route = useRoute();

  switch (route.name) {
    case "search":
      return <SearchPage />;
    case "podcasts":
      return <PodcastsPage />;
    case "podcast":
      return <PodcastPage podcastId={route.id} />;
    case "episode":
      return <EpisodePage episodeId={route.id} />;
    case "people":
      return <PeoplePage />;
    case "person":
      return <PersonPage personId={route.id} />;
  }
};
