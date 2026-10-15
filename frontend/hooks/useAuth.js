'use client'

/**
 * hooks/useAuth.js
 *
 * Shared authentication hook for Kavach.
 *
 * Must be used inside a component that is a descendant of MsalProvider
 * (i.e. inside the Providers wrapper in the root layout).
 *
 * Returns:
 *   isAuthenticated {boolean}
 *       true once the user has a valid Entra session.
 *
 *   user {AccountInfo|null}
 *       The signed-in user's MSAL account object.  Key fields:
 *         user.name     — display name (e.g. "Jane Doe")
 *         user.username — UPN / email (e.g. "jane@baxter.com")
 *       null when not authenticated.
 *
 *   login()  {async}
 *       Trigger Microsoft Entra login redirect.  MSAL stores the current
 *       page URL and returns to it after a successful sign-in.
 *
 *   logout() {async}
 *       Sign the user out and redirect to the post-logout URI.
 *
 *   getToken() {async → string|null}
 *       Acquire a Kavach backend access token silently from the cache or
 *       Entra's token endpoint.  If the cached token has expired and
 *       silent renewal fails, triggers a login redirect (the page reloads
 *       after sign-in and the caller should not expect a return value).
 *
 *       IMPORTANT: always await getToken().  Calling it without await is
 *       incorrect — the returned Promise must be resolved before the token
 *       string is available.
 */

import { useMsal, useIsAuthenticated } from '@azure/msal-react'
import { loginRequest } from '../lib/msalConfig'

export function useAuth() {
  const { instance, accounts } = useMsal()
  const isAuthenticated = useIsAuthenticated()

  // The first account in the list is the one MSAL set as active via the
  // event callback in msalInstance.js.  It is populated from sessionStorage
  // on every page load, so it survives refreshes.
  const user = accounts[0] ?? null

  /**
   * Trigger a Microsoft Entra login redirect.
   *
   * MSAL saves the current page URL before redirecting and navigates back
   * to it after a successful sign-in (navigateToLoginRequestUrl: true).
   */
  async function login() {
    await instance.loginRedirect(loginRequest)
  }

  /**
   * Sign the user out.
   *
   * The browser is redirected to NEXT_PUBLIC_MS_AUTH_POST_LOGOUT_REDIRECT_URI
   * after Entra completes the sign-out.
   */
  async function logout() {
    await instance.logoutRedirect({
      account: instance.getActiveAccount(),
    })
  }

  /**
   * Acquire an access token for the Kavach backend API.
   *
   * Attempts a silent acquisition first (no network round-trip if the token
   * is still cached).  If silent acquisition fails (expired token, empty
   * cache) a login redirect is triggered to re-establish the session.
   *
   * Always await this function.  The returned Promise resolves to the access
   * token string, or to null when a redirect was triggered (in which case
   * the browser navigates away and the calling component will remount after
   * sign-in completes).
   *
   * The scopes requested here include NEXT_PUBLIC_MS_AUTH_API_SCOPE so that
   * the returned token is valid for the Kavach backend, not Microsoft Graph.
   *
   * @returns {Promise<string|null>}
   */
  async function getToken() {
    const account = instance.getActiveAccount()
    if (!account) {
      throw new Error('getToken: no active account — the user must be authenticated first')
    }
    try {
      const response = await instance.acquireTokenSilent({
        ...loginRequest,
        account,
      })
      return response.accessToken
    } catch {
      // Silent acquisition failed.  Redirect to Entra to re-authenticate;
      // after sign-in the user returns to the current page.
      await instance.loginRedirect({
        ...loginRequest,
        account,
      })
      // The browser navigates away — this line is never reached.
      return null
    }
  }

  return { isAuthenticated, user, login, logout, getToken }
}
