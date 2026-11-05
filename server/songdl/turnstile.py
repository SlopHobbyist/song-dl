"""Cloudflare Turnstile: checks the token the login form's widget produced."""

import logging

import aiohttp

log = logging.getLogger('songdl')

VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'


async def verify(secret, token, ip=None):
    """True if Cloudflare says the token is valid. Anything that goes wrong counts as no."""
    if not token or not isinstance(token, str) or len(token) > 2048:
        return False
    data = {'secret': secret, 'response': token}
    if ip:
        data['remoteip'] = ip
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(VERIFY_URL, data=data) as res:
                body = await res.json(content_type=None)
    except Exception as e:
        log.warning('Turnstile check failed: %r', e)
        return False
    if not body.get('success'):
        log.info('Turnstile rejected a login: %s', body.get('error-codes'))
    return bool(body.get('success'))
