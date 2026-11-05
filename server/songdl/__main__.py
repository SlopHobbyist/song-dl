"""Song-DL entry point.

    python -m songdl setup    write slskd/slskd.yml from config.yaml
    python -m songdl serve    run the web server
"""

import logging
import ssl
import sys

from aiohttp import web

from . import config, credentials, setup
from .captcha import Captcha
from .engine import Engine
from .jobs import Manager
from .server_queue import ServerQueue
from .slskd import Slskd
from .web import build_app

log = logging.getLogger('songdl')


def tls_context(cfg):
    """Uses certs/cert.pem and certs/key.pem, creating a self-signed pair if missing."""
    cert, key = cfg.certs_dir / 'cert.pem', cfg.certs_dir / 'key.pem'
    if not (cert.exists() and key.exists()):
        log.warning('No certificate in %s, creating a self-signed one. Browsers will show a warning; '
                    'put a real cert.pem and key.pem there to fix that.', cfg.certs_dir)
        make_self_signed(cert, key)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def make_self_signed(cert_path, key_path):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Song-DL')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main(argv):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    command = argv[1] if len(argv) > 1 else 'serve'

    try:
        path = config.config_path()
        changed = credentials.fill_defaults(path)
        if changed:
            log.warning('%s had the example credentials, so random ones were saved into it: %s. '
                        'Look in config.yaml for the passwords.', path, ', '.join(changed))
        cfg = config.load(path)
    except config.ConfigError as e:
        log.error('%s', e)
        return 1

    if command == 'setup':
        path = setup.render(cfg)
        log.info('Wrote %s for Soulseek user %s', path, cfg.username)
        return 0

    if command == 'serve':
        cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
        engine = Engine(cfg, Slskd(cfg.slskd_url, setup.api_key(cfg)))
        captcha = Captcha(cfg, engine) if cfg.captcha else None
        app = build_app(cfg, Manager(cfg, engine), ServerQueue(cfg, engine), captcha)
        ssl_context = tls_context(cfg) if cfg.https else None
        scheme = 'https' if ssl_context else 'http'
        log.info('Song-DL listening on %s://%s:%d', scheme, cfg.ip, cfg.web_port)
        web.run_app(app, host=cfg.ip, port=cfg.web_port, ssl_context=ssl_context, print=None)
        return 0

    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
