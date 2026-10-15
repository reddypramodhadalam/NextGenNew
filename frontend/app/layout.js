import './globals.css'
// Self-hosted Tabler icons webfont. Served from our own bundle instead of the
// jsDelivr CDN, so corporate Tracking Prevention / proxies can't block it.
import '@tabler/icons-webfont/dist/tabler-icons.min.css'
import Providers from '../components/Providers/Providers'
import AppShell from '../components/AppShell/AppShell'

export const metadata = {
  title: 'NextGen2 — Test Intelligence',
  description: 'AI-powered test automation platform',
}

export default function RootLayout({ children }) {
  return (
    // suppressHydrationWarning: the inline script below rewrites data-theme
    // from localStorage before React hydrates, so the server value ("dark")
    // and the client value can differ by design. This silences only that
    // element's attribute mismatch, not real hydration bugs elsewhere.
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        {/* Apply saved theme before paint to avoid a flash */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('kavach-theme')||'dark';document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
          }}
        />
      </head>
      <body>
        {/*
          Providers wraps the entire application with:
            MsalProvider  — one PublicClientApplication, initialised once.
            AuthGuard     — redirects to Entra login if not authenticated;
                            covers every page without per-page logic.
        */}
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  )
}
