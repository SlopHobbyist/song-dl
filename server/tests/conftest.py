import json
import re
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from songdl import engine, jobs  # noqa: E402
from songdl.captcha import Captcha  # noqa: E402
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
    monkeypatch.setattr('songdl.captcha.POLL_SECONDS', 0.1)
    monkeypatch.setattr(engine, 'SEARCHES_PER_WINDOW', 10_000)   # tests search far faster than Soulseek allows
    monkeypatch.setattr('songdl.imports.TICK_SECONDS', 0.1)

    # Tests never reach the real SoundCloud or YouTube; the ones about them put a fake in
    def offline(*args):
        raise ConnectionError('no network in tests')

    for name in ('songdl.soundcloud_audio', 'songdl.youtube_audio'):
        for func in ('_search', '_download'):
            monkeypatch.setattr(f'{name}.{func}', offline)
    monkeypatch.setattr('songdl.youtube_audio._probe', offline)


@pytest_asyncio.fixture
async def fake(tmp_path):
    f = Fake(tmp_path / 'downloads', speed=20, search_seconds=0.2)
    server = TestServer(make_app(f))
    await server.start_server()
    f.url = str(server.make_url('')).rstrip('/')
    yield f
    await server.close()


class FakeLLM:
    """Stands in for the llm container. Gullible on purpose: it calls anything
    with a quoted word a captcha, so the checks around it can be seen working."""

    def __init__(self):
        self.read = []   # the messages it was asked about
        self.url = None

    def app(self):
        async def complete(request):
            body = await request.json()
            text = body['messages'][-1]['content']
            self.read.append(text)
            m = re.search(r'"([^"]+)"', text)
            verdict = {'captcha': bool(m), 'keyword': m.group(1) if m else ''}
            return web.json_response({'choices': [{'message': {'content': json.dumps(verdict)}}]})

        app = web.Application()
        app.router.add_post('/v1/chat/completions', complete)
        return app


@pytest_asyncio.fixture
async def llm():
    f = FakeLLM()
    server = TestServer(f.app())
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
        captcha=False,
    )
    base.update(over)
    return Config(**base)


async def start_site(tmp_path, fake, **over):
    cfg = make_config(tmp_path, fake, **over)
    eng = Engine(cfg, Slskd(cfg.slskd_url, 'key'))
    manager, queue = jobs.Manager(cfg, eng), ServerQueue(cfg, eng)
    captcha = Captcha(cfg, eng) if cfg.captcha else None
    client = TestClient(TestServer(build_app(cfg, manager, queue, captcha)))
    await client.start_server()
    client.cfg, client.manager, client.queue, client.engine = cfg, manager, queue, eng
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


@pytest_asyncio.fixture
async def captcha_site(tmp_path, fake, llm):
    client = await start_site(tmp_path, fake, captcha=True, llm_url=llm.url)
    yield client
    await client.close()
