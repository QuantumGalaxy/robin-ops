"""Consistent SQLite backups plus auxiliary state; excludes credentials and lab quote cache."""
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
    for path in source.rglob('*.sqlite3'):
        target = stage / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as src:
            with sqlite3.connect(target) as dst:
                if path.name == 'comparison.sqlite3':
                    # Durable experiment identity and performance; rolling quote evidence
                    # stays on the server and is not duplicated in 168 hourly archives.
                    src.execute('BEGIN')
                    for table in ('metadata', 'equity'):
                        schema = src.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()[0]
                        dst.execute(schema)
                        cursor = src.execute(f'SELECT * FROM {table}')
                        placeholders = ','.join('?' for _ in cursor.description)
                        dst.executemany(f'INSERT INTO {table} VALUES ({placeholders})', cursor)
                else:
                    src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
    for name in ['reference.json', 'orders.json']:
        for path in source.rglob(name):
            target = stage / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    if (source / "reports").exists():
        shutil.copytree(source / "reports", stage / "reports")
    with tarfile.open(out / (now+'.tar.gz'), 'w:gz') as archive:
        archive.add(stage, arcname='state')
for old in sorted(out.glob('*.tar.gz'))[:-168]:
    old.unlink()
