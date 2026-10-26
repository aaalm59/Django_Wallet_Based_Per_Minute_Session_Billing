# Assignment Deliverables

## Automated tests

Run all tests:

```powershell
python manage.py test billing
```

The required scenarios are covered in `billing/tests.py`:

1. A user with fewer than five minutes of balance cannot start a session.
2. Recharge, session start, two billed minutes, and session end debit the wallet correctly.
3. A session automatically ends without creating a negative wallet balance when funds run out.
4. An invalid state transition returns HTTP 400.
5. Repeating a successful recharge callback credits the wallet only once.
6. Repeating billing for the same elapsed minutes does not charge those minutes twice.

Additional tests cover user registration, JWT authentication, and the complete API flow.

## Session state diagram

```text
unassigned
    |
    | provider assigns self
    v
assigned
    |
    | assigned provider accepts
    v
accepted_by_provider
    |
    | user starts (wallet must cover five minutes)
    v
in_progress -------------------- user ends --------------------> completed
    |                                 provider ends ------------> completed
    |
    +-- next minute cannot be paid --> completed
                                        ended_by=system_insufficient_balance
```

## ER diagram

```text
User (role: user/provider)
  | 1:1
  +---- Wallet (balance)
  |
  +----< RechargeTransaction (amount, reference, status)
  |
  +----< ConsultationSession (user)
               |
               +----> User (provider, optional until assigned)
               |
               +----< SessionBill (minute_number, amount)
```

## Important implementation decisions

- `DecimalField` is used for all currency. Wallet values are never represented as floating-point values.
- Session state changes and wallet deductions execute inside `transaction.atomic()` blocks. Rows that affect balance or session state are locked with `select_for_update()`.
- The start action requires `5 × per_minute_cost` and saves that price in `locked_per_minute_cost`, preventing later rate changes from affecting an active session.
- `billed_minutes` records settled minutes. A database uniqueness constraint on `(session, minute_number)` makes minute-level billing idempotent.
- A successful recharge callback only credits a pending transaction once. The reference is unique and the callback locks the transaction and wallet.
- Protected endpoints use JWT Bearer authentication. User registration, token obtain, token refresh, and the mock payment callback are the only public endpoints.
- The project uses SQLite locally and switches to PostgreSQL automatically when the Docker PostgreSQL environment variables are supplied.
