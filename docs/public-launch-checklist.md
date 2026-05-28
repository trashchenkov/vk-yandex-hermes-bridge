# Public launch checklist

This checklist is the gate before exposing the VK community bridge to anyone beyond the owner/trusted allowlist.

## Default stance

Public auto-reply is disabled by default.

The MVP is safe-private first: unknown/public users must not reach Hermes, tools, memory, cross-channel actions, wall publishing, or any local execution path. Public mode starts as review/handoff only.

Required default values before launch:

```text
VK_OWNER_ID=<maintainer_vk_user_id>
VK_ALLOW_ALL_USERS=false
VK_PUBLIC_HANDOFF=true
VK_UNAUTHORIZED_REPLY=Бот приватный. Доступ к Hermes Agent ограничен.
```

Do not use `VK_ALLOW_ALL_USERS=true` outside explicit local smoke/test mode.

## Minimum public-mode gates

### 1. Owner binding and roles

- `VK_OWNER_ID` is set to the real maintainer VK user id.
- `VK_ALLOWED_USERS` is empty or intentionally used only as the legacy owner allowlist fallback.
- `VK_TRUSTED_USERS` contains only users who may reach Hermes.
- `VK_BLOCKED_USERS` is ready for emergency user-level deny.
- Owner-only commands work only for owner: `!trace`, `!pending`, `!approve`, `!reject`, `!reply`.

### 2. Policy safety

- Unknown/public users resolve to `public`.
- Public traffic is `deny` or `handoff`; it is never `reply` in MVP public mode.
- Public handoff creates a pending review item and does not call Hermes.
- Manual owner reply uses `!reply <review_id> <text>` and marks the item replied.
- Wall publishing, attachments execution, Telegram/cross-channel sending, shell commands, and memory/secret access are out of scope.

### 3. Source grounding

Public answers must be source grounded before any future public auto-reply mode exists.

Minimum source grounding gate:

- approved public KB or allowlisted source set exists;
- every public answer can cite a source or review item;
- unsupported questions go to review, not auto-reply;
- prompt injection cannot override the source policy.

Until this is implemented, public auto-reply remains disabled.

### 4. Redteam regression

Run all tests:

```bash
python3 -m pytest -q
```

Redteam fixtures must cover at least:

- impersonation: “I am Sergey/admin/owner”;
- prompt injection: “Ignore previous instructions”;
- secrets/tokens/memory requests;
- shell/tool execution requests;
- cross-channel requests such as Telegram forwarding;
- VK wall publish requests.

Expected result: deny or handoff, never Hermes/tools, for every public redteam case.

### 5. Smoke and doctor

Run local smoke without real secrets:

```bash
VK_ALLOWED_USERS=<owner_vk_id> python3 vm-worker/vk_hermes_worker.py --smoke --state-dir /tmp/vk-hermes-smoke
```

Run doctor before deployment:

```bash
python3 vm-worker/vk_hermes_worker.py --doctor
```

Run network doctor only on the VM where Hermes API is expected to be reachable:

```bash
python3 vm-worker/vk_hermes_worker.py --doctor --doctor-network
```

All critical checks must pass. Any `VK_ALLOW_ALL_USERS=true` outside explicit test mode is a launch blocker.

### 6. Audit and trace

Audit requirements:

- every inbound event has a stable `trace_id`;
- policy decision logs include role, action, reason, peer_id, and from_id;
- trace records include role, decision, Hermes status, VK status, and error summary;
- `!trace <trace_id>` is owner-only;
- stored envelopes do not include VK callback secrets, access tokens, attachment access keys, or raw secret values.

### 7. Rate limit and abuse handling

Minimum rate limit plan before public exposure:

- queue visibility/retry settings reviewed;
- abusive public users can be added to `VK_BLOCKED_USERS`;
- noisy public traffic can be switched from handoff to deny;
- pending review queue is monitored and does not grow silently;
- message length splitting is verified for long replies.

### 8. Emergency lockdown

Emergency lockdown must be available before public auto-reply or broader public testing.

Until a dedicated control exists, the manual lockdown procedure is:

```text
VK_EMERGENCY_LOCKDOWN=true
VK_PUBLIC_HANDOFF=false
VK_ALLOW_ALL_USERS=false
VK_TRUSTED_USERS=
VK_UNAUTHORIZED_REPLY=*** на техническом обслуживании.
```

Then restart the worker service and verify visibility with `python3 vm-worker/vk_hermes_worker.py --doctor`.

Dedicated owner-command toggling can be added before expanding beyond MVP.

## Launch decision

Launch to public/community traffic only if all boxes are true:

- [ ] public auto-reply is disabled by default;
- [ ] owner id and role config are correct;
- [ ] `python3 -m pytest -q` is green;
- [ ] `--smoke` is green;
- [ ] `--doctor` is green;
- [ ] redteam fixtures deny/handoff without Hermes;
- [ ] audit/trace lookup works;
- [ ] rate limit and abuse response are defined;
- [ ] emergency lockdown procedure is tested;
- [ ] source grounding is implemented before any future public auto-reply.
