/**
 * lib/msalConfig.js
 *
 * Centralised Microsoft Entra ID (MSAL) configuration for Kavach.
 * Every value comes from environment variables — nothing is hardcoded here.
 *
 * Required variables (defined in .env.local):
 *   NEXT_PUBLIC_MS_AUTH_CLIENT_ID
 *       Entra app registration client ID (GUID).
 *   NEXT_PUBLIC_MS_AUTH_AUTHORITY
 *       https://login.microsoftonline.com/<tenant-id>
 *   NEXT_PUBLIC_MS_AUTH_REDIRECT_URI
 *       URI Entra redirects to after a successful login (must match the
 *       app registration's redirect URI list).
 *   NEXT_PUBLIC_MS_AUTH_POST_LOGOUT_REDIRECT_URI
 *       URI Entra redirects to after logout.
 *   NEXT_PUBLIC_MS_AUTH_API_SCOPE
 *       Backend API scope, e.g. api://<client-id>/access_as_user
 *       Including this scope ensures the acquired access token is issued
 *       for Kavach's own backend, not a generic Microsoft Graph token.
 */

export const msalConfig = {
  auth: {
    clientId: process.env.NEXT_PUBLIC_MS_AUTH_CLIENT_ID,
    authority: process.env.NEXT_PUBLIC_MS_AUTH_AUTHORITY,
    redirectUri: process.env.NEXT_PUBLIC_MS_AUTH_REDIRECT_URI,
    postLogoutRedirectUri: process.env.NEXT_PUBLIC_MS_AUTH_POST_LOGOUT_REDIRECT_URI,
    // After a successful login redirect, MSAL navigates the user back to
    // the page they originally requested.  This is the built-in mechanism
    // for URL preservation — no manual sessionStorage handling is needed.
    navigateToLoginRequestUrl: true,
  },
  cache: {
    // sessionStorage is cleared when the browser tab closes, which is
    // appropriate for enterprise environments where shared computers are
    // common and persistent sessions are a security risk.
    cacheLocation: 'sessionStorage',
    storeAuthStateInCookie: false,
  },
}

/**
 * TEMPORARY: NEXT_PUBLIC_MS_AUTH_API_SCOPE is intentionally excluded here.
 *
 * "Expose an API" is not yet configured on the Entra app registration
 * (Client ID 91468164-...), so requesting this scope fails with
 * AADSTS65005 ("scope doesn't exist"). Standard OIDC scopes only, for now.
 *
 * IMPACT: login/logout work correctly, but the access token returned by
 * getToken() will NOT have an audience matching the Kavach backend. Do not
 * implement or test backend token audience validation until this is
 * resolved with the Entra admin. See .github/copilot-instructions.md.
 *
 * TO RESTORE: uncomment the process.env line below once "Expose an API"
 * is confirmed configured.
 */
export const loginRequest = {
  scopes: [
    'openid',
    'profile',
    'email',
    // process.env.NEXT_PUBLIC_MS_AUTH_API_SCOPE,
  ].filter(Boolean),
}