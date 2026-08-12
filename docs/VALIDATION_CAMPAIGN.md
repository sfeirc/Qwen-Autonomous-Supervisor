# Live E2E, chaos and endurance validation

Unit tests prove local invariants; they do not certify long-horizon autonomy.
Use only a disposable GitHub repository and a dedicated least-privilege bot.

## 1. Bootstrap the disposable product

```bash
python e2e/bootstrap.py ../autonomous-supervisor-e2e-test \
  --bot-login YOUR_BOT \
  --maintainer-login YOUR_MAINTAINER \
  --github-repository OWNER/DISPOSABLE_REPO
```

Configure branch protection manually: required `verify` check, pull request,
CODEOWNERS approval for governance paths, no bot bypass, merge commits only.
Create a supervisor config with the disposable checkout as `projectRoot` and a
fresh external runtime directory. Run `qas doctor` before spending model calls.
Confirm token/cost ceilings, disk threshold, artifact retention and provider
pricing in that config; do not use zero pricing when cost gates are required.

Then verify that the governance boundary is technically enforced:

```bash
qas --config e2e-supervisor.yml audit-governance
```

Do not start the live campaign unless this audit reports `safe: true`.

## 2. Prove one real cycle

```bash
set QAS_LIVE_E2E_CONFIG=/path/to/e2e-supervisor.yml
pytest -m live_e2e tests/test_live_e2e.py -s
```

Verify Issue → branch → tested commit → fresh reviewer session → PR → required
CI → merge → targeted dogfood. The ledger must contain distinct implementation
and reviewer session IDs.

## 3. Prove consecutive cycles

```bash
qas --config e2e-supervisor.yml campaign \
  --duration 4h \
  --minimum-successful-ticks 3
```

Reports are written under `runtime/campaigns/`. Confirm that dogfood creates a
`source:self-discovery` Issue and that a later tick delivers it without human
input.

## 4. Inject process crashes

Manual injection targets only the PID recorded as the active Qwen child:

```bash
qas --config e2e-supervisor.yml chaos-kill-active --reason "recovery drill"
```

Automated injection:

```bash
qas --config e2e-supervisor.yml campaign \
  --duration 12h \
  --chaos-every 45m \
  --minimum-successful-ticks 5
```

After each kill, verify the same Issue/branch/PR is reconciled and no duplicate
remote object exists. The operation journal specifically covers the dangerous
window where a remote mutation succeeded before SQLite recorded completion.

## 5. Reboot drill

During an active disposable run, reboot the machine through normal host
operations. The service/task must restart the supervisor. Do not inject any
prompt. Verify reconstruction from SQLite, Git, GitHub and checkpoints, then
compare the Issue, branch, HEAD, PR, checks, lease and pending operations with
the pre-reboot snapshot.

## 6. Progressive endurance ladder

Run and retain reports for: 15 minutes, 1 hour, 4 hours, 12 hours, 24 hours,
72 hours, 7 days and 16 days. Stop promotion when any gate fails.

The 72-hour readiness gate is:

- zero human interventions;
- at least 10 delivered Issues;
- multiple `source:self-discovery` Issues;
- injected crashes and one host reboot recovered;
- zero duplicate Issue, PR, comment, push or merge;
- zero lost lease and governance violation;
- every merge has local tests, CI and an independent reviewer;
- no quarantined Issue is selected again without explicit new evidence.
- no hourly/daily/Issue budget breach and no disk threshold breach;
- SQLite `quick_check` remains healthy and every stale live PID is reported as
  `run_hung` before termination.

Record tokens/Issue, estimated cost/merge, review rejection rate, mean time
between failures and mean recovery time from `/status`, `/metrics` and campaign
reports.
