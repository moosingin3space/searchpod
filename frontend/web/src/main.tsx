import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RebootClientProvider } from "@reboot-dev/reboot-react";
import { App } from "./App";
import "./app.css";

// In dev the SPA is served by Vite while the backend listens elsewhere,
// so the URL comes from `web/.env.development`. In a dist build the
// backend serves these assets itself, and the origin fallback is right.
const REBOOT_URL =
  (import.meta.env.VITE_REBOOT_URL as string | undefined) ??
  window.location.origin;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RebootClientProvider url={REBOOT_URL}>
      <App />
    </RebootClientProvider>
  </StrictMode>
);
