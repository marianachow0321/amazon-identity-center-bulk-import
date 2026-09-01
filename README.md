# Amazon Identity Center bulk import

Import users from a CSV into an IAM Identity Center group. Each row becomes a
user in the identity store and a member of the group you name.

## Running it in AWS CloudShell

These instructions assume CloudShell, which is a good fit for a one-off import:
Python 3, boto3, git, and the AWS CLI are already installed, and credentials come
from your console sign-in, so there's nothing to configure and no long-lived
access key to create. Nothing here is CloudShell-specific though — the script is
plain Python and boto3, so it runs anywhere you have credentials.

## 1. Capture your instance details

Identity Center is regional. Open CloudShell from the AWS console (terminal icon
in the top bar), then read the instance details straight out of the API rather
than copying them by hand:

```bash
aws sso-admin list-instances \
  --query 'Instances[].{IdentityStoreId:IdentityStoreId,Region:PrimaryRegion,Status:Status}' \
  --output table
```

```
--------------------------------------------
|               ListInstances              |
+------------------+-------------+---------+
|  IdentityStoreId |   Region    | Status  |
+------------------+-------------+---------+
|  d-9067e5bbba    |  us-east-1  |  ACTIVE |
+------------------+-------------+---------+
```

If exactly one instance is listed, capture both values into variables and use
them for the rest of this guide:

```bash
IDS=$(aws sso-admin list-instances --query 'Instances[0].IdentityStoreId' --output text)
REGION=$(aws sso-admin list-instances --query 'Instances[0].PrimaryRegion' --output text)
echo "$IDS $REGION"
```

Check the count first if you're unsure — `Instances[0]` silently picks the wrong
one if there are several:

```bash
aws sso-admin list-instances --query 'length(Instances)' --output text
```

Note that `list-instances` only returns instances in the Region the CLI is
pointed at, so if it comes back empty, switch the CloudShell Region and retry.
`PrimaryRegion` is the Region the identity store actually lives in, and it's what
the script needs — not wherever your CloudShell tab happens to be.

## 2. Confirm the environment (optional)

Nothing to install, but if you want to check:

```bash
python3 --version
pip3 list | grep boto3
```

If boto3 is somehow missing, `pip3 install --user boto3` puts it in `$HOME` so it
survives the session.

## 3. Get the script into CloudShell

Clone the repo — this gives you `idc_import.py` and `users.example.csv`:

```bash
git clone https://github.com/marianachow0321/amazon-identity-center-bulk-import.git ~/idc-import
cd ~/idc-import && chmod +x idc_import.py
```

Git is preinstalled, so there's nothing to set up first. To pick up later
changes, `git -C ~/idc-import pull`.

If the repo is private, plain `git clone` won't work — CloudShell has no GitHub
credentials. Either clone over HTTPS with a personal access token as the
password, or skip git and upload `idc_import.py` with **Actions → Upload file**.

Your own CSV isn't in the repo, so add it separately. Either paste it:

```bash
cat > users.csv <<'EOF'
email,firstName,lastName
ada@example.com,Ada,Lovelace
grace@example.com,Grace,Hopper
EOF
```

Or upload it with **Actions → Upload file**, which lands the file in your home
directory — move it into place with `mv ~/users.csv ~/idc-import/`.

Keep everything under `$HOME`. Files written anywhere else are deleted when the
session ends. `$HOME` gives you 1 GB of persistent storage per Region.

## 4. Check the CSV

Makes no AWS calls, so it costs nothing to run first:

```bash
./idc_import.py --identity-store-id "$IDS" --region "$REGION" \
                --group my-group --csv users.csv --validate-only
```

Fix anything it reports. It lists every problem at once with line numbers.

## 5. Import

```bash
./idc_import.py --identity-store-id "$IDS" --region "$REGION" \
                --group my-group --csv users.csv
```

The script looks up every row first and shows you the plan before writing:

```
CSV OK: 2 user(s) in users.csv
target group: my-group (9418f4c8-f0f1-7062-c31e-b88635de7fc4)
plan: create 2 new user(s), 0 already exist; all 2 added to my-group
  new: ada@example.com (Ada Lovelace)
  new: grace@example.com (Grace Hopper)
proceed? [y/N]
```

Read the create count before answering. If the group is mapped to a paid role in
a downstream application, every new member is a subscription.

Rerunning the same CSV is safe — existing users are reused and existing
memberships are left alone — so if you're unsure, run it again rather than
guessing at what happened.

## Permissions

CloudShell uses the identity you signed into the console as. That principal needs:

```
identitystore:GetGroupId
identitystore:GetUserId
identitystore:CreateUser
identitystore:CreateGroupMembership
```

`--validate-only` needs none of them. `AccessDeniedException` on `GetUserId` in
the output means the first two are missing.

## CloudShell-specific gotchas

**Idle timeout.** Sessions end after 20–30 minutes of inactivity (10 in GovCloud
Regions). A large import prints output continuously so it won't idle out mid-run,
but if your browser tab dies, the run stops wherever it got to. Just rerun — it
picks up the remainder and skips what already exists. For very large files,
`tmux` is preinstalled and survives a dropped connection:

```bash
tmux new -s import
# ctrl-b d to detach, tmux attach -t import to come back
```

**Your CSV contains personal data.** Home directory storage is private to you,
but delete the file when you're done rather than leaving names and email
addresses sitting in persistent storage:

```bash
shred -u users.csv failed.csv 2>/dev/null || rm -f users.csv failed.csv
```

**Retrying failures.** Failed rows are written to `failed.csv` in the same format
as the input, so a retry is just:

```bash
./idc_import.py --identity-store-id "$IDS" --region "$REGION" \
                --group my-group --csv failed.csv
```

## Options

| Flag | Purpose |
|---|---|
| `--identity-store-id` | required, `d-…` from `list-instances` |
| `--region` | required, Region of the Identity Center instance |
| `--group` | required, group `displayName` |
| `--csv` | required, path to the input file |
| `--validate-only` | check the CSV and exit, no AWS calls |
| `--yes` / `-y` | skip the confirmation prompt |
| `--failures` | where to write retryable rows (default `failed.csv`) |
| `--sleep` | pause between users (default `0.2`); raise if throttled |
| `--profile` | AWS profile; not needed in CloudShell |

Exit codes: `0` clean, `1` some users failed, `2` CSV invalid, `3` group not
found, `4` confirmation declined.

## Running it unattended

The prompt refuses to auto-proceed when there's no terminal, so a piped or
scripted run needs `--yes`:

```bash
./idc_import.py --identity-store-id "$IDS" --region "$REGION" \
                --group my-group --csv users.csv --yes 2>&1 | tee import.log
```

## Before your first real run

The write path has no automated coverage, so pilot on a couple of rows before
pointing it at the full file:

```bash
head -3 users.csv > pilot.csv   # header + 2 users
./idc_import.py --identity-store-id "$IDS" --region "$REGION" \
                --group my-group --csv pilot.csv
```

Confirm those two look right in the Identity Center console, then run the full
CSV. Rerunning is safe, so the pilot rows are simply skipped the second time.
