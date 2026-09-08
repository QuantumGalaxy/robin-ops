
import pytest

from optionsagent.mcp.cloud_credentials import EncryptedStore
from optionsagent.mcp.oauth import RESOURCE


def test_encrypted_rotation_restart_and_tamper(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet, InvalidToken

    credentials = tmp_path / 'credentials'
    credentials.mkdir()
    key = credentials / 'oauth-key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(credentials))
    target = tmp_path / 'private' / 'oauth.enc'
    store = EncryptedStore(target)
    assert store.read() == {}
    record = {'resource': RESOURCE, 'refresh_token': 'secret-one'}
    store.save(record)
    assert b'secret-one' not in target.read_bytes()
    assert EncryptedStore(target).read() == record
    record['refresh_token'] = 'rotated-two'
    store.save(record)
    assert EncryptedStore(target).read() == record
    target.write_bytes(b'corrupted')
    with pytest.raises(InvalidToken):
        store.read()
    store.clear()
    assert store.read() == {}
    key.chmod(0o644)
    with pytest.raises(ValueError):
        EncryptedStore(target)


def test_cloud_missing_runtime_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    with pytest.raises(ValueError):
        EncryptedStore(tmp_path / 'oauth')
