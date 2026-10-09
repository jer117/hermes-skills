# A separate GitHub account for your review agent

You can run `pr-review` as your own GitHub account. A separate machine
account is better:

- Reviews are visibly the agent's, not yours.
- Without write access, nothing it does counts toward your merge rules: not
  an approval, and not a change request. (The posting script has no approve
  option either. The agent is instructed not to approve any other way.)
- You decide exactly what its token can reach.

Below, `my-review-bot` is the machine account and `me` is your own account.

## What access to give it

**Public repositories: no access (recommended).** Don't add the account as
a collaborator. By default, anyone can review and comment on a public repo,
so the bot can still:

- leave reviews, inline comments, and PR comments.

It cannot:

- push;
- count toward required approvals: under branch protection and rulesets,
  only approvals from people with write access count;
- block a merge by requesting changes. GitHub documents that only reviewers
  with write access block, though its pages word this inconsistently, so
  treat it as observed behavior.

The trade-off: you cannot pick it from a PR's **Reviewers** list. GitHub
only lets you request reviews from collaborators. You start a review by
asking your agent instead.

Two repository settings can still get in the way:

- **Code review limits** (repo or org setting): only users with explicit
  access may approve or request changes. Comment reviews still work.
- **Interaction limits**: can stop new or outside users from commenting at
  all.

**Private repositories** need explicit access:

| Repo owner | Access | Token | Do its reviews count toward merge rules? |
|---|---|---|---|
| Your personal account | Collaborator (personal repos only offer write) | Classic, `repo` + `read:org` | Yes: its approvals count and its change requests block. The script cannot approve, but the token could, so this setup relies on the agent following its instructions. |
| An org, bot is a member | Read role | Fine-grained, resource owner = the org, only the repos to review, Contents: Read, Pull requests: Read and write | No |
| An org, bot is an outside collaborator | Read role | Classic, `repo` + `read:org` (fine-grained tokens can't be used here) | No |

Org owners can restrict or block personal access tokens. Org-owned
fine-grained tokens may need an owner's approval before they work.

## 1. Create the account

GitHub's Terms of Service allow one free machine account per person. A human
must create it and stays responsible for what it does, and it may only be
used for automation.

1. In a private browser window, sign up at <https://github.com/signup>.
   - Username: for example `me-review-bot`.
   - Email: one not already on a GitHub account. Plus-addressing
     (`me+bot@example.com`) usually works.
2. Turn on two-factor authentication: **Settings → Password and
   authentication**. Store the recovery codes in your password manager.
   GitHub may require 2FA for accounts that contribute code, and an org that
   requires 2FA removes members without it.
3. Make it obvious who runs it. **Settings → Public profile**, bio:
   `Automated PR reviewer operated by @me`.
4. Optional: in **Settings → Notifications**, turn off email for
   participating threads.

## 2. Create its token

Sign in as the bot. For **public repos owned by others**, use a **classic**
token. Fine-grained tokens can't write to public repos owned by someone else
unless the account is a member of the owning org.

1. **Settings → Developer settings → Personal access tokens → Tokens
   (classic) → Generate new token (classic)**
2. Note: `pr-review`. Expiration: 90 days.
3. Scopes: **`repo`** and **`read:org`** only.

`gh auth login` refuses classic tokens that lack those two scopes, so
`public_repo` alone does not work. A token never grants more than its
account can do. With no collaborator access, `repo` can only write to the
bot's own repositories. On everyone else's public repos it can review and
comment, and nothing more.

For private repos, use the token type from the table above.

## 3. Log the agent's `gh` in as the bot

On the machine where the agent runs, as the user it runs as:

```bash
gh auth login --hostname github.com --git-protocol https --with-token
```

Paste the token, press Enter, then Ctrl-D. Check it:

```bash
gh api user --jq .login
gh auth status
```

The first command must print the bot's login.

If your own account is also logged in there, log it out. Then the agent
cannot post as you, even by mistake:

```bash
gh auth logout --hostname github.com --user me
```

On a headless Linux machine, `gh` may store the token in
`~/.config/gh/hosts.yml`. Keep that file private:

```bash
chmod 600 ~/.config/gh/hosts.yml
```

Some agents, Hermes included, do not pass `GH_TOKEN` or `GITHUB_TOKEN`
through to the commands they run, so rely on `gh auth login`, not
environment tokens. The skill points its cached clones' git credential
helper at `gh auth git-credential`, so private-repo fetches use the same
login.

## 4. Tell the skill which account to expect

```bash
mkdir -p ~/.config/pr-review
cat > ~/.config/pr-review/config.json <<'EOF'
{"reviewer_login": "me-review-bot", "mention_login": "me"}
EOF
```

Then run the check from the installed skill directory (for Hermes,
`~/.hermes/skills/pr-review`):

```bash
python3 ~/.hermes/skills/pr-review/scripts/post_review.py --check
```

## Clearing an old "Changes requested"

The skill never approves, so the bot cannot clear its own earlier "Changes
requested". When a re-review says the blocking issues are fixed, someone
with write access can dismiss the old review:

1. On the PR's **Conversation** tab, find the merge box at the bottom.
2. Expand the review summary.
3. Open the **⋯** menu next to the bot's review and choose **Dismiss
   review**.

If branch rules restrict who may dismiss reviews, only the people on that
list can do it.

Without collaborator access, that review never blocked the merge. It only
shows in the PR's review list.

## Rotating or revoking

- Revoke the token any time from the bot's **Settings → Developer
  settings**.
- At expiry, generate a new token and repeat step 3.
