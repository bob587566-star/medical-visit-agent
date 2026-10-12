"""Validated public-knowledge snapshots; rollback is an explicit local action."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .knowledge import Knowledge
from .storage import now


def knowledge_version(knowledge: Knowledge) -> str:
    items = [item.model_dump(mode='json') for item in knowledge.all()]
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class KnowledgeVersions:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.directory = self.path.parent / 'versions'

    def snapshot(self) -> dict:
        library = Knowledge(self.path)
        items = library.all()
        if library.load_error or not items:
            raise ValueError('资料库未通过校验，未保存版本。')
        version = knowledge_version(library)
        payload = {'version': version, 'created_at': now(), 'items': [item.model_dump(mode='json') for item in items]}
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (version + '.json')
        if not target.exists():
            with target.open('x', encoding='utf-8', newline='\n') as file:
                file.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        return {'version': version, 'document_count': len(items)}

    def versions(self) -> list[dict]:
        if not self.directory.exists():
            return []
        items = []
        for path in sorted(self.directory.glob('*.json')):
            if re.fullmatch('[a-f0-9]{64}', path.stem):
                payload = json.loads(path.read_text(encoding='utf-8'))
                items.append({'version': path.stem, 'created_at': payload.get('created_at'), 'document_count': len(payload.get('items', []))})
        return items

    def rollback(self, version: str) -> dict:
        if not re.fullmatch('[a-f0-9]{64}', version):
            raise ValueError('版本号无效。')
        target = self.directory / (version + '.json')
        library = Knowledge(target)
        items = library.all()
        if library.load_error or not items or knowledge_version(library) != version:
            raise ValueError('目标版本内容或哈希校验失败，未覆盖当前资料。')
        current = Knowledge(self.path)
        if current.all() and not current.load_error:
            self.snapshot()  # Preserve a valid current version before switching.
        temporary = self.path.with_suffix('.rollback.tmp')
        temporary.write_text(json.dumps([item.model_dump(mode='json') for item in items], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)
        return {'version': version, 'document_count': len(items)}
