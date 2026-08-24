#!/usr/bin/env python3
"""
Migration script to encrypt existing bot tokens at rest.

Run this ONCE after deploying utils/crypto.py and before redeploying handlers.

Usage:
    python scripts/encrypt_bot_tokens.py --dry-run   # See what will change
    python scripts/encrypt_bot_tokens.py --execute    # Actually encrypt the tokens
"""

import asyncio
import argparse
import logging
from typing import List, Tuple

import asyncpg
from config import DATABASE_URL
from utils.crypto import secret_manager

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def get_unencrypted_tokens() -> List[Tuple[int, str]]:
    """
    Query for tokens that look like plaintext (not encrypted ciphertext).
    
    Encrypted tokens from Fernet start with 'gAAAAAB' (base64-encoded).
    Plaintext tokens from Telegram are 32+ alphanumeric chars like '123456789:ABCDefGHijKlmnOpqrsTu'.
    
    This is a heuristic; adjust if needed.
    """
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=1)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT clone_id, bot_token 
            FROM cloned_bots 
            WHERE bot_token IS NOT NULL 
            AND bot_token != ''
            AND bot_token NOT LIKE 'gAAAAAB%'
            ORDER BY clone_id
        """)
    await pool.close()
    return [(row['clone_id'], row['bot_token']) for row in rows]


async def encrypt_tokens(clone_ids: List[int], dry_run: bool = True):
    """Encrypt tokens for given clone IDs."""
    if not clone_ids:
        logger.info("No tokens to encrypt.")
        return
    
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=1)
    
    encrypted_count = 0
    failed_count = 0
    
    for clone_id in clone_ids:
        try:
            async with pool.acquire() as conn:
                # Fetch current token
                row = await conn.fetchrow(
                    "SELECT bot_token FROM cloned_bots WHERE clone_id = $1",
                    clone_id
                )
                
                if not row:
                    logger.warning(f"Clone ID {clone_id} not found")
                    failed_count += 1
                    continue
                
                plaintext_token = row['bot_token']
                
                # Encrypt it
                encrypted_token = secret_manager.encrypt(plaintext_token)
                
                logger.info(f"Clone {clone_id}: {plaintext_token[:10]}... -> {encrypted_token[:20]}...")
                
                if not dry_run:
                    # Update in database
                    await conn.execute(
                        "UPDATE cloned_bots SET bot_token = $1 WHERE clone_id = $2",
                        encrypted_token,
                        clone_id
                    )
                    logger.info("  ✓ Encrypted and saved")
                
                encrypted_count += 1
        
        except Exception as e:
            logger.error(f"Clone {clone_id}: {e}")
            failed_count += 1
    
    await pool.close()
    
    logger.info("")
    logger.info(f"Results: {encrypted_count} encrypted, {failed_count} failed")
    if dry_run:
        logger.info("(This was a dry run. Use --execute to actually encrypt.)")


async def main():
    parser = argparse.ArgumentParser(description="Encrypt existing plaintext bot tokens.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually encrypt tokens (default is dry-run)"
    )
    args = parser.parse_args()
    
    # secret_manager.__init__ now raises RuntimeError at import time if
    # ENCRYPTION_KEY isn't set (utils/crypto.py), so if we've reached this
    # line the key is already confirmed present — nothing to check here.
    
    logger.info("Scanning for unencrypted bot tokens...")
    tokens = await get_unencrypted_tokens()
    
    if not tokens:
        logger.info("No unencrypted tokens found.")
        return
    
    logger.info(f"Found {len(tokens)} unencrypted tokens:")
    for clone_id, token in tokens:
        logger.info(f"  Clone {clone_id}: {token[:15]}...")
    
    logger.info("")
    dry_run = not args.execute
    if dry_run:
        logger.info("DRY RUN MODE (no changes will be made)")
    
    await encrypt_tokens([cid for cid, _ in tokens], dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
