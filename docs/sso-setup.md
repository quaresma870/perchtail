# Setting up OIDC single sign-on

PerchTail supports single sign-on with OpenID Connect (OIDC); SAML isn't supported. Local accounts keep working alongside SSO, including the break-glass super admin. This guide covers what to register at your identity provider (IdP) and what to set on the **Settings → Single sign-on** page.

## At a glance

**Settings → Single sign-on** holds one OIDC provider at a time:

| Field | What it is | Where it comes from |
|---|---|---|
| **Display name** | The name on the login page's **Sign in with …** button | Your choice |
| **Issuer URL** | The IdP's OIDC issuer URL, `https://` only | Your IdP — see the per-provider notes below |
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
- **PKCE**: PerchTail sends a PKCE challenge (`code_challenge_method=S256`) with every sign-in and the matching `code_verifier` with the token request, as well as the client secret. IdPs that offer a "require PKCE" option for confidential clients can have it switched on.
- **HTTPS**: the issuer URL, and the authorization, token and JWKS endpoints its discovery document names, must be `https://`. PerchTail refuses an `http://` issuer when you save the provider, and checks the endpoints before every request to the IdP. For a local test IdP only, `SSO_ALLOW_HTTP_LOCALHOST=true` also accepts `http://` on `localhost`, `127.0.0.1` and `::1` ([configuration.md](configuration.md)).
- **Public address**: PerchTail's requests to the IdP (the discovery document, the JWKS and the token request) follow the same address policy as alert webhooks. The host must resolve only to public addresses, PerchTail connects to the address it checked, and it doesn't follow redirects. An IdP at a private, loopback or link-local address is refused. `SSO_ALLOW_HTTP_LOCALHOST=true` also lets through `localhost`, `127.0.0.1` and `::1`, and nothing else.
- **Signature algorithms**: PerchTail accepts an ID token signed with one of the asymmetric algorithms (RS*, PS*, ES*, EdDSA) the discovery document lists in `id_token_signing_alg_values_supported`, or RS256 when it lists none. A token signed with `none` or HS* is always refused.
- **Starting a sign-in** is limited to 20 a minute per client address (then 429), and PerchTail reuses the IdP's discovery document for 10 minutes; saving the provider clears it.
- **Issuer check**: the discovery document's `issuer` must be the **Issuer URL** exactly (a trailing slash aside, so copy it with the same case and path as the IdP publishes it). Otherwise PerchTail refuses the sign-in and the test, and audits it (`user.sso_login_failed` with reason `issuer_mismatch`, or `sso_provider.issuer_mismatch` for **Test connection**).
- **ID token**: it must carry `exp` and `iat`, besides `iss`, `aud`, `sub` and the `nonce` PerchTail sent; a token without them is refused. A token with several audiences must also carry `azp` with PerchTail's client ID.

### How a sign-in maps to an account

- PerchTail identifies an SSO account by the IdP's issuer and the ID token's `sub` claim. A user from one IdP is never matched to an account another IdP created.
- On the first sign-in, PerchTail creates the account. Its username is the `email` claim when the token says `email_verified: true`, otherwise `preferred_username`, or `sub` when there's neither. The username doesn't change on later sign-ins.
- On every sign-in, the `name` claim (or `given_name` and `family_name`) and the `email` claim set the account's name and email under **My profile**, where SSO users see them read-only. An email the IdP marks as unverified (`email_verified: false`), or one another account already has, is left out.
- **Usernames are shared by local and SSO accounts.** PerchTail doesn't link an SSO sign-in to an existing local account. If the new account's username is already taken (by a local account, or by an SSO account from another issuer), the first sign-in fails and redirects to the login page with an error; the log has `sso.callback.login_failed`. Give local accounts usernames your IdP will never send, for example not an email address.
- **MFA is for local accounts only.** The TOTP second factor under **My profile** exists only for local accounts. SSO users get their second factor, if any, from the IdP. Changing the password in PerchTail is also for local accounts only: an SSO account can't sign in with the local form, and an admin can't give it a password.
- A deactivated account can't sign in with SSO either.

## 2. Group-claim to role mapping (optional)

An SSO account's role comes from the group mappings at every sign-in. Without a group claim, or without a mapping that matches, an SSO account has the built-in no-access role, and a role assigned to it in **Settings → Users** lasts only until its next sign-in.

To automate that, set **Group claim** and add one or more mappings under **Group → role mapping** on the same page (**+ Add mapping**: an **IdP group name** and a **Role**). PerchTail reads the named claim from the ID token and matches its values against the mappings. Mappings are evaluated in order, and the last match wins, the same rule as source rules. New mappings go to the end of the list.

What happens on each sign-in:

- **A mapping matches:** the account gets that role, on every sign-in. This overrides a role assigned directly in PerchTail, as long as the user's groups still match.
- **No mapping matches:** the account gets the no-access role, new or existing, audited as `user.sso_role_synced` with reason `no_matching_mapping`. This also replaces a role assigned directly in PerchTail.

So **removing someone from an IdP group takes the mapped role away** at their next sign-in. Sessions already open keep the role until they end; to remove access at once, deactivate the account in **Settings → Users** (and remove them at the IdP).

The last active super admin is never demoted by a sign-in: if their groups no longer give them a super-admin role, they keep it, and the log has `sso.role_sync_kept_last_super_admin`. Keep a local super admin (the break-glass account) so this never matters.

See the per-provider notes below for what it takes to get a usable group claim onto the ID token — several IdPs don't include one by default.

An admin with **Manage SSO settings** who isn't a super admin can only create, change or delete a mapping whose role is within their own access: no super-admin role, no global capability they lack, and no capability in a customer, folder or source that they don't hold there themselves. Since the last matching mapping wins, they also can't create, change, move or delete a mapping at or after the position of a mapping whose role is beyond their own access: it would override that mapping, or uncover it, for people in both groups. They also can't create a provider, or change its **Issuer URL**, **Client ID** or **Group claim** or switch it on, while an existing mapping grants a role beyond their own access. Only a super admin deletes the provider (**Remove**) or switches it off. **Settings → Single sign-on** shows such an admin every mapping, but a role beyond their own access as "A role outside your access", without its name, and offers **Remove** only on the mappings they may remove.

## 3. Test before enabling

1. Fill in the form with **Enabled** off and click **Save**.
2. Click **Test connection**. It appears once the provider is saved, and tests the saved settings: it fetches the IdP's discovery document (`{Issuer URL}/.well-known/openid-configuration`), checks that it names the configured issuer and the authorization, token and JWKS endpoints, and fetches the JWKS. It doesn't attempt a real login, so it can't catch a wrong client ID or secret. When it fails, the page only says so; the reason is in the application log (`sso.test.failed`, `sso.test.discovery_incomplete` or `sso.test.issuer_mismatch`).
3. Switch on **Enabled** and click **Save**. The login page now shows **Sign in with** and the display name.

Test the whole flow with a test user before you tell anyone; keep a local super admin session open in another browser while you do.

## Per-provider notes

### Azure AD / Entra ID

- **Issuer URL**: `https://login.microsoftonline.com/{tenant-id}/v2.0`
- Register under **App registrations** → **New registration**, platform **Web**, redirect URI as above.
- **Client secret**: **Certificates & secrets** → **New client secret** — note its expiry; Entra ID secrets aren't permanent.
- **Group claim gotcha**: Entra ID's `groups` claim, once enabled (**Token configuration** → **Add groups claim**), emits group **object IDs** (GUIDs), not display names, by default — use those GUIDs as the **IdP group name** of your mappings, unless you've configured a groups claim transformation. Accounts in more than 200 groups get a "groups overage" indicator instead of the claim, and PerchTail has no fallback for that (no Microsoft Graph lookup) — group mapping won't work for such accounts, and they sign in with the no-access role.

### Okta

- **Issuer URL**: `https://{yourOktaDomain}/oauth2/default` (or a custom authorization server's issuer, if you've set one up).
- **Applications** → **Create App Integration** → **OIDC — Web Application**.
- **Group claim gotcha**: Okta's default authorization server doesn't emit a groups claim at all until you add one — **Security** → **API** → your authorization server → **Claims** → add a claim named e.g. `groups`, scoped to the ID token, with a group filter (e.g. `.*` to include everything, or a prefix to scope it down).

### Google Workspace

- **Issuer URL**: `https://accounts.google.com`
- Register under **Google Cloud Console** → **APIs & Services** → **Credentials** → **OAuth client ID**, application type **Web application**.
- **Group mapping isn't usable with Google directly**: standard OIDC ID tokens from Google don't carry group membership at all (it's exposed separately via the Admin SDK Directory API, and PerchTail only reads the ID token's own claims). With a Google provider, SSO accounts sign in with the no-access role (see "Group-claim to role mapping" above).

### Keycloak / Authentik

- **Issuer URL**: `https://{host}/realms/{realm}` (Keycloak) or `https://{host}/application/o/{app-slug}/` (Authentik).
- Both support a group claim out of the box, more directly than the SaaS IdPs above: Keycloak via a client scope with a **Group Membership** mapper (name it `groups`, token claim name `groups`); Authentik via an OAuth2/OIDC provider's **Property Mapping**, adding a scope that maps `request.user.ak_groups` (or similar) to a `groups` claim.

## Troubleshooting

- **The login page says "SSO sign-in failed"** (the address ends in `#/login?sso_error=1`) — the browser gets no detail on purpose: a detailed OIDC error is diagnostic information, not something to hand to an unauthenticated browser. The application log has the reason, at `WARNING`: `sso.callback.idp_error` (the IdP returned an error; its `error` and `error_description` are cut to 200 characters), `sso.callback.state_cookie_mismatch` or `sso.callback.invalid_state` (the sign-in took longer than 10 minutes, or the browser lost the state cookie), or `sso.callback.login_failed` with an `error` field (token exchange failed, invalid ID token, deactivated account, or a username already taken); and at `ERROR`, `sso.callback.issuer_mismatch` (see below) or `sso.callback.config_unreadable` (the saved settings can't be decrypted, see the last item). Each of these failures is also recorded in the audit log as `user.sso_login_failed` with its reason. Read the log with `docker compose logs perchtail` or in `LOG_DIR`. A super admin who can still sign in with a local account can also open the **PerchTail application logs** source (marked **system**) in the **Viewer**: it reads `LOG_DIR`.
- **The SSO button comes straight back to the login page with "SSO sign-in failed"**, without showing the IdP's page — PerchTail couldn't start the sign-in. When it couldn't fetch the discovery document, the log has `sso.login.discovery_failed`, at `ERROR`. Check the **Issuer URL** and that the PerchTail host can reach the IdP. With `sso.login.issuer_mismatch` instead, the discovery document names another issuer: copy the IdP's `issuer` value into **Issuer URL** exactly. With `sso.login.insecure_idp_url`, the issuer or an endpoint its discovery document names isn't `https://` (see "HTTPS" above).
- **The SSO button answers "Too many sign-in attempts. Try again later."** — more than 20 sign-ins were started from that address within a minute; wait a minute (see "Starting a sign-in" above).
- **`unexpected audience in ID token` in the log** — the **Client ID** in Settings → Single sign-on doesn't match the `aud` claim the IdP issued; check you copied the right client ID, not a related one (e.g. Okta's API Access Management client vs. the web app's).
- **A returning user's role keeps reverting after you change it manually** — expected: the group mappings set an SSO account's role at every sign-in, and with no match it is the no-access role (see above). Change the mappings or the user's IdP groups rather than the role in PerchTail.
- **"SSO sign-in failed", or a failed Test connection, with "resolves to a disallowed address" in the log** (`sso.login.discovery_failed`, or `sso.test.failed` with `UnsafeWebhookURLError` after **Test connection**) — the IdP's host, or a URL its discovery document names, resolves to a private, loopback or link-local address, which PerchTail refuses (see "Public address" above).
- **Settings → Single sign-on says "The saved client secret can't be decrypted with the current key; enter it again"** — `CREDENTIAL_ENCRYPTION_KEY` or the credential salt changed outside `python -m app.rotate_credential_key`. The issuer URL, client ID, scopes and group claim were saved with the secret, so enter them all again with the secret and click **Save**; with the group claim left empty, every SSO user gets **No Access**. Until then, every SSO sign-in fails (audited as `user.sso_login_failed` with the reason `secret_unreadable`).
