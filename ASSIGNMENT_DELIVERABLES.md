# Assignment Deliverables

## Automated tests

Run all tests (inside the virtual environment):

```powershell
python manage.py test billing
```

The required scenarios are covered in `billing/tests.py`:

| # | Scenario | Test |
| --- | --- | --- |
| 1 | Less than five minutes of balance cannot start a session | `test_insufficient_balance_cannot_start` |
| 2 | Recharge → create → assign → accept → start → bill 2 minutes → end debits correctly | `test_complete_jwt_api_flow`, `test_happy_path_bills_two_minutes_and_ends` |
| 3 | Balance runs out: session auto-ends, wallet never negative, reason recorded | `test_balance_runs_out_without_going_negative`, `test_assignment_example_wallet_stays_at_40_when_next_minute_is_unaffordable`, `test_session_ends_before_an_unaffordable_minute_is_used` |
| 4 | Invalid state transition returns 400 | `test_invalid_transition_returns_400`, `test_accepting_an_unassigned_session_returns_400` |
| 5 | Duplicate success callback credits once | `test_duplicate_success_callback_credits_once` |
| 6 | Duplicate billing does not double-charge | `test_duplicate_billing_does_not_double_charge_a_minute` |

Additional tests cover money validation, the demo seed command, registration, JWT authentication, one active session per user, provider rate locking, partial minutes, provider session discovery, ownership (403), and cancellation.

## Session state diagram

![Session state diagram](docs/state-diagram.svg)

The same lifecycle as a Mermaid diagram (rendered natively by GitHub):

```mermaid
stateDiagram-v2
    [*] --> unassigned : create (user)
    unassigned --> assigned : assign (provider) - rate quoted
    assigned --> accepted_by_provider : accept (assigned provider)
    accepted_by_provider --> in_progress : start (user) - wallet >= 5 x rate, no other in_progress, rate locked
    in_progress --> in_progress : bill - each minute charged once
    in_progress --> completed : end (user) - ended_by user
    in_progress --> completed : provider-end (provider) - ended_by provider
    in_progress --> completed : system - wallet cannot cover next minute
    unassigned --> cancelled : cancel
    assigned --> cancelled : cancel
    accepted_by_provider --> cancelled : cancel
    completed --> [*]
    cancelled --> [*]
```

Any other transition returns HTTP 400 with a message naming the current and required state; acting on someone else's session returns 403.

## ER diagram

![ER diagram](docs/er-diagram.svg)

The same model as a Mermaid diagram (rendered natively by GitHub):

```mermaid
erDiagram
    USER ||--|| WALLET : owns
    USER ||--o{ RECHARGE_TRANSACTION : recharges
    USER ||--o{ CONSULTATION_SESSION : "books (user)"
    USER |o--o{ CONSULTATION_SESSION : "serves (provider)"
    CONSULTATION_SESSION ||--o{ SESSION_BILL : charges

    USER {
        bigint id PK
        varchar username UK
        varchar role "user | provider"
        decimal per_minute_rate "NULL, providers only"
    }
    WALLET {
        bigint id PK
        bigint user_id FK,UK
        decimal balance "CHECK >= 0"
    }
    RECHARGE_TRANSACTION {
        bigint id PK
        bigint user_id FK
        decimal amount "> 0"
        varchar reference UK
        varchar status "pending | success | failed"
        datetime created_at
    }
    CONSULTATION_SESSION {
        bigint id PK
        bigint user_id FK "one in_progress per user"
        bigint provider_id FK "NULL until assigned"
        varchar status "unassigned ... completed | cancelled"
        decimal per_minute_cost "quoted at assignment"
        decimal locked_per_minute_cost "frozen at start"
        datetime started_at
        datetime ended_at
        varchar ended_by "user | provider | system_insufficient_balance"
        int billed_minutes
        decimal billed_amount
        int actual_billable_minutes
    }
    SESSION_BILL {
        bigint id PK
        bigint session_id FK "UNIQUE with minute_number"
        int minute_number
        decimal amount
        datetime billed_at
    }
```

## Important implementation decisions

- `DecimalField` is used for all currency; money is never a float.
- Every state change and wallet movement runs inside `transaction.atomic()` with `select_for_update()` on the session and wallet rows, so concurrent billing/end/callback requests serialise.
- Business rules are also enforced by the database: `balance >= 0` check constraint, a partial unique index allowing one `in_progress` session per user, and `UNIQUE(session, minute_number)` on bills.
- Providers own their price. The rate is quoted onto the session at assignment and frozen into `locked_per_minute_cost` at start; billing always uses the locked value.
- Billing is per complete minute and prepaid: after settling elapsed minutes, if the wallet cannot cover the next minute the system ends the session immediately, so no unpaid minute is ever consumed and the wallet keeps its remainder (e.g. ₹40 at a ₹50 rate). A trailing partial minute is not charged when a session is ended.
- Billing is synchronous via `POST /api/sessions/{id}/bill/`; a client or scheduler calls it periodically. Repeat calls are idempotent because only unbilled elapsed minutes are charged.
- Recharge callbacks are idempotent: only a `pending` transaction may change state, so duplicate success callbacks and late callbacks after a failure never credit twice.
- Ownership violations return 403; wrong-state actions return 400 with a message naming the current and required state.
- Protected endpoints use JWT Bearer authentication. Registration, token endpoints, and the mock payment callback are public; a real gateway callback would require signature verification.
- Money inputs are validated (positive, finite, two decimal places, capped) before they reach the database.
- `python manage.py seed_demo` builds demo data for every lifecycle path through the same service functions the API uses.
- SQLite is used locally; PostgreSQL is selected automatically when `POSTGRES_HOST` is set (Docker Compose).
