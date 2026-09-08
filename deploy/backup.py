"""Consistent SQLite backups plus auxiliary state; excludes credentials."""
import datetime
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile

source = pathlib.Path('/home/ubuntu/robin-ops/state/robinhood-paper')
out = pathlib.Path('/var/backups/robin-ops')
out.mkdir(mode=0o700, parents=True, exist_ok=True)
now = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
with tempfile.TemporaryDirectory() as tmp:
    stage = pathlib.Path(tmp)
    for path in source.glob('*.sqlite3'):
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as src:
            with sqlite3.connect(stage / path.name) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
    for name in ['reference.json', 'orders.json']:
        if (source/name).exists():
            shutil.copy2(source/name, stage/name)
    with tarfile.open(out / (now+'.tar.gz'), 'w:gz') as archive:
        archive.add(stage, arcname='state')
for old in sorted(out.glob('*.tar.gz'))[:-168]:
    old.unlink()
