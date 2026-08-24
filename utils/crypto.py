import os
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class SecretManager:
    """Encrypt/decrypt sensitive values (bot tokens, API keys) at rest."""
    
    def __init__(self):
        """Initialize with encryption key from environment.

        Hard-fails if ENCRYPTION_KEY is missing rather than silently
        falling back to plaintext storage. This key protects clone bot
        tokens AND clone owners' own payment gateway secret keys
        (database.py's set_clone_payment_provider) — storing those in
        plaintext is a real leak risk, not a degraded-mode nice-to-have,
        so a missing key should stop the app from starting rather than
        quietly downgrading security.
        """
        key_str = os.getenv("ENCRYPTION_KEY")

        if not key_str:
            raise RuntimeError(
                "ENCRYPTION_KEY is not set. This key protects clone bot tokens "
                "and clone owners' payment gateway keys at rest — the app "
                "will not start without it. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' "
                "then set ENCRYPTION_KEY=<that value> in your environment."
            )

        try:
            self.cipher = Fernet(key_str.encode() if isinstance(key_str, str) else key_str)
        except Exception as e:
            logger.error(f"[v0] Failed to initialize cipher with ENCRYPTION_KEY: {e}")
            raise ValueError(
                "ENCRYPTION_KEY is invalid. Generate a new one with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
    
    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a plaintext secret.
        
        Args:
            plaintext: The secret to encrypt (e.g., bot token)
            
        Returns:
            Base64-encoded ciphertext
        """
        try:
            ciphertext = self.cipher.encrypt(plaintext.encode())
            return ciphertext.decode()
        except Exception as e:
            logger.error(f"[v0] Encryption failed: {e}")
            raise
    
    def decrypt(self, ciphertext: str) -> Optional[str]:
        """
        Decrypt a ciphertext secret.
        
        Args:
            ciphertext: Base64-encoded ciphertext from encrypt()
            
        Returns:
            Plaintext secret, or None if decryption fails
        """
        try:
            plaintext = self.cipher.decrypt(ciphertext.encode())
            return plaintext.decode()
        except InvalidToken:
            logger.error(
                "[v0] Decryption failed: invalid token. "
                "This usually means either: "
                "(1) the ciphertext is corrupted, "
                "(2) ENCRYPTION_KEY doesn't match the key used to encrypt it, "
                "or (3) a plaintext value is being treated as ciphertext after a key rotation."
            )
            return None
        except Exception as e:
            logger.error(f"[v0] Decryption error: {e}")
            return None


# Global instance
secret_manager = SecretManager()
