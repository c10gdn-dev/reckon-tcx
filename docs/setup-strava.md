# Setting up Strava for Reckon

Reckon uploads your corrected activities to Strava. Strava will not let a program
do that until you have registered it — even a program you wrote yourself,
uploading only your own activities.

This is **much easier than the Google side**. There is no project, no publishing,
no verification and no review. It takes about five minutes.

**You only do this once**, and unlike Google, the permission it gives you does
not expire.

---

## Before you start

You need a Strava account with an email address and password set on it. If you
only ever sign in to Strava with "Continue with Google" or "Continue with
Facebook", the developer settings page will not let you create an application —
it asks you to confirm your password and you do not have one.

To fix that, go to **Settings → My Account** and set a password. You can carry on
signing in however you like afterwards.

---

## 1. Open the API settings page

Go to **[strava.com/settings/api](https://www.strava.com/settings/api)**.

If you have never created an application before, this page *is* the creation
form. If you have, it shows the application you already made, and you should use
that rather than making a second one.

---

## 2. Fill in the form

Only three of these matter.

| Field | What to put | Why |
|---|---|---|
| **Application Name** | `Reckon` | Shown on the approval screen and in your Strava settings. Any name works. |
| **Category** | *Data Importer* | A label, **not a permission setting** — see below. Any value works; this is the honest one. |
| **Club** | leave blank | For club-specific apps. Not this. |
| **Website** | any URL you control | Required, never checked. Your GitHub profile is fine. |
| **Application Description** | anything, or blank | Never shown to you again. |
| **Authorization Callback Domain** | **`localhost`** | **This one matters — see below.** |

> ### ⚠️ The callback domain is the one field that can break everything
>
> Put in exactly the word **`localhost`**. Nine characters, nothing else.
>
> Not `http://localhost`, not `localhost:8721`, not a trailing slash. Strava
> wants a bare domain here, and it silently rejects the authorisation later — not
> now — if this is wrong. The error you would get says the redirect URI is
> invalid, and it appears after you have already approved the app, which makes it
> look like something else went wrong.

You may also be asked to upload an icon. It is optional and nothing uses it.

> **Category does not grant or restrict anything.** It is tempting to read *Data
> Importer* as a permission — it is not. Strava decides what an application may do
> from the scope requested at authorisation time, never from the category, and
> changing it would not change what Reckon can do. Nothing in this form affects
> permissions at all.

Click **Create**.

---

## 3. Write down your two numbers

The page now shows **My API Application**, with:

- **Client ID** — a five- or six-digit number. Not secret.
- **Client Secret** — a 40-character string, hidden behind a *Show* link. **This
  one is a password.** Anyone holding it can act as your application.

It also shows your rate limits, which will be 100 requests every 15 minutes and
1,000 per day. Reckon uses a handful per activity, so you will not approach them
unless you upload years of history at once — which is why `reckon catchup` has a
limit built into it.

---

## 4. Put the secret in a file, not on the command line

You can pass these to Reckon as command-line flags, and you should not. A secret
typed as a flag is stored in your shell history and is visible in `ps` output to
every other account on the machine, for as long as the command runs.

Instead, make a small file. Anywhere outside the repository is fine:

```console
$ mkdir -p ~/.config/reckon
$ nano ~/.config/reckon/strava-credentials.json
```

Put this in it, with your own two values:

```json
{
  "installed": {
    "client_id": "12345",
    "client_secret": "paste the 40-character secret here"
  }
}
```

Then lock it down so only you can read it:

```console
$ chmod 600 ~/.config/reckon/strava-credentials.json
```

> **Why `installed`?** It is the shape Google's own downloaded credentials file
> uses, and Reckon reads both with the same code rather than having two ways to
> do one thing. Strava does not offer a download, so you write the file yourself.

---

## 5. Authorise

```console
$ python scripts/authorize.py strava \
    --credentials ~/.config/reckon/strava-credentials.json
```

This prints a link. Open it, approve the app, and your browser will land on a
page that fails to load at `localhost:8721` — **that is expected**. Copy the
whole address out of the address bar and paste it back into the terminal.

The address contains a one-time code, which Reckon exchanges for a token and
saves to `~/.config/reckon/store.json`. That file is created `0600` because it
holds a credential.

**Strava's permission does not expire**, so this really is once. The access token
it hands out lasts about six hours and Reckon refreshes it silently; the refresh
token behind it lasts until you revoke it under **Settings → My Apps**.

---

## 6. Turn off the built-in connection

> ### ⚠️ Do this before your first upload
>
> If Google Health or Fitbit is already sending your activities to Strava
> automatically, you will get every activity **twice** — once uncorrected from
> them, once corrected from Reckon. Reckon can only avoid duplicating *its own*
> uploads; it has no way to recognise theirs.
>
> Disconnect it in Strava under **Settings → My Apps**.

---

## Where Strava does *not* tell you what you granted

Worth knowing before you go looking, because two pages seem like they should
answer this and neither does.

**The API settings page shows tokens that are not Reckon's.** Alongside your
Client ID and secret, `strava.com/settings/api` shows "Your Access Token" and
"Your Refresh Token", labelled **`scope: read`**. Those are convenience tokens
Strava mints for you as the *owner* of the application so you can try the API by
hand. Reckon never uses them. Its token comes from the authorisation you ran, is
a different token entirely, and lives in `store.json`.

**Nothing on that page will ever say `write`.** Strava applications do not
declare scopes at registration — the scope is requested in the authorisation URL
each time and recorded against *your grant*, not against the app. There is no
field for it to appear in. This is a real difference from Google, where scopes
are configured on the consent screen, and it makes a correct setup look broken.

**Settings → My Apps lists a name and a Revoke button**, and nothing about
permissions.

So the only moment the granted scopes are visible is the redirect URL you paste
back during authorisation — which is why `authorize.py` now prints them:

```console
$ python scripts/authorize.py strava --credentials ~/.config/reckon/strava-credentials.json
...
stored strava tokens in /Users/you/.config/reckon/store.json
access token expires in 360 min
granted scopes: activity:read_all, activity:write
```

**Read that line.** Strava answers a malformed scope request by granting a
*subset* rather than refusing, so asking for something and quietly not getting it
is a real outcome. If anything you asked for is missing, the script says so and
exits non-zero.

If you need to check an existing token rather than a fresh one, the only way is
to ask the API and read the error. A write-only token answers `200` for your
profile and `401 activity:read_permission missing` for your activities.

---

## Scopes: what Reckon asks for, and when that changes

Reckon asks for the narrowest permission that does the job.

| Scope | What it allows | Needed for |
|---|---|---|
| `activity:write` | uploading activities | `sync`, `local`, `catchup` |
| `activity:read_all` | reading your activity list, including private ones | `reconcile` |

Reckon requests **both**. `read_all` rather than plain `activity:read` because
the narrower one cannot see activities set to *Only You*, and an activity
`reconcile` cannot see is one `catchup` would upload a second copy of.

**If you authorised before September 2026 you have only `activity:write`**, and
`reckon reconcile` fails with `401 activity:read_permission missing`. That is not
a bug and does not mean the scope is unavailable — your grant simply predates it.
Re-run step 5 and read the `granted scopes:` line.

### Where a scope is added, and where it is not

**Not on strava.com.** There is no control anywhere in Strava's settings for
granting an application a scope. Looking for one is a dead end, and a reasonable
thing to go looking for.

A scope is added in three steps, and only the middle one is yours:

1. **In Reckon's source**, `SCOPES` in `src/reckon/clients/strava.py`. That tuple
   becomes the `scope` parameter of the authorisation URL. Until it changes,
   re-authorising grants exactly what you already have.
2. **By re-running step 5.** The browser opens Strava's approval screen, which
   lists the permissions being asked for. **That screen is where the grant
   happens** — it is the only place in Strava that shows or changes what an
   application may do.
3. **Automatically**, as the new token replaces the old one in the same store.

An existing token never gains permissions, so step 2 is required rather than
optional. Reckon sends `approval_prompt=force` precisely so Strava asks again
instead of silently handing back the grant you already gave — without it, widening
a scope looks like it worked and changes nothing.

Check the `granted scopes:` line the script prints afterwards. That is the
confirmation, and there is nowhere else to get it.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| The API settings page asks for a password you do not have | Your account signs in through Google or Facebook. Set a password under **Settings → My Account** first. |
| *"redirect_uri is invalid"* after approving | The Authorization Callback Domain is not exactly `localhost`. Fix it on the settings page and authorise again. |
| *"Authorization Error ... activity:read_permission missing"* | Your grant predates `activity:read_all`. Re-run step 5; the new token replaces the old one. |
| The browser page fails to load after approving | Expected. Reckon is not running a web server. Copy the address bar contents and paste them back. |
| An upload is rejected as a duplicate | Strava already has that activity, matched on the id Reckon sent. Reckon treats this as success — the activity is there, which is what mattered. |

---

## What Reckon can see, and what it keeps

Reckon can upload activities and read your activity list. It cannot post,
follow, give kudos or comment, and it reads nothing about anyone else. The list
is used for one thing: working out which of your activities Strava already has,
so it does not upload a second copy.

Your Strava tokens live in `~/.config/reckon/store.json` on your own machine,
alongside the Google ones, `0600` and re-chmodded every time it is opened.
Revoke them whenever you like under **Settings → My Apps**; nothing else is
needed to cut Reckon off completely.
