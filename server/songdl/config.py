"""Loads config.yaml and resolves the paths the app needs.

Inside Docker, the SONGDL_* environment variables set in docker-compose.yaml
point at the mounted volumes and take precedence over paths in config.yaml.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLACEHOLDER_USERNAMES = {'', 'yourusername'}
PLACEHOLDER_PASSWORDS = {'', 'yourpassword', 'change-me-please'}
MIN_ADMIN_PASSWORD = 8


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # soulseek
    address: str
    port: int
    listen_port: int
    username: str
    password: str
    description: str
    # downloads
    downloads_dir: Path
    share: bool
    max_file_age: float  # days; 0 or less keeps files forever
    # webserver
    ip: str
    web_port: int
    https: bool
    rate_limiting: bool
    rate_limit: int
    trust_proxy: bool    # take the visitor's IP and scheme from proxy headers
    admins: dict = field(default_factory=dict)   # username -> password; empty turns the admin pages off
    turnstile_site_key: str = ''
    turnstile_secret: str = ''
    # captcha
    captcha: bool = False  # answer uploaders' "type download to prove you're human" messages
    llm_url: str = 'http://localhost:8081'
    # runtime locations
    data_dir: Path = Path('data')   # the server queue is remembered here across restarts
    slskd_dir: Path = Path('slskd')
    slskd_url: str = 'http://localhost:5030'
    certs_dir: Path = Path('certs')
    webui_dir: Path = Path('webui')

    @property
    def queue_file(self):
        return self.data_dir / 'queue.json'


def _path(value, base):
    # The example config can use Windows style paths like .\downloads
    p = Path(str(value).replace('\\', '/')).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _admins(raw):
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError('webserver.admins must be a list of "username: password" lines.')
    admins = {}
    for name, password in raw.items():
        name, password = str(name).strip(), str(password if password is not None else '')
        if not name:
            raise ConfigError('webserver.admins has a user without a name.')
        if len(password) < MIN_ADMIN_PASSWORD or password.lower() in PLACEHOLDER_PASSWORDS:
            raise ConfigError(f'The password for admin "{name}" must be at least {MIN_ADMIN_PASSWORD} characters '
                              'and not the example one. Remove the user to turn the admin pages off.')
        admins[name] = password
    return admins


def load(path=None):
    path = Path(path or os.environ.get('SONGDL_CONFIG', 'config.yaml')).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except FileNotFoundError:
        raise ConfigError(f'{path} not found. Copy config.yaml.example to config.yaml and edit it.')
    except yaml.YAMLError as e:
        raise ConfigError(f'{path} is not valid YAML: {e}')

    base = path.parent
    slsk = raw.get('soulseek') or {}
    dl = raw.get('downloads') or {}
    web = raw.get('webserver') or {}
    ts = web.get('turnstile') or {}
    captcha = raw.get('captcha') or {}
    repo = Path(__file__).resolve().parents[2]

    username = str(slsk.get('username') or '').strip()
    password = str(slsk.get('password') or '')
    if username.lower() in PLACEHOLDER_USERNAMES or not password or password == 'YourPassword':
        raise ConfigError('Set soulseek.username and soulseek.password in config.yaml.')

    site_key, secret = str(ts.get('site_key') or '').strip(), str(ts.get('secret_key') or '').strip()
    if bool(site_key) != bool(secret):
        raise ConfigError('Set both webserver.turnstile.site_key and secret_key, or neither.')

    bind = os.environ.get('SONGDL_BIND')
    ip, web_port = (bind.rsplit(':', 1) if bind else (web.get('ip', '0.0.0.0'), web.get('port', 8080)))

    try:
        return Config(
            address=str(slsk.get('address', 'vps.slsknet.org')),
            port=int(slsk.get('port', 2271)),
            listen_port=int(slsk.get('listen_port', 50300)),
            username=username,
            password=password,
            description=str(slsk.get('description') or '').strip(),
            downloads_dir=_path(os.environ.get('SONGDL_DOWNLOADS') or dl.get('downloads_folder', 'downloads'), base),
            share=bool(dl.get('share', True)),
            max_file_age=float(dl.get('max_file_age', 7)),
            ip=str(ip),
            web_port=int(web_port),
            https=bool(web.get('https', False)),
            rate_limiting=bool(web.get('rate_limiting', True)),
            rate_limit=max(1, int(web.get('rate_limit', 5))),
            trust_proxy=bool(web.get('trust_proxy', False)),
            admins=_admins(web.get('admins')),
            turnstile_site_key=site_key,
            turnstile_secret=secret,
            captcha=bool(captcha.get('enabled', False)),
            llm_url=os.environ.get('SONGDL_LLM_URL', 'http://localhost:8081').rstrip('/'),
            data_dir=_path(os.environ.get('SONGDL_DATA', 'data'), base),
            slskd_dir=_path(os.environ.get('SONGDL_SLSKD_DIR', 'slskd'), base),
            slskd_url=os.environ.get('SONGDL_SLSKD_URL', 'http://localhost:5030').rstrip('/'),
            certs_dir=_path(os.environ.get('SONGDL_CERTS', 'certs'), base),
            webui_dir=_path(os.environ.get('SONGDL_WEBUI', repo / 'webui'), base),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f'Invalid value in config.yaml: {e}')
