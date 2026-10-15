'use client'

/**
 * components/AuthGuard/AuthGuard.jsx
 *
 * Shared protected layout covering every page in Kavach.
 *
 * Uses MsalAuthenticationTemplate from @azure/msal-react, which:
 *   - Renders children only when the user is authenticated.
 *   - Automatically triggers a login redirect when not authenticated.
 *   - Waits for MSAL's initialisation to complete before acting (avoids
 *     spurious redirects during the startup window).
 *
 * URL preservation is handled by MSAL's built-in mechanism
 * (navigateToLoginRequestUrl: true in msalConfig).  No manual
 * sessionStorage handling is required.
 *
 * The loadingComponent renders nothing while MSAL is starting up or while
 * the redirect is in flight; the browser navigates away almost immediately
 * so this blank state is imperceptible in practice.
 */

import { useEffect } from 'react'
import { InteractionType, InteractionRequiredAuthError } from '@azure/msal-browser'
import { MsalAuthenticationTemplate, useMsal } from '@azure/msal-react'
import { loginRequest } from '../../lib/msalConfig'

/** Blank while MSAL initialises or the redirect is in progress. */
function LoadingState() {
  return null
}

// Error codes that mean "the session/token expired or the silent refresh
// timed out" — i.e. the fix is simply to sign in again, not to show an
// error. monitor_window_timeout is the slow-iframe silent-refresh timeout;
// the interaction_required family means Entra wants the user to
// re-authenticate (expired session, Conditional Access, MFA, etc.).
const RECOVERABLE_ERROR_CODES = new Set([
  'monitor_window_timeout',
  'interaction_required',
  'login_required',
  'consent_required',
])

// Loop guard: if we auto-redirected within this window and we're still
// landing on the error screen, stop auto-redirecting and show the manual
// screen instead, so the user can never get trapped in a redirect cycle.
const AUTO_REDIRECT_FLAG = 'kavach.authAutoRedirectAt'
const AUTO_REDIRECT_COOLDOWN_MS = 30000

function isRecoverable(error) {
  if (!error) return false
  if (error instanceof InteractionRequiredAuthError) return true
  return RECOVERABLE_ERROR_CODES.has(error.errorCode)
}

function redirectedRecently() {
  try {
    const last = Number(sessionStorage.getItem(AUTO_REDIRECT_FLAG) || 0)
    return last > 0 && Date.now() - last < AUTO_REDIRECT_COOLDOWN_MS
  } catch {
    return false
  }
}

/**
 * Shown when authentication fails.
 *
 * For recoverable errors (expired session / silent-refresh timeout), the
 * user is sent straight to the Microsoft sign-in page instead of seeing an
 * error — if their Microsoft session is still valid the round-trip is
 * invisible, otherwise they simply sign in again and return to their page.
 * A full-page loginRedirect also avoids the hidden-iframe path that causes
 * monitor_window_timeout on slow corporate networks.
 *
 * A 30-second loop guard ensures that if the redirect does not resolve the
 * problem, we fall back to the manual "Try again" screen rather than
 * bouncing the user to login repeatedly.
 */
function AuthError({ error }) {
  const { instance } = useMsal()
  const recoverable = isRecoverable(error) && !redirectedRecently()

  useEffect(() => {
    if (!recoverable) return
    try {
      sessionStorage.setItem(AUTO_REDIRECT_FLAG, String(Date.now()))
    } catch {
      /* sessionStorage unavailable — still attempt the redirect */
    }
    instance.loginRedirect(loginRequest).catch(() => {
      /* If the redirect itself fails, the render below shows the manual
         screen on the next error cycle. */
    })
  }, [recoverable, instance])

  // While the recoverable redirect is in flight, render nothing — the
  // browser is navigating to Microsoft sign-in.
  if (recoverable) {
    return null
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        gap: '12px',
        fontFamily: 'var(--font-body)',
        padding: '24px',
        textAlign: 'center',
      }}
    >
      <p style={{ fontSize: 16, margin: 0 }}>
        Sign-in failed:{' '}
        {error?.message ?? 'An unexpected authentication error occurred.'}
      </p>
      <button
        onClick={() => window.location.reload()}
        style={{ padding: '8px 16px', cursor: 'pointer' }}
      >
        Try again
      </button>
    </div>
  )
}

export default function AuthGuard({ children }) {
  return (
    <MsalAuthenticationTemplate
      interactionType={InteractionType.Redirect}
      authenticationRequest={loginRequest}
      loadingComponent={LoadingState}
      errorComponent={AuthError}
    >
      {children}
    </MsalAuthenticationTemplate>
  )
}
