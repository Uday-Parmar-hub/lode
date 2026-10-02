import "server-only";

import { headers } from "next/headers";

/** Who is looking at the dashboard, according to Azure Easy Auth. */
export interface Viewer {
  email: string;
  name: string | null;
}

/**
 * The signed-in user, from the headers Container Apps' Easy Auth injects after Entra sign-in.
 *
 * LODE has no session of its own — the platform authenticates and passes the identity through, so
 * there is nothing to verify here and nothing to spoof from outside: Easy Auth strips these headers
 * from inbound requests and sets them itself. Locally there is no Easy Auth, so this returns null
 * and the usage page falls back to LODE_USAGE_LOCAL_VIEWER (see below).
 *
 * `x-ms-client-principal-name` is the user's UPN for Entra sign-in. The base64 `x-ms-client-principal`
 * blob carries the full claim set if more is ever needed.
 */
export async function currentViewer(): Promise<Viewer | null> {
  const h = await headers();
  const email = h.get("x-ms-client-principal-name");
  if (email) {
    return { email: email.toLowerCase(), name: claimName(h.get("x-ms-client-principal")) };
  }
  // Local development: no Easy Auth in front, so allow an explicit stand-in rather than pretending
  // nobody is signed in. Never set in production.
  const local = process.env.LODE_USAGE_LOCAL_VIEWER;
  return local ? { email: local.toLowerCase(), name: "local" } : null;
}

/** Pull a display name out of Easy Auth's base64 claims blob. Best-effort — the name is cosmetic. */
function claimName(principal: string | null): string | null {
  if (!principal) return null;
  try {
    const decoded = JSON.parse(Buffer.from(principal, "base64").toString("utf-8")) as {
      claims?: { typ: string; val: string }[];
    };
    const claims = decoded.claims ?? [];
    const by = (t: string) => claims.find((c) => c.typ === t)?.val ?? null;
    return by("name") ?? by("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname") ?? null;
  } catch {
    return null;
  }
}

/**
 * Whether this viewer may see the usage page.
 *
 * The page is unlisted — nothing in the UI links to it — but LODE sits behind org-wide sign-in, so
 * obscurity is not access control: any colleague who guessed the path would be signed in already.
 * This is the actual gate. Set LODE_USAGE_EMAILS to a comma-separated list; unset means nobody,
 * which is the safe default for a page about who is using the tool.
 */
export async function canSeeUsage(): Promise<boolean> {
  const allowed = (process.env.LODE_USAGE_EMAILS ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  if (!allowed.length) return false;
  const viewer = await currentViewer();
  return !!viewer && allowed.includes(viewer.email);
}
