# Snapchat complete headline bot — 80 Creatives

This package contains the full single-account bot, workflow, helper scripts and
instructions. It supports 1–20 selected Ad Squads within one Ad Account. A squad
can belong to any Campaign in that account. Use a separate repository for another
Ad Account.

**Your own headlines are the default.** Put 500 or 800 lines in `headline_pool.txt`
and choose `headline_source: manual`. No OpenAI key or product-context Secret is
needed for manual mode. Each Creative gets a random unused suitable headline.
Read `PERSONAL_HEADLINES.txt` for the exact steps. When updating, keep your own
headline file; the ZIP contains sample lines, not your personal list.

**Replacing every old file? Read `REPLACE_ALL_FILES.txt`.** Upload the complete
package into the same repository and keep your existing Secrets. If you delete
`state.json.enc`, this version recovers its latest saved copy from the repository's
commit history. Keep that history and the original `STATE_ENCRYPTION_KEY`.
You do not need to upload the old state file or generate another refresh token.

## What the bot changes

The bot changes only a Creative's headline, up to 34 characters. An edit requires
all linked live Ads to be `REJECTED`, the Creative to be `DISAPPROVED`, and every
linked Ad to belong to your selected squads. Approved, pending and unknown review
statuses block the edit. Creatives shared with Ads outside the selection are skipped.

Before each edit, the bot refreshes the account's Ad list, then reads each linked
Ad and the Creative again. It sends conditional tests for the Creative's review
status and exact old headline in the same PATCH as the headline replacement.
Failed or incomplete responses stop processing. It does not remove these tests
or retry an unguarded edit.

Snapchat documents that a Creative edit affects all associated Ads. Its documented
PATCH conditions apply to one entity; the linked-Ad reads remain separate requests.
The guards cannot guarantee protection against every concurrent Ad status or link
change. See [Creative PATCH](https://developers.snap.com/marketing-api/Ads-API/creatives#patch-a-creative-patch)
and [conditional operations](https://developers.snap.com/marketing-api/Ads-API/api-patterns#supported-operations).

After an edit, the bot waits for review. A later confirmed rejection can become
eligible for a headline it has not used. A recorded approval permanently protects
that Creative while the saved state is retained.

## Headline sharing

The same suitable headline can be used on 10, 80, or more **different Creatives**.
There is no account-wide uniqueness requirement. The bot checks the current
headline and each Creative's usage within this workflow run. A repeat within
that run is blocked, including changes only to spacing,
punctuation, diacritics or invisible formatting. Another Creative's history does not block it.
Length and internal-name checks still apply.

The pool is considered independently for every eligible Creative. The bot filters
out that Creative's current wording and wording seen or reserved in this run, then chooses randomly
from the remaining suitable unique lines. It does not follow file order. Different
Creatives can still receive the same headline by chance. The same selection and
sharing rules apply to optional OpenAI suggestions.

For personal headlines, distinct wording from your list is allowed even if it
resembles another line. Optional OpenAI suggestions retain the extra near-duplicate
check against that Creative's usage in this run. An exhausted list is not recycled
during the same run. Starting another workflow run makes older lines available again.

Only headline eligibility resets. Approval records, pending review waits, uncertain
edits and target selections are retained. Historical headline data is kept as an
audit; it does not block using an older headline in a new run. The currently
displayed headline is still excluded. Keep the encrypted state and encryption key
so the review protections can continue.

## One workflow run

All 60-second checks in one overnight job share the same per-Creative used list.
The list does not reset between checks. A new manual workflow run, a scheduled
workflow run, or a re-run attempt starts fresh headline usage automatically.
Approved/pending protections remain active across all of them.

The worker identifies this boundary using `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT`;
the attempt increases when an existing run is re-run. See
[GitHub's run variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
Checkout requests the current branch so it loads the latest saved review state
instead of a historical event commit. See the
[checkout ref input](https://github.com/actions/checkout#usage).
For local monitoring, use one `HEADLINE_RUN_ID` value for all checks of a session;
without that optional local variable, each standalone invocation starts a new run.

## Saving before each edit

On GitHub Actions, `checkpoint_encrypted_state.py` encrypts, commits and pushes
each planned headline to the repository before the worker sends the edit. Its
history includes the previous and planned wording. The worker then refreshes all
status and link checks, so a status change during the save can still block the
edit. A failed checkpoint stops the monitor with exit code 90 before that edit.
This adds one encrypted commit per planned edit plus the final cycle save.

An interruption after an accepted edit can recover the reservation from GitHub;
the headline remains used for that workflow run even if a cycle never finishes.
Reservations are not recycled within that run if a later safety check skips an edit.
If an interrupted edit's outcome
cannot be confirmed, that Creative waits; the bot does not blindly retry it.
Local runs save state atomically on disk; remote checkpoints apply to GitHub Actions.
Keep the same repository, branch, encrypted state and encryption key. A new run
resets headline usage without clearing approval or review records.

## Limits and timing

| Setting | Behavior |
| --- | --- |
| `max_updates` | Up to 80 unique eligible Creatives across all selected squads per check |
| Manual form default | 80; enter 60, 1, or another value within 1–80 |
| Scheduled fallback | 30 per check |
| Per-Creative attempt limit | None; confirmed rejections can be retried while running |
| Overnight mode | Waits 60 seconds after each completed check, for roughly 330 minutes |
| Scheduled mode | Requests a run every five minutes when enabled; start times can be delayed |

The ceiling is not a guaranteed edit count. Eligibility and suitable-headline
availability determine the actual count. Status reads take time, particularly in
large accounts. Checks therefore do not start exactly every minute. OpenAI
generation requests contain at most 30 candidates each; 80 candidates use three
requests when all need generation.

## New bot setup

1. Extract this ZIP into a folder. Create a GitHub repository for this Ad Account.
2. Upload the files and folders inside the extracted folder. Confirm that
   `snap_headline_bot.py`, `state_crypto.py` and `requirements.txt` are at the
   repository root.
3. Confirm the workflow exists at
   `.github/workflows/snapchat-headline-editor.yml`. A YAML file at the repository
   root will not run. If browser upload did not include the folder, use **Add file
   → Create new file**, enter that full path, paste the supplied YAML and commit.
   See [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
4. Open **Settings → Secrets and variables → Actions → Secrets**. Add the values
   listed below. Credentials are entered here, not in the source files.

| Secret | Value |
| --- | --- |
| `SNAP_CLIENT_ID` | Your Snapchat OAuth Client ID |
| `SNAP_CLIENT_SECRET` | The matching OAuth Client Secret |
| `SNAP_REFRESH_TOKEN` | A token for that app and a user with access to the target account |
| `SNAP_AD_ACCOUNT_ID` | The one Ad Account UUID this bot will use |
| `SNAP_TARGET_1` | One Ad Squad UUID, or up to 20 separated by commas |
| `PRODUCT_CONTEXT` | Only needed for optional OpenAI modes: truthful product, market and offer facts |
| `OPENAI_API_KEY` | Only needed for optional OpenAI modes; omit for `manual` |
| `STATE_ENCRYPTION_KEY` | A newly generated key for this new bot |

`SNAP_TARGET_1` is an Ad Squad selection: the equivalent of an ad-set selection.
Example structure: `first-squad-uuid,second-squad-uuid`. It is not a Campaign ID.
Optional `SNAP_TARGET_2` through `SNAP_TARGET_5` store other squad selections in
the same account. One workflow run selects one slot, which may contain many squads.

If you already have working Snapchat credentials, use them. Run `get_snap_ids.ps1`
only if you need the target IDs. `CROSS_ORGANIZATION_SETUP.md` explains how your own
OAuth app can authenticate a user with access to a partner account.

5. For a new bot, open PowerShell inside the extracted folder and run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\generate_state_key.ps1
   ```

   The key is copied to your clipboard. Paste it directly into the
   `STATE_ENCRYPTION_KEY` Secret. Preserve this key once encrypted history exists.

6. Replace `headline_pool.txt` with your personal list, one headline per line.
   Repository Variables are optional for manual runs. The default source is
   `manual`; the Run workflow form supplies source, mode and target. If desired,
   configure these initial values under **Actions → Variables**:

   | Variable | Value |
   | --- | --- |
   | `HEADLINE_SOURCE` | `manual` |
   | `RUN_MODE` | `test` |
   | `BOT_ENABLED` | `false` |

7. In **Settings → Actions → General → Workflow permissions**, enable **Read and
   write permissions** so the workflow can commit its encrypted history.
8. Open **Actions → Snapchat Second Ad Account Headline Monitor → Run workflow**:

   ```text
   target_slot: target_1
   run_mode: test
   headline_source: manual
   max_updates: 80
   monitoring: one_check
   ```

   Confirm the intended squad count and `State encryption preflight passed.`
   If candidates exist, the log also reports the fresh-check protection and
   eligible `WOULD UPDATE` previews. Test mode does not send a Creative PATCH.

9. Run the same target in `live`, `max_updates=1`, `monitoring=one_check`.
   Verify the one changed Creative in Snapchat and that `state.json.enc` was
   saved. This exercises the conditional PATCH, which a preview cannot validate.
10. After that succeeds, use `live`, `max_updates=60` or `80`, and
    `monitoring=overnight`.

For scheduled continuation, set `RUN_MODE=live` and `BOT_ENABLED=true`. Scheduled
headline selection defaults to `manual`; `HEADLINE_SOURCE` can explicitly select
an optional AI mode. `OPENAI_MODEL` is used only by AI modes and defaults to
`gpt-5.4-nano`. Scheduled
runs continue the squad IDs saved by the last manual live start. A pending run
uses its selected slot only when it starts. Avoid queueing multiple manual runs.
Setting `BOT_ENABLED=false` prevents scheduled work from starting; it does not
stop an already-running overnight job. Let that job finish before updating files
or switching targets.

## History and existing data

The workflow verifies encryption before Snapchat access. It uses AES-256-GCM to
save `state.json.enc` after each cycle, including cycles that report an error.
The Python worker also saves a local reservation before each PATCH. An ambiguous
PATCH result keeps that reservation and blocks automatic re-editing until the
worker can confirm what happened. Do not delete history to bypass this guard.

The ZIP has no account-specific state or credentials. If `state.json.enc` is
missing, `restore_encrypted_state.py` searches the current branch's full Git
history and restores the latest saved encrypted file. It restores no old code.
The workflow decrypts the recovered state before Snapchat access. An unreadable
or undecryptable saved file stops the workflow; it never silently uses older state.

This supports deleting all files and uploading the new package in the **same
repository**, while keeping only your existing Secrets. The original encryption
key and Git history are needed for recovery. If no state was ever committed or
the history was erased, the bot reports that it is starting new history. Secrets
cannot reconstruct unsaved approval records or past attempts. Current approved
and pending status checks still apply.

Replacing all files installs the supplied default headline pool and archived
hash list. Recovered state preserves approval records and pending review waits.
Headline usage resets for each new workflow run. A bot for another account should use a separate
repository and encryption key.

## Token refresh errors

An HTTP 400 during token refresh happens before any Snapchat Ad read or edit.
It does not identify the exact cause by itself. This revision reports only a
recognized `oauth_error` code plus fixed guidance, with all response text hidden.
See `TOKEN_REFRESH_HELP.txt` for the credential checks and token-helper steps.
Keep `STATE_ENCRYPTION_KEY`: it is unrelated to Snapchat authentication.

## OpenAI HTTP 429

Manual mode makes no OpenAI calls. This section applies only when you choose
`manual_then_openai` or `openai` and supply the optional OpenAI Secrets.

This revision shows a recognized `openai_error` and `openai_type` while keeping
API response text private. See `OPENAI_429_HELP.txt` for the next steps.

Temporary rate limits and server errors receive at most three attempts per
request within a 180-second retry window. Valid `Retry-After` values are honored;
other retries wait about 15 then 30 seconds, with a small random delay. A server
wait beyond the window stops the run. Quota, billing, authentication and unknown
429 errors are not automatically retried. The workflow saves state and stops
the current monitor on exit code 78, without restarting the worker five times.
See [OpenAI retry guidance](https://developers.openai.com/api/docs/guides/rate-limits).

Scheduled jobs can still start later. Set `BOT_ENABLED=false` while resolving an
account problem. The same 80-Creative ceiling and approved/pending protections
apply. Every accepted headline still passes the existing fresh status checks
after any OpenAI wait. Repeat checks use that Creative's usage in the current run;
internal-name checks also remain active. A request failure aborts generation
before this cycle's headline edits.

## Files

| File | Purpose |
| --- | --- |
| `snap_headline_bot.py` | Complete worker, status guards, generation and history |
| `.github/workflows/snapchat-headline-editor.yml` | Manual runs, scheduling, encryption and persistence |
| `state_crypto.py` | Encrypt/decrypt state |
| `checkpoint_encrypted_state.py` | Push each encrypted headline reservation before a live edit |
| `restore_encrypted_state.py` | Recover deleted encrypted state from repository history |
| `requirements.txt` | Python dependencies installed by GitHub Actions |
| `headline_pool.txt` | Replace the 100 sample lines with your own list, including 500 or 800 headlines |
| `blocked_headline_hashes.txt` | Archived global hashes; no longer applied to headline selection |
| `get_snap_ids.ps1` | Select displayed squads using an existing refresh token |
| `get_snap_token.ps1` | Optional OAuth authorization and token helper |
| `generate_state_key.ps1` | Generate and copy a new state encryption key |
| `START_HERE.txt` | Choose the existing-bot or new-bot instructions |
| `REPLACE_ALL_FILES.txt` | Replace every old file in the same repository |
| `TOKEN_REFRESH_HELP.txt` | Diagnose OAuth errors without exposing credentials |
| `OPENAI_429_HELP.txt` | Diagnose OpenAI limits and install this recovery update |
| `PERSONAL_HEADLINES.txt` | Use your own large headline list without an OpenAI key |
| `CROSS_ORGANIZATION_SETUP.md` | Authentication for a partner account |
| `VERSION.txt` | Release and validation notes |

The ID helpers list up to 1,000 Campaigns and up to 1,000 squads per Campaign;
they do not paginate those setup lists. If your intended squad is missing, obtain
its exact ID from Ads Manager. The Python worker does paginate its account reads
and independently verifies the selected target scope.

No headline guarantees approval. Product, video, landing-page or account-policy
issues may require corrections beyond a headline. Offline tests cover random
selection, repeat prevention between checks in one run, resets between workflow
runs, interrupted runners using local Git
remotes, status guards, state recovery and optional OpenAI recovery.
The full package was checked for source
completeness, syntax and encryption compatibility; live account authorization and
Windows helper execution must be verified in your environment.
