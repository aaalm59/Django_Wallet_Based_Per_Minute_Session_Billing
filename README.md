# Wallet-Based Per-Minute Consultation Billing

Small Django REST Framework backend for prepaid one-to-one consultations. It uses SQLite locally; swap `DATABASES` in `config/settings.py` for PostgreSQL in deployment.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py test
```

Create users through Django shell (a provider must use `role='provider'`):

```powershell
python manage.py shell
>>> from billing.models import User
>>> customer = User.objects.create_user('customer', password='password', role='user')
>>> provider = User.objects.create_user('expert', password='password', role='provider')
```

## Authentication

Get JWT tokens with `POST /api/token/`:

```json
{ "username": "customer", "password": "secret" }
```

Send the returned access token on every protected endpoint:

```text
Authorization: Bearer <access_token>
```

Refresh an expired access token with `POST /api/token/refresh/` and `{ "refresh": "..." }`.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/token/` | Obtain JWT access and refresh tokens |
| POST | `/api/token/refresh/` | Refresh an access token |
| POST | `/api/users/` | Register: `{ "username": "customer", "password": "secret", "role": "user" }` |
| GET | `/api/wallet/` | Current caller's wallet |
| POST | `/api/recharges/` | Initiate mock recharge: `{ "amount": "500.00" }` |
| POST | `/api/recharges/{reference}/callback/` | Mock callback: `{ "status": "success" }` |
| GET/POST | `/api/sessions/` | List/create customer sessions; creation needs `per_minute_cost` |
| POST | `/api/sessions/{id}/assign/` | Provider assigns self |
| POST | `/api/sessions/{id}/accept/` | Assigned provider accepts |
| POST | `/api/sessions/{id}/start/` | Customer starts; requires five minutes' balance |
| POST | `/api/sessions/{id}/bill/` | Participant triggers periodic billing |
| POST | `/api/sessions/{id}/end/` | Customer ends |
| POST | `/api/sessions/{id}/provider-end/` | Provider ends |

## State diagram

```text
unassigned -> assigned -> accepted_by_provider -> in_progress -> completed
                                                   |                ^
                                                   +-- user/provider |
                                                   +-- system: insufficient balance
```

## Implementation notes

- Every mutating balance/state operation uses `transaction.atomic()` and row locks.
- A recharge becomes successful and credits the wallet in the same transaction. Later duplicate success callbacks only return the recorded transaction.
- Billing calculates full elapsed minutes less `billed_minutes`; `SessionBill(session, minute_number)` has a database uniqueness constraint, so a minute cannot be billed twice.
- The per-minute rate is copied to `locked_per_minute_cost` when the session starts. If another complete minute cannot be paid, the system closes the session without changing the wallet balance.
