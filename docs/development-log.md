# Development log

A record of what was built, what was verified against a real org, and what bit us
along the way. Weighted toward findings rather than a changelog — the code shows
*what* it does, this explains *why* it does it that way.

Companion to the [README](../README.md), which describes the target architecture.

---

## Status

| Layer | State |
|---|---|
| Salesforce org: metadata, permissions, demo data | ✅ deployed and verified |
| `soql.py` — escaping and ID validation | ✅ 56 tests |
| `models.py` — domain model | ✅ frozen, `extra="forbid"` |
| `config.py` — settings and guards | ✅ per-subsystem validation |
| `salesforce_auth.py` — sf CLI + JWT bearer | ✅ both providers |
| `salesforce_client.py` — REST transport | ✅ pagination, re-auth, error mapping |
| `salesforce_mapping.py` — SObject ↔ domain | ✅ config-driven field names |
| `salesforce_provider.py` — `CrmProvider` impl | ✅ validated against live org |
| Live integration suite | ✅ 16 tests, `pytest -m liveorg` |
| `fake_provider.py` + recordings | ✅ sanitized live snapshot, stateful writes |
| Tool registry and handlers | ✅ 16 tools, derived idempotency keys |
| Tool API + OpenAPI spec | ✅ routes generated from the registry |
| Foundry project + agent | ✅ provisioned, calling tools over Voice Live |
| Tool API on Container Apps | ✅ deployed, key-authenticated, **live Salesforce org** |
| Voice CLI | ✅ audio in/out, barge-in, interim responses |
| Connected App + JWT | ✅ deployed as metadata, key in Key Vault |

```
pytest              256 passed,  16 deselected   (~4s, no credentials)
pytest -m liveorg    16 passed, 256 deselected   (~71s, real org)
```

---

## Verified against a live org

Each of these began as an assumption in the plan. All were proven, and several were
wrong in ways that would have cost far more to discover later.

### Task and Event custom fields live on `Activity`

Deploying `Idempotency_Key__c` to `Task` or `Event` fails:

```
Entity Enumeration Or ID: bad value for restricted picklist field: Task
```

They share the `Activity` object. Define the field once there and it surfaces on both.
Field-level *security*, however, is still granted per-object as
`Task.Idempotency_Key__c` and `Event.Idempotency_Key__c` — **defined once, permissioned
twice**.

### Field-Level Security failure is silent, and looks like a bug elsewhere

Immediately after a successful deploy, `sf sobject describe --sobject Opportunity`
listed **none** of the new fields. Not an error — an absence. A field without FLS is
omitted from API responses and dropped on write, so a permissions gap presents exactly
like a field-mapping bug.

Assigning the permission set and rerunning the identical command made all of them
appear. Nothing about the fields changed; only who was allowed to see them.

One field *was* visible before the grant: the ledger's `Idempotency_Key__c`, because
`required` fields have implicit FLS. That is also why it must be omitted from
`fieldPermissions` — Salesforce rejects explicit FLS on required fields.

### Upsert reports `created` in the response body

The plan specified inspecting HTTP `201` vs `204`. The API is simpler than that:

```jsonc
PATCH /sobjects/Task/Idempotency_Key__c/{key}
{ "id": "00T…", "success": true, "created": true }   // then false on replay
```

Proven: identical PATCH twice → same record id, `created` flips, `COUNT(Id) = 1`.
Reading the body is more robust than status codes across API versions.

### `/chatter/users` is not a mention-capability filter

It is the purpose-built endpoint and it looks correct. It also returns
**Identity-licence users**, who resolve by name and can never receive a notification —
precisely the silent failure the design exists to prevent.

`Profile.UserLicense.Name` is the signal that holds. `resolve_user` uses an **allowlist**
because the failure directions are asymmetric: excluding a valid user surfaces instantly
as "can't find them", while including an un-notifiable one has no symptom at all.

### Chatter mentions survive as structured segments

Posting `body.messageSegments = [Text, {type: Mention, id: 005…}, Text]` and reading the
feed element back returns the `Mention` segment intact, carrying the user id — not
downgraded to text. The read-back assertion is the regression test; the same content sent
as a plain string would return three `Text` segments and notify nobody, without erroring.

### `CreatedDate` is insert-only and off by default

Without **Setup → User Interface → "Set Audit Fields upon Record Creation"** plus the
`CreateAuditFields` permission, every seeded record is created *today* and "oldest entry
date" becomes meaningless.

This must be decided **before** seeding: `CreatedDate` cannot be set on update, so
enabling it afterwards means delete-and-recreate. With it enabled, the seeded pipeline
reports an oldest entry of March 2025 — which is what makes the demo's *"oldest entered
March last year"* a real answer rather than a scripted one.

### Stage picklist values are not what people say

The stock org ships `Proposal/Price Quote` and `Negotiation/Review`. A rep says
"proposal" and "negotiation". Writing the spoken string fails. `resolve_stage` matches in
tiers — exact, normalised-exact, prefix, substring — and returns the *narrowest* tier that
matches, so `closed` correctly reports two candidates rather than guessing.

### Other org-specific traps

| Symptom | Cause |
|---|---|
| `FIELD_INTEGRITY_EXCEPTION` on Account create | State/Country picklists enabled; a state requires a country. Address fields dropped entirely — they add nothing and hurt portability. |
| `data value too large` on permission set | `<description>` caps at 255 characters. |
| `only aggregate expressions use field aliasing` | SOQL allows `AS` aliases only on aggregates. |

---

## Verified against Voice Live

Both of these were wrong in the plan, and neither surfaces until a real connection is open.

### A Prompt Agent cannot run on a realtime model

The architecture said the agent's model was `gpt-realtime`. Connecting in agent mode fails:

```
Foundry agent service response error: This model is not supported by Responses API.
```

Voice Live in **agent mode** supplies speech itself and delegates reasoning to the Foundry
agent service, which runs the agent through the **Responses API** — and that API rejects
realtime models. They are separate roles: Voice Live owns the audio, the agent needs an
ordinary text deployment. `gpt-4.1-mini` now backs the agent.

The failure is quiet, which is what makes it expensive. `response.done` arrives with no
content, zero input tokens and zero output tokens; the reason appears only in
`status_details`. Read as a transcript it looks like the agent simply had nothing to say.

### Instructions are read-only in agent mode

Sending them on `session.update`, exactly as model mode expects, is rejected:

```
Instructions are read-only and cannot be modified in agent mode
```

The agent definition owns them. `build_session` therefore omits `instructions` by default,
and the session config stored in agent metadata carries none — a client replaying that
config verbatim would otherwise fail on connect.

### The tool credential lives in a project connection

`azure-ai-projects` can read connections but not create them, so the custom-keys connection
is created against ARM directly. The key belongs there rather than in the agent definition
for the same reason it is a security scheme rather than a header parameter in the spec: a
credential the agent carries is a credential that can leak into a tool schema. Rotating it
now means updating one connection, not cutting a new agent version.

### Scene 1 answers correctly end to end

```
> How many open opportunities does Demo Building Supply have?
  Demo Building Supply has fourteen open opportunities.        <- aggregate, 13 words

> Read me the first past due one.
  Northgate Commons Phase 2. Amount is forty-two thousand,
  stage Bidding, close date April thirtieth... Next?           <- one item, then waits
```

The count comes from `get_pipeline_summary`, not from the model counting records, and the
list is read one item at a time on a cue — both design rules holding under a real call.

### Cancelling with nothing in flight is an error

The barge-in rule reads "on speech, cancel the response". Implemented literally it produces
a steady stream of:

```
Cancellation failed: no active response found.
```

Speech detection fires on any sound, and most of the time the agent is not talking — in a
moving car, most of the time nothing is talking. Dropping queued playback is always right;
sending `response.cancel` is only right while a response is actually open, so the handler
tracks `response.created` / `response.done` and cancels only in between.

The first run also showed the design working: a mis-heard remark produced `agent: One sec…`
from the interim-response config before the real answer arrived — the silence-filling
behaviour that keeps a tool call from sounding like a dropped call.

---

## Environment findings

These are workstation-level and cost real time.

### `npm i -g @salesforce/cli` is blocked behind a registry proxy

The package bundles a pinned `npm` release that the proxy does not mirror:

```
npm error 404  GET https://<proxy>/npm/-/npm-11.19.0.tgz
```

The Homebrew cask is deprecated for failing the macOS Gatekeeper check and is disabled
from 2026-09-01. The **standalone tarball** avoids both — it ships its own Node runtime
and contacts no registry:

```bash
curl -fsSL -o /tmp/sf.tar.xz \
  https://developer.salesforce.com/media/salesforce-cli/sf/channels/stable/sf-darwin-arm64.tar.xz
tar -xJf /tmp/sf.tar.xz -C ~/.local/
ln -sf ~/.local/sf/bin/sf ~/.local/bin/sf
```

### `sf org login web` fails behind a browser proxy

Salesforce redirects correctly to `http://localhost:1717/OauthRedirect`, but a browser
whose proxy configuration does not exempt loopback cannot reach the listener. The CLI
reports `AuthTimeoutError`, which reads like the user was slow rather than like a network
failure. Chrome and Safari bypass proxies for localhost by default; Firefox often does
not.

`--browser chrome` works. JWT avoids the callback leg entirely.

### The CLI redacts secrets in `--json`

`sf org display --json` returns, literally:

```
"[REDACTED] Use 'sf org auth show-access-token' to view"
```

for both `accessToken` and `sfdxAuthUrl`, in every variant including `--verbose`. Passing
that through as a bearer token produces `INVALID_AUTH_HEADER` — which reads like a
malformed request, not a masked value.

`SfCliTokenProvider` therefore reads the instance URL from `org display` and the token
from `org auth show-access-token`, and explicitly rejects anything still beginning with
`[REDACTED`. A CLI update could reintroduce this silently, so there is a test for it.

### Key Vault is private-endpoint only here, which reshapes the network

The architecture puts the Salesforce JWT signing key in Key Vault. The vault provisions,
but its data plane refuses everything:

```
Public network access is disabled and request is not from a trusted service
nor via an approved private link.          code: ForbiddenByConnection
```

The template asks for `publicNetworkAccess: 'Enabled'`. So does `az keyvault update`,
which reports `Disabled` straight back — and so does a bare `az keyvault create`, which is
what confirms this is environmental rather than a template bug. No policy assignment shows
as non-compliant and the only management group is the tenant root, so it is a tenant-level
guardrail on the MCAP subscription.

The useful reframe: **this does not block Key Vault, it blocks *public* Key Vault.**
`publicNetworkAccess: Disabled` is exactly the state a private endpoint expects, so the
guardrail is pushing toward private networking rather than away from the vault. The
deployment therefore carries a VNet, a private endpoint, a `privatelink.vaultcore.azure.net`
zone, and a VNet-integrated Container Apps environment.

Two rules bit on the way, and read together they look like a contradiction:

| Symptom | Cause |
|---|---|
| `ManagedEnvironmentV1SubnetDelegationNotAllowed` | Raised while trying to *update* the pre-existing **consumption-only (V1)** environment, which rejects a delegated subnet. |
| `ManagedEnvironmentSubnetDelegationError` | Raised when *creating* a fresh environment, which defaults to **workload profiles** and requires the `Microsoft.App/environments` delegation. |

Same subnet, opposite demands. The delegation is required — the first error only appeared
because the old environment was still there. Which leads to the third rule:
`ManagedEnvironmentCannotAddVnetToExistingEnv`. VNet integration is **create-time only**,
so retrofitting it means deleting the environment, which changes the app FQDN and forces
the OpenAPI `servers` URL, the Foundry connection target, and the agent version to be
re-cut. Budget for that ripple rather than discovering it mid-change.

The signing key itself is written **through ARM**, not uploaded from a workstation.
`Microsoft.KeyVault/vaults/secrets` is a control-plane resource, so it is not subject to
the data-plane firewall — which is the only reason a secret can be placed into a
private-only vault from outside the VNet. Without that the design would be circular. It
travels as base64 so the multi-line PEM survives as a single parameter, and
`base64ToString` restores it in the template.

Proven rather than assumed: with `CRM_PROVIDER=salesforce` the revision reaches
**Running / Healthy** with `sf-private-key` resolving from
`https://kv-….vault.azure.net/secrets/sf-private-key`, which only happens if the container
reached the vault over the private endpoint.

### Two azd deployment traps

`azd deploy`'s remote build fails in this environment:

```
InvalidCorrelationRequestId: The correlation request ID must be a GUID in canonical D format.
```

It had worked earlier in the same project, so it is intermittent rather than
configuration. With no local Docker daemon there is no fallback, but `az acr build`
does the same job and needs neither azd nor Docker:

```bash
az acr build --registry <acr> --image crm-tools:latest --file Dockerfile .
az containerapp update -n <app> -g <rg> --image <acr>.azurecr.io/crm-tools:latest
```

The second trap is quieter. The Bicep template carries a placeholder image so the app can
boot before any deploy — the standard azd pattern, because `azd deploy` immediately
replaces it. When the deploy step is broken, every subsequent `azd provision` silently
reverts the running app to that placeholder, which listens on port 80 and so never passes
a health probe on 8000. The revision sits in `Activating` forever and the logs blame a
probe failure rather than the image. The image is now a `containerImage` parameter, so
provisioning preserves whatever is deployed.

### "New Connected App" is gone from the UI, but the metadata type is not

Newer orgs offer only **New Lightning App** and **New External Client App** in App
Manager. The documented Connected App walkthrough — enable OAuth, upload the certificate,
pick scopes — has no button behind it any more.

The `ConnectedApp` metadata type still deploys, which is the better route regardless:
`scripts/deploy_connected_app.sh` renders the app from `.secrets/server.crt` at deploy
time and pushes it, so the same command works against any org and nothing keypair-specific
is committed. `isAdminApproved` plus `profileName` in the metadata replaces the two
Setup steps (pre-authorize, then assign a profile) that are easy to miss — and missing
them produces `user hasn't approved this consumer`, which sounds like a scope problem
rather than a policy one.

The consumer key comes back the same way. Retrieving the app returns `consumerKey` in
`oauthConfig`, so it never has to be copied out of **Manage Consumer Details** by hand.

---

## Decisions worth remembering

**Live org first, no mock dataset.** Building against invented fixtures tends to produce
tools shaped around imagined records, with every schema surprise arriving late. Going
live-first surfaced picklist resolution, External ID upserts and query escaping as
*design* concerns rather than integration bugs.

**Escaping was built before any query code.** `soql.py` landed first, with 56 tests
covering the full SOSL reserved-character set and injection breakout attempts. Retrofitting
escaping is how injection bugs survive.

**Absolute values only, never deltas.** This is what makes updates naturally idempotent —
`Amount = 750000` twice leaves the same state — so `Opportunity` needs no External ID
field. A delta-shaped API (`increase_amount_by`) could not be made replay-safe, which is
why that shape is absent from the tool surface.

**No PII in the repository.** Account, opportunity and mention-target names all resolve
from `.env` at runtime. Users are looked up by query, never by hardcoded id, so the seed
script runs against any org.

**Modules were not split for their own sake.** The plan sketched separate
`aggregates.py`, `chatter.py`, `users.py` and `ledger.py`. Each was 15–20 lines; splitting
them would have added indirection without separation. `salesforce_mapping.py` stayed
separate because it genuinely isolates Salesforce vocabulary from the domain model.

**Idempotency keys are derived, not asked for.** The tool schema accepts one, but when it
is absent the handler hashes the request itself. Asking the model to invent a key gets the
failure backwards in both directions: a fresh random value on every retry defeats dedupe
entirely, while a reused one silently collapses two genuinely different tasks. Hashing the
payload makes "same command twice" and "different command" mean exactly what they say.
Scoping the hash to a conversation is still open, and belongs with the API layer.

**An ambiguous stage cannot reach a write.** `resolve_stage` is a tool the agent is told to
call, but `update_opportunity` resolves again and refuses anything that is not exactly one
match. The instruction is the ergonomics; the refusal is the guarantee.

**The API key is a security scheme, not a header parameter.** Declared with `Header(...)`
it generated correctly and worked - and published `x-api-key` into the tool schema as an
optional string for the agent to fill in. A credential the model can supply is a
credential the model can invent. `APIKeyHeader` moves it into `securitySchemes`, where the
caller supplies it and the schema does not mention it at all. There is a test asserting it
never reappears as a parameter.

---

## Next

1. Salesforce Connected App and JWT, so the deployed API can run `CRM_PROVIDER=salesforce`
2. Tune VAD thresholds against real car audio rather than a quiet room

Outstanding questions are tracked in the README: real production field API names, whether
`Bidding` is the correct product-detail trigger stage, and pinning the Voice Live
`api_version` against the installed SDK.
