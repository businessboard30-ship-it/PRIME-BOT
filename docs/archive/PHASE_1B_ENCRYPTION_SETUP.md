# Phase 1b: Encrypt Secrets at Rest (Issue 1.2)

## Problem

Bot tokens and API keys stored as plaintext `TEXT` columns in Postgres. Anyone with DB access (leaked `DATABASE_URL`, compromised dashboard, etc.) can read live Telegram bot tokens and impersonate them.

**Status:** FIXED - Encryption infrastructure now in place.

---

## Solution Implemented

### 1. New Encryption Module: `utils/crypto.py`

A `SecretManager` class that encrypts/decrypts using Fernet (symmetric encryption):
- Automatically initializes from `ENCRYPTION_KEY` environment variable
- Graceful degradation: warns if key not set, returns plaintext (dev-friendly)
- Constant-time comparison to prevent padding oracle attacks
- Clear error messages on decryption failure (helps diagnose key rotation issues)

**Key properties:**
- Uses `cryptography.fernet.Fernet` (industry-standard, from PyCA)
- Symmetric encryption (one key for encrypt/decrypt — suitable for app secrets)
- Base64-encoded ciphertext (safe for databases, logs, etc.)
- Authenticated encryption (tampered ciphertext fails closed)

### 2. Database Integration

**Changes to `database.py`:**

- Import: `from utils.crypto import secret_manager`
- `add_cloned_bot()`: Encrypt `bot_token` before storing in DB
- `get_user_clones()`: Decrypt `bot_token` when retrieving (with fallback if decrypt fails)

**Encryption happens at application layer, not DB layer** — means:
- Encryption is consistent across any DB provider (Supabase, Neon, Railway, etc.)
- Keys never touch the database
- App code controls when decryption happens (e.g., only when calling Telegram API, never in logs)

### 3. Migration Script: `scripts/encrypt_bot_tokens.py`

Safely migrates existing plaintext tokens:
```bash
# Dry-run: see what will change
python scripts/encrypt_bot_tokens.py --dry-run

# Actually encrypt
python scripts/encrypt_bot_tokens.py --execute
```

Identifies unencrypted tokens using a heuristic (Fernet ciphertext starts with `gAAAAAB`, Telegram tokens don't), encrypts each, updates the DB.

---

## Deployment Steps

### 1. Generate an Encryption Key

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Output will look like: `5Qx_EeWQQ_RLOuEkCYXj3_X_Wj_6_3_5_x_9_z_a_1b2_c3d4e5f6g7h8=`

### 2. Add to Vercel Environment

**Do NOT commit this to git.** Add to Vercel project settings:

```bash
vercel env add ENCRYPTION_KEY
# Then paste the key when prompted
```

Or via CLI:
```bash
vercel env add ENCRYPTION_KEY 5Qx_EeWQQ_RLOuEkCYXj3_X_Wj_6_3_5_x_9_z_a_1b2_c3d4e5f6g7h8=
```

**Verify:**
```bash
vercel env ls
# Should show ENCRYPTION_KEY in the list
```

### 3. Deploy New Code

Push the changes:
```bash
git add utils/crypto.py scripts/encrypt_bot_tokens.py database.py
git commit -m "feat: Encrypt bot tokens at rest (Issue 1.2)"
git push
vercel deploy
```

### 4. Run Migration (if you have existing tokens)

**Only if there are existing plaintext tokens in production:**

```bash
# Connect to production DB, run dry-run first
python scripts/encrypt_bot_tokens.py --dry-run

# If it looks good:
python scripts/encrypt_bot_tokens.py --execute
```

After migration, verify a token can be decrypted:
```python
python -c "
import asyncio
from database import Database
from config import ADMIN_ID

async def test():
    db = Database()
    clones = await db.get_user_clones(ADMIN_ID)
    for clone in clones:
        print(f'Clone {clone[\"clone_id\"]}: token={clone[\"bot_token\"][:20]}...')

asyncio.run(test())
"
```

---

## Security Model After This Fix

**Before:** Bot tokens in plaintext in Postgres
```
DB read access → steal live Telegram tokens → impersonate bots
```

**After:** Bot tokens encrypted in Postgres
```
DB read access → encrypted tokens → useless without ENCRYPTION_KEY
ENCRYPTION_KEY stored in Vercel secrets (not in DB, not in code)
Tokens only decrypted in-process when actually needed for Telegram API call
```

**Remaining assumptions:**
- Vercel's encrypted env var storage is secure (it is, it's industry-standard)
- Your `ENCRYPTION_KEY` is not logged anywhere (check logs after deploying)
- Your `ENCRYPTION_KEY` is backed up securely (if you rotate it, you need to re-encrypt every token with the new key — plan this)

---

## Key Rotation (Future)

If you ever need to rotate keys (e.g., suspected compromise, team turnover):

1. Generate new key (same command as above)
2. Create `scripts/rotate_encryption_key.py` that:
   - Reads all tokens with old key
   - Re-encrypts them with new key
   - Writes back to DB
3. Update `ENCRYPTION_KEY` env var in Vercel
4. Run rotation script
5. Verify no tokens are corrupted

---

## Verification Checklist

After deployment:

- [ ] `ENCRYPTION_KEY` is set in Vercel env vars (NOT in `.env` or git)
- [ ] `utils/crypto.py` imported successfully (no syntax errors)
- [ ] `database.py` imports crypto module without error
- [ ] Existing cloned bots still work (tokens are encrypted/decrypted transparently)
- [ ] New cloned bot creation works (token is encrypted on insert)
- [ ] If you have existing tokens, migration script ran without errors
- [ ] Logs do NOT contain plaintext bot tokens (search logs for the token value)

---

## What's NOT Yet Encrypted

This phase only fixed bot tokens. For Phase 1a (if going Path A for real cloning), also encrypt:

- `users.stripe_key` (not currently used, but if Stripe integration is built)
- Any API keys stored in `cloned_bots.custom_data` JSON (add a helper to encrypt/decrypt specific JSON fields)

Also consider encrypting:
- `payment_logs` sensitive fields
- User PII if you collect it (emails, phone numbers)

But those are out of scope for Phase 1b. Focus: **bot tokens, the highest-custody risk.**

---

## Testing

Manual test in Python:
```python
from utils.crypto import secret_manager

# Encrypt
ciphertext = secret_manager.encrypt("123456789:ABCDefGHijKlmnOpqrsTu")
print(f"Encrypted: {ciphertext[:50]}...")

# Decrypt
plaintext = secret_manager.decrypt(ciphertext)
assert plaintext == "123456789:ABCDefGHijKlmnOpqrsTu"
print("✓ Round-trip successful")

# Tampered ciphertext fails gracefully
tampered = ciphertext[:-5] + "xxxxx"
result = secret_manager.decrypt(tampered)
assert result is None
print("✓ Tampered ciphertext handled safely")
```

---

## Impact on Other Features

**Handlers:** No changes needed. `get_user_clones()` returns decrypted tokens transparently.

**Webhooks:** If you store webhook tokens/secrets anywhere, add encryption to those too (future task).

**Logs:** Audit logs — make sure you're NOT logging full bot tokens anywhere. Check for `logger.info(bot_token)` or similar.

**Admin Panel:** If admins need to see bot tokens, the UI should display them as `••••••••` (masked) or not at all, never plaintext.

---

## Estimated Impact

**Performance:** Negligible. Encryption/decryption is sub-millisecond per token. DB round-trip is the bottleneck, not crypto.

**Storage:** Ciphertext is ~30% larger than plaintext (base64 encoding overhead). Negligible for the number of bots you'll have.

**Maintenance:** Low. Fernet handles padding, versioning, timestamp validation automatically.

