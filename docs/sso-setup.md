# Setting up OIDC single sign-on

PerchTail supports single sign-on with OpenID Connect (OIDC); SAML isn't supported. Local accounts keep working alongside SSO, including the break-glass super admin. This guide covers what to register at your identity provider (IdP) and what to set on the **Settings → SSO** page.

## At a glance

**Settings → SSO** holds one OIDC provider at a time:

| Field | What it is | Where it comes from |
|---|---|---|
| **Display name** | The name on the login page's **Sign in with …** button | Your choice |
| **Issuer URL** | The IdP's OIDC issuer URL | Your IdP — see the per-provider notes below |
| **Client ID** / **Client secret** | The OAuth client credentials of PerchTail at the IdP | Created when you register PerchTail as a client at your IdP |
| **Scopes** | OAuth scopes requested at login | Default `openid email profile` — leave as-is unless your IdP needs more to emit a group claim |
| **Group claim** (optional) | Name of the ID token claim carrying group membership (e.g. `groups`, `roles`) | Only needed for group-to-role mapping; see below |
| **Enabled** | Shows the SSO button on the login page and accepts SSO sign-ins | Switch it on after the test below |

There's no field for a redirect URI — it's always `{PUBLIC_BASE_URL}/auth/sso/callback` (`PUBLIC_BASE_URL` is the `.env` setting from the [Quick start](../README.md#quick-start)), and that's the exact value to register at the IdP.

## 1. Register PerchTail as an OIDC client at your IdP

Whatever your IdP calls it ("application," "OAuth client," "OIDC integration"), you're creating one with:

- **Grant type / flow**: Authorization Code (`response_type=code`) — PerchTail doesn't support implicit or device flows.
- **Redirect URI**: `{PUBLIC_BASE_URL}/auth/sso/callback` — must match exactly, including scheme (see the [reverse proxy + TLS](../README.md#deployment-reverse-proxy-nginx--tls) section — this needs to already be the real `https://` URL users will reach PerchTail at, not `http://localhost`).
- **Token endpoint authentication**: PerchTail sends `client_id` and `client_secret` in the token request's POST body (`client_secret_post`), not in an `Authorization: Basic` header. If your IdP lets you pick a token endpoint auth method, choose that one — some (Okta, Auth0-style configurations) default to Basic and need this changed explicitly.
- **Scopes**: at minimum `openid` (required by the protocol) plus whatever yields an `email` claim — the default `openid email profile` covers this for every provider below.

### How a sign-in maps to an account

- PerchTail identifies an SSO account by the IdP's issuer and the ID token's `sub` claim. A user from one IdP is never matched to an account another IdP created.
- On the first sign-in, PerchTail creates the account. Its username is the `email` claim, or `preferred_username` when there's no email, or `sub` when there's neither. The username doesn't change on later sign-ins.
- On every sign-in, the `name` claim (or `given_name` and `family_name`) and the `email` claim set the account's name and email under **My profile**, where SSO users see them read-only. An email the IdP marks as unverified (`email_verified: false`), or one another account already has, is left out.
- **Usernames are shared by local and SSO accounts.** PerchTail doesn't link an SSO sign-in to an existing local account. If the new account's username is already taken (by a local account, or by an SSO account from another issuer), the first sign-in fails and redirects to the login page with an error; the log has `sso.callback.login_failed`. Give local accounts usernames your IdP will never send, for example not an email address.
- **MFA is for local accounts only.** The TOTP second factor under **My profile** exists only for local accounts. SSO users get their second factor, if any, from the IdP. Changing the password in PerchTail is also for local accounts only.
- A deactivated account can't sign in with SSO either.

## 2. Group-claim to role mapping (optional)

Without a group claim, a new SSO account gets the built-in no-access role, and an admin assigns the real one afterward in **Settings → Users**.

To automate that, set **Group claim** and add one or more mappings under **Group → role mapping** on the same page (**+ Add mapping**: an **IdP group name** and a **Role**). PerchTail reads the named claim from the ID token and matches its values against the mappings. Mappings are evaluated in order, and the last match wins, the same rule as source rules. New mappings go to the end of the list.

What happens on each sign-in:

- **A mapping matches:** the account gets that role, on every sign-in. This overrides a role assigned directly in PerchTail, as long as the user's groups still match.
- **No mapping matches:** a new account gets the no-access role; an existing account keeps the role it has.

So **removing someone from an IdP group doesn't take the mapped role away**. Their role stays until an admin changes it in **Settings → Users**, or until another mapping matches. To remove access, change the user's role or deactivate the account in **Settings → Users** (and remove them at the IdP).

See the per-provider notes below for what it takes to get a usable group claim onto the ID token — several IdPs don't include one by default.

An admin with **Manage SSO settings** who isn't a super admin can only map a group to a role within their own access: no global capability they lack, and no capability in a customer, folder or source that they don't hold there themselves. They also can't create a provider, or change its **Issuer URL**, **Client ID** or **Group claim** or switch it on, while an existing mapping grants a role beyond their own access.

## 3. Test before enabling

1. Fill in the form with **Enabled** off and click **Save**.
2. Click **Test connection**. It appears once the provider is saved, and tests the saved settings: it fetches the IdP's discovery document (`{Issuer URL}/.well-known/openid-configuration`), checks that it names the authorization, token and JWKS endpoints, and fetches the JWKS. It doesn't attempt a real login, so it can't catch a wrong client ID or secret.
3. Switch on **Enabled** and click **Save**. The login page now shows **Sign in with** and the display name.

Test the whole flow with a test user before you tell anyone; keep a local super admin session open in another browser while you do.

## Per-provider notes

### Azure AD / Entra ID

- **Issuer URL**: `https://login.microsoftonline.com/{tenant-id}/v2.0`
- Register under **App registrations** → **New registration**, platform **Web**, redirect URI as above.
- **Client secret**: **Certificates & secrets** → **New client secret** — note its expiry; Entra ID secrets aren't permanent.
- **Group claim gotcha**: Entra ID's `groups` claim, once enabled (**Token configuration** → **Add groups claim**), emits group **object IDs** (GUIDs), not display names, by default — use those GUIDs as the **IdP group name** of your mappings, unless you've configured a groups claim transformation. Accounts in more than 200 groups get a "groups overage" indicator instead of the claim, and PerchTail has no fallback for that (no Microsoft Graph lookup) — group mapping won't work for such accounts; they'll need a manual role assignment.

### Okta

- **Issuer URL**: `https://{yourOktaDomain}/oauth2/default` (or a custom authorization server's issuer, if you've set one up).
- **Applications** → **Create App Integration** → **OIDC — Web Application**.
- **Group claim gotcha**: Okta's default authorization server doesn't emit a groups claim at all until you add one — **Security** → **API** → your authorization server → **Claims** → add a claim named e.g. `groups`, scoped to the ID token, with a group filter (e.g. `.*` to include everything, or a prefix to scope it down).

### Google Workspace

- **Issuer URL**: `https://accounts.google.com`
- Register under **Google Cloud Console** → **APIs & Services** → **Credentials** → **OAuth client ID**, application type **Web application**.
- **Group mapping isn't usable with Google directly**: standard OIDC ID tokens from Google don't carry group membership at all (it's exposed separately via the Admin SDK Directory API, and PerchTail only reads the ID token's own claims). Leave **Group claim** empty for a Google provider and assign roles manually after each new user's first sign-in.

### Keycloak / Authentik

- **Issuer URL**: `https://{host}/realms/{realm}` (Keycloak) or `https://{host}/application/o/{app-slug}/` (Authentik).
- Both support a group claim out of the box, more directly than the SaaS IdPs above: Keycloak via a client scope with a **Group Membership** mapper (name it `groups`, token claim name `groups`); Authentik via an OAuth2/OIDC provider's **Property Mapping**, adding a scope that maps `request.user.ak_groups` (or similar) to a `groups` claim.

## Troubleshooting

- **The login page says "SSO sign-in failed"** (the address ends in `#/login?sso_error=1`) — the browser gets no detail on purpose: a detailed OIDC error is diagnostic information, not something to hand to an unauthenticated browser. The application log has the reason, at `WARNING`: `sso.callback.idp_error` (the IdP returned an error), `sso.callback.state_cookie_mismatch` or `sso.callback.invalid_state` (the sign-in took longer than 10 minutes, or the browser lost the state cookie), or `sso.callback.login_failed` with an `error` field (token exchange failed, invalid ID token, deactivated account, or a username already taken). Read it with `docker compose logs perchtail` or in `LOG_DIR`. A super admin who can still sign in with a local account can also open the **PerchTail application logs** source (marked **system**) in the **Viewer**: it reads `LOG_DIR`.
- **The SSO button answers "Could not reach the identity provider"** — PerchTail couldn't fetch the discovery document; the log has `sso.login.discovery_failed`. Check the **Issuer URL** and that the PerchTail host can reach the IdP.
- **`unexpected audience in ID token` in the log** — the **Client ID** in Settings → SSO doesn't match the `aud` claim the IdP issued; check you copied the right client ID, not a related one (e.g. Okta's API Access Management client vs. the web app's).
- **A returning user's role keeps reverting after you change it manually** — expected if a group mapping matches them (see above); change the mapping, or remove the user from the matching IdP group, rather than changing the role in PerchTail.
