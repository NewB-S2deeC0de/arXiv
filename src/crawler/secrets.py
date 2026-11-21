import os

def load_semantic_scholar_key(secret_name: str = 'SEMANTIC_SCHOLAR_API_KEY') -> str | None:
    value=os.getenv(secret_name,'').strip()
    if value: return value
    try:
        from google.colab import userdata
    except ImportError:
        return None
    try:
        value=userdata.get(secret_name)
    except userdata.SecretNotFoundError:
        return None
    except userdata.NotebookAccessError:
        raise RuntimeError(f'Enable notebook access to {secret_name} in Colab Secrets') from None
    return value.strip() if value else None

def resolve_s2_key(key_or_name: str | None = None) -> str | None:
    """Resolve an S2 API key from either None (default), an env var/Secret name, or a raw key string."""
    if not key_or_name or not str(key_or_name).strip():
        return load_semantic_scholar_key()
    candidate = str(key_or_name).strip()
    val = load_semantic_scholar_key(candidate)
    if val:
        return val
    return candidate

