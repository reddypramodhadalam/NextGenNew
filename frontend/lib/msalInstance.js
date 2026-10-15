/**
 * lib/msalInstance.js
 *
 * The one PublicClientApplication for Kavach.
 *
 * Import `msalInstance` wherever you need the MSAL instance.
 * Never call `new PublicClientApplication()` anywhere else in the codebase.
 *
 * The event callback keeps the active account in sync with every successful
 * auth event (initial login, silent renewal, SSO).  This ensures that
 * `instance.getActiveAccount()` always returns the current user for the
 * lifetime of the session, including after a page refresh where MSAL
 * restores state from sessionStorage.
 */

import { PublicClientApplication, EventType } from '@azure/msal-browser'
import { msalConfig } from './msalConfig'

export const msalInstance = new PublicClientApplication(msalConfig)

// Restore / update the active account after every successful authentication
// event.  MSAL fires these events both when the user first signs in and
// when a silent token renewal succeeds, so this handler covers all cases.
msalInstance.addEventCallback((event) => {
  if (
    (event.eventType === EventType.LOGIN_SUCCESS ||
      event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS ||
      event.eventType === EventType.SSO_SILENT_SUCCESS) &&
    event.payload?.account
  ) {
    msalInstance.setActiveAccount(event.payload.account)
  }
})
