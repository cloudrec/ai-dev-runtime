# Delivery attribution — a red gate caused by a stale test, not a regression (2026-08-29)

## What was red

`tests/test_delivery_attribution.py::test_agent_send_threads_attribution_to_the_record`
failed on branch `ai-runtime/220-windows-bridge` at `b30ebf8`:

```
assert captured["kw"] == {"actor": "api:hmac/x", "source": "1.2.3.4:5"}
E   Left contains 4 more items:
E   {'action': 'agent_send', 'idempotency_key': 'key-1',
E    'target': 'proj:0.0', 'text': 'hello'}
```

It was one of the two pre-existing failures in the 2542-test suite.

## Root cause: the test, not the code

The assertion compared the captured `**kwargs` dict for **exact equality**. That
pinned *how* `agent_send` calls `_deliver` (which arguments are positional vs
keyword), when the behaviour the test is named for is *what* it threads through.

Commit `2356691` ("a queued message is not a delivered one") added the busy-refusal
branch to `agent_send` and, in the same change, switched its call from

```python
return _deliver(target, text, "agent_send", idempotency_key,
                actor=actor, source=source)
```

to an all-keyword call. `actor`/`source` are still passed correctly — the two
attribution keys were present and correct in the failure output. The four extra
keys simply moved from `args` into `kwargs` and broke the equality.

**This was never a production defect.** `core/agent_control.py` is untouched by
this fix. `agent_answer`, which still calls positionally, was unaffected — which is
why only half the test failed.

## The fix

Bind the recorded call against the real `_deliver` signature and assert by
**parameter name**, so the test is indifferent to call style:

```python
bound = inspect.signature(real_deliver).bind(*captured["args"], **captured["kw"])
bound.apply_defaults()
```

The rewritten test also asserts the payload (`target`, `text`, `action`,
`idempotency_key`) arrives intact next to the attribution, so it is strictly
stronger than the assertion it replaces rather than merely looser.

## Verification

- Mutation check: changing the `agent_send` call site to pass `actor=None` makes
  the rewritten test fail (`At index 0 diff: None != 'api:hmac/x'`), proving it
  still catches a real attribution regression. Production file restored to a
  clean diff afterwards.
- `tests/test_delivery_attribution.py` — 17 passed.
- Delivery/actuator regression gate — **231 passed**:
  `test_delivery_attribution`, `test_agent_control`, `test_actuator_blind_pane_guard`,
  `test_control_plane_actuator`, `test_control_plane_delivery`,
  `test_queued_input_delivery_failure`, `test_wake_delivery_verification`.

## Scope / state

- Branch `fix/delivery-attribution-test`, worktree
  `.claude/worktrees/delivery-attribution`, baseline `b30ebf8`, commit `6277609`.
- Test-only change. No production change, no push, no deploy, no restart, no
  credential or owner-gated action.
- Rollback: `git worktree remove .claude/worktrees/delivery-attribution --force`
  and `git branch -D fix/delivery-attribution-test`.
- The main tree's 29 dirty/untracked entries and the pre-existing
  `stall-doctor-repeat-guard` worktree were left untouched.
