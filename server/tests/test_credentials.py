import re
from pathlib import Path

import pytest
import yaml

from songdl import config, credentials

EXAMPLE = (Path(__file__).resolve().parents[2] / 'config.yaml.example').read_text(encoding='utf-8')


def test_random_credentials_look_right():
    names = {credentials.soulseek_username() for _ in range(200)}
    assert len(names) == 200
    for name in names:
        assert re.fullmatch(r'(?:[A-Z][a-z]+){3}\d{4}', name) and len(name) <= credentials.MAX_USERNAME
    pw = credentials.password()
    assert len(pw) == 32 and pw.isascii() and pw.isalnum()
    assert credentials.password() != pw


def test_example_config_gets_random_credentials_and_keeps_the_rest(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_bytes(EXAMPLE.replace('\n', '\r\n').encode())
    changed = credentials.fill_defaults(path)
    assert len(changed) == 3

    cfg = config.load(path)
    assert re.fullmatch(r'(?:[A-Z][a-z]+){3}\d{4}', cfg.username)
    assert len(cfg.password) == 32 and list(cfg.admins) == ['admin'] and len(cfg.admins['admin']) == 32
    assert len({cfg.password, cfg.admins['admin']}) == 2

    text = path.read_bytes().decode()
    assert '\r\n' in text and '\n' not in text.replace('\r\n', '')
    before, after = EXAMPLE.splitlines(), text.splitlines()
    differing = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(differing) == 3
    assert all(a.split(':')[0] == b.split(':')[0] for a, b in differing)   # same keys, same indents

    assert credentials.fill_defaults(path) == []          # nothing left to replace
    assert path.read_bytes().decode() == text


def test_own_credentials_are_left_alone(tmp_path):
    path = tmp_path / 'config.yaml'
    mine = (EXAMPLE.replace('YourUsername', 'MyName').replace('YourPassword', 'my soulseek pw')
            .replace('admin: change-me-please', 'admin: my-admin-pw\n    bob: change-me-please  # todo'))
    path.write_text(mine, encoding='utf-8')
    assert credentials.fill_defaults(path) == ['password for admin "bob"']
    cfg = config.load(path)
    assert (cfg.username, cfg.password, cfg.admins['admin']) == ('MyName', 'my soulseek pw', 'my-admin-pw')
    assert len(cfg.admins['bob']) == 32
    assert '# todo' in path.read_text(encoding='utf-8')


def test_quoted_and_empty_placeholders(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('soulseek:\n  username: "YourUsername"\n  password:\nwebserver:\n  admins:\n'
                    "    'admin': ''\n", encoding='utf-8')
    assert len(credentials.fill_defaults(path)) == 3
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert len(data['soulseek']['password']) == 32 and len(data['webserver']['admins']['admin']) == 32


def test_unwritable_config_says_so(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(EXAMPLE, encoding='utf-8')

    def refuse(*_):
        raise PermissionError('read-only file system')
    monkeypatch.setattr(Path, 'write_bytes', refuse)
    with pytest.raises(config.ConfigError, match='couldn\'t be saved'):
        credentials.fill_defaults(path)
    assert path.read_text(encoding='utf-8') == EXAMPLE


def test_missing_or_broken_config_is_left_for_load_to_report(tmp_path):
    assert credentials.fill_defaults(tmp_path / 'nope.yaml') == []
    bad = tmp_path / 'bad.yaml'
    bad.write_text('soulseek: [\n', encoding='utf-8')
    assert credentials.fill_defaults(bad) == []
