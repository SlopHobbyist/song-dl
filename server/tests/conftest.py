import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from songdl import engine, jobs  # noqa: E402
from songdl.config import Config  # noqa: E402
from songdl.engine import Engine  # noqa: E402
from songdl.server_queue import ServerQueue  # noqa: E402
from songdl.slskd import Slskd  # noqa: E402
from songdl.web import build_app  # noqa: E402
from tests.fake_slskd import Fake, make_app  # noqa: E402

ADMIN = {'admin': 'correct-horse-battery'}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(engine, 'POLL_SECONDS', 0.1)
    monkeypatch.setattr('songdl.server_queue.TICK_SECONDS', 0.2)
    monkeypatch.setattr('songdl.web.FAIL_DELAY', 0)


@pytest_asyncio.fixture
async def fake(tmp_path):
    f = Fake(tmp_path / 'downloads', speed=20, search_seconds=0.2)
    server = TestServer(make_app(f))
    await server.start_server()
    f.url = str(server.make_url('')).rstrip('/')
    yield f
    await server.close()


def make_config(tmp_path, fake, **over):
    base = dict(
        address='x', port=1, listen_port=1, username='u', password='p', description='',
        downloads_dir=tmp_path / 'downloads', share=False, max_file_age=7,
        ip='127.0.0.1', web_port=0, https=False, rate_limiting=True, rate_limit=50, trust_proxy=False,
        admins=ADMIN, data_dir=tmp_path / 'data', slskd_dir=tmp_path / 'slskd', slskd_url=fake.url,
        certs_dir=tmp_path / 'certs', webui_dir=Path(__file__).resolve().parents[2] / 'webui',
    )
    base.update(over)
    return Config(**base)


async def start_site(tmp_path, fake, **over):
    cfg = make_config(tmp_path, fake, **over)
    eng = Engine(cfg, Slskd(cfg.slskd_url, 'key'))
    manager, queue = jobs.Manager(cfg, eng), ServerQueue(cfg, eng)
    client = TestClient(TestServer(build_app(cfg, manager, queue)))
    await client.start_server()
    client.cfg, client.manager, client.queue = cfg, manager, queue
    return client


@pytest_asyncio.fixture
async def site(tmp_path, fake):
    client = await start_site(tmp_path, fake)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def admin(site):
    res = await site.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
    assert res.status == 200
    return site
