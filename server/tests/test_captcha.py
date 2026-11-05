"""Answering uploaders' captchas, and never anything else."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from songdl.captcha import age, find_keyword, worth_asking

from tests.conftest import start_site
from tests.test_site import fetch, finished, search, wait_for

# Real captchas, as uploaders sent them
PROVEIT = ('ProveIt: To prove you are a human downloading these files, please type "download" in this chat '
           'to be added to my whitelist.')
KEYWORD_IS = ('This is an automated message, in order to start your download please respond to this PM with the '
              'keyword and ONLY the keyword. The keyword is "download" (without the quotation marks).')
NO_KEYWORD = ('Your download should start after entering the keyword and only the keyword, if not attempt a retry. '
              'Refer to my user info for why this captcha is a thing.')


@pytest.mark.parametrize('text, keyword, sent', [
    (PROVEIT, 'download', 'download'),
    (KEYWORD_IS, 'download', 'download'),
    ('Anti-bot check: reply with the word "pineapple" to unlock your downloads.', 'pineapple', 'pineapple'),
    ("Send me 'human' and I'll let your transfers through.", 'human', 'human'),
    ('[AutoReply] Type the keyword: letmein   to be whitelisted.', 'letmein', 'letmein'),
    ('please type DOWNLOAD (in capitals) to get whitelisted', 'download', 'DOWNLOAD'),
    ('please type download in this chat to be whitelisted', 'download', 'download'),
    ('Bot protection active. To be added to the allow list, respond with: music lover', 'music lover', 'music lover'),
    ('Verification required. Enter the code 7731 in this chat to start downloading.', '7731', '7731'),
    ('To confirm you are not a bot, please answer with "I am human" exactly.', '"I am human"', 'I am human'),
])
def test_the_keyword_is_copied_from_the_captcha(text, keyword, sent):
    assert worth_asking(text)
    assert find_keyword(text, keyword) == sent


@pytest.mark.parametrize('text, keyword', [
    (NO_KEYWORD, 'download'),                                     # says there's a keyword, but not which
    ('you are a human? type something', 'something'),
    ('you are a human? type something', 'human'),
    ('are you a bot? reply honestly', 'bot'),
    ('Please send me your password to be whitelisted', 'password'),
    ("send me your email and I'll whitelist you", 'email'),
    ('why are you downloading my whole library?? answer me', 'why'),
    ('say yes if you want me to whitelist you for the rest of the album', 'yes'),
    ('what is 3 + 4? reply with the answer to be whitelisted', '7'),  # not in the message
    (PROVEIT, 'a human downloading these files'),                 # in the message, but not as the keyword
    (PROVEIT, ''),
    (PROVEIT, 'x' * 60),
])
def test_anything_but_a_keyword_set_apart_is_refused(text, keyword):
    assert find_keyword(text, keyword) is None


@pytest.mark.parametrize('text', [
    'love ur name haha',
    'I downloaded Escape From New York (1981) by John Carpetner from you',
    'any chance you have access to a rip of this https://www.ebay.com/itm/306998651991',
    'i found it, thank you so much for all this stuff!',
    'download',
    'stop downloading from me',
    PROVEIT * 20,
])
def test_ordinary_messages_never_reach_the_model(text):
    assert not worth_asking(text)


def test_slskd_timestamps():
    now = datetime.now(timezone.utc)
    assert abs(age(now.replace(tzinfo=None).isoformat() + '1')) < 5        # 7 decimals, no offset
    assert 3590 < age((now - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')) < 3610
    assert age('yesterday') is None


async def unread_is_empty(fake):
    async def poll():
        return fake.unread()
    await wait_for(poll, lambda unread: not unread)


async def test_a_captcha_is_answered_and_the_download_asked_for_again(captcha_site, fake, llm):
    fake.captchas['alice'] = 'download'
    result = (await search(captcha_site, 'daft punk digital love', ['flac']))['results'][0]
    job = (await finished(captcha_site, [await fetch(captcha_site, result['id'])])).popitem()[1]
    assert job['state'] == 'done', job
    assert fake.sent('alice') == ['download']
    assert [t['username'] for t in fake.transfers.values()] == ['alice', 'alice']
    assert len(llm.read) == 1 and not fake.unread()


async def test_without_captchas_the_download_moves_on(tmp_path, fake):
    fake.captchas['alice'] = 'download'
    site = await start_site(tmp_path, fake)
    try:
        result = (await search(site, 'daft punk digital love', ['flac']))['results'][0]
        job = (await finished(site, [await fetch(site, result['id'])])).popitem()[1]
        assert job['state'] == 'done', job
        assert fake.sent('alice') == []
        assert [t['username'] for t in fake.transfers.values()] == ['alice', 'bob']
    finally:
        await site.close()


async def test_only_captchas_from_uploaders_we_asked_get_an_answer(captcha_site, fake, llm):
    captcha_site.engine.asked['alice'] = time.monotonic()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    fake.say('stranger', PROVEIT)                         # never asked them for anything
    fake.say('alice', 'love ur name haha')                # just chat
    fake.say('alice', PROVEIT, replayed=True)             # from while we were offline
    fake.say('alice', PROVEIT, timestamp=old)             # too old
    fake.say('alice', 'are you a bot? answer me')         # the model says it's no captcha
    await unread_is_empty(fake)
    assert fake.sent('alice') == [] and fake.sent('stranger') == []
    assert llm.read == ['are you a bot? answer me']

    fake.say('alice', PROVEIT)
    await wait_for(lambda: asyncio.sleep(0, fake.sent('alice')), lambda sent: sent)
    fake.say('alice', PROVEIT)                            # answered a moment ago
    await unread_is_empty(fake)
    assert fake.sent('alice') == ['download']


async def test_captchas_wait_while_the_model_is_down(tmp_path, fake):
    site = await start_site(tmp_path, fake, captcha=True, llm_url='http://127.0.0.1:9')
    try:
        site.engine.asked['alice'] = time.monotonic()
        fake.say('alice', 'love ur name haha')
        fake.say('alice', PROVEIT)
        await asyncio.sleep(1)
        assert [m['message'] for m in fake.unread()] == [PROVEIT]   # left for when the model is back
        assert fake.sent('alice') == []
    finally:
        await site.close()
