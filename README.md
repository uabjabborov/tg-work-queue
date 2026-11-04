# Telegram Work Queue Bot

A Telegram bot that manages task queues for GitLab/GitHub merge requests in channels/groups with automatic cron-based reminders and weekly contributor and reviewer leaderboards.

## Commands

### Task Management

| Command | Description |
|---------|-------------|
| `!wadd <MR/PR URL> [...] [@username ...]` | Add one or more merge requests (optionally assign to one or more users) |
| `!w` | List all tasks in the queue |
| `!wd` | Privately show this chat's full queue with description previews; try to delete the command from the group |
| `!wgithub` | Manage this chat's GitHub App repository connections (chat admins only) |
| `!wme` | Privately show reviews assigned to you, and your PRs/MRs that were approved (ready to merge) or bounced |
| `!wmerged <PR/MR reference>` | Mark one of your approved PRs/MRs as merged, so it leaves `!wme` |
| `!wclear <PR/MR references>` / `!wclear all` | Clear your approved or bounced PRs/MRs from `!wme` (privately lists what was cleared) |
| `!wdone <PR/MR reference>` | Remove a completed review |
| `!wbounce <PR/MR reference>` | Remove a review with a "Changes required" comment in the chat |
| `!wassign <PR/MR reference> @username [...]` | Assign or reassign a review (replaces all existing assignees) |
| `!whelp` | Show help instructions |

### Reminders

| Command | Description |
|---------|-------------|
| `!wreminder-set <cron_expression>` | Set automatic reminder with cron schedule (UTC time) |
| `!wreminder` | Show current reminder configuration |
| `!wreminder-off` | Disable reminder (keeps configuration) |
| `!wreminder-remove` | Delete reminder configuration |

### Leaderboards

| Command | Description |
|---------|-------------|
| `!wleaderboard` | Show this week's contributor and reviewer standings |
| `!wleaderboard last` | Show the previous Monday–Sunday |
| `!wleaderboard-off` | Disable automatic weekly recaps; keep tracking activity |
| `!wleaderboard-on` | Enable recaps starting with the next scheduled delivery |

Each chat has separate rankings. Both show the top five people, counts, date range, and **Asia/Tashkent** timezone, ordered by count descending and then name alphabetically. On-demand standings include empty states.

| Action | Contributor credit | Reviewer credit |
|--------|--------------------|-----------------|
| `!wdone` | The person who submitted the queue entry | The command sender, unless also the submitter |
| `!wbounce` | None | The command sender, unless also the submitter |

Each person earns at most **one point per PR/MR per ranking per week**, including when a review is re-added or bounced multiple times. Different reviewers can each earn a point for the same PR/MR. Assignees do not receive credit automatically. Points belong to the completion week, regardless of when the review was submitted. PR/MR identities use the URL's host, full project path, and normalized number, ignoring scheme, query, fragment, and trailing slashes.

Automatic recaps run every **Monday at 09:00 Asia/Tashkent**, covering the previous Monday–Sunday. Existing chats are enrolled during migration; new chats are enrolled on their first successful submission. Any chat member can toggle recaps, and an opt-out persists across submissions and restarts. Recaps are skipped if both rankings are empty. After a restart, the bot attempts only the most recent overdue recap that was enabled when due; successful deliveries are saved to prevent duplicates. Re-enabling does not send an overdue recap immediately.

Telegram user IDs keep credit together when a username changes. Existing queue entries retain their saved submitter names; legacy names match known IDs only when unambiguous. Otherwise credit stays under the legacy name. Unknown or anonymous senders receive no individual entry. History starts at rollout, including later completions of currently queued reviews; previously deleted reviews cannot be reconstructed. Scoring reflects bot commands and identifies the queue submitter as the contributor.

## PR/MR References

PR/MR references are derived from the merge request or pull request URL:

| URL | PR/MR reference |
|-----|---------|
| `http://gitlab.example.com/group/monorepo/-/merge_requests/120` | `monorepo/120` |
| `https://github.com/owner/repo/pull/45` | `repo/45` |

Use `repo/N`, the full PR/MR URL, or a bare PR/MR number such as `120` with `!wdone`, `!wbounce`, `!wassign`, `!wmerged`, and `!wclear`. A bare number works only when one review in the current chat has that number. If several reviews have it, use a qualified reference such as `monorepo/120`. Queue sequence numbers and legacy `#N` commands are not accepted; use `!w` to see current references.

## Setup

### 1. Create a Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token provided

### 2. Configure Environment

Create a `.env` file with your bot token:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

Public GitHub PR titles and descriptions are fetched automatically without repository credentials
([GitHub pull request API](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)).
No GitHub or GitLab token is needed. The bot deliberately does not read `GITHUB_TOKEN`, `GH_TOKEN`,
`GITLAB_URL`, or `GITLAB_TOKEN` for metadata: a public bot must not share one team's repository
access with unrelated groups.

Private repositories on **github.com** work through per-chat GitHub App connections, configured
below. GitLab MRs still work in the queue, but their metadata integration is deferred to a separate
change. There is no global repository-token fallback.

Metadata requests go only to `https://api.github.com`, with TLS verification enabled and redirects
disabled. Public requests use GitHub's unauthenticated rate limits; connected repositories use App
installation access. GitHub device authorization uses `https://github.com`. No callback website,
webhook endpoint, or additional service is needed.

#### Optional: private repositories with a GitHub App

The bot operator registers one public GitHub App. Teams install it on their own selected repositories
and authorize those repositories for individual Telegram chats.

1. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).
   Set **Pull requests: Read-only** repository permission (Metadata read access is included).
   Request no write or organization permissions. Allow installation on **Any account**.
2. Enable **Device Flow**. Leave **Request user authorization (OAuth) during installation** disabled;
   the bot handles authorization through device codes instead. Webhooks can be disabled.
3. Generate a private key. Store it as `secrets/github-app.pem`, restrict its file permissions, and
   add these operator settings to `.env`:

   ```dotenv
   GITHUB_APP_ID=123456
   GITHUB_APP_PRIVATE_KEY_FILE=/app/secrets/github-app.pem
   ```

   Docker Compose mounts `./secrets` read-only. For a direct Python deployment, use the actual local
   PEM path. Keep the key out of source control and image build contexts; both are ignored here.
   Restart the bot after changing App configuration or rotating its signing key.

The App key identifies the service; it is not a team member's personal repository token. It is still
a privileged deployment secret and must be protected. Only validated chat/repository bindings are
used to request installation tokens; each token is restricted to one repository ID and read-only
pull-request permission. [GitHub App authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)

For each team, an administrator sends this in the target Telegram group:

```text
!wgithub connect owner/repo
```

The bot privately sends an installation link and a short-lived device code. Install the App on the
selected repository, enter the code at `https://github.com/login/device`, then send:

```text
!wgithub finish
```

The same person must be both a Telegram group administrator and a GitHub administrator of that
repository. Read access alone is insufficient. Finishing explicitly allows **all current and future
members of that chat** to see PR titles and descriptions from this repository. This is a team grant,
not individual GitHub permission checking for every Telegram reader. Connect only appropriate groups.
In a direct message, the connection belongs to that private conversation; it does not grant access
to group queues. Repeat the flow for each repository a chat needs.

`!wgithub` lists connections privately. `!wgithub cancel` cancels your pending device flow.
`!wgithub disconnect owner/repo` removes only that chat's binding and clears its metadata cache.
It does not remove the GitHub installation or previously sent Telegram messages. To revoke access
for all chats, remove the repository from the App installation, suspend it, or uninstall it on GitHub.
If a connect, disconnect, or cancellation succeeds but its private confirmation fails, the bot posts
the action's outcome without repository names. Use `!wgithub` to check the current connections.

Device flows are bound to the initiating Telegram user and chat, expire within 15 minutes, and are
lost on bot restart. If GitHub asks you to wait, finish authorization there and run `!wgithub finish`
again after a few seconds. Org approval or SAML sign-in may be needed before GitHub grants access.
[GitHub device authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app#using-the-device-flow-to-generate-a-user-access-token)

SQLite stores repository/installation IDs and who authorized the binding, never GitHub access or
refresh tokens. Device codes and access tokens remain in memory only. Metadata from connected
repositories is fetched afresh and is **not persisted**: failures or revoked GitHub access show just
the queue link instead of stale private details. Reminders also recheck access before showing titles.

### 3. Install Dependencies

Use Python 3.11 or later (the Docker image includes Python 3.11).

```bash
pip install -r requirements.txt
```

This includes:
- `python-telegram-bot` - Telegram Bot API wrapper
- `python-dotenv` - Environment variable management
- `APScheduler` - Cron-based reminder scheduling

### 4. Run the Bot

#### Option A: Run Directly

```bash
python bot.py
```

#### Option B: Run with Docker

```bash
# Build the image
docker-compose build

# Start the bot
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the bot
docker-compose down
```

### 5. Add Bot to Channel/Group

1. Add the bot to your Telegram channel or group
2. Make the bot an **admin** with "Post Messages" permission
3. Start using commands!

## Usage Examples

### Task Management

```
# Add a task without assignee
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120

# Response:
# monorepo/120 (clickable link)

# Add a task with single assignee
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice

# Response:
# monorepo/120 → @alice

# Add a task with multiple assignees
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice @bob @charlie

# Response:
# monorepo/120 → @alice, @bob, @charlie

# Add several tasks at once: @usernames assign the links before them on the same line
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/121 http://gitlab.example.com/group/backend/-/merge_requests/45 @alice
http://gitlab.example.com/group/backend/-/merge_requests/46 @bob
http://gitlab.example.com/group/backend/-/merge_requests/47

# Response:
# Added 4
# monorepo/121 → @alice
# backend/45 → @alice
# backend/46 → @bob
# backend/47

# List all tasks
!w

# Response:
# monorepo/120 → @alice, @bob (by @dave)
# backend/45 (by @dave)

# Show reviews assigned to you, and your own PRs/MRs to merge or fix (only you see the reply)
!wme

# Response (visible only to @alice):
# Assigned to you (1)
# monorepo/120 → @alice, @bob (by @dave)
#
# Approved, ready to merge (1)
# backend/48 — approved by @bob on Oct 28
# After merging, send !wmerged <PR/MR reference> in its chat.
#
# Your PRs/MRs that need changes (1)
# backend/52 — bounced by @bob on Oct 28
# Re-add one with !wadd once it's fixed.
# Approvals and bounces older than 14 days are hidden. Clear them sooner with !wclear <PR/MR references> or !wclear all.

# After merging an approved PR/MR, take it off your list
!wmerged backend/48

# Response:
# ✅ Marked backend/48 as merged.

# Clear several approved or bounced PRs/MRs from your !wme list at once
!wclear 52, 55, 60-100

# Private response (only you see it in a group):
# 🧹 Cleared 3 from your !wme list
# backend/52 — bounced by @bob on Oct 28
# ...

# Or clear everything approved or bounced
!wclear all

# Assign or reassign a task (replaces all existing assignees)
!wassign monorepo/120 @eve
# Or use the PR/MR URL:
!wassign http://gitlab.example.com/group/monorepo/-/merge_requests/120 @eve @frank

# Response:
# monorepo/120 → @eve, @frank

# Mark a review as done (by PR/MR number when unambiguous)
!wdone 120

# Response:
# Removed monorepo/120 (added by @dave)

# Or by qualified PR/MR reference:
!wdone backend/45

# Remove a review that needs changes (by PR/MR reference or URL)
!wbounce monorepo/120
# Or by URL:
!wbounce http://gitlab.example.com/group/monorepo/-/merge_requests/120

# Response:
# Removed monorepo/120 (added by @dave)
# Changes required.
```

### Reminders

```
# Set daily reminder at 9 AM UTC
!wreminder-set 0 9 * * *

# Response:
# ✅ Reminder set successfully!
# Schedule: 0 9 * * *
# Timezone: UTC

# Set weekday reminders at 9 AM and 5 PM UTC
!wreminder-set 0 9,17 * * 0-4

# Check current configuration
!wreminder

# Response:
# Reminder Configuration
# Status: ✅ Enabled
# Schedule: 0 9 * * *
# Timezone: UTC

# Disable reminder temporarily
!wreminder-off

# Delete reminder configuration
!wreminder-remove
```

## Supported URLs

- **GitLab**: `http://host/group/project/-/merge_requests/N`
- **GitHub**: `https://github.com/owner/repo/pull/N`

## Cron Expression Format

Reminders use 5-part cron expressions in UTC timezone:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── Day of week (0-6, 0=Monday, 1=Tuesday, ..., 6=Sunday)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

**Note:** Day of week uses 0=Monday (not Sunday like standard cron).

### Examples:

| Expression | Description |
|------------|-------------|
| `0 9 * * *` | Daily at 9:00 AM UTC |
| `0 9,17 * * *` | Daily at 9:00 AM and 5:00 PM UTC |
| `0 9 * * 0-4` | Weekdays (Mon-Fri) at 9:00 AM UTC |
| `0 9 * * 5,6` | Weekends (Sat, Sun) at 9:00 AM UTC |
| `0 */4 * * *` | Every 4 hours |
| `*/30 9-17 * * *` | Every 30 minutes between 9 AM-5 PM UTC |
| `0 0 * * 6` | Every Sunday at midnight UTC |

**Timezone Note:** Calculate your local time to UTC. For example, if you're in GMT+5 and want 9 AM local time, use `0 4 * * *` (4 AM UTC).

## Reminder Behavior

- Reminders are sent only when there are **pending tasks** in the queue
- Each channel/group can have its own independent reminder schedule
- Reminders persist across bot restarts
- You can temporarily disable reminders without losing the configuration

## Features

- **Multiple Assignees**: Assign tasks to multiple team members
- **Isolated Queues**: Each channel/group has its own independent task queue
- **Unique Tasks**: PR/MR references are unique per channel (the same MR can't be added twice)
- **Clickable Links**: Tasks are displayed as clickable links to the MR/PR
- **Private Assignment View**: `!wme` replies with an ephemeral message only the sender can see, including your PRs/MRs that are ready to merge or need changes
- **Custom Reminders**: Each channel can configure its own reminder schedule
- **Weekly Leaderboards**: Separate contributor and reviewer rankings and Monday recaps for each chat
- **Persistent Storage**: Data is stored in SQLite database (`workqueue.db`)
- **Flexible Assignment**: Reassign tasks at any time, replacing all existing assignees

## Adding Several PRs/MRs (`!wadd`)

Put several links in one `!wadd`, separated by spaces, commas, or new lines. `@username`s assign the links before them on the same line, back to the previous `@username`s, so each line can have its own reviewers and links with no `@username` after them stay unassigned. A line of only `@username`s assigns the line before it. If any link or username is invalid, nothing is added. Links already in the queue are skipped and listed in the reply.

## Private Queue (`!wd`)

Send `!wd` in a group to see the same pending reviews as `!w`, including titles, assignees,
submitters, and description previews. It includes all queued reviews in that chat, not just your
assignments. Only the requester receives the details. Long queues are split into private messages.
In a direct message, `!wd` shows that conversation's queue, just like `!w`.

The bot must be a group admin to send the private response. It first creates a neutral placeholder
and checks Telegram's private-message identifiers, then fills it using the
[ephemeral-only edit method](https://core.telegram.org/bots/api#editephemeralmessagetext).
If private replies are unsupported or delivery fails, it reports an error without posting queue
details publicly. No descriptions are sent as a public fallback.

After handling `!wd`, the bot tries to delete the command from the group. Give it **Delete Messages**
permission in supergroups ([Telegram deletion permissions](https://core.telegram.org/bots/api#deletemessage)).
If deletion fails, the private response still works and the command remains visible. The command
can be seen before deletion; deleting it cannot retract notifications or anything already seen.
Commands in direct messages are kept.

## Private Replies (`!wme`)

`!wme` lists the reviews in the current chat whose assignees include your `@username`. Matching ignores case. Long lists are split across several messages.

`!wadd` automatically fetches titles and descriptions from public GitHub PRs and this chat's connected
repositories. The add confirmation, `!w`, and
scheduled reminders show titles only. Request `!wd` for the full queue or `!wme` for your reviews to see description previews privately,
including approved and bounced items: in a group, the reply is addressed only to the command sender;
in a direct message, it stays in that private conversation. Failed private delivery produces only
an explanatory notice in the group, without description content.
Descriptions appear as escaped plain text (Markdown source), with previews capped at 1,200 UTF-16
units and titles at 240; follow the PR/MR link for the full content. Public details are cached in SQLite
for five minutes and refreshed on `!w`, `!wd`, `!wme`, or a new `!wadd`. Existing queue entries are enriched
on their next `!w`, `!wd`, or `!wme`. Reminders use saved public titles; when repositories are connected,
they refresh metadata with a new access check. Connected metadata is never persisted or reused after
an authorization failure. If GitHub is unavailable or rate-limited, the queue still works, using
cached public details or just the link for connected repositories.
Each metadata batch has an eight-second deadline with at most four requests at a time. Replies use
the details that arrived before the deadline; unfinished lookups can be retried on the next command.
Different chats process commands independently, while each chat keeps its command order. Up to
eight chats run at once and four commands can wait or run per chat; excess commands receive a
coalesced busy notice and must be resent. Oversized entries are split without dropping assignees
or breaking links and formatting.
Failed lookups are also cached for five minutes to avoid repeated requests. Metadata is stored by
chat and URL: one group's cached details are never read by another group. When upgrading from the
earlier shared-cache implementation, its metadata cache is discarded because its records have no
trustworthy team ownership. Queues, assignments, and review history are preserved.

It also lists the PRs/MRs you added that a reviewer finished, with who finished them and when (Asia/Tashkent date):

- **Approved, ready to merge:** a reviewer ran `!wdone`. The bot can't see when you merge, so after merging send `!wmerged <PR/MR reference>` in the same chat to take it off your list. `!wmerged` only works on your own approved PRs/MRs. Running `!wdone` on your own PR/MR doesn't count as an approval. Marking a PR/MR as merged doesn't change the leaderboard.
- **Need changes:** a reviewer ran `!wbounce`. It stays on your list until it's added to the queue again with `!wadd`, so re-add it once the changes are made.
- **Clearing:** `!wclear` takes approved or bounced PRs/MRs off your list without touching the queue or the leaderboard. List references separated by spaces or commas (`!wclear 55, 67, 88`), give a PR/MR number range (`!wclear 55-100` or `!wclear 55..100`), or use `!wclear all`. In a group it clears that chat's entries; in a direct message with the bot it clears entries from every group you're still in. If a cleared PR/MR is approved or bounced again, it comes back. Reviews assigned to you can't be cleared; finish them with `!wdone` or `!wbounce`. Like `!wme`, the list of what was cleared is a private reply.

Only each PR/MR's latest outcome counts: re-adding one takes it off the list, and bouncing an approved one moves it to "need changes". Approvals and bounces older than 14 days are hidden, since by then the PR/MR was probably merged, closed, or replaced. They're matched to you by your Telegram account, so they appear even without a username. Approvals from before `!wmerged` existed are treated as already merged.

- In groups the reply is a Telegram [ephemeral message](https://core.telegram.org/bots/api#ephemeral-messages-and-commands) (this implementation uses Bot API 10.3+ parameters): only you and the bot see it. Telegram may drop it if you are offline or after an app restart, so just send `!wme` again.
- The bot must be a group **admin** to send ephemeral messages. Otherwise it replies with a short notice instead of posting your list publicly. `!wd`, `!wme`, and GitHub onboarding send only a neutral placeholder until private delivery is verified, then use an ephemeral-only edit to add details. If an older server posts that placeholder publicly, the bot tries to delete it and sends no details.
- `!wclear` clears before it replies, so if the private reply fails, the public notice still says how many PRs/MRs were cleared (without naming them). Sending `!wclear` again won't show them.
- Your `!wme` or `!wclear` message itself stays visible to the group; only the reply is private. Usage and invalid-reference errors from `!wclear` are still public, since they don't list your PRs/MRs. When `!wmerged` matches several of your approved PRs/MRs, that list is private too.
- In a direct message with the bot, `!wme` lists your reviews from all groups you're still a member of, as a normal reply, under each group's name. If Telegram can't confirm your membership in a group right now (for example, a timeout), the reply says so and you can send `!wme` again.

## Notes

- **Task Assignment**: Use `!wassign` to change assignees - this replaces all existing assignees with the new ones
- **Task Removal**: When a task is removed, all its assignees are automatically cleaned up
- **Migration**: Existing single-assignee tasks are automatically migrated to support multiple assignees on first run
- **Completion History**: Review outcomes and identities are archived in the same transaction that removes a review and its assignees
