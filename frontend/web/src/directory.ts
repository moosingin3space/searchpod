// The catalog is one shared `Directory` actor with a well-known id
// (`"global"`, created by the backend's `initialize` hook), so every
// page that talks to the catalog resolves the same handle.
export const DIRECTORY_ID = "global";
