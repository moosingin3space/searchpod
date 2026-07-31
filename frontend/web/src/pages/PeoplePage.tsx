// Person search.
//
// `Directory.findPerson` returns *every* plausible match rather than a
// single best guess, because a name is ambiguous — so this page always
// renders a list and lets the user pick, rather than auto-navigating
// even when there is exactly one hit.
//
// An empty name matches everything, which makes the unsubmitted state a
// useful "browse all guests" rather than a blank page.
import { useState, type FC } from "react";
import { useDirectory } from "@api/searchpod/v1/podcasts_rbt_react";
import { DIRECTORY_ID } from "../directory";
import { friendlyError } from "../errors";
import { hrefFor } from "../router";

export const PeoplePage: FC = () => {
  const [nameInput, setNameInput] = useState("");
  const [name, setName] = useState("");

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setName(nameInput.trim());
  };

  return (
    <div className="page">
      <h1 className="pageTitle">People</h1>

      <form className="searchForm" onSubmit={submit}>
        <label className="field">
          <span className="fieldLabel">Name</span>
          <input
            className="input"
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
            placeholder="leave blank to list every guest"
          />
        </label>
        <button className="primaryButton" type="submit">
          Find
        </button>
      </form>

      {/*
        Keyed on the name so a new lookup remounts instead of reusing
        the subscription: a reader keeps serving the previous query's
        response until the new one lands, and rendering that would
        briefly assert "nobody matches <new name>" from the old result.
      */}
      <PeopleResults key={name} name={name} />
    </div>
  );
};

const PeopleResults: FC<{ name: string }> = ({ name }) => {
  const directory = useDirectory({ id: DIRECTORY_ID });
  const { response, isLoading, aborted } = directory.useFindPerson({ name });

  if (aborted !== undefined) {
    return <div className="error">{friendlyError(aborted)}</div>;
  }

  if (response === undefined) {
    return <p className="notice">{isLoading ? "loading…" : "No results."}</p>;
  }

  const matches = response.matches;

  if (matches.length === 0) {
    return (
      <p className="notice">
        {name
          ? `Nobody in the catalog matches “${name}”.`
          : "No guests recorded yet."}
      </p>
    );
  }

  return (
    <>
      {matches.length > 1 && (
        <p className="notice">
          {matches.length} people match — pick the one you meant.
        </p>
      )}

      <ul className="cardList">
        {matches.map((match) => (
          <li className="card" key={match.personId}>
            <a
              className="cardTitle"
              href={hrefFor({ name: "person", id: match.personId })}
            >
              {match.name}
            </a>
          </li>
        ))}
      </ul>
    </>
  );
};
