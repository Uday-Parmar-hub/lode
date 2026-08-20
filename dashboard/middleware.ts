import { NextRequest, NextResponse } from "next/server";

// Lightweight password gate for the hosted trial. Set LODE_BASIC_AUTH="user:pass" in the App Service
// config to require Basic auth on every request; leave it unset (local dev) and the app is open.
// This is a stopgap for the feedback deploy — replace with Entra SSO once the direction is committed.
export function middleware(req: NextRequest) {
  const expected = process.env.LODE_BASIC_AUTH;
  if (!expected) return NextResponse.next();

  const header = req.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    try {
      if (atob(header.slice(6)) === expected) return NextResponse.next();
    } catch {
      // malformed header → fall through to the challenge
    }
  }
  return new NextResponse("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="LODE", charset="UTF-8"' },
  });
}

// Gate everything except Next's static assets (so the login prompt appears once, not per-asset).
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
