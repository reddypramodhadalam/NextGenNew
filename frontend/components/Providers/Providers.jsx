'use client'

/**
 * components/Providers/Providers.jsx
 *
 * Client-side provider tree for the Kavach root layout.
 *
 * This is the only place MsalProvider appears in the application.
 * It receives the singleton PublicClientApplication from lib/msalInstance.js.
 *
 * On every mount, MsalProvider:
 *   1. Calls msalInstance.initialize() (required in @azure/msal-browser v3).
 *   2. Processes any Entra redirect response via handleRedirectPromise(),
 *      which runs inside initialize().  This completes the login flow when
 *      the browser returns from the Entra sign-in page.
 *   3. Emits auth events that the event callback in msalInstance.js uses
 *      to keep the active account in sync.
 *
 * AuthGuard is nested inside MsalProvider so it has access to the
 * initialised MSAL context when it decides whether to render children
 * or trigger a login redirect.
 */

import { MsalProvider } from '@azure/msal-react'
import { msalInstance } from '../../lib/msalInstance'
import AuthGuard from '../AuthGuard/AuthGuard'

export default function Providers({ children }) {
  return (
    <MsalProvider instance={msalInstance}>
      <AuthGuard>{children}</AuthGuard>
    </MsalProvider>
  )
}
