# CRM Sales Companion

A hands-free voice sales assistant for field sales representatives, built on **Microsoft Foundry Agent Service** and the **Voice Live API**.

A rep driving between customer sites talks to the assistant like a sales-ops colleague — pulling up accounts, reviewing opportunities, updating amounts and stages, and creating follow-up tasks — without touching a keyboard.

> **Status:** Wired to a live Salesforce Developer Edition org from the start — no mock CRM dataset. A `CrmProvider` seam keeps tools decoupled from the data source, with a recorded in-memory fake used only for tests and offline prompt iteration so `pytest` needs no network and voice tuning doesn't burn API quota or litter the org.
>
> Build progress, and the org-specific findings that shaped the design, are recorded in the
> [development log](docs/development-log.md).

---

## Table of contents

- [Target experience](#target-experience)
- [Architecture](#architecture)
  - [System context](#system-context)
  - [Voice flow](#voice-flow)
  - [Tool flow and the write-safety protocol](#tool-flow-and-the-write-safety-protocol)
  - [Provider layer](#provider-layer)
- [Salesforce integration](#salesforce-integration)
  - [Authentication](#authentication)
  - [Metadata bundle](#metadata-bundle)
  - [Object mapping](#object-mapping)
  - [Aggregate queries](#aggregate-queries)
  - [Chatter posts and @mentions](#chatter-posts-and-mentions)
  - [Durable idempotency](#durable-idempotency)
  - [Picklist resolution](#picklist-resolution)
  - [Query injection safety](#query-injection-safety)
  - [API limits](#api-limits)
- [Azure reference architecture](#azure-reference-architecture)
- [Key architectural decisions](#key-architectural-decisions)
- [Tool surface](#tool-surface)
- [Repository layout](#repository-layout)
- [Implementation plan](#implementation-plan)
- [Setup](#setup)
- [Local execution](#local-execution)
- [Deployment](#deployment)
- [Verification](#verification)
- [Future enhancements](#future-enhancements)
- [Development log](docs/development-log.md)

---

## Target experience

The scenario comes from field feedback: a rep parked outside a customer, needing to triage that account's pipeline before walking in, then capture the outcome from the car afterwards. Today that means writing a list down on paper on the way in, and typing notes later — if at all.

**Scene 1 — parked outside the customer, before the meeting**

```
Rep:    How many open opportunities does Contoso Building Supply have?
Agent:  Fourteen open. Oldest entered March last year. Six are past their
        close date.

Rep:    Read me the past due ones.
Agent:  Northgate Commons Phase 2, forty-two thousand, Bidding, closed date
        was April 30th.
        ...

Rep:    Next.
Agent:  Ashwood Commons, eighteen thousand, Negotiation, was June 15th.
```

**Scene 2 — back in the car, after the meeting**

```
Rep:    Update Northgate Commons Phase 2.
Agent:  Got it. Forty-two thousand, Bidding.

Rep:    Customer need: they want the wide plank finish in slate gray, about
        twelve hundred square feet.
Agent:  Customer Need will read: "They want the wide plank finish in slate
        gray, about twelve hundred square feet." Save it?

Rep:    Yes.
Agent:  Saved.

Rep:    Push the close date to October 15th.
Agent:  Close date April 30th to October 15th. Save it?

Rep:    Yes. And post to Chatter that pricing is sent, mention Dana
        Whitfield to confirm product availability.
Agent:  Posting to Northgate Commons Phase 2, mentioning Dana Whitfield.
        Post it?

Rep:    Yes.
Agent:  Posted. Dana's been notified.
```

Two things in Scene 1 are load-bearing. *"How many"* and *"how many past due"* are answered by a **SOQL aggregate**, not by the model counting records aloud — see [Aggregate queries](#aggregate-queries). And the past-due list is read one at a time on the rep's cue, because a fourteen-item list read continuously to someone in a car is useless.

In Scene 2, `Customer Need` is consumed downstream by supply chain for manufacturing, so it is read back verbatim before writing rather than summarised. The Chatter mention is a real notification to a named user, not text that merely looks like one — see [Chatter posts and @mentions](#chatter-posts-and-mentions).

Design rules baked into the agent instructions:

| Rule | Why |
|---|---|
| Responses under ~15 words unless reading back a change | The rep is driving; long responses are unsafe and unusable |
| Lists are read one item at a time, on cue | Fourteen opportunities read continuously is noise, not information |
| Counts and dates come from the query, never from the model | Aggregates are exact; LLM arithmetic over 40 records is slow and wrong |
| Free-text notes read back **verbatim** before writing | Supply chain manufactures from `Customer Need`; a paraphrase is a defect |
| Read back every field change before writing | Guards against misrecognition of amounts and dates |
| Never invent a record ID | Writes only accept IDs returned by a read in the same conversation |
| **Absolute values only, never deltas** | "Set it to 750k" survives a replay; "raise it by 250k" compounds |
| Every create carries an idempotency key | Road noise and repeated phrases must not create duplicate records |
| Stage names resolved against the live picklist | Spoken shorthand rarely matches the org's actual values |
| @mentions resolved to a User ID, ambiguity asked aloud | A mention that doesn't resolve fails **silently** — nobody is notified |
| Deep noise suppression + semantic VAD | Car cabin audio, engine noise, passengers, radio |

---

## Architecture

### System context

```mermaid
graph TB
    subgraph Client["Rep's device"]
        MIC["Microphone / Speaker<br/>PCM16 · 24 kHz · mono"]
        CLI["Voice client<br/>Python CLI → browser app"]
    end

    subgraph Foundry["Microsoft Foundry"]
        VL["Voice Live API<br/>WebSocket · Realtime-compatible"]
        AGENT["CRM Sales Companion<br/>Prompt Agent"]
        MODEL["Model deployment<br/>gpt-realtime"]
    end

    subgraph ACA["Azure Container Apps"]
        API["CRM Tool API<br/>FastAPI"]
        MCP["MCP Server<br/>later"]
    end

    subgraph Data["Data"]
        SF["Salesforce<br/>Developer Edition org"]
        FAKE["Recorded fake<br/>tests + prompt tuning"]
    end

    MIC <--> CLI
    CLI <-->|"WSS · audio + events"| VL
    VL <-->|"agent_name · project_name"| AGENT
    AGENT --> MODEL
    AGENT -->|"OpenAPI tool · HTTPS"| API
    MCP -.->|"other clients"| API
    API -->|"REST · JWT bearer"| SF
    API -.->|"CRM_PROVIDER=fake"| FAKE

    classDef azure fill:#0078D4,stroke:#004578,color:#fff
    classDef app fill:#107C10,stroke:#0B5A0B,color:#fff
    classDef data fill:#5C2D91,stroke:#3B1D5E,color:#fff
    class VL,AGENT,MODEL azure
    class API,MCP,CLI app
    class SF,FAKE data
```

The critical property: **the client never executes tools**. It streams audio and renders audio. All reasoning and all CRM access happen server-side, which means the browser client added later inherits the full capability set with zero additional trust.

### Voice flow

```mermaid
sequenceDiagram
    participant R as Rep
    participant C as Voice client
    participant VL as Voice Live
    participant A as Foundry Agent
    participant T as CRM Tool API

    C->>VL: connect(agent_name, project_name)<br/>Entra token
    VL-->>C: session.created
    C->>VL: session.update<br/>modalities, audio format, interim_response
    VL-->>C: session.updated
    C->>VL: conversation.item.create (system) + response.create
    VL-->>C: response.audio.delta ×N  "Ready when you are."

    loop Conversation turn
        R->>C: speaks
        C->>VL: input_audio_buffer.append (50 ms chunks)
        VL-->>C: input_audio_buffer.speech_started
        Note over C: skip_pending_audio()<br/>response.cancel() — barge-in
        VL-->>C: speech_stopped
        VL-->>C: transcription.completed
        VL->>A: turn + conversation context
        A->>T: HTTPS tool call
        Note over VL,C: interim_response fires on TOOL trigger<br/>"Let me pull that up..."
        T-->>A: JSON result
        A-->>VL: response text
        VL-->>C: response.audio.delta ×N
        C->>R: speaks
        VL-->>C: response.done
    end
```

Two details that make or break the in-car feel:

- **Barge-in.** On `speech_started` the client drops queued playback and cancels the in-flight response. Playback packets carry sequence numbers so late-arriving audio from the cancelled response is discarded rather than played over the rep.
- **Interim responses.** `LlmInterimResponseConfig(triggers=[TOOL, LATENCY], latency_threshold_ms=100)` makes the agent say something natural while a CRM round-trip is in flight. Without it, tool calls produce multi-second silences that sound like a dropped call.

### Tool flow and the write-safety protocol

Writes are gated. The agent cannot mutate a record it has not read, and cannot mutate without an explicit spoken confirmation of a concrete diff.

```mermaid
sequenceDiagram
    participant R as Rep
    participant A as Foundry Agent
    participant T as CRM Tool API
    participant D as CrmProvider

    R->>A: "How many open opps does Contoso Building Supply have?"
    A->>T: search_accounts(query="Contoso Building Supply")
    T->>D: SOSL FIND, escaped
    D-->>T: 001xx… Account
    rect rgba(0,120,212,0.12)
        Note over A,D: Aggregate — the query counts, not the model
        A->>T: get_pipeline_summary(account_id="001xx…")
        T->>D: SELECT COUNT(Id), MIN(CreatedDate)<br/>WHERE IsClosed=false
        T->>D: SELECT COUNT(Id) WHERE IsClosed=false<br/>AND CloseDate < TODAY
        D-->>T: 14 · 2025-03-11 · 6
    end
    A-->>R: "Fourteen open. Oldest March last year.<br/>Six past their close date."

    R->>A: "Read me the past due ones"
    A->>T: list_past_due_opportunities(account_id="001xx…")
    T-->>A: 6 records, ordered by CloseDate
    A-->>R: reads one, waits for cue

    Note over R,D: — meeting happens —

    R->>A: "Customer need: six inch cedar texture, slate gray…"
    rect rgba(255,193,7,0.15)
        Note over A,T: Preview — read-only, resolves picklists
        A->>T: preview_opportunity_update(006xx…,<br/>customer_need="…", close_date=2026-10-15)
        T->>D: describe Opportunity (cached)
        T-->>A: diff{Customer_Need__c: ∅→"…",<br/>CloseDate: 2026-04-30→2026-10-15}
    end
    A-->>R: reads the note back **verbatim**, "Save it?"

    R->>A: "Yes"
    rect rgba(16,124,16,0.15)
        A->>T: update_opportunity(006xx…, absolute values)
        T->>D: PATCH /sobjects/Opportunity/006xx…
        D-->>T: 204 No Content
    end
    A-->>R: "Saved."

    R->>A: "Post to Chatter, mention Dana Whitfield"
    A->>T: resolve_user(name="Dana Whitfield")
    T-->>A: 005xx… (exactly one match)
    rect rgba(92,45,145,0.15)
        Note over A,D: Ledger first, then post — FeedItem<br/>cannot carry an External ID
        A->>T: post_chatter_update(006xx…, text,<br/>mentions=[005xx…], idempotency_key)
        T->>D: upsert Voice_Write_Log__c/Idempotency_Key__c
        D-->>T: created: true — not a replay
        T->>D: POST /chatter/feed-elements<br/>messageSegments[Mention 005xx…]
    end
    A-->>R: "Posted. Dana's been notified."
```

| Threat | Mitigation |
|---|---|
| Hallucinated record | Writes reject any ID not returned by a read in this conversation |
| Miscounted pipeline | Counts and dates come from SOQL aggregates, never model arithmetic |
| Paraphrased manufacturing note | `Customer Need` read back verbatim; the diff carries the exact string |
| Misheard amount or date | `preview_opportunity_update` returns a diff the agent reads back |
| Replayed command compounding a value | Write tools accept absolute values only — never deltas |
| Duplicate task from repeated speech | Upsert on a **Unique External ID** field — enforced by Salesforce |
| Duplicate Chatter post | Write ledger upsert gates the post — `FeedItem` can't hold a custom field |
| **@mention that notifies nobody** | Name resolved to a User ID; ambiguous or unresolved is asked aloud, never guessed |
| Invalid stage name rejected by the API | Spoken stage resolved against cached `describe` picklist values |
| Spoken input reaching the query engine | SOSL with escaped reserved characters; IDs regex-validated |
| Background speech triggering a write | Confirmation phrase required; VAD tuned with deep noise suppression |
| Accidental destructive edit | No delete tools exist in the surface at all |

### Provider layer

One registry is the single source of truth. REST routes, the OpenAPI document, and the MCP tool list are all generated from it — they cannot drift.

```mermaid
graph LR
    subgraph Core["Tool core — no Azure dependency"]
        REG["tools/registry.py<br/>name · description · schemas<br/>handler · is_write"]
        HAND["tools/handlers.py"]
        PROV["crm/provider.py<br/>CrmProvider Protocol"]
    end

    subgraph Impl["Implementations"]
        SFDC["SalesforceProvider<br/>REST · JWT · describe cache<br/>DEFAULT"]
        FAKE["FakeCrmProvider<br/>recorded responses<br/>tests + prompt tuning"]
    end

    subgraph Surfaces["Generated surfaces"]
        REST["FastAPI routes"]
        SPEC["openapi/crm-tools.json"]
        MCPS["MCP server"]
    end

    REG --> HAND
    HAND --> PROV
    PROV -.implements.-> SFDC
    PROV -.implements.-> FAKE
    REG --> REST
    REG --> SPEC
    REG --> MCPS
    SPEC -->|"registered as<br/>OpenAPI tool"| AGENT["Foundry Agent"]

    classDef core fill:#107C10,stroke:#0B5A0B,color:#fff
    classDef gen fill:#0078D4,stroke:#004578,color:#fff
    classDef prim fill:#00A1E0,stroke:#0071A8,color:#fff
    class REG,HAND,PROV core
    class REST,SPEC,MCPS gen
    class SFDC prim
```

`CRM_PROVIDER` selects the implementation. `salesforce` is the default and what every demo and integration test runs against. `fake` exists so `pytest` needs no network or credentials, and so the dozens of iterations needed to tune prompts and barge-in don't consume API quota or leave junk records behind.

The fake is seeded from **recorded** Salesforce responses, not hand-written JSON — if the org's schema shifts, re-record rather than re-imagine.

---

## Salesforce integration

### Authentication

Two credential paths, each suited to its environment. The provider abstracts which one is in play.

```mermaid
graph TB
    subgraph Local["Local development"]
        SFCLI["sf CLI<br/>sf org login web"]
        TOK["access token<br/>+ instance URL"]
        SFCLI --> TOK
    end

    subgraph Deployed["Container Apps"]
        UAMI["User-assigned<br/>managed identity"]
        KV["Key Vault<br/>RSA private key"]
        JWT["Signed JWT<br/>RS256"]
        UAMI -->|"get secret"| KV
        KV --> JWT
    end

    TOK --> PROV["SalesforceProvider"]
    JWT -->|"grant_type=jwt-bearer"| SFOAUTH["Salesforce token endpoint"]
    SFOAUTH -->|"access_token<br/>cached to expiry"| PROV
    PROV --> SFAPI["Salesforce REST API"]

    classDef az fill:#0078D4,stroke:#004578,color:#fff
    classDef sf fill:#00A1E0,stroke:#0071A8,color:#fff
    class UAMI,KV az
    class SFOAUTH,SFAPI,SFCLI sf
```

The JWT bearer flow, used by the deployed API:

```http
POST https://login.salesforce.com/services/oauth2/token
grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
assertion=<RS256-signed JWT>
```

| JWT claim | Value |
|---|---|
| `iss` | Connected App consumer key |
| `sub` | Integration user's username |
| `aud` | `https://login.salesforce.com` (`test.salesforce.com` for sandboxes) |
| `exp` | now + 3 minutes |

The Connected App needs **Use digital signatures** with the public cert uploaded, OAuth scopes `api` and `refresh_token offline_access`, and the integration user's profile pre-authorized.

Salesforce credentials never leave the tool API. The agent holds no Salesforce identity — it authenticates to *our* API, and our API brokers onward. There is no password anywhere in the flow.

> **Reading the local session token.** Recent `sf` CLI versions redact secrets in `--json` output:
> `org display` returns the literal string `[REDACTED] Use 'sf org auth show-access-token' to view`
> for both `accessToken` and `sfdxAuthUrl`. Treating that as a token produces
> `INVALID_AUTH_HEADER`, which reads like a malformed request rather than a masked value.
> `SfCliTokenProvider` therefore reads the instance URL from `org display` and the token from
> `org auth show-access-token`, and rejects anything still starting with `[REDACTED`.

### Metadata bundle

`sf project deploy start --source-dir sfdx/force-app` creates **one custom object, eight custom
fields, and one permission set**. Nothing else — no data, no users, no layout changes, no profile
edits.

| Component | Object | Type | Why it exists |
|---|---|---|---|
| `Idempotency_Key__c` | `Activity` | Text(64) · External ID · Unique | Lets task creation be an upsert, so a repeated voice command can't create a second record. Defined once on `Activity`; surfaces on both `Task` and `Event` |
| `Customer_Need__c` | `Opportunity` | LongTextArea(32768) | The manufacturing note, written verbatim |
| `Comments__c` | `Opportunity` | LongTextArea(32768) | Dictated meeting notes |
| `Voice_Write_Log__c` | — | Custom object | Idempotency ledger for records that can't carry their own key |
| `Idempotency_Key__c` | `Voice_Write_Log__c` | Text(64) · External ID · Unique · required | The ledger key itself |
| `Operation__c` | `Voice_Write_Log__c` | Text(64) | Which tool issued the write |
| `Target_Record_Id__c` | `Voice_Write_Log__c` | Text(18) | Record the write targeted |
| `Result_Record_Id__c` | `Voice_Write_Log__c` | Text(18) | Record the write produced, so a replay can report it |
| `CRM_Companion_Integration` | — | Permission set | Grants FLS on the above and object access on the ledger |

> **`Task` and `Event` do not take custom fields directly.** They share the `Activity` object,
> which is where the field is defined. Targeting `Task` or `Event` fails the deploy with
> `bad value for restricted picklist field`. Field-level *security*, however, is still granted
> per-object on `Task` and `Event` — the field is defined once but permissioned twice.

#### Three concepts the bundle depends on

**External ID + Unique.** Marking a field as an External ID makes it usable as an alternate key,
so `PATCH /sobjects/Task/Idempotency_Key__c/{value}` lets Salesforce decide whether that is a
create or an update. `Unique` is what makes the guarantee real — the database rejects a second
row with the same key. Without both flags, deduplication degrades to application memory, which
does not survive a restart or a second replica.

**Field-Level Security.** FLS is per-profile read/edit permission on each individual field,
layered on top of object permissions. Fields created by a metadata deploy get no FLS by default,
and `Modify All Data` does not bypass it. An unreadable field is not an error — it is silently
omitted from query results and dropped on write, so a missing permission looks exactly like a
field-mapping bug. The permission set removes that ambiguity.

This is observable: immediately after deploying, `sf sobject describe --sobject Opportunity`
lists none of the new fields. Assign the permission set, re-run the same command unchanged, and
they all appear. Nothing about the fields changed — only who was allowed to see them.

**Permission set.** A grantable bundle of object and field permissions that can be assigned to a
user without editing their profile. Assign it to whichever user the integration authenticates as:

```bash
sf org assign permset --name CRM_Companion_Integration --target-org devorg
```

The `description` element is capped at **255 characters**; exceeding it fails the deploy with
`data value too large`.

#### Deliberately not in the bundle

| Omitted | Why |
|---|---|
| `Bidding` stage value | `StageName` is a `StandardValueSet`; deploying one **replaces every value in it**. Too much blast radius for one entry — add it in Setup. |
| Page layout changes | Layout metadata is bulky and overwrites whatever is already there. The fields will not appear in the UI until added manually. |
| Chatter enablement | Org-level setting, not deployable source. |
| Users | Created in Setup; the demo needs a second one as an @mention target. |
| Stretch product fields | Product finish, colour, area, trim length and prices are designed but not yet authored — they are not needed for the core scenario. |

That second row matters during verification: the acceptance script asks you to confirm
`Customer_Need__c` in the Salesforce UI, and you will not see the field on the Opportunity page
until you add it to the layout. Until then, read it back with a query instead:

```bash
sf data query --query "SELECT Id, Name, Customer_Need__c FROM Opportunity LIMIT 5" --target-org devorg
```

### Object mapping

| Domain model | Salesforce object | Notable fields |
|---|---|---|
| Account | `Account` | `Id`, `Name`, `Industry`, `Phone`, `BillingCity`, `BillingState` |
| Contact | `Contact` | `Id`, `AccountId`, `Name`, `Title`, `Email`, `Phone` |
| Opportunity | `Opportunity` | `Id`, `AccountId`, `Name`, `Amount`, `StageName`, `CloseDate`, `CreatedDate`, `IsClosed` |
| Task | `Task` | `Subject`, `ActivityDate`, `WhatId`, `WhoId`, `OwnerId`, `Status`, `Priority`, `Description` |
| Logged call | `Task` with `Type='Call'`, `Status='Completed'` | Salesforce models a completed call as a Task, not a distinct object |
| Meeting | `Event` | `Subject`, `StartDateTime`, `WhatId` |
| Chatter post | `FeedItem` via `/chatter/feed-elements` | Structured `messageSegments`, not a plain string |
| User (mention target) | `User` | `Id`, `Name`, `IsActive` |

The mapping is explicit in `crm/salesforce_mapping.py` rather than inferred. Field names like `WhatId` and `ActivityDate` carry no meaning to the model, so the domain layer speaks `account_id` and `due_date` and the mapping translates.

#### Approximated custom fields

The fields the field team actually works in don't exist in a stock Developer Edition org, so the POC creates stand-ins with the same shape and purpose. **These are approximations — real API names must be confirmed against the production org before any of this leaves POC status.**

| Purpose | POC field | Type | Notes |
|---|---|---|---|
| Meeting notes / comments | `Comments__c` | Long Text Area (32k) | Production may use standard `Description` |
| Manufacturing requirement | `Customer_Need__c` | Long Text Area (32k) | **Consumed downstream by supply chain** — written verbatim |
| Product finish | `Product_Texture__c` | Picklist | Stretch scope |
| Product colour | `Product_Color__c` | Picklist | Stretch scope |
| Primary area | `Area_Sqft__c` | Number | Stretch scope |
| Trim length | `Trim_Linear_Ft__c` | Number | Stretch scope |
| Area / trim price | `Area_Price__c`, `Trim_Price__c` | Currency | Stretch scope |
| Write ledger | `Voice_Write_Log__c` | Custom object | Idempotency for objects that can't carry an External ID |

A `Bidding` value is also added to the `StageName` picklist — field feedback suggests that is the stage which triggers product detail capture, but **that is unconfirmed** and needs verification against the real sales process.

Because the mapping is config-driven, pointing at the production org later is a configuration change plus a `describe` dump, not a rewrite.

### Aggregate queries

*"How many open opportunities, what's the oldest, how many are past due"* is the question that opens the workflow — and it is the wrong job for a language model. Fetching forty records so the model can count them and compare dates is slow, expensive, and unreliable, and the failure is invisible: a confidently wrong number read aloud in a car.

Salesforce answers it directly:

```sql
-- summary
SELECT COUNT(Id) total, MIN(CreatedDate) oldest
FROM Opportunity
WHERE AccountId = '001xx...' AND IsClosed = false

-- past due
SELECT Id, Name, Amount, StageName, CloseDate, CreatedDate
FROM Opportunity
WHERE AccountId = '001xx...' AND IsClosed = false AND CloseDate < TODAY
ORDER BY CloseDate ASC
```

`TODAY` is a SOQL date literal evaluated server-side in the user's locale, so "past due" needs no client clock and no timezone reasoning. `IsClosed` is a formula field maintained by Salesforce from the stage, so it stays correct when stages are customised.

`get_pipeline_summary` returns counts and dates as **structured values**; the agent's only job is to speak them. `list_past_due_opportunities` returns records ordered by `CloseDate` so the agent can read them one at a time on the rep's cue rather than emptying the list into the cabin.

### Chatter posts and @mentions

The requirement is *"put an @Person Name to send an alert to the person we need to update."* The word "alert" is the requirement — a post that merely contains the characters `@Dana Whitfield` notifies nobody. It looks right in the UI and silently does nothing, which is the worst possible failure mode for a hands-free tool.

A real mention is a structured segment referencing a **User ID**:

```json
POST /services/data/vXX.X/chatter/feed-elements
{
  "feedElementType": "FeedItem",
  "subjectId": "006xx...",
  "body": {
    "messageSegments": [
      { "type": "Text",    "text": "Pricing sent. " },
      { "type": "Mention", "id": "005xx..." },
      { "type": "Text",    "text": " please confirm product availability." }
    ]
  }
}
```

So `resolve_user` sits in front of every mention:

| Resolution outcome | Behaviour |
|---|---|
| Exactly one active user matches | Proceed, name read back in the confirmation |
| Several match | Agent asks aloud which one — never picks |
| None match | Agent says so and offers to post without the mention |
| User inactive | Treated as no match |

The same pattern as picklist resolution: resolve against the org, surface ambiguity as a spoken question, never guess.

### Durable idempotency

Deduping an `idempotency_key` in a process-local dictionary works right up until the Container App restarts or scales to a second replica — then a replayed voice command creates a **second real record in the customer's CRM**. In-process state is the wrong place for this guarantee.

For objects that accept custom fields, Salesforce enforces it directly. A custom field on `Task`:

```xml
<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Idempotency_Key__c</fullName>
    <label>Idempotency Key</label>
    <type>Text</type>
    <length>64</length>
    <externalId>true</externalId>
    <unique>true</unique>
</CustomField>
```

Creates become upserts keyed on that field:

```http
PATCH /services/data/vXX.X/sobjects/Task/Idempotency_Key__c/{key}
```

| Response body | Meaning | What the agent says |
|---|---|---|
| `{"id": "00T…", "created": true}` | First time this key was seen | "Task created." |
| `{"id": "00T…", "created": false}` | Replay — same record, updated in place | "Already got that one." |

The response body reports which happened, so no HTTP status inspection is needed. Verified against the org: issuing the identical PATCH twice returns the same record id with `created` flipping from `true` to `false`, and `SELECT COUNT(Id) ... WHERE Idempotency_Key__c = ?` returns exactly 1. That flag is what lets the assistant respond honestly to a repeated command instead of silently reporting success twice.

**`FeedItem` can't carry a custom field**, so Chatter posts can't use that mechanism. They go through a write ledger instead — a `Voice_Write_Log__c` custom object with its own Unique External ID. The tool upserts the ledger row first; `201` means proceed with the post, `204` means this is a replay and the post is skipped. Same guarantee, one extra call, and it generalises to any future object that can't be extended.

**Updates need none of this.** Setting `CloseDate = 2026-10-15` twice leaves the same state, so `Opportunity` needs no External ID field. That property only holds because write tools accept **absolute values and never deltas** — the reason that rule is enforced in the tool contract rather than left to the prompt.

### Picklist resolution

`Opportunity.StageName` is a picklist whose values are org-specific. A stock org ships:

```
Prospecting · Qualification · Needs Analysis · Value Proposition
Id. Decision Makers · Perception Analysis · Proposal/Price Quote
Negotiation/Review · Closed Won · Closed Lost
```

A rep says *"move it to Proposal."* Writing the literal string `Proposal` fails — the value is `Proposal/Price Quote`. So the provider calls `describe` on `Opportunity`, caches the picklist, and resolves spoken shorthand against it. An unresolvable or ambiguous stage is an error the agent surfaces as a spoken question, never a guess.

This generalises: any picklist the tools write to gets resolved the same way, which is what keeps the assistant portable to a production org with customised stages.

### Query injection safety

`search_accounts` builds a query from **spoken user input**, and the Salesforce REST API has **no bind parameters** for SOQL — the query is a string you assemble. That is a live injection surface.

Two distinct input classes, handled differently:

**Record IDs** — always sourced from a prior read, never from speech. Validated against `^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$` before interpolation. A value failing that regex is rejected outright, which makes ID interpolation safe by construction.

**Free text** — account names, note bodies, user names. Never interpolated into SOQL. Handled by:

1. **SOSL for name search** — `FIND {term} IN NAME FIELDS RETURNING Account(Id, Name, Industry)`, with SOSL reserved characters escaped: `? & | ! { } [ ] ( ) ^ ~ * : \ " ' + -`
2. **Escaped SOQL literals** where SOSL doesn't fit — backslash and single-quote escaped, length-capped, character-class validated
3. **Request bodies, not queries**, for note text — `Customer_Need__c` travels as JSON in a `PATCH` body, so it never touches the query parser at all

Escaping lives in one place, `crm/soql.py`, with tests covering the reserved-character set. It is not open-coded per call site.

### API limits

Developer Edition allows **15,000 API calls per rolling 24 hours**. A single voice turn can spend several — search, describe, read, preview, write.

That budget is fine for demos and integration runs, and completely inadequate for the iteration loop of tuning agent instructions. Hence `CRM_PROVIDER=fake` for prompt work and the full test suite, `salesforce` for anything that needs to be real.

---

## Azure reference architecture

```mermaid
graph TB
    subgraph Edge["Client tier"]
        DEV["Rep device<br/>CLI · browser · mobile web"]
    end

    subgraph RG["Resource group — rg-crm-companion"]
        subgraph AI["Microsoft Foundry — existing"]
            FRES["AI Services resource<br/>Voice Live enabled region"]
            PROJ["Foundry project"]
            AGT["Prompt Agent<br/>CRM Sales Companion<br/>+ Voice Live config in metadata"]
            MDL["Model deployment<br/>gpt-realtime"]
            CONN["Custom keys connection<br/>tool API credential"]
        end

        subgraph COMPUTE["Container Apps environment"]
            CAPP["ca-crm-tools<br/>FastAPI · external ingress · HTTPS"]
            CMCP["ca-mcp-server<br/>later"]
        end

        subgraph SUPPORT["Platform services"]
            ACR["Azure Container Registry"]
            UAMI["User-assigned managed identity"]
            KVLT["Key Vault<br/>Salesforce JWT private key"]
            LAW["Log Analytics workspace"]
            APPI["Application Insights"]
        end
    end

    SFDC["Salesforce<br/>Developer Edition org"]

    DEV -->|"WSS 443<br/>Entra token"| FRES
    FRES --> PROJ
    PROJ --> AGT
    AGT --> MDL
    AGT -->|"HTTPS · OpenAPI tool<br/>auth via connection"| CAPP
    AGT -.-> CONN
    CAPP --> UAMI
    UAMI -->|"AcrPull"| ACR
    UAMI -->|"get secret"| KVLT
    CAPP -->|"REST · JWT bearer"| SFDC
    CAPP --> APPI
    CMCP --> APPI
    APPI --> LAW
    CAPP -.->|"container image"| ACR

    classDef foundry fill:#0078D4,stroke:#004578,color:#fff
    classDef compute fill:#107C10,stroke:#0B5A0B,color:#fff
    classDef platform fill:#5C2D91,stroke:#3B1D5E,color:#fff
    classDef ext fill:#00A1E0,stroke:#0071A8,color:#fff
    class FRES,PROJ,AGT,MDL,CONN foundry
    class CAPP,CMCP compute
    class ACR,UAMI,KVLT,LAW,APPI platform
    class SFDC ext
```

### Resource inventory

| Resource | Purpose | Provisioned by |
|---|---|---|
| Microsoft Foundry AI Services + project | Hosts Voice Live and the agent | **Existing** — referenced as a parameter |
| Model deployment (`gpt-realtime`) | Agent reasoning + native audio | Existing |
| Prompt Agent | Instructions, Voice Live session config, OpenAPI tool binding | `agent/provision.py` |
| Salesforce Developer Edition org | System of record | **Existing** — Connected App added during setup |
| Container Apps environment | Runtime for tool API and MCP server | Bicep |
| Container App — CRM Tool API | Executes CRM tools; called by Foundry, calls Salesforce | Bicep + `azd deploy` |
| Azure Container Registry | Container images | Bicep |
| User-assigned managed identity | ACR pull, Key Vault access | Bicep |
| Key Vault | Salesforce JWT signing key | Bicep |
| Log Analytics + Application Insights | Traces, tool latency, Salesforce API call counts | Bicep |

### Identity and authentication

```mermaid
graph LR
    DEV["Rep device"] -->|"AzureCliCredential<br/>role: Foundry User"| VL["Voice Live"]
    VL -->|"internal"| AGT["Agent"]
    AGT -->|"API key via connection<br/>→ managed identity JWT"| API["CRM Tool API"]
    API -->|"UAMI"| KV["Key Vault"]
    API -->|"OAuth JWT bearer"| SF["Salesforce"]

    classDef n fill:#0078D4,stroke:#004578,color:#fff
    class VL,AGT,API,KV n
```

Non-negotiables:

- **Voice Live agent mode is Entra-only.** API keys are rejected outright for agent invocation. Local dev uses `AzureCliCredential`; deployed clients use managed identity.
- **The tool API is never anonymous.** It exposes write endpoints on a public ingress that mutate a real CRM. It authenticates via an API key stored in a Foundry custom-keys connection, moving to managed identity with Entra JWT validation.
- **No secrets in source, in agent metadata, or in the container image.** The Salesforce private key lives in Key Vault and is fetched at runtime via managed identity. Agent metadata holds Voice Live session config only.
- **The `.key` and `.pem` files generated during setup are gitignored.** They are the credential.

---

## Key architectural decisions

### Agent mode over model mode

Voice Live supports two topologies. We use **agent mode**.

| | Model mode | **Agent mode (chosen)** |
|---|---|---|
| Tool execution | Client-side, in-process | **Server-side, by Foundry** |
| Instructions live in | Session config, per connection | **Agent definition, versioned** |
| Auth | API key or Entra | **Entra only** |
| Tool API reachability | Not required | **Must be publicly reachable** |
| New client cost | Reimplement tools per client | **Zero — clients only stream audio** |

The tradeoff we accepted: Foundry has to reach the tool API, so even local development needs a dev tunnel. We took it because it means the browser client, and any future mobile or telephony client, inherits every capability without reimplementing a single tool.

### OpenAPI tools for the voice path, MCP as a parallel surface

The MCP tool's `require_approval` defaults to `always`, and that approval handshake is submitted through the runs API — which a Voice Live session does not surface. An MCP write tool would therefore hang forever in a voice conversation.

So: **the voice path uses OpenAPI tools.** The MCP server ships as a Phase 2 deliverable exposing the same registry to other clients (IDE agents, chat surfaces), where the approval loop is both available and desirable.

### Salesforce REST directly, not the Salesforce DX MCP Server

Worth stating plainly because the question comes up: this project talks to Salesforce's REST API through its own provider rather than consuming [`@salesforce/mcp`](https://github.com/salesforcecli/mcp). Assessed August 2026, three properties rule it out for this runtime — any one of them alone would.

| | Salesforce DX MCP Server | What this runtime needs |
|---|---|---|
| Data surface | `run_soql_query` — one tool, read-only | Update opportunity, create task, post Chatter |
| Auth | Locally authorized orgs via the `sf` CLI | Server-side JWT bearer, no human at a keyboard |
| Transport | `npx` stdio, local process | HTTPS endpoint Foundry can reach |

It has no record write tools at all, so the entire Scene 2 half of the demo has nothing to call.

The deeper mismatch survives even if those three are fixed. `run_soql_query` hands the model **raw query power**, which is the exact opposite of what a rep doing 70mph needs. This tool surface is deliberately narrow and opinionated — `preview_opportunity_update` returns a diff to read back, `resolve_stage` refuses to guess between two picklist values, creates carry idempotency keys, and no tool accepts a relative adjustment. Those properties are the product. A general-purpose query tool cannot express them, and a voice agent holding one can invent record IDs, do its own arithmetic, and duplicate records on a repeated phrase.

**Where it genuinely fits: the developer inner loop.** Deploying metadata, running Apex tests, Code Analyzer, LWC scaffolding, querying an org while writing code — that is what its 60+ tools are built for, and it is good at it. It is developer tooling, not a runtime CRM data plane. Salesforce's own docs warn that enabling every toolset "can overwhelm the LLM context."

This is not a bet against MCP. The convergence point is the other direction: `mcp/server.py` publishes *this* registry over MCP, so an MCP-standardised organisation gets the same sixteen voice-safe tools on the protocol it already uses. And because `CrmProvider` is a seam, a future Salesforce MCP server with transactional write tools and a service-principal auth model could be added as another implementation without touching the tool layer.

### Preview-then-commit instead of client-side confirmation

Confirmation logic lives in the tool contract, not just the prompt. `preview_opportunity_update` is a real read-only endpoint returning a structured diff with the picklist already resolved. The agent is instructed to call it before any mutation. Prompt-only confirmation is one jailbreak away from a bad write; this is defense in depth.

### Idempotency in the database, not the process

The obvious implementation — a dictionary of seen idempotency keys — silently fails on restart or a second replica, and the failure mode is duplicate records in a customer's CRM. Pushing the constraint into a **Unique External ID** field makes Salesforce itself the arbiter, and the `201` vs `204` response tells the agent which happened.

This only works because writes are restricted to absolute values. A delta-based API (`increase_amount_by`) cannot be made idempotent this way, which is why that shape was excluded from the tool surface.

### Live org from day one, recorded fake for iteration

Building against mock data first tends to produce tools shaped around imagined records — and every real-schema surprise arrives late. Going live-first surfaced picklist resolution, External ID upserts, and SOQL escaping as *design* concerns rather than integration bugs.

The fake still exists, but it is a recorded test double, not a parallel data model: `pytest` runs offline, prompt iteration doesn't consume the 15k/day API budget, and demos run against real records.

---

## Tool surface

| Tool | Kind | Notes |
|---|---|---|
| `search_accounts` | read | SOSL name search, escaped. Entry point for "how many opps does X have" |
| `get_account` | read | Account with related open opportunities |
| `get_pipeline_summary` | read | **Aggregate** — open count, oldest `CreatedDate`, past-due count |
| `list_past_due_opportunities` | read | `IsClosed = false AND CloseDate < TODAY`, ordered for one-at-a-time reading |
| `get_opportunity` | read | By ID, or open opportunities for an account |
| `get_contact` | read | Contact detail by ID or account + name |
| `list_tasks` | read | Upcoming tasks for the running user |
| `resolve_user` | read | Name → User ID for Chatter mentions; ambiguity returned, never guessed |
| `preview_opportunity_update` | **preview** | Read-only diff, picklists resolved, note text carried verbatim |
| `update_opportunity` | **write** | Stage, close date, amount. Absolute values only |
| `update_opportunity_notes` | **write** | `Comments__c` and `Customer_Need__c`. Verbatim, no summarisation |
| `post_chatter_update` | **write** | Structured `messageSegments` with real mentions; ledger-gated |
| `create_task` | **write** | Upsert on `Idempotency_Key__c` |
| `create_activity` | **write** | Completed `Task` of `Type='Call'`, or `Event` for meetings |
| `create_call_report` | **write** | Completed call Task carrying notes in `Description` |
| `set_opportunity_product_details` | **write** | *Stretch* — finish, colour, area, trim length, prices |

No delete operations exist, and no tool accepts a relative adjustment. A voice interface in a moving car is the wrong place for destructive verbs or arithmetic that compounds on replay.

---

## Repository layout

```
azure.yaml                          azd service + hook definitions
pyproject.toml                      dependencies, tooling, pytest config
.env.example                        every required variable, documented
infra/
  main.bicep                        subscription-scope entry point
  main.parameters.json
  modules/                          ACA env, container app, ACR, UAMI,
                                    Key Vault, monitoring
sfdx-project.json                   sf CLI project root, points at sfdx/force-app
sfdx/
  force-app/main/default/
    objects/
      Activity/fields/Idempotency_Key__c.field-meta.xml  shared by Task + Event
      Opportunity/fields/
        Comments__c.field-meta.xml                       approximated
        Customer_Need__c.field-meta.xml                  approximated
      Voice_Write_Log__c/                                ledger for FeedItem dedupe
        Voice_Write_Log__c.object-meta.xml
        fields/Idempotency_Key__c.field-meta.xml         External ID + Unique
        fields/Operation__c.field-meta.xml
        fields/Target_Record_Id__c.field-meta.xml
        fields/Result_Record_Id__c.field-meta.xml
    permissionsets/
      CRM_Companion_Integration.permissionset-meta.xml   FLS for the above
src/crm_companion/
  config.py                         pydantic-settings; fails fast on missing config
  crm/
    models.py                       pydantic domain models
    provider.py                     CrmProvider Protocol — the seam
    salesforce_provider.py          DEFAULT — REST, JWT, describe cache, upsert
    salesforce_auth.py              sf CLI token (local) / JWT bearer (deployed)
    salesforce_mapping.py           domain ↔ SObject field translation, config-driven
    aggregates.py                   COUNT / MIN / past-due SOQL
    chatter.py                      feed-elements, messageSegments, mention building
    users.py                        name → User ID resolution + ambiguity
    ledger.py                       Voice_Write_Log__c upsert gate
    soql.py                         SOSL/SOQL escaping + ID regex validation
    fake_provider.py                recorded responses; tests + prompt tuning
    recordings/                     captured API responses, refreshed not authored
  tools/
    registry.py                     single source of truth for all tools
    handlers.py                     the ten handlers
  api/
    app.py                          FastAPI; routes generated from registry
    security.py                     API key → Entra JWT
    openapi.py                      spec exporter
  mcp/
    server.py                       MCP streamable-HTTP over the same registry
  agent/
    instructions.py                 driving-optimized prompt + confirmation policy
    voicelive_config.py             session config + 512-char metadata chunking
    provision.py                    create/update agent version
    smoketest.py                    text-mode agent test — no audio
  voice/
    audio.py                        PyAudio capture/playback, barge-in queue
    session.py                      shared Voice Live event loop
    cli.py                          agent-mode CLI entry point
openapi/crm-tools.json              generated artifact, committed
scripts/
  seed_org.py                       creates the demo Account + Opportunity
  record_fixtures.py                captures live responses for the fake
  new_jwt_cert.sh                   self-signed cert for the Connected App
tests/                              handler, idempotency, escaping, spec validity
docs/                               deep-dive architecture notes
```

---

## Implementation plan

### Phase 1 — Org access and preparation
*Nothing else can be verified until the API answers.*

1. Install the Salesforce CLI, `sf org login web`, confirm API access with a raw REST call
2. Create the Connected App: digital signatures, self-signed cert, scopes `api` + `refresh_token offline_access`, pre-authorized profile
3. Deploy the metadata bundle — `Idempotency_Key__c` on `Task`/`Event`, the approximated Opportunity fields, the `Bidding` stage value, and `Voice_Write_Log__c`
4. Confirm Chatter is enabled and a second test user exists to be @mentioned
5. Run `scripts/seed_org.py` — a demo account with ~14 open opportunities, 6 deliberately past due, spread across stages and entry dates
6. Verify the JWT bearer flow independently of the app

### Phase 2 — Domain and provider

7. pydantic domain models and the `CrmProvider` Protocol
8. `salesforce_auth.py` — sf CLI token locally, JWT bearer deployed, with token caching
9. `soql.py` — escaping and ID regex validation with tests over the full reserved-character set, **before** any query is built
10. `salesforce_provider.py` — reads, describe cache, stage resolution, upsert-by-External-ID
11. `aggregates.py`, `users.py`, `chatter.py`, `ledger.py`
12. `record_fixtures.py` → `fake_provider.py` seeded from real captured responses

### Phase 3 — Tool core

13. `tools/registry.py` — the keystone every other surface generates from
14. The handlers, including `get_pipeline_summary`, `preview_opportunity_update`, `post_chatter_update`
15. pytest against the fake: aggregates, idempotency replay, ledger gating, mention resolution, stage resolution, escaping

### Phase 4 — Tool API and OpenAPI spec

14. FastAPI routes generated from the registry, explicit `operationId` per tool
15. API-key validation; anonymous auth is off the table for endpoints that mutate a real CRM
16. OpenAPI 3.1 export with populated `servers[]`, validated in CI

### Phase 5 — Foundry agent

17. Instructions: driving-optimized, preview-before-write, absolute values, stage resolution
18. Voice Live session config plus 512-char metadata chunk/reassemble helpers
19. `provision.py` — create/update the agent version with the OpenAPI tool attached
20. `smoketest.py` — **text-mode** test proving tool calling before audio enters the picture

### Phase 6 — Voice CLI

21. 24 kHz PCM16 mono audio, 50 ms chunks, sequence-numbered playback for barge-in
22. Agent-mode connect, proactive greeting, barge-in, dual logging

### Phase 7 — Deploy

23. Bicep: ACR, Container Apps, managed identity, **Key Vault for the Salesforce key**, monitoring
24. `azd up`, repoint `servers[0].url`, re-provision the agent

### Phase 8 — MCP and docs

25. MCP server over the same registry, deep-dive docs, stretch-goal design notes

---

## Setup

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | |
| Salesforce CLI | See below — `npm i -g @salesforce/cli` may be blocked by a corporate registry proxy |
| Salesforce Developer Edition org | With **admin/Setup access** — required for the Connected App and custom field |
| Azure CLI | `az login` — agent mode requires Entra auth |
| Azure Developer CLI (`azd`) | Deployment |
| `devtunnel` CLI | Local dev — Foundry must reach your tool API |
| PortAudio | macOS: `brew install portaudio` (PyAudio dependency) |
| Microsoft Foundry project | In a Voice Live–supported region, with a model deployment |
| Role: **Foundry User** | On the Foundry resource, for your account |

### Installing the Salesforce CLI

The npm package bundles a pinned `npm` release of its own, which some corporate registry
proxies do not mirror. The Homebrew cask is deprecated for failing the macOS Gatekeeper check.
The standalone tarball avoids both — it ships its own Node runtime and touches no registry:

```bash
curl -fsSL -o /tmp/sf.tar.xz \
  https://developer.salesforce.com/media/salesforce-cli/sf/channels/stable/sf-darwin-arm64.tar.xz
tar -xJf /tmp/sf.tar.xz -C ~/.local/
ln -sf ~/.local/sf/bin/sf ~/.local/bin/sf
sf --version
```

Swap `darwin-arm64` for `darwin-x64` or `linux-x64` as needed. Uninstall is
`rm -rf ~/.local/sf ~/.local/bin/sf`.

### Salesforce org preparation

```bash
# 1. Generate the keypair the Connected App will trust
./scripts/new_jwt_cert.sh                   # writes .secrets/server.key + server.crt

# 2. Create the Connected App in Setup > App Manager > New Connected App
#      - Enable OAuth Settings
#      - Callback URL: http://localhost:1717/OauthRedirect  (required field,
#        unused by JWT)
#      - Use digital signatures -> upload .secrets/server.crt
#      - Scopes: "Manage user data via APIs (api)" and
#                "Perform requests at any time (refresh_token, offline_access)"
#      - Save, then Manage > Edit Policies:
#          Permitted Users = "Admin approved users are pre-authorized"
#        then Manage > Profiles > add your profile
#      Copy the Consumer Key into SF_CLIENT_ID
#
#      Connected App changes take 2-10 minutes to propagate. The first JWT
#      attempt failing with invalid_grant usually just means you were early.

# 3. Authenticate the CLI with JWT - no browser, no localhost callback
sf org login jwt \
  --username "<your-org-username>" \
  --jwt-key-file .secrets/server.key \
  --client-id "<consumer key>" \
  --alias devorg --set-default

sf org display --target-org devorg           # confirm connection

# 4. Deploy the metadata bundle
#    sfdx-project.json is at the repo root, so this runs from there
sf project deploy start --source-dir sfdx/force-app --target-org devorg

# 5. Grant the integration user access to the new fields
#    Metadata-deployed fields carry no field-level security; without this they
#    are silently absent from API responses and dropped on write.
sf org assign permset --name CRM_Companion_Integration --target-org devorg

# 6. Add the "Bidding" stage value by hand
#    Setup > Object Manager > Opportunity > Fields > Stage > New
#    Deliberately NOT in the metadata bundle: StageName is a StandardValueSet,
#    and deploying one replaces every value in it. Not worth the blast radius
#    to add a single entry.

# 7. Confirm Chatter is on and create a second user to @mention
#    Setup > Chatter Settings > Enable
#    Setup > Users > New User  (the @mention target used by the demo)

# 8. Seed the demo records
python -m scripts.seed_org

# 9. Capture fixtures for the offline fake
python -m scripts.record_fixtures
```

> **On `sf org login web`.** The web flow starts a listener on `localhost:1717` and waits for
> the browser to redirect back to it. Browsers configured with a corporate proxy that does not
> exempt loopback will fail to reach it, and the CLI reports `AuthTimeoutError` even though the
> login itself succeeded. Chrome and Safari bypass proxies for localhost by default; Firefox
> often does not. JWT avoids the callback leg entirely, which is why it is the documented path
> here as well as the deployed one.

> `.secrets/` is gitignored. `server.key` is the credential that grants API access to the org — it belongs in Key Vault, never in the repo or the container image.

### Configuration

Copy `.env.example` to `.env` and fill in:

```bash
# Provider selection
CRM_PROVIDER=salesforce          # or 'fake' for offline prompt iteration

# Salesforce
SF_LOGIN_URL=https://login.salesforce.com   # test.salesforce.com for sandboxes
SF_CLIENT_ID=<Connected App consumer key>
SF_USERNAME=<integration user>
SF_PRIVATE_KEY_PATH=.secrets/server.key     # local only; Key Vault when deployed
SF_API_VERSION=<pin after checking /services/data>
SF_ORG_ALIAS=devorg                         # used by the sf CLI auth path

# Foundry project
PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
PROJECT_NAME=<project>
MODEL_DEPLOYMENT_NAME=gpt-realtime

# Voice Live
VOICELIVE_ENDPOINT=https://<resource>.services.ai.azure.com/
VOICELIVE_API_VERSION=<pin to installed SDK — see note below>
VOICE_NAME=en-US-Ava:DragonHDLatestNeural

# Agent
AGENT_NAME=crm-sales-companion
AGENT_VERSION=            # optional — pin for controlled rollout
CONVERSATION_ID=          # optional — resume a prior conversation

# Tool API
TOOL_API_BASE_URL=https://<devtunnel-or-aca-fqdn>
TOOL_API_KEY=<generated>
```

> **Version pinning.** Current docs show `2026-01-01-preview` for agent-mode connect and `2026-04-10` for model mode. Pin both against the installed `azure-ai-voicelive` and `azure-ai-projects` versions before writing session code rather than trusting the doc snippets. Same for the Salesforce REST version — read `/services/data` and pin it.

---

## Local execution

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Tests — offline, no Azure or Salesforce needed
pytest

# 3. Confirm live Salesforce access
python -m crm_companion.crm.salesforce_provider --check   # whoami + describe + API limits

# 4. Generate and validate the OpenAPI spec
python -m crm_companion.api.openapi
openapi-spec-validator openapi/crm-tools.json

# 5. Run the tool API against the dev org
uvicorn crm_companion.api.app:app --reload --port 8000

# 6. Expose it to Foundry (separate terminal)
devtunnel host -p 8000 --allow-anonymous
# Copy the tunnel URL into TOOL_API_BASE_URL

# 7. Provision the agent
az login
python -m crm_companion.agent.provision

# 8. Text-mode smoke test — verify tool calling before touching audio
python -m crm_companion.agent.smoketest

# 9. Full voice conversation
python -m crm_companion.voice.cli
```

Step 8 exists deliberately. Debugging tool invocation and audio plumbing simultaneously is miserable; proving the agent calls tools correctly over text first removes an entire class of confusion from step 9.

While iterating on agent instructions, set `CRM_PROVIDER=fake`. Prompt tuning takes dozens of runs, and each live turn spends several of Developer Edition's 15,000 daily API calls while leaving real Tasks behind in the org. Switch back to `salesforce` to validate.

---

## Deployment

```bash
azd auth login
azd env new crm-companion-poc

# Point at the existing Foundry project
azd env set AZURE_AI_PROJECT_ENDPOINT "<project endpoint>"
azd env set AZURE_LOCATION "<voice-live-supported region>"

azd up

# Repoint the tool spec at the deployed API and re-register the agent
azd env get-values | grep TOOL_API_BASE_URL
python -m crm_companion.api.openapi
python -m crm_companion.agent.provision
```

`azd up` provisions the Container Apps environment, registry, managed identity, and monitoring, then builds and deploys the tool API. The Foundry project is referenced, never created — the plan assumes you already own it.

---

## Verification

| # | Check | Command |
|---|---|---|
| 1 | Aggregates, idempotency replay, ledger gating, mention resolution, escaping | `pytest` |
| 2 | Assumptions only a real org can confirm | `pytest -m liveorg` |
| 3 | Live org reachable; JWT flow works; metadata deployed | `python -m crm_companion.crm.salesforce_provider --check` |
| 4 | OpenAPI spec is valid 3.1 with populated `servers[]` | `python -m crm_companion.api.openapi && openapi-spec-validator openapi/crm-tools.json` |
| 5 | Every operation responds against the dev org | `uvicorn ...` + curl each `operationId` |
| 6 | Agent invokes tools correctly | `python -m crm_companion.agent.smoketest` |
| 7 | Full spoken loop | `python -m crm_companion.voice.cli` |
| 8 | Deployed path | `azd up` → re-provision → repeat 7 |

The offline suite needs no credentials and runs in about a second. `pytest -m liveorg` is
opt-in because it spends API quota and writes to the org; it covers the things mocks
cannot — that fields are actually readable, that `Bidding` really is an Open stage, that
Salesforce genuinely dedupes an upsert, and that a Chatter mention survives as a
structured segment rather than becoming plain text.

**Acceptance script for check 6** — speak both scenes end to end:

*Scene 1 — triage*
1. "How many open opportunities does &lt;demo account&gt; have?" → counts match a manual SOQL run **exactly**
2. "Read me the past due ones" → reads one, stops, waits
3. "Next" → reads the second. Confirm it never dumps the whole list

*Scene 2 — capture*
4. "Update &lt;demo opportunity&gt;" → correct opportunity identified
5. Dictate a Customer Need → agent reads it back **word for word**, not summarised
6. "Yes" → verify in the Salesforce UI that `Customer_Need__c` matches the spoken text exactly
7. "Push the close date to October 15th" → diff read back, confirmed, written
8. Interrupt mid-sentence → playback stops immediately, agent yields
9. "Post to Chatter, mention &lt;demo user&gt;" → confirm, post
10. **Log in as that user and confirm the notification arrived** — a post containing the literal text `@Name` is a failure, not a pass
11. Repeat step 9 verbatim → agent says it already posted, and the feed shows **exactly one** post

Steps 6, 10 and 11 are the ones that get skipped, and each covers a failure that is invisible from the driver's seat: a paraphrased manufacturing note, a mention that notifies nobody, and a duplicate post. All three are verifiable in the org rather than by trusting the agent's own account of itself.

---

## Future enhancements

### Near term

- **Browser voice client.** Web Audio capture → WebSocket relay in Container Apps → Voice Live. Phone-friendly, no local audio dependencies, and the natural demo vehicle for a rep in a vehicle.
- **Managed identity for the tool API.** Replace the API-key connection with Foundry managed identity plus Entra JWT validation at the Container App.
- **Conversation resume.** Thread `conversation_id` through reconnects so a dropped cellular connection resumes mid-thought instead of starting over.
- **Per-rep record ownership.** Today one integration user owns every write. Real deployment needs the rep's own Salesforce identity so `OwnerId` and `CreatedById` reflect who actually spoke.

### Stretch capabilities

| Capability | Utterance | Composition |
|---|---|---|
| Pre-meeting briefing | "Brief me on my next customer" | `list_tasks` + `get_account` + `get_opportunity`, summarized for audio |
| Post-meeting capture | "Record today's notes" | Extended dictation → `create_call_report` + `create_activity` |
| Forecast coaching | "What should I focus on this week?" | Pipeline query ranked by stage, amount, and staleness |
| Generated follow-ups | Automatic after a call report | Draft task, activity, and follow-up email for review |

### Platform

- **Multi-rep identity.** Per-rep Entra identity federated to Salesforce, so the assistant acts *as* the rep rather than as a shared integration user.
- **Evaluation suite.** Batch evals over recorded conversations for tool-selection accuracy and confirmation compliance — the two behaviors that must not regress.
- **Custom voice.** A brand-aligned voice via Azure custom neural voice (requires eligibility approval).
- **Telephony.** The same agent behind a phone number for reps without the app.
- **Production org hardening.** Field-level security review, a dedicated integration profile with least-privilege object permissions, and API call budgeting against the production org's limits.
