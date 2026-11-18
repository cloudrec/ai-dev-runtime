# ServerWarden (HostSecure): compact handoff for the tech lead, 2026-11-18

Read-only summary; nothing in `/opt/hostsecure` was changed. Trigger: an automated instruction was received.

## Git
- Repo `/opt/hostsecure`, branch `main`, HEAD `96f8d18`. **5 commits ahead of `origin/main` (8b592c7), not pushed.** Push is the owner's decision.
- Commits, oldest first:

| Commit | Change |
|---|---|
| `d0765a0` | nft boot persistence (`hs-firewall.service`) |
| `9716624` | auth: post-brute-force success escalated by causal ordering |
| `bb1ca25` | auth: implausible-year timestamps rejected |
| `239377a` | livefire: PermitRootLogin `without-password`/`prohibit-password` alias |
| `96f8d18` | test: VPN rule content unchanged by boot restore |

## Dirty files: preserve, never clean or stash
- Modified: `src/hostsecure/lab/drivers/lxd.py` (+20 −1). This is not part of the 5 commits, and its owner is unknown.
- Untracked:
  - `docs/LEAD_REVIEW_HANDOFF.md`
  - `docs/NFT_BOOT_PERSIST_REVIEW.patch`
  - `docs/AUTH_PARSER_SSHD_REVIEW.patch`
  - `hs_measure.py`, `hs_sentinel_agent.py`, `hs_watcher.py`
  - `pay-ready.html`
  - `screen/`
- MCP `project_git_status` reports `dirty_count 10`.

## Reports to read (MCP `agent_report`, project_dir `/opt/hostsecure`)
- `docs/LEAD_REVIEW_HANDOFF.md` is the review artifact for the 5 commits. Sections: git state, problem and fix, test results, the remaining deployment gate, rollback, provenance, and the UID-1001 CI claim.
- Patch `docs/NFT_BOOT_PERSIST_REVIEW.patch` covers commits 1 and 5.
- Patch `docs/AUTH_PARSER_SSHD_REVIEW.patch` covers commits 2–4.
- Also: `docs/LIVE_PROOF_STATUS.md`, `docs/DEMO_RUNBOOK.md`, `docs/HANDOFF_INDEX.md`, `docs/BACKLOG.md`, `STATUS.md`.
- Reading by `path` currently comes back empty in ChatGPT. See `reports/OWNER_OS_REPORT_TRANSPORT_2026-11-18.md`. Until that is fixed, ask the agent to paste the excerpts you need.

## Outstanding
- **Owner-gated deployment step** (handoff §"Remaining deployment gate"). Hosts installed before `d0765a0` have no `hs-firewall.service`, so after a reboot their scoped nft table is not re-applied. Their live protection is **unverified, not known absent**. Writing the unit, enabling it, or any firewall or reboot action is owner-only and has not been done.
- Review of the 5 commits by the tech lead, then a push decision by the owner.
- The `lxd.py` change: decide whose it is before any commit.

## Boundaries
- No push, deploy, firewall apply, reboot, SSH/VPN change or key/credential action without the owner.
- `pay-ready.html` and anything billing-related are money topics, so owner-only.
- Full Cycle: repo `hostsecure` is allowed (as of 2026-11-18). A project copy takes the committed HEAD only, so the dirty files above stay out of it.
- Wake route `hostsecure` still points at chat `6afbff8d…`. The rebind to the owner's new chat `6afd9cb3…` is pending the owner's own command (see `reports/OWNER_OS_HOSTSECURE_TOOLS_2026-11-18.md`).
