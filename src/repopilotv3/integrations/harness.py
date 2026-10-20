"""Explicit previewed workspace harness generation from the packaged static bundle."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from importlib.resources import files
from pathlib import Path

from repopilotv3 import __version__
from repopilotv3.core import PilotError, atomic_bytes, atomic_json, digest, load_json, now, safe_path, uid

BEGIN, END = '<!-- repopilotv3:begin -->', '<!-- repopilotv3:end -->'
INSTRUCTIONS = '.github/copilot-instructions.md'


class HarnessManager:
    """Own managed file hashes, immutable previews and recoverable explicit writes."""

    def __init__(self, ws):
        """Bind all workspace writes and recovery material to one locally selected root."""
        self.ws = ws
        self.base = ws.state / 'harness'
        self.record = self.base / 'managed.json'

    def source(self, name: str) -> str:
        """Read identical packaged assets or the authoritative development-checkout bundle."""
        packaged = files('repopilotv3').joinpath('harness/workspace', name)
        if packaged.is_file():
            return packaged.read_text('utf-8')
        for parent in Path(__file__).resolve().parents:
            candidate = parent / 'harness/workspace' / name
            if candidate.is_file() and ((parent / 'backend/manifest.json').is_file() or (parent / 'pyproject.toml').is_file()):
                return candidate.read_text('utf-8')
        raise PilotError('Packaged workspace harness is missing; reinstall the complete VSIX', 'missing_resource')

    def rendered(self, repository_root: str | None, repository_state: str | None, server_name: str) -> dict:
        """Render paths/commands while keeping the source bundle unchanged and host independent."""
        root = Path(repository_root).resolve() if repository_root else self.ws.root
        if not root.is_dir() or not root.is_relative_to(self.ws.root):
            raise PilotError('Harness repository must be inside the selected target folder', 'unsafe_repository')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', server_name):
            raise PilotError('Invalid MCP server name', 'invalid_request')
        state = str(Path(repository_state).resolve()) if repository_state else str(root / '.repopilotv3')
        result = {}
        for role in ['plan', 'code', 'ask', 'review']:
            name = f'.github/agents/repopilotv3-{role}.agent.md'
            result[name] = self.source(name).replace('repopilotv3/*', server_name + '/*')
        text = self.source(INSTRUCTIONS)
        if not (root / 'src/repopilotv3').is_dir():
            text = text.split('For the RepoPilotV3 checkout:')[0].rstrip() + '\n'
        result[INSTRUCTIONS] = text
        hooks = json.loads(self.source('.github/hooks/repopilotv3.json'))
        for event, entries in hooks['hooks'].items():
            argv = [sys.executable, '-I', '-B', '-m', 'repopilotv3.integrations.hooks', '--repo', str(root), '--state', state, '--event', event]
            for hook in entries:
                hook['bash'] = shlex.join(argv)
                hook['powershell'] = '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in argv)
        result['.github/hooks/repopilotv3.json'] = json.dumps(hooks, indent=2) + '\n'
        return result

    def status(self) -> dict:
        """Expose upgrade drift and interrupted generation without modifying workspace files."""
        previous = load_json(self.record, {})
        interrupted = [p.parent.name for p in self.base.glob('recovery/*/journal.json') if load_json(p, {}).get('status') in {'prepared', 'recovery_required'}]
        return {'generated': bool(previous), 'target': str(self.ws.root), 'version': previous.get('version'),
                'stale_runtime': bool(previous) and (previous.get('python') != sys.executable or previous.get('version') != __version__),
                'recovery_required': interrupted}

    def preview(self, repository_root: str | None = None, repository_state: str | None = None, server_name: str = 'repopilotv3') -> dict:
        """Create a concrete no-write-to-workspace preview and identify locally modified conflicts."""
        tracked = load_json(self.record, {}).get('files', {})
        rows = []
        for name, desired in self.rendered(repository_root, repository_state, server_name).items():
            target = safe_path(self.ws.root, name)
            old = target.read_bytes() if target.exists() else None
            conflict = None
            content = desired.encode('utf-8')
            managed_hash = digest(content)
            if name == INSTRUCTIONS:
                text = old.decode('utf-8') if old is not None else ''
                newline = '\r\n' if '\r\n' in text else '\n'
                block = (BEGIN + '\n' + desired.rstrip() + '\n' + END).replace('\n', newline)
                start, end = text.find(BEGIN), text.find(END)
                if text.count(BEGIN) > 1 or text.count(END) > 1 or (start < 0) != (end < 0) or (start >= 0 and end < start):
                    conflict = 'Malformed or repeated managed instruction block'
                elif start >= 0:
                    existing = text[start:end + len(END)]
                    if existing != block and digest(existing) != tracked.get(name, {}).get('managed_hash'):
                        conflict = 'Managed instruction block was changed locally'
                    text = text[:start] + block + text[end + len(END):]
                else:
                    text += (newline * 2 if text and not text.endswith(newline * 2) else '') + block + newline
                content = text.encode('utf-8')
                managed_hash = digest(block)
            elif old is not None and old != content and digest(old) != tracked.get(name, {}).get('managed_hash'):
                conflict = 'Existing file is not unchanged managed content'
            rows.append({'path': name, 'before_hash': digest(old) if old is not None else None,
                         'original': old.decode('utf-8') if old is not None else '', 'proposed': content.decode('utf-8'),
                         'managed_hash': managed_hash, 'conflict': conflict,
                         'action': 'conflict' if conflict else 'keep' if old == content else 'create' if old is None else 'update'})
        key = uid('harness')
        result = {'plan_id': key, 'target': str(self.ws.root), 'python': sys.executable, 'version': __version__,
                  'files': rows, 'conflicts': [r['path'] for r in rows if r['conflict']]}
        self.ws.put('harness_plan', key, result)
        return result

    def apply(self, plan_id: str) -> dict:
        """Recheck the full preview, back up originals and apply all files with rollback on failure."""
        if self.status()['recovery_required']:
            raise PilotError('Recover the previous harness update first', 'recovery_required')
        plan = self.ws.get('harness_plan', plan_id)
        if plan['target'] != str(self.ws.root) or plan['conflicts']:
            raise PilotError('Resolve the preview conflicts before generation', 'harness_conflict')
        if plan['python'] != sys.executable or plan['version'] != __version__:
            raise PilotError('Backend changed; generate a new preview', 'stale_preview')
        previous = load_json(self.record, {})
        originals = {}
        for row in plan['files']:
            target = safe_path(self.ws.root, row['path'])
            data = target.read_bytes() if target.exists() else None
            if (digest(data) if data is not None else None) != row['before_hash']:
                raise PilotError(f"File changed after preview: {row['path']}", 'stale_preview')
            originals[row['path']] = data
        recovery = self.base / 'recovery' / plan_id
        journal = {'status': 'prepared', 'files': [], 'previous': previous}
        for row in plan['files']:
            target = safe_path(self.ws.root, row['path'])
            old = originals[row['path']]
            if old is not None:
                atomic_bytes(recovery / row['path'], old)
            journal['files'].append({'path': row['path'], 'before_hash': row['before_hash'],
                                     'after_hash': digest(row['proposed']),
                                     'mode': target.stat().st_mode if old is not None else None})
        atomic_json(recovery / 'journal.json', journal)
        try:
            for row in plan['files']:
                target = safe_path(self.ws.root, row['path'])
                current = target.read_bytes() if target.exists() else None
                if current != originals[row['path']]:
                    raise PilotError('Workspace changed during generation', 'stale_preview')
                if row['action'] != 'keep':
                    mode = target.stat().st_mode if current is not None else None
                    atomic_bytes(target, row['proposed'].encode('utf-8'))
                    if mode is not None:
                        os.chmod(target, mode)
            atomic_json(self.record, {'version': __version__, 'python': sys.executable, 'at': now(),
                                     'files': {r['path']: {'managed_hash': r['managed_hash']} for r in plan['files']}})
            journal['status'] = 'applied'
            atomic_json(recovery / 'journal.json', journal)
        except BaseException:
            self.recover(plan_id)
            raise
        return {'status': 'generated', 'directory': str(self.ws.root / '.github'),
                'changed_files': [r['path'] for r in plan['files'] if r['action'] != 'keep'], 'recovery_id': plan_id}

    def recover(self, recovery_id: str) -> dict:
        """Restore an interrupted generation without overwriting subsequent developer edits."""
        from repopilotv3.core import identifier

        recovery = self.base / 'recovery' / identifier(recovery_id)
        journal = load_json(recovery / 'journal.json', {})
        if journal.get('status') not in {'prepared', 'recovery_required'}:
            raise PilotError('No interrupted harness generation to recover', 'not_found')
        restored = {}
        for row in journal['files']:
            target = safe_path(self.ws.root, row['path'])
            data = target.read_bytes() if target.exists() else None
            if (digest(data) if data is not None else None) not in {row['before_hash'], row['after_hash']}:
                journal['status'] = 'recovery_required'
                atomic_json(recovery / 'journal.json', journal)
                raise PilotError(f"Recovery would overwrite a later edit: {row['path']}", 'recovery_required')
            original = (recovery / row['path']).read_bytes() if row['before_hash'] is not None else None
            if original is not None and digest(original) != row['before_hash']:
                raise PilotError('Harness recovery backup is corrupt', 'artifact_corrupt')
            restored[row['path']] = original
        for row in reversed(journal['files']):
            target = safe_path(self.ws.root, row['path'])
            original = restored[row['path']]
            if original is None:
                target.unlink(missing_ok=True)
            else:
                atomic_bytes(target, original)
                if row['mode'] is not None:
                    os.chmod(target, row['mode'])
        atomic_json(self.record, journal['previous'])
        journal['status'] = 'rolled_back'
        atomic_json(recovery / 'journal.json', journal)
        return {'status': 'rolled_back', 'recovery_id': recovery_id}
