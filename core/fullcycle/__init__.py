"""Full-cycle autonomous development: spec → task → executor → checks → GPT review → next.

Built on the pieces that already work: `core.task_contract` is the task's fail-closed
state machine, `core.control_plane.api` carries the events (the wake bridge turns them
into ChatGPT turns), the leases and the owner gates, `core.agent_creation_policy` /
`core.agent_control` hold the payment hard-deny and the send policy.

  spec.py          versioned project specification + acceptance criteria
  executor.py      technology-agnostic executor contract (envelope / observation)
  hosts.py         where an executor runs: local, SSH; Windows bridge is a stated TODO
  tmux_adapter.py  tmux-hosted agent (Claude today), and the live agent_control adapter
  gates.py         permission gates checked before dispatch and on the returned work
  engine.py        the persisted state machine, review inbox, retries, crash recovery
"""
