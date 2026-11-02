# Setting up OIDC single sign-on

PerchTail's SSO is OIDC only for now (SAML is a later phase, built only if a
real need shows up — see ROADMAP.md). Local accounts always keep working
alongside SSO, including the break-glass super-admin. This guide covers what
to register at your identity provider (IdP) and configure on the **Settings
→ SSO** admin page — see [CLAUDE.md](../CLAUDE.md)'s Access control section
for the underlying `AuthProvider`/`SSOProviderConfig` design.

## At a glance

| Field (Settings → SSO) | What it is | Where it comes from |
|---|---|---|
| Issuer | The IdP's OIDC issuer URL | Your IdP — see per-provider notes below |
| Client ID / Client secret | This app's registered OAuth client credentials | Created when you register PerchTail as a client at your IdP |
| Scopes | OAuth scopes requested at login | Default `openid email profile` — leave as-is unless your IdP needs more to emit a group claim |
| Group claim | Name of the ID token claim carrying group membership (e.g. `groups`, `roles`) | Optional — only needed for group-to-role auto-mapping; see below |

There's no field for a redirect URI in the UI — it's always
`{PUBLIC_BASE_URL}/auth/sso/callback` (`PUBLIC_BASE_URL` is the `.env`
setting from the [Quick start](../README.md#quick-start)), and that's the
exact value to register at the IdP.

## 1. Register PerchTail as an OIDC client at your IdP

Whatever your IdP calls it ("application," "OAuth client," "OIDC integration"),
you're creating one with:

- **Grant type / flow**: Authorization Code (`response_type=code`) — PerchTail
  doesn't support implicit or device flows.
- **Redirect URI**: `{PUBLIC_BASE_URL}/auth/sso/callback` — must match
  exactly, including scheme (see the [reverse proxy + TLS](../README.md#deployment-reverse-proxy-nginx--tls)
  section — this needs to already be the real `https://` URL users will
  reach PerchTail at, not `http://localhost`).
- **Token endpoint authentication**: PerchTail sends `client_id`/
  `client_secret` in the token request's POST body (`client_secret_post`),
  not an `Authorization: Basic` header. If your IdP lets you pick a token
  endpoint auth method, choose that one — some (Okta, Auth0-style
  configurations) default to Basic and need this changed explicitly.
- **Scopes**: at minimum `openid` (required by the protocol) plus whatever
  yields an `email` claim — the default `openid email profile` covers this
  for every provider below. PerchTail identifies a returning user by the ID
  token's `sub` claim (always present) and uses `email` (falling back to
  `preferred_username`) as the displayed username — if neither claim is
  entirely absent, login fails.

## 2. Group-claim to role auto-mapping (optional)

Without a group claim configured, every SSO login (new or returning) that
doesn't already have a mapped role gets the built-in no-access role — an
admin assigns the real one manually afterward (Settings → Users), per
CLAUDE.md's phase-1 scope. Configuring a **group claim** here plus one or
more **group mappings** (also on Settings → SSO) automates that: PerchTail
reads the named claim out of the ID token, matches its values (last match in
`order` wins, same precedence rule as Rule's include/exclude) against your
configured mappings, and assigns that role — re-synced on *every* login, so
removing someone from the IdP group also revokes the mapped role on their
next sign-in. See per-provider notes below for what it takes to get a usable
group claim onto the ID token in the first place — several IdPs don't include
one by default.

## 3. Test before enabling

Use the **Test connection** action on the provider row before flipping
**Enabled** on — it fetches the IdP's discovery document and JWKS
(reachability + key-set validity) without attempting a real login, so a
typo'd issuer URL or an unreachable IdP surfaces immediately instead of at
someone's actual sign-in attempt.

## Per-provider notes

### Azure AD / Entra ID

- **Issuer**: `https://login.microsoftonline.com/{tenant-id}/v2.0`
- Register under **App registrations** → **New registration**, platform
  **Web**, redirect URI as above.
- **Client secret**: **Certificates & secrets** → **New client secret** —
  note its expiry; Entra ID secrets aren't permanent.
- **Group claim gotcha**: Entra ID's `groups` claim, once enabled (**Token
  configuration** → **Add groups claim**), emits group **object IDs**
  (GUIDs), not display names, by default — map your `SSOGroupRoleMapping`
  rows to those GUIDs, not human-readable names, unless you've additionally
  configured a groups claim transformation. Accounts in more than 200
  groups get a "groups overage" indicator instead of the claim entirely,
  which this app has no fallback for (no Microsoft Graph lookup) — group
  auto-mapping won't work for such accounts; they'll need a manual role
  assignment.

### Okta

- **Issuer**: `https://{yourOktaDomain}/oauth2/default` (or a custom
  authorization server's issuer, if you've set one up).
- **Applications** → **Create App Integration** → **OIDC — Web Application**.
- **Group claim gotcha**: Okta's default authorization server doesn't emit a
  groups claim at all until you add one — **Security** → **API** → your
  authorization server → **Claims** → add a claim named e.g. `groups`,
  scoped to the ID token, with a group filter (e.g. `.*` to include
  everything, or a prefix to scope it down).

### Google Workspace

- **Issuer**: `https://accounts.google.com`
- Register under **Google Cloud Console** → **APIs & Services** →
  **Credentials** → **OAuth client ID**, application type **Web
  application**.
- **Group mapping isn't usable with Google directly**: standard OIDC ID
  tokens from Google don't carry group membership at all (it's exposed
  separately via the Admin SDK Directory API, which is out of scope for
  this app's login flow — it only ever reads the ID token's own claims).
  Leave **Group claim** empty for a Google provider and assign roles
  manually after each new user's first login.

### Keycloak / Authentik

- **Issuer**: `https://{host}/realms/{realm}` (Keycloak) or
  `https://{host}/application/o/{app-slug}/` (Authentik).
- Both support a group claim out of the box, more directly than the SaaS
  IdPs above: Keycloak via a client scope with a **Group Membership**
  mapper (name it `groups`, token claim name `groups`); Authentik via an
  OAuth2/OIDC provider's **Property Mapping**, adding a scope that maps
  `request.user.ak_groups` (or similar) to a `groups` claim.

## Troubleshooting

- **Login redirects back to `/login?sso_error=1` with no detail** — check the
  application logs (`docker compose logs perchtail`, or Settings → the
  built-in log viewer if you can still reach it via local auth): the
  callback handler logs the specific reason (state mismatch, IdP error
  parameter, token exchange failure, invalid ID token) at `WARNING`, but
  deliberately shows the browser only a generic redirect — see
  CLAUDE.md's Application logging section for why (a detailed OIDC error
  is diagnostic information, not something to hand back to an
  unauthenticated browser).
- **"unexpected audience in ID token"** — the client ID registered in
  Settings → SSO doesn't match the `aud` claim the IdP actually issued;
  double check you copied the right client ID, not a related-but-different
  one (e.g. Okta's API Access Management client vs. the web app's).
- **A returning user's role keeps reverting after you change it manually** —
  expected if a group mapping matches them (see "re-synced on every login"
  above); adjust the mapping itself, or remove the user from the matching
  IdP group, rather than fighting it via a manual role change in PerchTail.
