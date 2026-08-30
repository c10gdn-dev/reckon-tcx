# Setting up Google Cloud for Reckon

Reckon reads your activities from Google Health. Google will not let a program do
that until you have registered it — even a program you wrote yourself, for your
own data, running on your own laptop.

This guide walks through that registration. It takes about half an hour, most of
it waiting. You do not need to understand any of it, but a few steps go wrong
quietly rather than loudly, and those are flagged as they come up.

**You only do this once.**

---

## Before you start

You need two things:

- **A Google account that has your activity data.** This is whichever account you
  use with the Fitbit or Google Health app. If you have never opened Google
  Health, do that first and check your walks and runs are there.
- **A Google account to register the program under.** This can be the same one.
  Many people use a separate account for anything development-related, and if you
  do, that is fine — but read the warning in step 7, because mixing the two up is
  the single most common way this goes wrong.

Throughout, "the developer account" means the one you register under, and "the
data account" means the one with your activities in it.

---

## 1. Create a project

A *project* is just a folder Google keeps your settings in. It costs nothing and
you will never think about it again.

Go to **[console.cloud.google.com/apis/library/health.googleapis.com][enable]**.

If you have never used Google Cloud, it will ask you to agree to some terms and
create a project first. Accept, and give the project any name you like —
`reckon` is fine. You may be asked for a country and whether you want a free
trial; you do not need a billing account or a credit card for this.

When the page settles, press **Enable**.

[enable]: https://console.cloud.google.com/apis/library/health.googleapis.com

> **What you should see:** a page saying the Google Health API is enabled, with
> some graphs that will stay empty.

---

## 2. Say who is allowed to use it

In the left-hand menu, find **Google Auth Platform**, then **Audience**. If you
cannot find the menu, go straight to
**[console.cloud.google.com/auth/audience][audience]**.

Set **User type** to **External**.

This word is misleading. "External" does not mean you are publishing anything to
the world; it means "accounts that are not part of a company Google Workspace".
A personal `@gmail.com` account is External. It is almost certainly your only
option.

[audience]: https://console.cloud.google.com/auth/audience

---

## 3. Choose what the program may read

Go to **Data Access** in the same menu, or
**[console.cloud.google.com/auth/scopes][scopes]**.

Add these two, exactly:

```
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
https://www.googleapis.com/auth/googlehealth.location.readonly
```

The first lets Reckon see the list of your activities. The second lets it see the
route you actually walked or ran.

[scopes]: https://console.cloud.google.com/auth/scopes

> ### ⚠️ The first quiet failure
>
> If you forget the second one, **nothing will look broken**. Reckon will
> connect, list your activities, and download files — and every file will arrive
> with no route in it. There is no error message. If you get to the end and your
> activities have no GPS, come back and check this page.

Both are marked *Restricted*, which sounds alarming and simply means location and
health data are treated carefully. It does not stop you using them.

---

## 4. Fill in the branding page

Go to **Branding**, or **[console.cloud.google.com/auth/branding][branding]**.

This is the page describing your program to you, on the permission screen you are
about to see. Fill in:

| Field | What to put |
|---|---|
| **App name** | `Reckon` |
| **User support email** | your own address |
| **Developer contact email** | your own address |
| **App logo** | **leave empty** — see below |
| **Application home page** | a web page you control (step 5) |
| **Privacy policy link** | a page on the same site (step 5) |
| **Terms of service link** | optional |
| **Authorized domain** | the domain those pages are on |

[branding]: https://console.cloud.google.com/auth/branding

> ### ⚠️ Do not upload a logo
>
> A logo is optional, and uploading one commits you to a Google review process
> you do not want and do not need. Leave it blank.

---

## 5. You need a web address

This is the annoying part, and there is no way around it: to finish step 6, Google
requires a home page and a privacy policy at an address you can prove you own.

If you do not have a website, **GitHub Pages** gives you one free. If you have a
GitHub account called `yourname`:

1. Create a new **public** repository named exactly `yourname.github.io`.
2. Put an `index.html` and a `privacy.html` in it. This project's own pages are a
   working example you can copy — see
   [c10gdn-dev.github.io](https://github.com/c10gdn-dev/c10gdn-dev.github.io).
   The privacy policy must honestly describe what happens to your data; if you
   are running Reckon unmodified, the example one is already accurate.
3. Wait a minute, then check `https://yourname.github.io/` loads in a browser.

Then prove to Google that the site is yours:

1. Go to **[Google Search Console](https://search.google.com/search-console)**.
2. Add a property. Choose **URL prefix**, not Domain — Domain needs access to DNS
   settings you do not have for a GitHub address.
3. Enter `https://yourname.github.io/`.
4. Choose the **HTML tag** method. It gives you a line starting
   `<meta name="google-site-verification"`.
5. Paste that line into your `index.html`, just below `<head>`, and push the
   change. Wait a minute for the site to update.
6. Back in Search Console, press **Verify**.

Now return to the Branding page and fill in the home page, privacy policy and
authorized domain (`yourname.github.io`).

---

## 6. Publish

Back on the **Audience** page, find **Publishing status** and press
**Publish app**.

> ### ⚠️ The unhelpful error
>
> If you see:
>
> > *Your app's OAuth configuration is incomplete. You must enter the missing
> > information to proceed. Please visit the Branding page to finish configuring
> > your app.*
>
> …and the Branding page shows nothing obviously missing, you are hitting a known
> Google bug. The message will not tell you which field it means. It is almost
> always one of: **application home page**, **privacy policy link**, or
> **authorized domain**. Fill in all three, save, and try again.

**Why publish at all?** Because of how long your login lasts. An unpublished app
in "Testing" gives out permissions that **expire after seven days**, so you would
have to redo the whole permission dance every week. A published app's permission
lasts until you revoke it. Publishing costs nothing and is not a public listing.

You do **not** need to submit for verification. That is a separate, much longer
process, and its only benefits are removing a warning screen and raising a
100-user limit you will never approach.

---

## 7. Create the credentials

Go to **[console.cloud.google.com/apis/credentials][creds]**, press
**Create credentials**, and choose **OAuth client ID**.

- **Application type:** `Web application`
- **Authorized redirect URI:** add exactly `http://localhost:8721/callback`

That address does not need to work and nothing will be listening there. It is
where Google sends you back to afterwards, and Reckon reads what you need out of
the browser's address bar.

Press **Create**, then **Download JSON**. Keep that file — it is what you hand to
Reckon in the next step, and it is a password. Do not email it or commit it to a
repository.

[creds]: https://console.cloud.google.com/apis/credentials

---

## 8. Give Reckon permission

In a terminal, in the Reckon folder:

```console
$ python scripts/authorize.py google --credentials ~/Downloads/client_secret_....json
```

It prints a long `accounts.google.com` link. Open it in a browser.

> ### ⚠️ The second quiet failure
>
> **Choose the account your activity data is in**, not the one you registered the
> project under. If you have both signed in, they will both be offered and they
> may look almost identical.
>
> Picking the wrong one appears to work perfectly — you will grant permission,
> Reckon will store the result, and everything will look fine — right up until
> the first time it asks for data, when it fails with *"this Google account is
> not linked to Google Health"*. If you see that, run the command again and pick
> the other account.

You will then see **"Google hasn't verified this app"**. This is expected: it is
your app, and you have not asked Google to review it. Click **Advanced**, then
**Go to Reckon (unsafe)**.

Tick **both** permission boxes. Untick either and you are back to the problem in
step 3.

Finally, your browser will land on a page that fails to load, at
`localhost:8721`. **That is supposed to happen.** Copy the whole address out of
the address bar — it will be long and full of punctuation — and paste it back
into the terminal.

If it worked, you will see something like:

```
stored google in /Users/you/.config/reckon/store.json
access token expires in 60 min
```

---

## 9. Check it

```console
$ export RECKON_GOOGLE_CLIENT_ID=...      # from the JSON file
$ export RECKON_GOOGLE_CLIENT_SECRET=...
$ reckon sync --dry-run
```

`--dry-run` means it will fetch your activities and work out what it *would* do,
without uploading anything to Strava or recording anything.

You should see one line per activity, with a correction factor for the ones that
have GPS, and a reason for the ones that do not — a yoga session or a gym session
has no route to correct, so Reckon leaves it exactly as it is.

Once that looks right, set Strava up the same way and drop `--dry-run`.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| *"this Google account is not linked to Google Health"* | You authorised the wrong account. Redo step 8 and pick the other one. |
| *"OAuth configuration is incomplete"* when publishing | Home page, privacy policy or authorized domain is missing. See step 6. |
| *"the google authorisation is no longer valid"* | Your permission expired or was revoked. Redo step 8. If this happens after about a week, your app is not actually published — check step 6. |
| Activities download but have no GPS | The location permission is missing. See step 3, then redo step 8. |
| *"redirect_uri_mismatch"* in the browser | The address in step 7 does not exactly match. It must be `http://localhost:8721/callback`, with no trailing slash. |

---

## What Reckon can see, and what it keeps

It reads two things: the list of your activities, and their routes. It cannot
write to your Google account or change anything there.

It keeps one file, `~/.config/reckon/store.json`, readable only by you. That
holds your login tokens and a note of which activities it has already uploaded,
so it never uploads one twice. Delete that file and Reckon knows nothing.

To take permission away entirely, go to
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) and
remove Reckon.
