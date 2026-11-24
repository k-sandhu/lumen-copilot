"""Trusted in-container housekeeping; never executed on the runner host."""

PROCESS_SNAPSHOT = """
import json, os
from pathlib import Path
baseline = {}
for path in Path('/proc').glob('[0-9]*/stat'):
    try:
        pid = int(path.parent.name)
        fields = path.read_text().rsplit(')', 1)[1].split()
        if pid != os.getpid():
            baseline[str(pid)] = fields[19]
    except FileNotFoundError:
        pass
print(json.dumps(baseline))
"""

# PID/starttime identities protect only the pre-execution idle tree. Process
# groups are insufficient: model code may setsid(), double-fork and close its
# descriptors. Zombies no longer consume execution resources; PID 1 reaps them.
PROCESS_CLEANUP = """
import json, os, signal, sys
from pathlib import Path
baseline = json.loads(sys.argv[1])
while True:
    active = False
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            pid = int(path.parent.name)
            fields = path.read_text().rsplit(')', 1)[1].split()
            if pid in (1, os.getpid()) or fields[0] == 'Z':
                continue
            if baseline.get(str(pid)) == fields[19]:
                continue
            os.kill(pid, signal.SIGKILL)
            active = True
        except (FileNotFoundError, ProcessLookupError):
            pass
    if not active:
        break
"""

# Install into a fresh target, then remove the previous distribution's RECORD
# before merging its replacement. Upgrading the shared target would leave stale
# metadata and erase namespace files contributed by other distributions.
PACKAGE_REPLACE = """
import importlib.metadata, json, re, shutil, sys
from pathlib import Path
root = Path('/workspace/.packages').resolve()
names = set(json.loads(sys.argv[1]))
for dist in importlib.metadata.distributions(path=[str(root)]):
    name = re.sub(r'[-_.]+', '-', dist.metadata['Name']).lower()
    if name not in names:
        continue
    if dist.files is None:
        raise RuntimeError('installed package has no replacement manifest')
    parents = set()
    for entry in dist.files:
        path = Path(dist.locate_file(entry))
        if not path.resolve().is_relative_to(root):
            raise RuntimeError('installed package manifest escapes target')
        path.unlink(missing_ok=True)
        parents.update(parent for parent in path.parents if parent.is_relative_to(root))
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
source = Path(sys.argv[2])
shutil.copytree(source, root, dirs_exist_ok=True)
shutil.rmtree(source)
"""
