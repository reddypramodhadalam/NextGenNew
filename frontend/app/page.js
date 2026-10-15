import { redirect } from 'next/navigation'

/**
 * app/page.js
 *
 * Root route ("/"). Kavach has no standalone home page — every visit to
 * "/" continues straight to "/dashboard", which opens on the Leadership
 * view by default (see app/dashboard/page.js).
 *
 * Why this also fixes the "not authenticated" case, not just the
 * logged-in one:
 *
 * AuthGuard (components/AuthGuard/AuthGuard.jsx) wraps every page via
 * the root layout and, when unauthenticated, triggers an Entra login
 * redirect using InteractionType.Redirect. MSAL then returns the user to
 * whatever URL originally triggered that redirect — navigateToLoginRequestUrl:
 * true in lib/msalConfig.js. Because this redirect() call runs on the
 * server before any client-side code (including AuthGuard) executes for
 * "/", an unauthenticated visit to "/" never actually reaches AuthGuard
 * at "/" — Next.js has already sent the browser to "/dashboard" by then.
 * AuthGuard therefore evaluates (and MSAL remembers) "/dashboard" as the
 * request URL, so a fresh login lands the user there directly instead of
 * back on "/".
 *
 * No 'use client' directive needed: redirect() from next/navigation is
 * designed to run in a Server Component and works via Next's routing
 * layer, not React state — nothing here touches MSAL or auth state
 * directly, so it can't conflict with AuthGuard's own logic.
 */
export default function RootPage() {
  redirect('/dashboard')
}