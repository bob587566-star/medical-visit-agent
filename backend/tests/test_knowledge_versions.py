import json
from pathlib import Path

import pytest

from backend.knowledge import Knowledge
from backend.knowledge_versions import KnowledgeVersions, knowledge_version


def fixture(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'knowledge' / 'articles.json'
    target = tmp_path / 'articles.json'
    target.write_bytes(source.read_bytes())
    return target, KnowledgeVersions(target)


def test_version_change_and_validated_rollback_restore_exact_sources(tmp_path):
    path, repository = fixture(tmp_path)
    old = repository.snapshot()
    items = json.loads(path.read_text(encoding='utf-8'))
    items[0]['title'] = '虚构版本测试：就诊准备材料'
    path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    new = repository.snapshot()
    assert new['version'] != old['version']
    assert len(repository.versions()) == 2
    repository.rollback(old['version'])
    assert knowledge_version(Knowledge(path)) == old['version']


def test_corrupt_snapshot_cannot_replace_active_library(tmp_path):
    path, repository = fixture(tmp_path)
    old = repository.snapshot()
    before = path.read_bytes()
    snapshot = repository.directory / (old['version'] + '.json')
    payload = json.loads(snapshot.read_text(encoding='utf-8'))
    payload['items'][0]['title'] = '被更改的来源'
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        repository.rollback(old['version'])
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        repository.rollback('../../outside')


def test_valid_snapshot_can_recover_invalid_active_library(tmp_path):
    path, repository = fixture(tmp_path)
    old = repository.snapshot()
    path.write_text('invalid JSON', encoding='utf-8')
    repository.rollback(old['version'])
    assert knowledge_version(Knowledge(path)) == old['version']
